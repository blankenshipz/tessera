"""Project administration event source; no provider effects or secret value custody."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from .admin_models import AdminCommand, AdminCreate, AdminRecord
from .admin_secrets import FakeAdminSecretBackend, UnavailableSecretBackend
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    canonical_digest,
    utc_now,
)


class AdminStore(Store):
    @staticmethod
    def _body(model, payload):
        try:
            return model.model_validate(payload).model_dump(mode="json")
        except (ValidationError, ValueError):
            raise StoreError("Invalid administrative request.") from None

    def _load(self, db, actor, project_id, ident):
        self._project(db, actor, project_id)
        row = db.execute(
            "SELECT payload_json FROM operator_audit_events WHERE org_id=? AND project_id=? "
            "AND event_type='admin.state' AND json_extract(payload_json,'$.id')=? "
            "ORDER BY cursor DESC LIMIT 1",
            (actor["org_id"], project_id, ident),
        ).fetchone()
        if row is None:
            raise NotFound("Administrative record not found.")
        return json.loads(row[0])

    def _save(self, db, actor, state, action):
        state["revision"] += 1
        state["updated_at"] = utc_now()
        AdminRecord.model_validate(state)
        self._audit(db, actor, "admin.state", "allowed", state, project_id=state["project_id"])
        summary = {
            "id": state["id"],
            "kind": state["spec"]["kind"],
            "action": action,
            "revision": state["revision"],
        }
        self._audit(db, actor, "admin.command", "allowed", summary, project_id=state["project_id"])
        self._event(
            db, actor, state["project_id"], None, "admin.changed", state["revision"], summary
        )

    def _states(self, db, actor, project_id, kind=None):
        # Latest states only. Bounded admission below caps each project's aggregates.
        rows = db.execute(
            "SELECT payload_json FROM operator_audit_events WHERE cursor IN ("
            "SELECT MAX(cursor) FROM operator_audit_events WHERE org_id=? AND project_id=? "
            "AND event_type='admin.state' GROUP BY json_extract(payload_json,'$.id')) "
            "ORDER BY cursor DESC LIMIT 201",
            (actor["org_id"], project_id),
        ).fetchall()
        states = [json.loads(row[0]) for row in rows]
        return [s for s in states if kind is None or s["spec"]["kind"] == kind]

    @staticmethod
    def _future(value, max_days=30):
        expiry = _parse_utc(value)
        if not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(days=max_days):
            raise Conflict("Expiry must be in the bounded future.")

    def _subject(self, db, actor, project_id, spec):
        if spec["subject_role"] in {"worker", "proxy", "monitor"}:
            raise Forbidden("Workload delegation requires unavailable service identity enrollment.")
        subject = self._live_principal(db, {"id": spec["subject_id"], "org_id": actor["org_id"]})
        self._project(db, subject, project_id)
        role = "agent" if spec["subject_role"] == "agent" else "human"
        if subject["role"] != role or self._is_root_owner(subject):
            raise Forbidden("Delegation subject role is not eligible.")
        if subject["expires_at"] is None or _parse_utc(spec["expires_at"]) > _parse_utc(
            subject["expires_at"]
        ):
            raise Forbidden("Delegation cannot outlive its subject credential.")
        return subject

    def _effective(self, db, actor, state):
        if state["spec"]["kind"] != "delegation" or state["state"] != "approved":
            return False
        if (
            db.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()[0]
            != "active"
        ):
            return False
        try:
            if _parse_utc(state["spec"]["expires_at"]) <= datetime.now(UTC):
                return False
            self._subject(db, actor, state["project_id"], state["spec"])
            target = self._load(db, actor, state["project_id"], state["spec"]["target_id"])
            if (
                target["state"] in {"revoked", "expired", "deleted", "rejected"}
                or target["spec"]["provider"] != state["spec"]["provider"]
            ):
                return False
            reviewer = self._live_principal(
                db, {"id": state["approved_by"], "org_id": actor["org_id"]}
            )
            return self._is_root_owner(reviewer)
        except (Forbidden, NotFound):
            return False

    def _allow(self, db, actor, project_id, action, target):
        if self._is_root_owner(actor):
            return
        for grant in self._states(db, actor, project_id, "delegation"):
            spec = grant["spec"]
            if (
                spec["subject_id"] == actor["id"]
                and spec["target_id"] == target["id"]
                and spec["provider"] == target["spec"]["provider"]
                and action in spec["actions"]
                and self._effective(db, actor, grant)
            ):
                return
        raise Forbidden("No current exact administrative scope permits this action.")

    @staticmethod
    def _owner(actor):
        if not Store._is_root_owner(actor):
            raise Forbidden("Root human administration is required.")

    def _secret_bindings(self, db, actor, project_id, spec):
        versions = {}
        for field, purpose in (
            ("management_secret_id", "salad_management"),
            ("gateway_secret_id", "ai_gateway"),
            ("kubeconfig_secret_id", "kubeconfig"),
            ("artifact_secret_id", "artifact_store"),
        ):
            if spec.get(field):
                secret = self._load(db, actor, project_id, spec[field])
                if (
                    secret["spec"]["kind"] != "secret"
                    or secret["spec"]["purpose"] != purpose
                    or secret["state"] != "active"
                    or _parse_utc(secret["spec"]["expires_at"]) <= datetime.now(UTC)
                ):
                    raise Conflict(
                        "Secret metadata binding is unavailable or has the wrong purpose."
                    )
                versions[secret["id"]] = secret["version"]
        return versions

    def _projected(self, db, actor, state):
        value = json.loads(json.dumps(state))
        if value["spec"]["kind"] in {"secret", "delegation"}:
            if value["state"] not in {"revoked", "deleted", "rejected", "expired"} and _parse_utc(
                value["spec"]["expires_at"]
            ) <= datetime.now(UTC):
                value["state"] = "expired"
                if value["spec"]["kind"] == "secret":
                    value["health"] = "expired"
                    if value["spec"]["backend"] == "os_keychain":
                        value["cleanup_pending"] = any(
                            not v.get("custody_deleted") for v in value["versions"]
                        )
        value["effective"] = self._effective(db, actor, state)
        if value["spec"]["kind"] == "configuration" and value["verified_revision"] is not None:
            try:
                versions = self._secret_bindings(db, actor, state["project_id"], state["spec"])
                if versions != state.get("verified_secret_versions", {}):
                    value["verification"] = "stale"
            except (Conflict, NotFound):
                value["verification"] = "stale"
        return value

    def workspace(self, actor, project_id, offset=0, limit=50):
        db = _connect(self.database_path)
        try:
            db.execute("BEGIN")
            actor = self._live_principal(db, actor)
            self._project(db, actor, project_id)
            visible = []
            for state in self._states(db, actor, project_id):
                if not self._is_root_owner(actor):
                    if state["spec"]["kind"] == "delegation":
                        if actor["id"] not in {state["proposed_by"], state["spec"]["subject_id"]}:
                            continue
                    elif state["spec"]["kind"] == "configuration":
                        try:
                            self._allow(db, actor, project_id, "configuration.read", state)
                        except Forbidden:
                            continue
                    elif state["proposed_by"] != actor["id"]:
                        continue
                visible.append(self._projected(db, actor, state))
            held = (
                db.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()[0]
                != "active"
            )
            return {
                "records": visible[offset : offset + limit],
                "root_admin": self._is_root_owner(actor),
                "control_status": "held" if held else "ready",
                "observed_at": utc_now(),
                "next_offset": offset + limit if offset + limit < len(visible) else None,
                "held": held,
            }
        finally:
            db.close()

    def create(self, actor, project_id, key, payload):
        body = self._body(AdminCreate, payload)
        spec = body["spec"]

        def command(db, live):
            self._project(db, live, project_id)
            if len(self._states(db, live, project_id)) >= 200:
                raise Conflict("Administrative record limit reached.")
            kind = spec["kind"]
            if kind == "secret":
                self._future(spec["expires_at"])
                if spec["backend"] == "os_keychain":
                    if live["role"] != "human":
                        raise Forbidden("Protected enrollment requires a human proposal.")
                else:
                    self._owner(live)
                    backend = (
                        FakeAdminSecretBackend()
                        if spec["backend"] == "fake"
                        else UnavailableSecretBackend()
                    )
                    with backend.materialize():
                        pass  # Fixed fixture marker is wiped; never stored or returned.
            elif kind == "delegation":
                self._future(spec["expires_at"], 1)
                self._subject(db, live, project_id, spec)
                target = self._load(db, live, project_id, spec["target_id"])
                if (
                    target["spec"]["kind"] != "configuration"
                    or target["spec"]["provider"] != spec["provider"]
                ):
                    raise Forbidden("Delegation target/provider mismatch.")
                if not self._is_root_owner(live) and live["id"] != spec["subject_id"]:
                    raise Forbidden("Non-owner principals may request only their own access.")
            else:
                # A new proposal does not create authority. All authenticated project
                # principals may propose; only root can approve another principal.
                self._secret_bindings(db, live, project_id, spec)
            now = utc_now()
            state = {
                "id": "admin_" + uuid.uuid4().hex,
                "project_id": project_id,
                "revision": 0,
                "spec": spec,
                "state": "active" if kind == "secret" else "proposed",
                "proposed_by": live["id"],
                "approved_by": None,
                "created_at": now,
                "updated_at": now,
                "version": 1 if kind == "secret" else 0,
                "versions": [{"version": 1, "created_at": now, "state": "active"}]
                if kind == "secret"
                else [],
                "last_used_at": None,
                "health": "synthetic_only" if kind == "secret" else "unknown",
                "verification": "unknown",
                "verified_revision": None,
                "enabled": False,
                "effective": False,
            }
            if kind == "secret" and spec["backend"] == "os_keychain":
                state.update(
                    state="proposed",
                    health="unknown",
                    reference="credential-ref:" + uuid.uuid4().hex,
                    enrollment_id=uuid.uuid4().hex,
                    versions=[{"version": 1, "created_at": now, "state": "proposed"}],
                )
            self._save(db, live, state, "create")
            return 201, self._projected(db, live, state)

        def replay(db, live, response):
            current = self._load(db, live, project_id, response["id"])
            if current["revision"] != response["revision"]:
                raise Conflict("Replay is stale; inspect current administration state.")
            if spec["kind"] == "secret":
                if spec["backend"] != "os_keychain":
                    self._owner(live)
                self._future(current["spec"]["expires_at"])
            if spec["kind"] == "delegation":
                self._subject(db, live, project_id, spec)
                self._future(spec["expires_at"], 1)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/admin/projects/{project_id}/records",
            canonical_digest(body),
            command,
            replay_guard=replay,
        )

    def command(self, actor, project_id, ident, key, payload):
        body = self._body(AdminCommand, payload)
        action = body["action"]

        def authorize(db, live, state):
            if action == "rotate" and state["spec"].get("backend") == "os_keychain":
                # Only the original non-owner human proposer can initiate rotation;
                # root subsequently reviews it. Other project humans have no grant.
                self._project(db, live, project_id)
                if (
                    live["role"] != "human"
                    or live["id"] != state["proposed_by"]
                    or self._is_root_owner(live)
                ):
                    raise Forbidden("Rotation requires the original independent human proposer.")
            elif action == "revise" and state["spec"]["kind"] == "configuration":
                self._allow(db, live, project_id, "configuration.propose", state)
            elif action == "verify" and state["spec"]["kind"] == "configuration":
                self._allow(db, live, project_id, "readiness.read", state)
            else:
                self._owner(live)

        def command(db, live):
            state = self._load(db, live, project_id, ident)
            authorize(db, live, state)
            if state["revision"] != body["expected_revision"]:
                raise Conflict("Administrative revision is no longer current.")
            kind = state["spec"]["kind"]
            protected = kind == "secret" and state["spec"]["backend"] == "os_keychain"
            if action == "enable":
                raise Forbidden("Live enablement is unavailable; configuration is not conformance.")
            if action in {"approve", "reject"}:
                if (kind == "secret" and not protected) or state["state"] != "proposed":
                    raise Conflict("Only a pending proposal can be reviewed.")
                if live["id"] in {state["proposed_by"], state["spec"].get("subject_id")}:
                    raise Forbidden("Self-approval or subject review is forbidden.")
                if kind == "delegation":
                    self._future(state["spec"]["expires_at"], 1)
                    self._subject(db, live, project_id, state["spec"])
                    target = self._load(db, live, project_id, state["spec"]["target_id"])
                    if target["state"] in {"revoked", "expired", "deleted", "rejected"} or (
                        target["spec"]["provider"] != state["spec"]["provider"]
                    ):
                        raise Conflict("Delegation target scope is no longer current.")
                elif not protected:
                    self._secret_bindings(db, live, project_id, state["spec"])
                if protected:
                    self._future(state["spec"]["expires_at"])
                    proposer = self._live_principal(
                        db, {"id": state["proposed_by"], "org_id": live["org_id"]}
                    )
                    self._project(db, proposer, project_id)
                    if proposer["role"] != "human":
                        raise Forbidden("A live human proposer is required.")
                    state["enrollment_id"] = uuid.uuid4().hex if action == "approve" else None
                    state["enrollment_deadline"] = (
                        datetime.now(UTC) + timedelta(minutes=5)
                    ).isoformat()
                state["state"] = "approved" if action == "approve" else "rejected"
                state["approved_by"] = live["id"] if action == "approve" else None
            elif action == "revise":
                if kind != "configuration" or state["state"] in {"revoked", "expired", "deleted"}:
                    raise Conflict("Configuration is not revisable.")
                # Delegated edits cannot change the provider bound by their grant.
                if body["configuration"]["provider"] != state["spec"][
                    "provider"
                ] and not self._is_root_owner(live):
                    raise Forbidden("Delegated provider scope cannot widen.")
                self._secret_bindings(db, live, project_id, body["configuration"])
                state.update(
                    spec=body["configuration"],
                    state="proposed",
                    proposed_by=live["id"],
                    approved_by=None,
                    verification="stale",
                    verified_revision=None,
                )
            elif action == "verify":
                if kind != "configuration" or state["state"] != "approved":
                    raise Conflict("Review configuration before local verification.")
                versions = self._secret_bindings(db, live, project_id, state["spec"])
                state.update(
                    verification="blocked",
                    verified_revision=state["revision"] + 1,
                    verified_secret_versions=versions,
                )
            elif action == "rotate":
                if (
                    kind != "secret"
                    or state["state"] != "active"
                    or _parse_utc(state["spec"]["expires_at"]) <= datetime.now(UTC)
                ):
                    raise Conflict("Only an active unexpired fake secret can rotate.")
                self._future(body["expires_at"])
                if not protected:
                    with FakeAdminSecretBackend().materialize():
                        pass
                state["versions"][-1]["state"] = "revoked"
                state["version"] += 1
                state["versions"].append(
                    {"version": state["version"], "created_at": utc_now(), "state": "active"}
                )
                if len(state["versions"]) > 100:
                    raise Conflict("Secret version limit reached.")
                state["spec"]["expires_at"] = body["expires_at"]
                if protected:
                    state.update(
                        state="proposed",
                        health="unknown",
                        proposed_by=live["id"],
                        approved_by=None,
                        enrollment_id=uuid.uuid4().hex,
                        cleanup_pending=True,
                    )
                    state["versions"][-1]["state"] = "proposed"
            elif action in {"revoke", "expire"}:
                if state["state"] in {"revoked", "expired", "deleted", "rejected"}:
                    raise Conflict("Administrative record is already terminal.")
                state["state"] = "revoked" if action == "revoke" else "expired"
                if kind == "secret":
                    state["health"] = state["state"]
                    state["versions"][-1]["state"] = state["state"]
                    if protected:
                        state["cleanup_pending"] = True
            elif action == "delete":
                projected = self._projected(db, live, state)
                if kind != "secret" or projected["state"] not in {"revoked", "expired"}:
                    raise Conflict("Revoke or expire secret metadata before tombstoning.")
                if protected:
                    state["cleanup_pending"] = projected["cleanup_pending"]
                state.update(state="deleted", health="deleted")
                state["versions"][-1]["state"] = "deleted"
            else:
                raise Conflict("Unsupported administrative transition.")
            self._save(db, live, state, action)
            return 200, self._projected(db, live, state)

        def replay(db, live, response):
            state = self._load(db, live, project_id, ident)
            authorize(db, live, state)
            if state["revision"] != response["revision"]:
                raise Conflict("Replay is stale; inspect current administration state.")
            if action == "verify" and self._projected(db, live, state)["verification"] == "stale":
                raise Conflict("Verification bindings are stale; inspect current state.")
            if state["spec"]["kind"] in {"secret", "delegation"} and action not in {
                "revoke",
                "expire",
                "delete",
                "reject",
            }:
                self._future(state["spec"]["expires_at"])
            if (
                state["spec"]["kind"] == "delegation"
                and action == "approve"
                and not self._effective(db, live, state)
            ):
                raise Forbidden("Delegation authority is no longer current.")

        return self._mutate(
            actor,
            key,
            "POST",
            f"/admin/projects/{project_id}/records/{ident}/commands",
            canonical_digest(body),
            command,
            replay_guard=replay,
        )

    def audit(self, actor, project_id, after=0, limit=50):
        db = _connect(self.database_path)
        try:
            actor = self._live_principal(db, actor)
            self._owner(actor)
            self._project(db, actor, project_id)
            rows = db.execute(
                "SELECT cursor,event_type,decision,actor_principal_id,created_at,"
                "correlation_id,payload_json "
                "FROM operator_audit_events WHERE org_id=? AND project_id=? AND cursor>? "
                "AND event_type='admin.command' ORDER BY cursor LIMIT ?",
                (actor["org_id"], project_id, after, limit),
            ).fetchall()
            return {
                "events": [
                    {
                        **{k: row[k] for k in row.keys() if k != "payload_json"},
                        "payload": json.loads(row["payload_json"]),
                    }
                    for row in rows
                ]
            }
        finally:
            db.close()
