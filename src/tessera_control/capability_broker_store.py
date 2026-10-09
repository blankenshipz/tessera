"""Control-only package signer, fresh authority, and historical journal intake.

No HTTP activation route and no default signing key. The worker calls this through
an authenticated trusted channel; it must never receive the HMAC key. Synthetic
packages cannot authorize real effects. Existing execution_enabled stays false.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from .capabilities import CapabilityStore
from .capability_package import BrokerPackage, SignedCapabilityPackage, encode
from .project_artifacts import FilesystemProjectArtifactBackend
from .store import Conflict, Forbidden, NotFound, canonical_digest


class CapabilityBrokerStore(CapabilityStore):
    def __init__(self, state_dir, *, signing_key: bytes):
        super().__init__(state_dir)
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("explicit control-only signing key required")
        self.__key = signing_key

    def _signature(self, payload):
        return hmac.new(self.__key, b"tessera-capability-v1\0" + payload, "sha256").hexdigest()

    def issue(self, worker, operation_id):
        def command(connection, live):
            op, policy, expiry = self._operation_current(connection, live, operation_id)
            binding = json.loads(op["binding_json"])
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (binding["execution_lease_id"],)
            ).fetchone()
            grant = self._active_worker_grant(connection, live, lease["grant_id"])
            declaration = json.loads(policy["declaration_json"])
            basis = declaration.get("broker_basis")
            if not basis or basis["data_digest"] != grant["snapshot_digest"]:
                raise Conflict("Explicit approved broker basis must match execution data.")
            body = BrokerPackage(
                mode="synthetic",
                org_id=live["org_id"],
                project_id=policy["project_id"],
                task_id=policy["task_id"],
                worker_id=live["id"],
                operation_id=operation_id,
                policy_id=policy["id"],
                grant_id=grant["id"],
                plan_digest=policy["plan_digest"],
                declaration_digest=policy["declaration_digest"],
                declaration=declaration,
                binding=binding,
                expires_at=expiry.isoformat(),
            )
            payload = encode(body)
            result = dict(
                payload=payload.decode(),
                digest=hashlib.sha256(payload).hexdigest(),
                signature=self._signature(payload),
            )
            self._capability_event(
                connection,
                live,
                policy,
                "broker_package_issued",
                {
                    "operation_id": operation_id,
                    "package_digest": result["digest"],
                    "mode": "synthetic",
                },
            )
            return 201, result

        def guard(connection, live, response):
            self._operation_current(connection, live, operation_id)

        _, result = self._mutate(
            worker,
            "broker-package-" + operation_id,
            "POST",
            f"/worker/capability-operations/{operation_id}/broker-package",
            canonical_digest({}),
            command,
            audience="worker",
            replay_guard=guard,
        )
        return SignedCapabilityPackage(
            result["payload"].encode(), result["digest"], result["signature"]
        )

    def _verify(self, package):
        body = package.decode()
        if not hmac.compare_digest(self._signature(package.payload), package.signature):
            raise Forbidden("Invalid control package signature.")
        return body

    def claim(self, worker, package, journal_id):
        """One control-durable owner across journals/hosts, before any effect."""
        self.check(worker, package)
        body = self._verify(package)
        if not isinstance(journal_id, str) or len(journal_id) != 64:
            raise Forbidden("Invalid broker journal identity.")

        def command(connection, live):
            _, policy, _ = self._operation_current(connection, live, body.operation_id)
            scope = canonical_digest(
                [body.org_id, body.project_id, body.worker_id, body.binding.environment_id]
            )
            outstanding = connection.execute(
                "SELECT 1 FROM events claimed WHERE claimed.event_type='capability.broker_claimed' "
                "AND claimed.org_id=? AND json_extract(claimed.payload_json,'$.scope')=? "
                "AND NOT EXISTS (SELECT 1 FROM events settled "
                "WHERE settled.event_type='capability.broker_journal_reconciled' "
                "AND settled.org_id=claimed.org_id "
                "AND json_extract(settled.payload_json,'$.operation_id')="
                "json_extract(claimed.payload_json,'$.operation_id') "
                "AND json_extract(settled.payload_json,'$.slot_released')=1)",
                (body.org_id, scope),
            ).fetchone()
            if outstanding:
                raise Conflict("Broker target has an unreconciled or unknown effect.")
            result = {
                "operation_id": body.operation_id,
                "journal_id": journal_id,
                "package_digest": package.digest,
                "scope": scope,
            }
            self._capability_event(connection, live, policy, "broker_claimed", result)
            return 201, result

        def guard(connection, live, response):
            self._operation_current(connection, live, body.operation_id)

        return self._mutate(
            worker,
            "broker-claim-" + body.operation_id,
            "POST",
            "/worker/broker/claim/" + body.operation_id,
            canonical_digest([package.digest, journal_id]),
            command,
            audience="worker",
            replay_guard=guard,
        )[1]

    def check(self, worker, package):
        """Uncached, authenticated, transactionally audited check before each step."""
        body = self._verify(package)

        def command(connection, live):
            op, policy, _ = self._operation_current(connection, live, body.operation_id)
            if connection.execute(
                "SELECT 1 FROM events WHERE org_id=? "
                "AND event_type='capability.broker_journal_reconciled' "
                "AND json_extract(payload_json,'$.operation_id')=?",
                (body.org_id, body.operation_id),
            ).fetchone():
                raise Conflict("Broker effect already terminal; reconciliation only.")
            lease = connection.execute(
                "SELECT grant_id FROM execution_leases WHERE id=?",
                (body.binding.execution_lease_id,),
            ).fetchone()
            if (
                body.org_id != live["org_id"]
                or body.worker_id != live["id"]
                or body.project_id != policy["project_id"]
                or body.task_id != policy["task_id"]
                or body.policy_id != policy["id"]
                or body.plan_digest != policy["plan_digest"]
                or body.declaration_digest != policy["declaration_digest"]
                or body.grant_id != lease["grant_id"]
                or body.binding.model_dump(mode="json") != json.loads(op["binding_json"])
                or datetime.fromisoformat(body.expires_at) <= datetime.now(UTC)
            ):
                raise Forbidden("Broker package identity is fenced.")
            self._capability_event(
                connection,
                live,
                policy,
                "broker_authority_checked",
                {
                    "operation_id": body.operation_id,
                    "package_digest": package.digest,
                    "mode": body.mode,
                },
            )
            return 200, {"valid": True}

        # Not an idempotent authority cache.
        import uuid

        self._mutate(
            worker,
            uuid.uuid4().hex,
            "POST",
            "/worker/broker/check",
            package.digest,
            command,
            audience="worker",
        )
        return True

    def reconcile(self, worker, package, bundle, content):
        """Historical evidence only. Never reauthorize, classify, or release bytes.

        A crash after no-overwrite object publication but before SQLite commit
        leaves an orphan reconciled by replay of the same retained journal bundle.
        """
        from tessera_worker.capability_journal import validate_bundle

        body = self._verify(package)
        validate_bundle(bundle, package.digest)
        if type(content) is not bytes or len(content) > body.binding.action.max_bytes:
            raise Conflict("Journal evidence exceeds custody limit.")
        if hashlib.sha256(content).hexdigest() != bundle["content_sha256"]:
            raise Conflict("Journal bytes differ from retained evidence.")
        if len(content) != bundle["size"] or len(content) > body.binding.action.max_bytes:
            raise Conflict("Journal evidence exceeds custody limit.")
        digest = canonical_digest(bundle)

        def command(connection, live):
            _, policy = self._operation(connection, live, body.operation_id)
            if (live["org_id"], live["id"], policy["project_id"]) != (
                body.org_id,
                body.worker_id,
                body.project_id,
            ):
                raise Forbidden("Journal scope differs.")
            action = body.binding.action
            reasons = []
            try:
                self._operation_current(connection, live, body.operation_id)
            except (Conflict, Forbidden, NotFound):
                reasons.append("stale_authority")
            if bundle["state"] != "completed":
                reasons.append("incomplete_effect")
            if bundle["content_sha256"] != action.sha256 or bundle["size"] != action.expected_size:
                reasons.append("artifact_mismatch")
            for receipt in bundle["receipts"]:
                if (
                    receipt["action_id"] != action.id
                    or receipt["artifact_name"] != action.name
                    or receipt["artifact_version"] != action.version
                    or receipt["artifact_sha256"] != action.sha256
                    or receipt["lockfile_sha256"] != action.lockfile_sha256
                    or receipt["evidence"] != body.mode
                    or receipt["max_bytes"] != action.max_bytes
                    or receipt["max_seconds"] != action.max_seconds
                    or receipt["bytes_retained"] > action.max_bytes
                    or receipt["elapsed_ms"] > action.max_seconds * 1000
                ):
                    reasons.append("receipt_identity_or_limit_mismatch")
                    break
            last = bundle["receipts"][-1]
            expected_inventory = (
                [dict(name=action.name, version=action.version, artifact_sha256=action.sha256)]
                if action.kind == "package"
                else []
            )
            if last["installed_packages"] != expected_inventory:
                reasons.append("inventory_mismatch")
            if action.kind == "package" and (
                last["inventory_digest"]
                != canonical_digest(((action.name, action.version, action.sha256),))
                or last["exit_code"] != 0
            ):
                reasons.append("install_verification_mismatch")
            if not last["termination_confirmed"]:
                reasons.append("termination_unknown")
            backend = FilesystemProjectArtifactBackend(
                self.database_path.parent / "project-artifacts"
            )
            artifact_id = "broker-evidence-" + package.digest
            stage = backend.stage(
                body.org_id, body.project_id, artifact_id, bundle["content_sha256"], content
            )
            placement = backend.commit(stage)
            result = {
                "bundle_digest": digest,
                "artifact_id": artifact_id,
                "object_key": placement.object_key,
                "classification": "unclassified",
                "custody": "quarantined_unreleased",
                "assessment": "quarantined" if reasons else "worker_reported",
                "reasons": reasons,
            }
            self._capability_event(
                connection,
                live,
                policy,
                "broker_journal_reconciled",
                {
                    "operation_id": body.operation_id,
                    "journal": bundle,
                    "placement": result,
                    "slot_released": not reasons and last["termination_confirmed"],
                },
            )
            if reasons and policy["status"] == "approved":
                connection.execute(
                    "UPDATE capability_policies SET status='revoked',revision=revision+1 "
                    "WHERE id=?",
                    (policy["id"],),
                )
                self._capability_event(
                    connection,
                    live,
                    {**policy, "revision": policy["revision"] + 1},
                    "automatically_revoked",
                    {
                        "operation_id": body.operation_id,
                        "reason": "broker_incomplete_effect",
                        "termination_confirmed": False,
                    },
                )
            return 201, result

        def guard(connection, live, response):
            self._operation(connection, live, body.operation_id)

        return self._mutate(
            worker,
            "broker-reconcile-" + package.digest,
            "POST",
            "/worker/broker/reconcile/" + body.operation_id,
            digest,
            command,
            audience="worker",
            replay_guard=guard,
        )[1]


class InProcessBrokerAuthority:
    """Offline trusted composition; production requires an authenticated channel.

    Never inject this object or control state into an agent process/sandbox.
    """

    def __init__(self, store, worker):
        self.store, self.worker = store, worker

    def __call__(self, package):
        return self.store.check(self.worker, package)

    def claim(self, package, journal_id):
        return self.store.claim(self.worker, package, journal_id)
