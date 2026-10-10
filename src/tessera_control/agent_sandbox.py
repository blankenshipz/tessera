"""Authority-aware direct Sandbox intents and one-Pod execution fences."""

import json
from dataclasses import asdict

from tessera_worker.agent_sandbox import (
    AgentSandboxBackend,
    SandboxUnsafe,
    collection_handoff,
    manifest,
)

from .agent_sandbox_models import SandboxPlan
from .artifact_manifests import ArtifactManifestStore
from .project_artifact_retention import require_project_artifact_reference_admissible
from .runtime_packages import RuntimePackageStore
from .store import Conflict, Forbidden, NotFound, _connect, canonical_digest


def create_agent_sandbox_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_sandbox_bindings (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        package_digest TEXT NOT NULL, blueprint_json TEXT NOT NULL,
        state_json TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")


class AgentSandboxStore(RuntimePackageStore):
    def __init__(self, state_dir, backend: AgentSandboxBackend | None = None):
        super().__init__(state_dir)
        self.backend = backend

    def _bound(self, connection, actor, operation_id):
        operation = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id=? AND org_id=?",
            (operation_id, actor["org_id"]),
        ).fetchone()
        if operation is None:
            raise NotFound("Sandbox operation was not found.")
        self._project(connection, actor, operation["project_id"])
        if actor["kind"] == "worker":
            if operation["worker_principal_id"] != actor["id"]:
                raise Forbidden("Worker does not own this Sandbox operation.")
        else:
            self._require_human(actor)
        row = connection.execute(
            "SELECT * FROM agent_sandbox_bindings WHERE operation_id=?", (operation_id,)
        ).fetchone()
        package_row = connection.execute(
            "SELECT * FROM execution_runtime_packages WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None or package_row is None or operation["adapter_version"] != "agent-sandbox-v1":
            raise NotFound("Sandbox binding was not found.")
        package = json.loads(package_row["package_json"])
        expected = json.loads(row["blueprint_json"])
        if (
            canonical_digest(package) != row["package_digest"]
            or row["package_digest"] != operation["package_digest"]
            or manifest(package) != expected
        ):
            raise Conflict("Immutable Sandbox binding conflicts with operation identity.")
        return dict(operation), package, expected, json.loads(row["state_json"])

    def _authority(self, connection, actor, operation, package, epoch):
        if actor["kind"] != "worker" or operation["epoch"] != epoch:
            raise Forbidden("Current assigned worker and exact operation epoch required.")
        self._fence_recovered_operation(connection, operation["operation_id"])
        if operation["desired_state"] != "admitted":
            raise Conflict("Sandbox execution is cancelled or fenced.")
        current, _ = self._package(connection, actor, package["lease_id"], epoch, None, None)
        if current["package_digest"] != operation["package_digest"]:
            raise Conflict("Sandbox package is no longer current.")

    def _save(self, connection, actor, operation, state, event):
        if state.get("sandbox_uid"):
            if operation["provider_id"] not in {None, state["sandbox_uid"]}:
                raise Conflict("Sandbox provider UID conflicts with durable operation identity.")
            connection.execute(
                "UPDATE external_operations SET provider_id=coalesce(provider_id,?),updated_at=? "
                "WHERE operation_id=?",
                (state["sandbox_uid"], self._now().isoformat(), operation["operation_id"]),
            )
        connection.execute(
            "UPDATE agent_sandbox_bindings SET state_json=?,updated_at=? WHERE operation_id=?",
            (json.dumps(state, sort_keys=True), self._now().isoformat(), operation["operation_id"]),
        )
        self._event(
            connection,
            actor,
            operation["project_id"],
            operation["task_id"],
            event,
            operation["epoch"],
            {
                "operation_id": operation["operation_id"],
                "sandbox_uid": state.get("sandbox_uid"),
                "phase": state["phase"],
                "execution_proven": False,
            },
        )

    @staticmethod
    def _response(operation, state):
        return {
            "operation_id": operation["operation_id"],
            "desired_state": operation["desired_state"],
            **state,
            "scientific_success": False,
        }

    def _provider(self):
        if self.backend is None:
            raise SandboxUnsafe("operator-configured Sandbox backend unavailable")
        return self.backend

    def _snapshot(self, actor, operation_id, action, body):
        """Tenant/current-authority read, then bounded network reads without a writer."""
        from tessera_worker.agent_sandbox_discovery import discover, prepared

        with _connect(self.database_path) as connection:
            live = self._live_principal(
                connection, actor, audience="worker" if actor["kind"] == "worker" else "control"
            )
            operation, package, expected, state = self._bound(connection, live, operation_id)
            if action in {"create", "run", "release"}:
                self._authority(connection, live, operation, package, body["expected_epoch"])
        target = SandboxPlan.model_validate(package["sandbox"]).target
        backend = self._provider()
        provider, client = discover(backend)
        provider.identity(target)
        if action in {"create", "run", "release"}:
            provider.preflight(target, expected["metadata"]["labels"])
        if action in {"release", "observe", "collect", "cleanup"}:
            _, _, pods = self._capture(provider, target, expected, state)
            if action == "release" and pods:
                provider.preflight(target, pods[0]["metadata"].get("labels", {}))
        else:
            provider.sandbox(target, expected, state["sandbox_uid"])
            if action == "stop":
                provider.pods(target, expected, state["sandbox_uid"])
        return prepared(backend, client, target)

    def create(self, worker, lease_id, key, body):
        epoch = body["expected_epoch"]
        response = self.execution_package(
            worker,
            lease_id,
            key + ":package",
            canonical_digest({"expected_epoch": epoch, "execution_adapter": "agent-sandbox"}),
            epoch,
            execution_adapter="agent-sandbox",
        )[1]
        package = response["package"]
        operation_id = package["operation_id"]
        expected = manifest(package)

        def intent(connection, live):
            operation = response["operation"]
            self._authority(connection, live, operation, package, epoch)
            old = connection.execute(
                "SELECT * FROM agent_sandbox_bindings WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if old is None:
                state = {
                    "phase": "create_intent",
                    "create_attempted": False,
                    "sandbox_uid": None,
                    "release_uid": None,
                    "release_attempted": False,
                    "pvc_uid": None,
                    "pod_history": {},
                    "quiescent": False,
                    "cleanup_attempted": False,
                    "cleanup_acknowledged": False,
                }
                connection.execute(
                    "INSERT INTO agent_sandbox_bindings VALUES (?,?,?,?,?)",
                    (
                        operation_id,
                        response["package_digest"],
                        json.dumps(expected, sort_keys=True),
                        json.dumps(state, sort_keys=True),
                        self._now().isoformat(),
                    ),
                )
                self._save(connection, live, operation, state, "sandbox.create_intent")
            operation, _, _, state = self._bound(connection, live, operation_id)
            return 201, self._response(operation, state)

        self._mutate(
            worker,
            key + ":intent",
            "POST",
            f"/worker/sandbox/leases/{lease_id}",
            canonical_digest(body),
            intent,
            audience="worker",
            replay_guard=lambda c, a, _: self._authority(
                c, a, self._bound(c, a, operation_id)[0], package, epoch
            ),
        )

        # Claim the single create before the external effect. A crash/lost response
        # thereafter permits exact-name lookup only, never another POST or new name.
        claimed = False
        try:
            self._snapshot(worker, operation_id, "create", body)
        except (SandboxUnsafe, OSError, ValueError, RuntimeError) as error:
            raise Conflict(
                "Sandbox prerequisites unavailable; create remains unclaimed."
            ) from error

        def claim(connection, live):
            nonlocal claimed
            operation, package, _, state = self._bound(connection, live, operation_id)
            self._authority(connection, live, operation, package, epoch)
            if not state["create_attempted"]:
                state["create_attempted"] = True
                claimed = True
                self._save(connection, live, operation, state, "sandbox.create_claimed")
            return 200, self._response(operation, state)

        self._mutate(
            worker,
            key + ":claim",
            "POST",
            f"/worker/agent-sandboxes/{operation_id}/create-claim",
            canonical_digest(body),
            claim,
            audience="worker",
            replay_guard=lambda c, a, _: self._authority(
                c, a, self._bound(c, a, operation_id)[0], package, epoch
            ),
        )
        return self._effect(
            worker, operation_id, key + ":effect", "create", body, create_claimed=claimed
        )

    def _capture(self, provider, target, expected, state):
        value = provider.sandbox(target, expected, state["sandbox_uid"])
        if value is None:
            raise SandboxUnsafe("Sandbox absent; absence is not termination evidence")
        state["sandbox_uid"] = value["metadata"]["uid"]
        pvc = provider.pvc(target, expected, state["sandbox_uid"], state["pvc_uid"])
        state["pvc_uid"] = pvc["metadata"]["uid"]
        provider.validate_pvc_set(target, expected, state["sandbox_uid"], state["pvc_uid"])
        pods = provider.pods(target, expected, state["sandbox_uid"])
        history = state["pod_history"]
        for pod in pods:
            uid = pod["metadata"]["uid"]
            gated = uid != state["release_uid"] or not state["release_attempted"]
            provider.validate_pod(expected, pod, gated=gated)
            previous = history.get(uid, {})
            node = pod.get("spec", {}).get("nodeName")
            if previous.get("node") and node != previous["node"]:
                raise SandboxUnsafe("Pod node identity changed")
            terminal = pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}
            statuses = pod.get("status", {}).get("containerStatuses", [])
            terminated = (
                terminal
                and len(statuses) == 1
                and statuses[0].get("name") == "agent"
                and bool(statuses[0].get("state", {}).get("terminated"))
            )
            node_uid = previous.get("node_uid")
            if not gated and node:
                node_uid = provider.node_identity(node, node_uid)
            history[uid] = {
                "node": node,
                "terminated": previous.get("terminated", False) or terminated,
                "gated": gated,
                "name": pod["metadata"]["name"],
                "node_uid": node_uid,
            }
        for item in history.values():
            if item["terminated"]:
                provider.node_identity(item["node"], item["node_uid"])
        conditions = value.get("status", {}).get("conditions", [])
        suspended = any(
            item.get("type") == "Suspended"
            and item.get("status") == "True"
            and item.get("reason") == "PodTerminated"
            and item.get("observedGeneration") == value["metadata"].get("generation")
            for item in conditions
        )
        contradictory = any(
            item.get("status") == "Unknown"
            or item.get("reason")
            in {"PodNotOwned", "MultiplePods", "ReconcilerError", "PodStateUnknown"}
            for item in conditions
        )
        state["quiescent"] = (
            value["spec"]["operatingMode"] == "Suspended"
            and suspended
            and not contradictory
            and not pods
            and all(item["gated"] or item["terminated"] for item in history.values())
            and (not state["release_attempted"] or state["release_uid"] in history)
        )
        state["observed_at"] = self._now().isoformat()
        state["phase"] = "quiescent" if state["quiescent"] else "observed_not_quiescent"
        return value, pvc, pods

    def _effect(self, actor, operation_id, key, action, body, *, create_claimed=False):
        audience = "worker" if actor["kind"] == "worker" else "control"
        try:
            prepared_provider = self._snapshot(actor, operation_id, action, body)
        except (SandboxUnsafe, OSError, ValueError, RuntimeError):
            prepared_provider = None

        def command(connection, live):
            operation, package, expected, state = self._bound(connection, live, operation_id)
            if action in {"create", "run", "release"}:
                self._authority(connection, live, operation, package, body["expected_epoch"])
            elif action == "observe" and operation["desired_state"] == "admitted":
                try:
                    assigned = self._live_principal(
                        connection,
                        {"id": operation["worker_principal_id"], "org_id": operation["org_id"]},
                        audience="worker",
                    )
                    self._authority(connection, assigned, operation, package, operation["epoch"])
                except (Conflict, Forbidden, NotFound, ValueError):
                    connection.execute(
                        "UPDATE external_operations SET desired_state='cancel_requested' "
                        "WHERE operation_id=?",
                        (operation_id,),
                    )
                    operation["desired_state"] = "cancel_requested"
            target = SandboxPlan.model_validate(package["sandbox"]).target
            try:
                if prepared_provider is None:
                    raise SandboxUnsafe("bounded discovery unresolved")
                provider = prepared_provider
                if action in {"create", "run", "release"}:
                    provider.client.before_write = lambda: self._authority(
                        connection, live, operation, package, body["expected_epoch"]
                    )
                provider.identity(target)
                if action in {"create", "run", "release"}:
                    provider.preflight(target, expected["metadata"]["labels"])
                if action == "create":
                    existing = provider.sandbox(target, expected, state["sandbox_uid"])
                    if existing is None and create_claimed:
                        # Current authority is serialized with revocation while this
                        # suspended create happens. Scheduling is still gated later.
                        self._authority(
                            connection, live, operation, package, body["expected_epoch"]
                        )
                        provider.client.request("POST", provider.path(target, "sandbox"), expected)
                    existing = provider.sandbox(target, expected, state["sandbox_uid"])
                    if existing is None:
                        raise SandboxUnsafe("create outcome unknown; lookup only, no resubmission")
                    state["sandbox_uid"] = existing["metadata"]["uid"]
                    state["phase"] = "created_suspended"
                elif action == "run":
                    if state["release_attempted"]:
                        raise SandboxUnsafe("one-Pod release consumed; Sandbox cannot be resumed")
                    value = provider.sandbox(target, expected, state["sandbox_uid"])
                    if value is None:
                        raise SandboxUnsafe("Sandbox create outcome unavailable")
                    if value["spec"]["operatingMode"] == "Suspended":
                        self._authority(
                            connection, live, operation, package, body["expected_epoch"]
                        )
                        provider.change_mode(target, value, "Running")
                    state["phase"] = "awaiting_gated_pod"
                elif action == "release":
                    value, _, pods = self._capture(provider, target, expected, state)
                    if (
                        not state["release_attempted"]
                        or len(pods) != 1
                        or pods[0]["metadata"]["uid"] != state["release_uid"]
                        or value["spec"]["operatingMode"] != "Running"
                    ):
                        raise SandboxUnsafe("exact one-use Pod release identity unavailable")
                    provider.validate_pod(expected, pods[0], gated=True)
                    provider.preflight(target, pods[0]["metadata"]["labels"])
                    self._authority(connection, live, operation, package, body["expected_epoch"])
                    provider.release(target, pods[0])
                    state["phase"] = "release_sent"
                    state["pod_history"][state["release_uid"]]["gated"] = False
                elif action == "stop":
                    # Stop never needs a fresh execution grant and never deletes
                    # Sandbox/PVC. It is safe recovery, not renewed dispatch authority.
                    value = provider.sandbox(target, expected, state["sandbox_uid"])
                    if value is None:
                        raise SandboxUnsafe("Sandbox absence does not prove a stop")
                    provider.pods(target, expected, state["sandbox_uid"])
                    if value["spec"]["operatingMode"] != "Suspended":
                        provider.change_mode(target, value, "Suspended")
                    state["phase"] = "stop_sent_not_confirmed"
                    state["quiescent"] = False
                elif action == "observe":
                    self._capture(provider, target, expected, state)
                    if operation["desired_state"] == "cancel_requested":
                        value = provider.sandbox(target, expected, state["sandbox_uid"])
                        if value["spec"]["operatingMode"] != "Suspended":
                            provider.change_mode(target, value, "Suspended")
                            state["quiescent"] = False
                            state["phase"] = "stop_sent_not_confirmed"
                state.pop("error", None)
            except (SandboxUnsafe, OSError, ValueError, RuntimeError):
                # Preserve intent and custody on any uncertainty. Never translate
                # provider errors/403/410/timeout into absent or stopped.
                state["quiescent"] = False
                state["phase"] = "uncertain"
                state["error"] = "provider_identity_effect_or_evidence_unresolved"
            self._save(connection, live, operation, state, "sandbox." + action + "_observed")
            return 200, self._response(operation, state)

        def guard(connection, live, cached):
            operation, package, _, _ = self._bound(connection, live, operation_id)
            if action in {"create", "run", "release"}:
                self._authority(connection, live, operation, package, body["expected_epoch"])

        return self._mutate(
            actor,
            key,
            "POST",
            f"/{audience}/agent-sandboxes/{operation_id}/{action}",
            canonical_digest(body),
            command,
            audience=audience,
            replay_guard=guard,
        )

    def action(self, actor, operation_id, key, action, body):
        if action == "release":
            fresh_claim = False
            release_provider = (
                None
                if self.status(actor, operation_id)["release_attempted"]
                else self._snapshot(actor, operation_id, "release", body)
            )

            def claim(connection, live):
                nonlocal fresh_claim
                operation, package, expected, state = self._bound(connection, live, operation_id)
                self._authority(connection, live, operation, package, body["expected_epoch"])
                if not state["release_attempted"]:
                    provider = release_provider
                    target = SandboxPlan.model_validate(package["sandbox"]).target
                    provider.preflight(target, expected["metadata"]["labels"])
                    value, _, pods = self._capture(provider, target, expected, state)
                    if value["spec"]["operatingMode"] != "Running" or len(pods) != 1:
                        raise Conflict("Exactly one gated Pod must exist before release.")
                    provider.validate_pod(expected, pods[0], gated=True)
                    uid = pods[0]["metadata"]["uid"]
                    if any(old != uid for old in state["pod_history"]):
                        raise Conflict("Replacement Pod cannot consume original release authority.")
                    state.update(release_uid=uid, release_attempted=True, phase="release_claimed")
                    # Uncertainty after this durable fence must count as possible
                    # execution, even if the PUT response is lost or never sent.
                    state["pod_history"][uid]["gated"] = False
                    fresh_claim = True
                    self._save(connection, live, operation, state, "sandbox.release_claimed")
                return 200, self._response(operation, state)

            self._mutate(
                actor,
                key + ":claim",
                "POST",
                f"/worker/agent-sandboxes/{operation_id}/release-claim",
                canonical_digest(body),
                claim,
                audience="worker",
                replay_guard=lambda c, a, _: self._authority(
                    c, a, *self._bound(c, a, operation_id)[:2], body["expected_epoch"]
                ),
            )
            if not fresh_claim:
                return 200, self.status(actor, operation_id)
        elif action == "stop":

            def cancel(connection, live):
                operation, _, _, state = self._bound(connection, live, operation_id)
                grant = self._grant(connection, live, operation["grant_id"])
                if grant["status"] == "active":
                    self._request_grant_cancellation(connection, grant, grant["revision"])
                connection.execute(
                    "UPDATE external_operations SET desired_state='cancel_requested' "
                    "WHERE operation_id=?",
                    (operation_id,),
                )
                operation["desired_state"] = "cancel_requested"
                state.update(quiescent=False, phase="stop_intent")
                self._save(connection, live, operation, state, "sandbox.stop_intent")
                return 200, self._response(operation, state)

            self._mutate(
                actor,
                key + ":intent",
                "POST",
                f"/agent-sandboxes/{operation_id}/stop-intent",
                canonical_digest(body),
                cancel,
                audience="worker" if actor["kind"] == "worker" else "control",
            )
        elif action not in {"run", "observe"}:
            raise Conflict("Unsupported Sandbox action.")
        return self._effect(actor, operation_id, key, action, body)

    def status(self, actor, operation_id):
        with _connect(self.database_path) as connection:
            live = self._live_principal(
                connection, actor, audience="worker" if actor["kind"] == "worker" else "control"
            )
            operation, _, _, state = self._bound(connection, live, operation_id)
            return self._response(operation, state) | {"observation": "last_recorded_not_live"}

    def _custody(self, connection, operation, package, body):
        row = connection.execute(
            "SELECT * FROM artifact_manifests WHERE id=? AND operation_id=? AND project_id=?",
            (
                body["manifest_id"],
                operation["operation_id"],
                operation["project_id"],
            ),
        ).fetchone()
        if row is None or row["manifest_digest"] != body["manifest_digest"]:
            raise Conflict("Exact durable upload manifest required for cleanup.")
        entries = json.loads(row["artifacts_json"])
        canonical = {
            "version": 1,
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "grant_id": row["grant_id"],
            "lease_id": row["lease_id"],
            "worker_principal_id": row["worker_principal_id"],
            "epoch": row["epoch"],
            "operation_id": row["operation_id"],
            "plan_digest": row["plan_digest"],
            "artifacts": entries,
        }
        if (
            canonical_digest(canonical) != row["manifest_digest"]
            or row["lease_id"] != package["lease_id"]
            or row["epoch"] != package["epoch"]
            or row["plan_digest"] != package["plan_digest"]
            or {item["relative_path"] for item in entries} != set(package["output_paths"])
        ):
            raise Conflict("Manifest checksum or frozen output identity conflicts.")
        verifier = ArtifactManifestStore(self.database_path.parent)
        for entry in entries:
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE id=? AND project_id=? AND operation_id=? "
                "AND lease_id=? AND epoch=?",
                (
                    entry["id"],
                    operation["project_id"],
                    operation["operation_id"],
                    package["lease_id"],
                    package["epoch"],
                ),
            ).fetchone()
            if artifact is None or any(
                artifact[field] != entry[field] for field in ("sha256", "size", "relative_path")
            ):
                raise Conflict("Manifest does not match durable uploaded artifacts.")
            require_project_artifact_reference_admissible(
                connection, operation["project_id"], entry["id"]
            )
            verifier._verify_placement(
                connection,
                operation,
                entry["id"],
                entry["sha256"],
                entry["size"],
                inspect_object=True,
            )
        return row

    def collect(self, actor, operation_id, key, body):
        """Fresh typed handoff only; no arbitrary exec or Job collector reuse."""
        collect_provider = self._snapshot(actor, operation_id, "collect", body)

        def command(connection, live):
            operation, package, expected, state = self._bound(connection, live, operation_id)
            if body["expected_epoch"] != operation["epoch"]:
                raise Conflict("Collection epoch does not match the frozen operation.")
            provider = collect_provider
            target = SandboxPlan.model_validate(package["sandbox"]).target
            provider.identity(target)
            _, pvc, _ = self._capture(provider, target, expected, state)
            if not state["quiescent"] or operation["desired_state"] != "cancel_requested":
                raise Conflict("Fresh stopped-Pod evidence required for a collection handoff.")
            if not package["output_paths"]:
                raise Conflict("Operation declares no outputs to collect.")
            handoff = asdict(collection_handoff(package, state["sandbox_uid"]))
            self._save(connection, live, operation, state, "sandbox.collection_requested")
            return 200, {
                **self._response(operation, state),
                "handoff": handoff,
                "pvc": {
                    "name": pvc["metadata"]["name"],
                    "uid": state["pvc_uid"],
                    "spec_sha256": canonical_digest(pvc["spec"]),
                },
                "bytes_collected": False,
            }

        return self._mutate(
            actor,
            key,
            "POST",
            f"/agent-sandboxes/{operation_id}/collection-handoff",
            canonical_digest(body),
            command,
            audience="worker" if actor["kind"] == "worker" else "control",
            # Cached handoffs do not claim new termination evidence.
            replay_guard=lambda c, a, _: self._bound(c, a, operation_id),
        )

    def cleanup(self, human, operation_id, key, body):
        # Separate human authorization, durable intent before destructive effect.
        claim_provider = self._snapshot(human, operation_id, "cleanup", body)

        def claim(connection, live):
            self._require_human(live)
            operation, package, expected, state = self._bound(connection, live, operation_id)
            if operation["desired_state"] != "cancel_requested":
                raise Conflict("Cleanup requires separately stopped execution.")
            provider = claim_provider
            target = SandboxPlan.model_validate(package["sandbox"]).target
            provider.identity(target)
            self._capture(provider, target, expected, state)
            if not state["quiescent"]:
                raise Conflict("Unknown or active Pods prevent collection/deletion.")
            self._custody(connection, operation, package, body)
            if state["cleanup_attempted"]:
                raise Conflict("Cleanup already claimed; explicit UID reconciliation required.")
            state.update(
                cleanup_attempted=True, cleanup_manifest=dict(body), phase="cleanup_intent"
            )
            self._save(connection, live, operation, state, "sandbox.cleanup_intent")
            return 200, self._response(operation, state)

        self._mutate(
            human,
            key + ":intent",
            "POST",
            f"/agent-sandboxes/{operation_id}/cleanup-intent",
            canonical_digest(body),
            claim,
        )
        effect_provider = self._snapshot(human, operation_id, "cleanup", body)

        def effect(connection, live):
            self._require_human(live)
            operation, package, expected, state = self._bound(connection, live, operation_id)
            self._custody(connection, operation, package, body)
            provider = effect_provider
            target = SandboxPlan.model_validate(package["sandbox"]).target
            try:
                provider.identity(target)
                value, pvc, _ = self._capture(provider, target, expected, state)
                if not state["quiescent"]:
                    raise SandboxUnsafe("fresh complete quiescence required before deletion")
                provider.delete(target, "pvc", pvc)
                provider.delete(target, "sandbox", value)
                state.update(phase="cleanup_delete_sent", cleanup_acknowledged=True)
            except (SandboxUnsafe, OSError, ValueError, RuntimeError):
                state.update(phase="cleanup_uncertain", quiescent=False, cleanup_acknowledged=False)
            self._save(connection, live, operation, state, "sandbox.cleanup_observed")
            return 200, self._response(operation, state)

        return self._mutate(
            human,
            key,
            "POST",
            f"/agent-sandboxes/{operation_id}/cleanup",
            canonical_digest(body),
            effect,
            replay_guard=lambda c, a, _: (
                self._require_human(a),
                self._custody(c, *self._bound(c, a, operation_id)[:2], body),
            ),
        )
