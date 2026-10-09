"""Native-helper enrollment journal; control API accepts metadata, never values.

Claim commits before Keychain access. A crash leaves an unusable enrolling version,
not an inferred success. Only explicitly revoked/expired/tombstoned versions can be
purged. Store authority and OS effects cannot share a transaction: orphan cleanup
is deliberate and visible, and a consumed enrollment is never replayed.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .admin import AdminStore
from .admin_models import Closed, Identifier, MetadataId
from .os_secret_store import SecretStoreError, validate_value
from .store import Conflict, Forbidden, _connect, _parse_utc, _token_hash, canonical_digest


class ProtectedRequest(Closed):
    project_id: Identifier
    record_id: MetadataId
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    enrollment_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    operation: Literal["enroll", "import", "health", "purge"]


def no_replay(*unused):
    raise Conflict("Protected request consumed; inspect metadata before any new action.")


class ProtectedSecretStore(AdminStore):
    def inspect(self, actor, project_id, ident):
        db = _connect(self.database_path)
        try:
            db.execute("BEGIN")
            live = self._live_principal(db, actor)
            self._owner(live)
            state = self._load(db, live, project_id, ident)
            if state["spec"].get("backend") != "os_keychain":
                raise Forbidden("Not a protected enrollment record.")
            return self._projected(db, live, state)
        finally:
            db.close()

    def _check(self, db, actor, request, *, state_name=None, revision_delta=0):
        self._owner(actor)
        state = self._load(db, actor, request["project_id"], request["record_id"])
        if (
            state["spec"].get("backend") != "os_keychain"
            or state["revision"] != request["expected_revision"] + revision_delta
            or state.get("enrollment_id") != request["enrollment_id"]
            or (state_name is not None and state["state"] != state_name)
        ):
            raise Conflict("Protected enrollment context is stale or belongs to another store.")
        if state_name:
            self._future(state["spec"]["expires_at"])
            if _parse_utc(state["enrollment_deadline"]) <= datetime.now(UTC):
                raise Conflict("Enrollment deadline elapsed.")
            proposer = self._live_principal(
                db, {"id": state["proposed_by"], "org_id": actor["org_id"]}
            )
            self._project(db, proposer, state["project_id"])
            if (
                state["approved_by"] != actor["id"]
                or proposer["id"] == actor["id"]
                or proposer["role"] != "human"
            ):
                raise Forbidden("Independent live human approval is required.")
        return state

    def _operation(self, actor, request, phase, command):
        return self._mutate(
            actor,
            request["enrollment_id"],
            "NATIVE",
            f"/protected-secrets/{request['record_id']}/{phase}",
            canonical_digest(request),
            command,
            replay_guard=no_replay,
        )[1]

    def claim(self, actor, payload):
        request = self._body(ProtectedRequest, payload)
        if request["operation"] not in {"enroll", "import"}:
            raise Forbidden("Enrollment operation required.")

        def command(db, live):
            state = self._check(db, live, request, state_name="approved")
            state.update(state="enrolling", health="unknown", cleanup_pending=True)
            state["versions"][-1]["state"] = "enrolling"
            self._save(db, live, state, "enrollment.claim")
            return 200, state

        return self._operation(actor, request, "claim", command)

    def finish(self, actor, payload, value, backend):
        written = []
        try:
            request = self._body(ProtectedRequest, payload)
            if request["operation"] not in {"enroll", "import"}:
                raise Forbidden("Enrollment operation required.")
            validate_value(value)
            if backend.status not in {"macos_keychain", "development_memory_NON_PRODUCTION"}:
                raise SecretStoreError()

            def intent(db, live):
                state = self._check(db, live, request, state_name="enrolling", revision_delta=1)
                state["versions"][-1]["write_attempted"] = True
                self._save(db, live, state, "enrollment.write_intent")
                return 200, state

            # Even a trusted caller cannot repeat a failed/uncertain OS write.
            # Persist this separately; the effect/completion transaction may roll back.
            self._operation(actor, request, "write-intent", intent)

            def command(db, live):
                state = self._check(db, live, request, state_name="enrolling", revision_delta=2)
                if db.execute(
                    "SELECT 1 FROM principals WHERE token_hash=?",
                    (_token_hash(value.decode("utf-8")),),
                ).fetchone():
                    raise Forbidden(
                        "Tessera credentials cannot be enrolled as provider credentials."
                    )
                reference, version = state["reference"], state["version"]
                # Record the possible effect before invoking an adapter (partial writes count).
                written.append((reference, version))
                backend.put(reference, version, value)
                if not backend.exists(reference, version):
                    raise SecretStoreError()
                state.update(state="active", health="available", custody=backend.status)
                state["versions"][-1].update(
                    state="active",
                    fingerprint=canonical_digest({"reference": reference, "version": version}),
                )
                # Older versions remain inaccessible; explicit purge removes physical custody.
                state["cleanup_pending"] = version > 1
                self._save(db, live, state, "enrollment.stored")
                return 200, state

            return self._operation(actor, request, "store", command)
        except BaseException:
            for reference, version in written:
                try:
                    backend.delete(reference, version)
                except Exception:
                    pass  # Claim is durable and cleanup_pending; never invent deletion evidence.
            raise
        finally:
            if isinstance(value, bytearray):
                value[:] = b"\0" * len(value)

    def health(self, actor, payload, backend):
        request = self._body(ProtectedRequest, payload)
        state = self.inspect(actor, request["project_id"], request["record_id"])
        if state["revision"] != request["expected_revision"]:
            raise Conflict("Stale secret metadata.")
        available = False
        if state["state"] == "active":
            try:
                available = backend.exists(state["reference"], state["version"])
            except SecretStoreError:
                pass
        return {"available": available, "store": backend.status, "version": state["version"]}

    def purge(self, actor, payload, backend):
        request = self._body(ProtectedRequest, payload)
        if request["operation"] != "purge":
            raise Forbidden("Purge operation required.")

        def command(db, live):
            state = self._check(db, live, request)
            projected = self._projected(db, live, state)
            if projected["state"] not in {"active", "revoked", "expired", "deleted"}:
                raise Conflict("Revoke uncertain enrollment before cleanup.")
            for version in state["versions"]:
                if projected["state"] == "active" and version["version"] == state["version"]:
                    continue
                backend.delete(state["reference"], version["version"])
                if backend.exists(state["reference"], version["version"]):
                    raise SecretStoreError()
                version["custody_deleted"] = True
            state["cleanup_pending"] = False
            self._save(db, live, state, "enrollment.purged")
            return 200, self._projected(db, live, state)

        # Physical deletion is idempotent, but no request automatically replays effects.
        return self._operation(actor, request, f"purge-{request['expected_revision']}", command)


class TrustedServerResolver:
    """In-process server composition only, not registered with API/IPC or live proxy.

    Admission is an injected server authority check, never a request's boolean. It
    must recheck the execution grant/lease/epoch for each use, returning a current
    control principal. No default admission or service identity is supplied.
    """

    def __init__(self, store, backend, *, admission, project_id, record_id, purpose, audience):
        self.store, self.backend, self.admission = store, backend, admission
        self.project_id, self.record_id = project_id, record_id
        self.purpose, self.audience = purpose, audience

    def resolve(self, reference, version):
        db = _connect(self.store.database_path)
        value = None
        try:
            db.execute("BEGIN IMMEDIATE")
            actor = self.store._live_principal(db, self.admission(db))
            if (
                db.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()[0]
                != "active"
            ):
                raise Forbidden("Held custody cannot resolve.")
            state = self.store._load(db, actor, self.project_id, self.record_id)
            reviewer = self.store._live_principal(
                db, {"id": state["approved_by"], "org_id": actor["org_id"]}
            )
            self.store._owner(reviewer)
            if (
                state["state"] != "active"
                or state["reference"] != reference
                or state["version"] != version
                or state["spec"]["purpose"] != self.purpose
                or state["spec"]["audience"] != self.audience
                or state["spec"]["backend"] != "os_keychain"
                or state.get("custody") != self.backend.status
                or _parse_utc(state["spec"]["expires_at"]) <= datetime.now(UTC)
            ):
                raise Forbidden("Credential scope or version unavailable.")
            value = self.backend.resolve(reference, version)
            self.store._audit(
                db,
                actor,
                "secret.resolve",
                "allowed",
                {
                    "id": state["id"],
                    "version": version,
                    "fingerprint": canonical_digest({"reference": reference, "version": version}),
                },
                project_id=state["project_id"],
            )
            db.execute("COMMIT")
            return value
        except BaseException:
            if value is not None:
                value.zeroize()
            raise
        finally:
            db.close()
