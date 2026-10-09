"""Authenticated immutable runtime bindings and conservative native reservations.

No native transport or provider credentials live here. Trusted owner adapters must
execute the private checks; serialized claim replies never become capabilities.
"""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from . import runtime_binding_models as dto
from .agent_authority import require_active_agent_lineage
from .store import Conflict, Forbidden, NotFound, Store, StoreError, _connect, canonical_digest


class NativeBridgeUnavailable(StoreError):
    pass


@dataclass(frozen=True)
class NativeCheckWindow:
    """Trusted adapter execution result, not agent JSON or a provider guarantee."""

    local_check_ref: str
    quota_check_ref: str
    operation_ref: str
    actor_credential_id: str
    nonce: str
    scope_digest: str
    pins_digest: str
    request_ref: str
    final_fingerprint: str
    order_revision: int
    sequence: int
    checked_at: datetime
    snapshot_checked_at: datetime
    valid_until: datetime


class RuntimeBindingAdapter(Protocol):
    def verify_enrollment(self, connection, actor, request, scope, pins) -> None: ...
    def verify_allocation(self, connection, actor, snapshot, request) -> None: ...
    def verify_claim(self, connection, actor, operation, request) -> NativeCheckWindow: ...


ENTITY_TABLES = {
    "policy": "runtime_policy_profiles",
    "enrollment": "runtime_enrollments",
    "financial": "runtime_financial_assertions",
}


def create_runtime_binding_schema(c):
    for table in ENTITY_TABLES.values():
        c.execute(f"""CREATE TABLE {table} (
          id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
          project_id TEXT NOT NULL REFERENCES projects(id),
          issuer_id TEXT NOT NULL REFERENCES principals(id),
          issuer_credential_id TEXT NOT NULL REFERENCES credentials(id),
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE runtime_binding_revocations (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('policy','enrollment','financial')),
      target_id TEXT NOT NULL, org_id TEXT NOT NULL REFERENCES organizations(id),
      issuer_id TEXT NOT NULL REFERENCES principals(id), created_at TEXT NOT NULL,
      UNIQUE(kind,target_id))""")
    c.execute("""CREATE TABLE runtime_authority_grants (
      id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL UNIQUE REFERENCES runtime_enrollments(id),
      parent_grant_id TEXT NOT NULL REFERENCES agent_authority_grants(id),
      reserved_units INTEGER NOT NULL DEFAULT 0 CHECK(reserved_units>=0),
      reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK(reserved_cents>=0))""")
    c.execute("""CREATE TABLE runtime_native_operations (
      id TEXT PRIMARY KEY, root_ref TEXT NOT NULL UNIQUE,
      enrollment_id TEXT NOT NULL REFERENCES runtime_enrollments(id),
      actor_credential_id TEXT NOT NULL REFERENCES credentials(id),
      nonce TEXT NOT NULL UNIQUE, ticket TEXT NOT NULL UNIQUE,
      payload_json TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('allocated','claimed','native-returned','uncertain')),
      revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE runtime_native_claims (
      id TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL UNIQUE REFERENCES runtime_native_operations(id),
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE runtime_native_settlements (
      id TEXT PRIMARY KEY, claim_id TEXT NOT NULL UNIQUE REFERENCES runtime_native_claims(id),
      actor_credential_id TEXT NOT NULL REFERENCES credentials(id),
      disposition TEXT NOT NULL CHECK(disposition IN ('native-returned','uncertain')),
      created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE runtime_native_root_fences (
      enrollment_id TEXT PRIMARY KEY REFERENCES runtime_enrollments(id),
      operation_id TEXT NOT NULL REFERENCES runtime_native_operations(id),
      state TEXT NOT NULL CHECK(state IN ('claimed','native-returned','uncertain')))""")
    c.execute("""CREATE TABLE runtime_native_entries (
      claim_id TEXT PRIMARY KEY REFERENCES runtime_native_claims(id), entered_at TEXT NOT NULL)""")
    immutable = [
        *ENTITY_TABLES.values(),
        "runtime_binding_revocations",
        "runtime_native_claims",
        "runtime_native_settlements",
        "runtime_native_entries",
    ]
    for table in immutable:
        for action in ("UPDATE", "DELETE"):
            c.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END"
            )


def time(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise Forbidden("binding_mismatch")
    return value.astimezone(UTC)


class RuntimeBindingStore(Store):
    def __init__(self, state_dir, adapter: RuntimeBindingAdapter | None = None):
        super().__init__(state_dir)
        self.adapter = adapter

    @staticmethod
    def _live_principal(c, principal, *, audience="control"):
        return Store._live_principal(c, principal, audience=audience, external_allowed=True)

    @staticmethod
    def _human(actor):
        if actor["role"] != "human" or actor["kind"] not in {"local_owner", "human_session"}:
            raise Forbidden("actor_not_eligible")

    def _adapter(self):
        if self.adapter is None:
            raise NativeBridgeUnavailable("native_bridge_unavailable")
        return self.adapter

    def _credential(self, c, actor, credential_id, audience="control"):
        r = c.execute(
            "SELECT p.id,p.org_id FROM credentials x JOIN principals p "
            "ON p.id=x.principal_id WHERE x.id=? AND p.org_id=?",
            (credential_id, actor["org_id"]),
        ).fetchone()
        if r is None:
            raise NotFound("binding_not_found")
        live = self._live_principal(c, dict(r), audience=audience)
        if live["credential_id"] != credential_id:
            raise Forbidden("binding_mismatch")
        return live

    def _entity(self, c, actor, kind, ref, *, active=True):
        row = c.execute(
            f"SELECT * FROM {ENTITY_TABLES[kind]} WHERE id=? AND org_id=?", (ref, actor["org_id"])
        ).fetchone()
        if row is None:
            raise NotFound("binding_not_found")
        self._project(c, actor, row["project_id"])
        body = json.loads(row["payload_json"])
        if active:
            if c.execute(
                "SELECT 1 FROM runtime_binding_revocations WHERE kind=? AND target_id=?",
                (kind, ref),
            ).fetchone():
                raise Forbidden("revoked")
            issuer = self._credential(c, actor, row["issuer_credential_id"])
            self._human(issuer)
            if issuer["id"] != row["issuer_id"]:
                raise Forbidden("binding_mismatch")
            expiry = (
                body["policy"]["expires_at"]
                if kind == "policy"
                else body.get("expires_at", body.get("valid_until"))
            )
            if time(expiry) <= datetime.now(UTC):
                raise Forbidden("expired")
        return body

    def _insert(self, c, actor, kind, project, ref, body):
        c.execute(
            f"INSERT INTO {ENTITY_TABLES[kind]} VALUES (?,?,?,?,?,?,?)",
            (
                ref,
                actor["org_id"],
                project,
                actor["id"],
                actor["credential_id"],
                json.dumps(body),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._event(
            c,
            actor,
            project,
            None,
            "runtime_binding." + kind + "_issued",
            1,
            {"ref": ref, "authority_effect": "explicit-human-issuance"},
        )

    def _command(self, actor, key, path, model, payload, command, guard, audience="control"):
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise StoreError("idempotency_conflict")
        body = model.model_validate(payload).model_dump(mode="json")
        scoped_key = canonical_digest({"credential": actor["credential_id"], "key": key})
        return self._mutate(
            actor,
            scoped_key,
            "POST",
            path,
            canonical_digest(body),
            lambda c, a: command(c, a, body),
            audience=audience,
            replay_guard=lambda c, a, r: guard(c, a, r, body),
        )

    def issue_policy(self, actor, project, key, payload):
        def command(c, a, body):
            self._human(a)
            self._project(c, a, project)
            now = datetime.now(UTC)
            expiry = time(body["expires_at"])
            if not now < expiry <= now + timedelta(hours=24):
                raise Forbidden("expired")
            if a["expires_at"] and expiry > time(a["expires_at"]):
                raise Forbidden("expired")
            ref = "runtime_policy_" + secrets.token_hex(16)
            projection = {
                k: body[k]
                for k in (
                    "version",
                    "assurance",
                    "max_financial_age_seconds",
                    "max_evidence_age_seconds",
                    "provider_unknown",
                )
            }
            projection.update(admission_policy_ref=ref, revision=1, operator_issuer=a["id"])
            result = dto.RuntimePolicyRecord(
                policy_ref=ref,
                revision=1,
                issuer=a["id"],
                operator_issuer=a["id"],
                project=project,
                admission_policy_digest=canonical_digest(projection),
                profile_digest=canonical_digest(body),
                issued_at=now,
                policy=body,
            ).model_dump(mode="json")
            self._insert(c, a, "policy", project, ref, result)
            return 201, result

        return self._command(
            actor,
            key,
            f"/projects/{project}/runtime-policies",
            dto.IssueRuntimePolicy,
            payload,
            command,
            lambda c, a, r, b: (self._human(a), self._entity(c, a, "policy", r["policy_ref"])),
        )

    def _current(self, c, actor, enrollment_ref, *, publisher=False):
        e = self._entity(c, actor, "enrollment", enrollment_ref)
        scope = e["scope"]
        policy = self._entity(c, actor, "policy", scope["policy_ref"])
        subject = self._credential(c, actor, scope["credential"])
        pub = self._credential(c, actor, e["publisher_credential_id"], e["publisher_audience"])
        if (
            subject["id"] != scope["principal"]
            or subject["kind"] != "agent"
            or pub["kind"] not in {"worker", "agent"}
        ):
            raise Forbidden("binding_mismatch")
        self._project(c, subject, scope["project"])
        self._project(c, pub, scope["project"])
        external_scope = c.execute(
            "SELECT destination,actions_json FROM external_agent_scopes WHERE credential_id=?",
            (scope["credential"],),
        ).fetchone()
        if (
            external_scope is None
            or external_scope["destination"] != scope["destination"]
            or "context:read" not in json.loads(external_scope["actions_json"])
        ):
            raise Forbidden("binding_mismatch")
        if pub["kind"] == "agent":
            publisher_scope = c.execute(
                "SELECT destination FROM external_agent_scopes WHERE credential_id=?",
                (pub["credential_id"],),
            ).fetchone()
            if publisher_scope is None or publisher_scope["destination"] != scope["destination"]:
                raise Forbidden("binding_mismatch")
        allowed = (
            {e["publisher_credential_id"]}
            if publisher
            else {scope["credential"], e["publisher_credential_id"]}
        )
        if actor["credential_id"] not in allowed:
            raise Forbidden("actor_not_eligible")
        parent = require_active_agent_lineage(c, e["parent_grant_ref"])
        bounds = json.loads(parent["bounds_json"])
        if (
            parent["org_id"] != actor["org_id"]
            or parent["project_id"] != scope["project"]
            or parent["subject_credential_id"] != scope["credential"]
            or parent["revision"] != e["parent_revision"]
            or parent["epoch"] != e["parent_epoch"]
            or bounds["model"] != policy["policy"]["model"]
            or bounds["destination"] != scope["destination"]
        ):
            raise Forbidden("binding_mismatch")
        context = c.execute(
            "SELECT * FROM external_agent_contexts WHERE id=? AND project_id=?",
            (scope["context_ref"], scope["project"]),
        ).fetchone()
        if (
            context is None
            or context["credential_id"] != scope["credential"]
            or context["destination"] != scope["destination"]
        ):
            raise Forbidden("binding_mismatch")
        creator = self._live_principal(c, {"id": context["created_by"], "org_id": actor["org_id"]})
        self._human(creator)
        context_body = json.loads(context["payload_json"])
        if (
            context_body.get("disclosure_approved") is not True
            or "body" not in context_body
            or canonical_digest(context_body) != scope["context_digest"]
        ):
            raise Forbidden("binding_mismatch")
        grant = c.execute(
            "SELECT * FROM runtime_authority_grants WHERE id=?", (scope["grant"],)
        ).fetchone()
        return e, policy, dict(grant), parent, bounds

    def issue_enrollment(self, actor, project, key, payload):
        def command(c, a, b):
            self._human(a)
            self._project(c, a, project)
            policy = self._entity(c, a, "policy", b["policy_ref"])
            if (
                b["expected_policy_revision"] != 1
                or policy["project"] != project
                or policy["issuer"] != a["id"]
            ):
                raise Forbidden("binding_mismatch")
            sub = self._credential(c, a, b["subject_credential_id"])
            pub = self._credential(c, a, b["publisher_credential_id"], b["publisher_audience"])
            if sub["kind"] != "agent" or pub["kind"] not in {"agent", "worker"}:
                raise Forbidden("actor_not_eligible")
            self._project(c, sub, project)
            self._project(c, pub, project)
            parent = require_active_agent_lineage(c, b["parent_agent_authority_grant_id"])
            bounds = json.loads(parent["bounds_json"])
            if (
                parent["org_id"] != a["org_id"]
                or parent["project_id"] != project
                or parent["subject_credential_id"] != sub["credential_id"]
                or parent["revision"] != b["expected_parent_revision"]
                or parent["epoch"] != b["expected_parent_epoch"]
                or bounds["destination"] != b["destination"]
                or bounds["model"] != policy["policy"]["model"]
                or policy["policy"]["max_exposure_cents"] > bounds["budget_cents"]
            ):
                raise Forbidden("binding_mismatch")
            now = datetime.now(UTC)
            expiry = time(b["expires_at"])
            limits = [
                time(policy["policy"]["expires_at"]),
                time(bounds["expires_at"]),
                time(sub["expires_at"]),
            ]
            if pub["expires_at"]:
                limits.append(time(pub["expires_at"]))
            if not now < expiry <= min(limits):
                raise Forbidden("expired")
            ref = "runtime_enrollment_" + secrets.token_hex(16)
            grant = "runtime_grant_" + secrets.token_hex(16)
            scope = dto.RuntimeScope(
                issuer=a["id"],
                publisher_ref=b["publisher_ref"],
                implementation=b["pins"]["terminal_implementation"],
                enrollment=ref,
                principal=sub["id"],
                credential=sub["credential_id"],
                opaque_owner_ref=b["opaque_owner_ref"],
                project=project,
                destination=b["destination"],
                session_ref=b["session_ref"],
                grant=grant,
                grant_revision=1,
                grant_epoch=1,
                policy_ref=b["policy_ref"],
                policy_digest=policy["admission_policy_digest"],
                actions_digest=canonical_digest({"actions": policy["policy"]["allowed_actions"]}),
                role=b["role"],
                context_ref=b["context_ref"],
                context_digest=b["context_digest"],
            ).model_dump(mode="json")
            self._adapter().verify_enrollment(c, a, b, scope, b["pins"])
            record = dto.RuntimeEnrollmentRecord(
                enrollment_ref=ref,
                revision=1,
                issuer=a["id"],
                issued_at=now,
                publisher_credential_id=pub["credential_id"],
                publisher_audience=b["publisher_audience"],
                scope=scope,
                pins=b["pins"],
                expires_at=expiry,
            ).model_dump(mode="json")
            stored = {
                **record,
                "parent_grant_ref": parent["id"],
                "parent_revision": parent["revision"],
                "parent_epoch": parent["epoch"],
            }
            self._insert(c, a, "enrollment", project, ref, stored)
            c.execute(
                "INSERT INTO runtime_authority_grants(id,enrollment_id,parent_grant_id) "
                "VALUES (?,?,?)",
                (grant, ref, parent["id"]),
            )
            # Validate the same enrolled state/context before committing human issuance.
            self._current(c, pub, ref, publisher=True)
            return 201, record

        return self._command(
            actor,
            key,
            f"/projects/{project}/runtime-enrollments",
            dto.IssueRuntimeEnrollment,
            payload,
            command,
            lambda c, a, r, b: (
                self._human(a),
                self._entity(c, a, "enrollment", r["enrollment_ref"]),
            ),
        )

    def issue_financial(self, actor, project, key, payload):
        def command(c, a, b):
            self._human(a)
            e = self._entity(c, a, "enrollment", b["enrollment_ref"])
            p = self._entity(c, a, "policy", b["policy_ref"])
            if (
                e["scope"]["project"] != project
                or e["scope"]["policy_ref"] != b["policy_ref"]
                or p["operator_issuer"] != a["id"]
                or b["expected_enrollment_revision"] != 1
                or b["expected_policy_revision"] != 1
            ):
                raise Forbidden("binding_mismatch")
            pub = self._credential(c, a, e["publisher_credential_id"], e["publisher_audience"])
            self._current(c, pub, b["enrollment_ref"], publisher=True)
            now = datetime.now(UTC)
            observed, expiry = time(b["observed_at"]), time(b["valid_until"])
            if observed > now or now - observed > timedelta(
                seconds=p["policy"]["max_financial_age_seconds"]
            ):
                raise Forbidden("stale_financial")
            if (
                not now
                < expiry
                <= min(
                    time(e["expires_at"]),
                    observed + timedelta(seconds=p["policy"]["max_financial_age_seconds"]),
                )
            ):
                raise Forbidden("expired")
            ref = "runtime_financial_" + secrets.token_hex(16)
            result = dto.RuntimeFinancialRecord(
                evidence_class="authenticated-operator-attestation",
                attestation_ref=ref,
                issuer=a["id"],
                opaque_owner_ref=e["scope"]["opaque_owner_ref"],
                project=project,
                destination=e["scope"]["destination"],
                policy_revision=1,
                observed_at=observed,
                asserted_at=now,
                valid_until=expiry,
                purchased_credit_balance=0,
                auto_top_up_off=True,
                automatic_reload_off=True,
            ).model_dump(mode="json")
            self._insert(
                c,
                a,
                "financial",
                project,
                ref,
                {**result, "enrollment_ref": b["enrollment_ref"], "policy_ref": b["policy_ref"]},
            )
            return 201, result

        return self._command(
            actor,
            key,
            f"/projects/{project}/runtime-financial-assertions",
            dto.IssueRuntimeFinancialAssertion,
            payload,
            command,
            lambda c, a, r, b: (
                self._human(a),
                self._entity(c, a, "financial", r["attestation_ref"]),
            ),
        )

    def revoke(self, actor, kind, ref, key, payload):
        def command(c, a, b):
            self._human(a)
            row = self._entity(c, a, kind, ref, active=False)
            if b["expected_revision"] != 1:
                raise Conflict("revision_conflict")
            if c.execute(
                "SELECT 1 FROM runtime_binding_revocations WHERE kind=? AND target_id=?",
                (kind, ref),
            ).fetchone():
                raise Conflict("revision_conflict")
            c.execute(
                "INSERT INTO runtime_binding_revocations VALUES (?,?,?,?,?,?)",
                (
                    secrets.token_hex(32),
                    kind,
                    ref,
                    a["org_id"],
                    a["id"],
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._event(
                c,
                a,
                row.get("project", row.get("scope", {}).get("project")),
                None,
                "runtime_binding.revoked",
                2,
                {"kind": kind, "ref": ref},
            )
            return 200, {"ref": ref, "revision": 2}

        return self._command(
            actor,
            key,
            f"/runtime-{kind}/{ref}/revoke",
            dto.RevokeRuntimeRecord,
            payload,
            command,
            lambda c, a, r, b: (self._human(a), self._entity(c, a, kind, ref, active=False)),
        )

    def revoke_policy(self, actor, ref, key, payload):
        return self.revoke(actor, "policy", ref, key, payload)

    def revoke_enrollment(self, actor, ref, key, payload):
        return self.revoke(actor, "enrollment", ref, key, payload)

    def revoke_financial(self, actor, ref, key, payload):
        return self.revoke(actor, "financial", ref, key, payload)

    def _snapshot(self, c, actor, body, publisher=False):
        e, p, g, parent, bounds = self._current(
            c, actor, body["enrollment_ref"], publisher=publisher
        )
        if any(
            body[n] != 1
            for n in (
                "expected_enrollment_revision",
                "expected_grant_revision",
                "expected_grant_epoch",
            )
        ):
            raise Conflict("revision_conflict")
        f = self._entity(c, actor, "financial", body["financial_ref"])
        if (
            f["enrollment_ref"] != body["enrollment_ref"]
            or f["policy_ref"] != e["scope"]["policy_ref"]
            or f["issuer"] != p["operator_issuer"]
        ):
            raise Forbidden("binding_mismatch")
        now = datetime.now(UTC)
        if now - time(f["observed_at"]) > timedelta(
            seconds=p["policy"]["max_financial_age_seconds"]
        ):
            raise Forbidden("stale_financial")
        remaining = p["policy"]["max_native_operations"] - g["reserved_units"]
        exposure = min(
            p["policy"]["max_exposure_cents"] - g["reserved_cents"],
            bounds["budget_cents"] - parent["reserved_cents"],
        )
        result = dto.RuntimeBindingSnapshot(
            version="tessera-runtime-binding-v1",
            scope=e["scope"],
            pins=e["pins"],
            enrollment_revision=1,
            policy_revision=1,
            financial_ref=body["financial_ref"],
            financial_issuer=f["issuer"],
            financial_observed_at=time(f["observed_at"]),
            financial_asserted_at=time(f["asserted_at"]),
            financial_valid_until=time(f["valid_until"]),
            expires_at=min(time(e["expires_at"]), time(f["valid_until"])),
            remaining_native_operations=max(0, remaining),
            remaining_exposure_cents=max(0, exposure),
            provider_entitlement="unknown",
            provider_billing_guarantee="unknown",
        ).model_dump(mode="json")
        return result, e, p, g, parent, bounds

    def resolve_binding(self, actor, key, payload, audience="control"):
        return self._command(
            actor,
            key,
            "/" + audience + "/runtime-bindings/resolve",
            dto.ResolveRuntimeBinding,
            payload,
            lambda c, a, b: (200, self._snapshot(c, a, b)[0]),
            lambda c, a, r, b: r.update(self._snapshot(c, a, b)[0]),
            audience,
        )

    def _read(self, actor, callback, audience):
        c = _connect(self.database_path)
        try:
            c.execute("BEGIN")
            return callback(c, self._live_principal(c, actor, audience=audience))
        finally:
            c.close()

    def resolve_financial(self, actor, ref, audience="control"):
        def read(c, a):
            f = self._entity(c, a, "financial", ref)
            self._snapshot(
                c,
                a,
                {
                    "enrollment_ref": f["enrollment_ref"],
                    "expected_enrollment_revision": 1,
                    "financial_ref": ref,
                    "expected_grant_revision": 1,
                    "expected_grant_epoch": 1,
                },
            )
            return {k: f[k] for k in dto.RuntimeFinancialRecord.model_fields}

        return self._read(actor, read, audience)

    @staticmethod
    def _next_root_predecessor(c, enrollment_ref, expected):
        """Only immutable returned settlement can advance the active projection."""
        fence = c.execute(
            "SELECT * FROM runtime_native_root_fences WHERE enrollment_id=?",
            (enrollment_ref,),
        ).fetchone()
        if fence is None:
            return None
        if fence["state"] != "native-returned":
            raise Conflict("operation_uncertain")
        prior = c.execute(
            "SELECT o.payload_json,o.state,s.disposition FROM runtime_native_operations o "
            "JOIN runtime_native_claims c ON c.operation_id=o.id "
            "JOIN runtime_native_settlements s ON s.claim_id=c.id "
            "WHERE o.id=? AND o.enrollment_id=?",
            (fence["operation_id"], enrollment_ref),
        ).fetchone()
        if (
            prior is None
            or prior["state"] != "native-returned"
            or prior["disposition"] != "native-returned"
        ):
            raise Conflict("operation_uncertain")
        previous = json.loads(prior["payload_json"])["expected"]
        if (
            expected["request_ref"] == previous["request_ref"]
            or expected["sequence"] <= previous["sequence"]
            or expected["order_revision"] < previous["order_revision"]
        ):
            raise Conflict("operation_replayed")
        return fence["operation_id"]

    @staticmethod
    def _require_owned_claim_fence(c, enrollment_ref, operation_ref):
        if not c.execute(
            "SELECT 1 FROM runtime_native_root_fences "
            "WHERE enrollment_id=? AND operation_id=? AND state='claimed'",
            (enrollment_ref, operation_ref),
        ).fetchone():
            raise Conflict("claim_mismatch")

    def allocate_operation(self, actor, key, payload, audience="control"):
        def command(c, a, b):
            snapshot, e, p, *_ = self._snapshot(c, a, b, publisher=True)
            if b["action"] not in p["policy"]["allowed_actions"] or b["pins"] != e["pins"]:
                raise Forbidden("binding_mismatch")
            if b["native_phase"] and b["native_phase"] not in p["policy"]["allowed_native_phases"]:
                raise Forbidden("binding_mismatch")
            predecessor = self._next_root_predecessor(c, e["enrollment_ref"], b)
            self._adapter().verify_allocation(c, a, snapshot, b)
            root = canonical_digest(
                {
                    "enrollment": e["enrollment_ref"],
                    "request": b["request_ref"],
                    "phase": b["native_phase"],
                    "sequence": b["sequence"],
                }
            )
            if c.execute(
                "SELECT 1 FROM runtime_native_operations WHERE root_ref=?", (root,)
            ).fetchone():
                raise Conflict("operation_replayed")
            # A different phase/sequence/key cannot re-mint an earlier request.
            for old in c.execute(
                "SELECT payload_json FROM runtime_native_operations WHERE enrollment_id=?",
                (e["enrollment_ref"],),
            ):
                if json.loads(old[0])["expected"]["request_ref"] == b["request_ref"]:
                    raise Conflict("operation_replayed")
            now = datetime.now(UTC)
            ref = secrets.token_hex(32)
            op = dto.RuntimeOperation(
                operation_ref=ref,
                root_ref=root,
                nonce=secrets.token_hex(32),
                ticket=secrets.token_hex(32),
                actor_credential_id=a["credential_id"],
                binding=snapshot,
                expected=b,
                expires_at=min(
                    time(snapshot["expires_at"]),
                    now + timedelta(seconds=p["policy"]["max_operation_age_seconds"]),
                ),
                revision=1,
                state="allocated",
            ).model_dump(mode="json")
            c.execute(
                "INSERT INTO runtime_native_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ref,
                    root,
                    e["enrollment_ref"],
                    a["credential_id"],
                    op["nonce"],
                    op["ticket"],
                    json.dumps({**op, "_predecessor_ref": predecessor}),
                    "allocated",
                    1,
                    now.isoformat(),
                ),
            )
            self._event(
                c,
                a,
                snapshot["scope"]["project"],
                None,
                "runtime_binding.operation_allocated",
                1,
                {"operation_ref": ref},
            )
            return 201, op

        return self._command(
            actor,
            key,
            "/" + audience + "/runtime-operations",
            dto.AllocateRuntimeOperation,
            payload,
            command,
            lambda c, a, r, b: self._operation(c, a, r["operation_ref"], publisher=True),
            audience,
        )

    def _operation(self, c, actor, ref, publisher=False, active=True):
        row = c.execute("SELECT * FROM runtime_native_operations WHERE id=?", (ref,)).fetchone()
        if row is None:
            raise NotFound("binding_not_found")
        op = json.loads(row["payload_json"])
        op.pop("_predecessor_ref", None)
        e = self._entity(c, actor, "enrollment", row["enrollment_id"], active=active)
        allowed = (
            {e["publisher_credential_id"]}
            if publisher
            else {e["publisher_credential_id"], e["scope"]["credential"]}
        )
        if actor["credential_id"] not in allowed:
            raise Forbidden("actor_not_eligible")
        if active:
            self._snapshot(c, actor, op["expected"], publisher=publisher)
        op.update(state=row["state"], revision=row["revision"])
        return op

    def get_operation(self, actor, ref, audience="control"):
        return self._read(actor, lambda c, a: self._operation(c, a, ref), audience)

    def _claim_current(self, c, actor, ref, body):
        op = self._operation(c, actor, ref, publisher=True)
        if time(op["expires_at"]) <= datetime.now(UTC):
            raise Forbidden("expired")
        for n in ("nonce", "ticket"):
            if body[n] != op[n]:
                raise Forbidden("claim_mismatch")
        for n in (
            "pins",
            "financial_ref",
            "request_ref",
            "final_fingerprint",
            "order_revision",
            "sequence",
            "expected_grant_revision",
            "expected_grant_epoch",
            "expected_enrollment_revision",
        ):
            if body[n] != op["expected"][n]:
                raise Forbidden("claim_mismatch")
        window = self._adapter().verify_claim(c, actor, op, body)
        if (
            not isinstance(window, NativeCheckWindow)
            or window.local_check_ref != body["local_check_ref"]
            or window.quota_check_ref != body["quota_check_ref"]
        ):
            raise Forbidden("binding_mismatch")
        expected_window = {
            "operation_ref": ref,
            "actor_credential_id": actor["credential_id"],
            "nonce": op["nonce"],
            "scope_digest": canonical_digest(op["binding"]["scope"]),
            "pins_digest": canonical_digest(body["pins"]),
            "request_ref": body["request_ref"],
            "final_fingerprint": body["final_fingerprint"],
            "order_revision": body["order_revision"],
            "sequence": body["sequence"],
        }
        if any(getattr(window, n) != value for n, value in expected_window.items()):
            raise Forbidden("binding_mismatch")
        now = datetime.now(UTC)
        _, _, p, *_ = self._snapshot(c, actor, op["expected"], publisher=True)
        max_age = timedelta(seconds=p["policy"]["max_evidence_age_seconds"])
        if (
            not now - max_age <= time(window.snapshot_checked_at) <= time(window.checked_at) <= now
            or not now < time(window.valid_until) <= time(window.checked_at) + max_age
        ):
            raise Forbidden("stale_evidence")
        return op, window

    def claim_operation(self, actor, ref, key, payload, audience="control"):
        def command(c, a, b):
            op, window = self._claim_current(c, a, ref, b)
            if op["state"] != "allocated":
                raise Conflict("operation_replayed")
            if b["expected_operation_revision"] != op["revision"]:
                raise Conflict("revision_conflict")
            if op["expected"]["action"] != "runtime:invoke":
                raise Forbidden("binding_mismatch")
            snapshot, e, p, g, parent, bounds = self._snapshot(c, a, op["expected"], publisher=True)
            predecessor = self._next_root_predecessor(c, e["enrollment_ref"], op["expected"])
            stored = c.execute(
                "SELECT payload_json FROM runtime_native_operations WHERE id=?", (ref,)
            ).fetchone()
            if json.loads(stored[0]).get("_predecessor_ref") != predecessor:
                raise Conflict("operation_replayed")
            cents = p["policy"]["exposure_per_operation_cents"]
            if (
                snapshot["remaining_native_operations"] < 1
                or snapshot["remaining_exposure_cents"] < cents
            ):
                raise Forbidden("budget_exhausted")
            # BEGIN IMMEDIATE serializes claims and parent reservations.
            c.execute(
                "UPDATE runtime_authority_grants SET reserved_units=reserved_units+1,"
                "reserved_cents=reserved_cents+? WHERE id=?",
                (cents, g["id"]),
            )
            c.execute(
                "UPDATE agent_authority_grants SET reserved_cents=reserved_cents+? WHERE id=?",
                (cents, parent["id"]),
            )
            claim = secrets.token_hex(32)
            result = dto.RuntimeClaimReceipt(
                version="tessera-runtime-claim-v1",
                operation_ref=ref,
                root_ref=op["root_ref"],
                claim_ref=claim,
                claim_revision=2,
                actor_credential_id=a["credential_id"],
                scope=snapshot["scope"],
                pins=op["expected"]["pins"],
                nonce=op["nonce"],
                ticket=op["ticket"],
                request_ref=b["request_ref"],
                final_fingerprint=b["final_fingerprint"],
                financial_ref=b["financial_ref"],
                local_check_ref=b["local_check_ref"],
                quota_check_ref=b["quota_check_ref"],
                order_revision=b["order_revision"],
                sequence=b["sequence"],
                reserved_native_operations=1,
                reserved_exposure_cents=cents,
                remaining_native_operations=snapshot["remaining_native_operations"] - 1,
                remaining_exposure_cents=snapshot["remaining_exposure_cents"] - cents,
                expires_at=min(time(op["expires_at"]), time(window.valid_until)),
                delivery="first-claim",
                state="claimed",
            ).model_dump(mode="json")
            c.execute(
                "INSERT INTO runtime_native_claims VALUES (?,?,?,?)",
                (claim, ref, json.dumps(result), datetime.now(UTC).isoformat()),
            )
            c.execute(
                "UPDATE runtime_native_operations SET state='claimed',revision=2 "
                "WHERE id=? AND state='allocated'",
                (ref,),
            )
            if predecessor is None:
                c.execute(
                    "INSERT INTO runtime_native_root_fences VALUES (?,?,?)",
                    (e["enrollment_ref"], ref, "claimed"),
                )
            elif (
                c.execute(
                    "UPDATE runtime_native_root_fences SET operation_id=?,state='claimed' "
                    "WHERE enrollment_id=? AND operation_id=? AND state='native-returned'",
                    (ref, e["enrollment_ref"], predecessor),
                ).rowcount
                != 1
            ):
                raise Conflict("operation_uncertain")
            self._event(
                c,
                a,
                snapshot["scope"]["project"],
                None,
                "runtime_binding.operation_claimed",
                2,
                {"operation_ref": ref, "claim_ref": claim},
            )
            return 201, result

        def replay(c, a, result, b):
            op, _ = self._claim_current(c, a, ref, b)
            current = c.execute(
                "SELECT payload_json FROM runtime_native_claims WHERE operation_id=?", (ref,)
            ).fetchone()
            if current is None:
                raise Conflict("claim_mismatch")
            saved = json.loads(current[0])
            if saved["claim_ref"] != result["claim_ref"]:
                raise Conflict("claim_mismatch")
            result.update(delivery="readback-only", state=op["state"])

        status, result = self._command(
            actor,
            key,
            "/" + audience + f"/runtime-operations/{ref}/claim",
            dto.ClaimRuntimeOperation,
            payload,
            command,
            replay,
            audience,
        )
        return (200 if result["delivery"] == "readback-only" else status), result

    def guarded_native_continuation(
        self, actor, ref, claim_ref, payload, continuation, *, audience="control"
    ):
        """Co-located trusted owner bridge only; never exposed as an HTTP permit.

        Holds the canonical writer fence across the exact continuation. The caller
        must be the trusted installed owner and supply an immutable lowered native
        primitive, not model input. Cross-process callers must fail closed instead
        of caching this method's result. No rollback can refund the committed claim.
        """
        c = _connect(self.database_path)
        try:
            c.execute("BEGIN IMMEDIATE")
            a = self._live_principal(c, actor, audience=audience)
            recovery = c.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()
            if recovery is None or recovery[0] != "active":
                raise Forbidden("revoked")
            body = dto.ClaimRuntimeOperation.model_validate(payload).model_dump(mode="json")
            op, _ = self._claim_current(c, a, ref, body)
            row = c.execute(
                "SELECT payload_json FROM runtime_native_claims WHERE id=? AND operation_id=?",
                (claim_ref, ref),
            ).fetchone()
            if row is None or op["state"] != "claimed":
                raise Conflict("claim_mismatch")
            self._require_owned_claim_fence(c, op["binding"]["scope"]["enrollment"], ref)
            claim = json.loads(row[0])
            if claim["actor_credential_id"] != a["credential_id"]:
                raise Forbidden("claim_mismatch")
            if time(claim["expires_at"]) <= datetime.now(UTC):
                raise Forbidden("expired")
            _, _, _, grant, *_ = self._snapshot(c, a, op["expected"], publisher=True)
            if (
                grant["reserved_units"] < 1
                or grant["reserved_cents"] < claim["reserved_exposure_cents"]
            ):
                raise Forbidden("claim_mismatch")
            # Exactly one bridge entry: uncertainty survives crash/exception before settlement.
            if c.execute(
                "SELECT 1 FROM runtime_native_entries WHERE claim_id=?", (claim_ref,)
            ).fetchone():
                raise Conflict("operation_replayed")
            c.execute(
                "INSERT INTO runtime_native_entries VALUES (?,?)",
                (claim_ref, datetime.now(UTC).isoformat()),
            )
            self._event(
                c,
                a,
                claim["scope"]["project"],
                None,
                "runtime_binding.native_entry",
                2,
                {"claim_ref": claim_ref},
            )
            c.commit()
            # Reacquire and recheck after durable entry, before crossing native boundary.
            c.execute("BEGIN IMMEDIATE")
            a = self._live_principal(c, actor, audience=audience)
            op, _ = self._claim_current(c, a, ref, body)
            self._require_owned_claim_fence(c, op["binding"]["scope"]["enrollment"], ref)
            if op["state"] != "claimed" or time(claim["expires_at"]) <= datetime.now(UTC):
                raise Forbidden("claim_mismatch")
            if (
                c.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()[0]
                != "active"
            ):
                raise Forbidden("revoked")
            return continuation()
        finally:
            c.rollback()
            c.close()

    def settle_operation(self, actor, ref, key, payload, audience="control"):
        def command(c, a, b):
            op = self._operation(c, a, ref, publisher=True, active=False)
            row = c.execute(
                "SELECT * FROM runtime_native_claims WHERE operation_id=?", (ref,)
            ).fetchone()
            if row is None or row["id"] != b["claim_ref"] or b["expected_claim_revision"] != 2:
                raise Conflict("claim_mismatch")
            if op["state"] != "claimed":
                raise Conflict("operation_replayed")
            self._require_owned_claim_fence(c, op["binding"]["scope"]["enrollment"], ref)
            c.execute(
                "INSERT INTO runtime_native_settlements VALUES (?,?,?,?,?)",
                (
                    secrets.token_hex(32),
                    row["id"],
                    a["credential_id"],
                    b["disposition"],
                    datetime.now(UTC).isoformat(),
                ),
            )
            c.execute(
                "UPDATE runtime_native_operations SET state=?,revision=3 WHERE id=?",
                (b["disposition"], ref),
            )
            c.execute(
                "UPDATE runtime_native_root_fences SET state=? WHERE operation_id=?",
                (b["disposition"], ref),
            )
            self._event(
                c,
                a,
                op["binding"]["scope"]["project"],
                None,
                "runtime_binding.operation_settled",
                3,
                {"operation_ref": ref, "disposition": b["disposition"]},
            )
            return 200, self._operation(c, a, ref, publisher=True, active=False)

        return self._command(
            actor,
            key,
            "/" + audience + f"/runtime-operations/{ref}/settle",
            dto.SettleRuntimeOperation,
            payload,
            command,
            lambda c, a, r, b: self._operation(c, a, ref, publisher=True, active=False),
            audience,
        )
