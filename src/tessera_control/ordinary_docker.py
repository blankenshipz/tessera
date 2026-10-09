"""Human-enrolled Docker targets, native packages/outbox and current worker fences."""

import hashlib
import json
import uuid
from datetime import timedelta

from pydantic import ValidationError

from tessera_worker.ordinary_docker_contract import DockerPackage, DockerTarget

from .experiments import _append_admitted_attempt_state, bind_admitted_attempt_package
from .ordinary_docker_models import DockerReadbackReply, DockerReportAck
from .runtime_packages import RuntimePackageStore
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, canonical_digest, utc_now


def create_ordinary_docker_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS ordinary_docker_targets (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        issued_by TEXT NOT NULL REFERENCES principals(id), bounds_json TEXT NOT NULL,
        target_digest TEXT NOT NULL, revision INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ordinary_docker_dispatches (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        target_id TEXT NOT NULL REFERENCES ordinary_docker_targets(id),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES research_experiment_admitted_attempts(id),
        package_json TEXT NOT NULL, package_digest TEXT NOT NULL,
        state TEXT NOT NULL, container_id TEXT, claimed_by TEXT REFERENCES principals(id),
        report_json TEXT, revision INTEGER NOT NULL)""")


class OrdinaryDockerStore(RuntimePackageStore):
    @staticmethod
    def lease_response(lease):
        return {
            key: lease[key]
            for key in (
                "id",
                "grant_id",
                "worker_principal_id",
                "epoch",
                "expires_at",
                "hard_expires_at",
            )
        }

    def target(self, connection, actor, ident):
        row = connection.execute(
            "SELECT * FROM ordinary_docker_targets WHERE id=?", (ident,)
        ).fetchone()
        if row is None:
            raise NotFound("Docker target not enrolled.")
        self._project(connection, actor, row["project_id"])
        bounds = json.loads(row["bounds_json"])
        self._live_principal(connection, {"id": row["issued_by"], "org_id": actor["org_id"]})
        from .execution import _parse_utc

        if row["revoked"] or _parse_utc(bounds["expires_at"]) <= self._now():
            raise Conflict("Docker target expired or revoked.")
        return row, bounds

    def enroll(self, actor, project, key, body):
        def command(connection, live):
            self._require_human(live)
            self._project(connection, live, project)
            self._worker(connection, live["org_id"], body["worker_principal_id"], project)
            self._parse_future(body["expires_at"], timedelta(hours=24), "Target expiry")
            ident = "docker_target_" + uuid.uuid4().hex
            target = DockerTarget.model_validate(body["target"])
            connection.execute(
                "INSERT INTO ordinary_docker_targets VALUES (?,?,?,?,?,1,0)",
                (ident, project, live["id"], json.dumps(body), target.binding.identity_digest),
            )
            self._event(
                connection,
                live,
                project,
                None,
                "docker.target_enrolled",
                1,
                {
                    "target_id": ident,
                    "target_digest": target.binding.identity_digest,
                    "runtime_enforcement_verified": False,
                },
            )
            return 201, {
                "id": ident,
                "revision": 1,
                "target_digest": target.binding.identity_digest,
                "bounds": body,
            }

        def guard(connection, live, cached):
            self._require_human(live)
            self.target(connection, live, cached["id"])

        return self._mutate(
            actor,
            key,
            "POST",
            f"/projects/{project}/docker-targets",
            canonical_digest(body),
            command,
            replay_guard=guard,
        )

    def validate_plan(self, connection, actor, project, worker, spec, image):
        plan = spec.get("docker")
        if not plan:
            raise Conflict("Explicit Docker target/resource plan required.")
        row, bounds = self.target(connection, actor, plan["target_id"])
        if (
            row["project_id"] != project
            or row["target_digest"] != plan["target_digest"]
            or bounds["worker_principal_id"] != worker
            or bounds["target"]["image"] != image
            or spec["max_output_bytes"] > bounds["max_output_bytes"]
            or any(plan[k] > v for k, v in bounds["resources"].items())
        ):
            raise Forbidden("Docker target,worker,image or resources exceed human enrollment.")
        return row, bounds

    def package(self, worker, lease_id, key, body):
        def prepare(connection, live):
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (lease_id,)
            ).fetchone()
            if lease is None or lease["worker_principal_id"] != live["id"]:
                raise NotFound("Docker lease not found.")
            grant = self._active_worker_grant(connection, live, lease["grant_id"])
            from .execution import _parse_utc

            if (
                lease["epoch"] != body["expected_epoch"]
                or grant["epoch"] != lease["epoch"]
                or _parse_utc(lease["expires_at"]) <= self._now()
            ):
                raise Conflict("Docker lease expired/fenced.")
            rev = connection.execute(
                "SELECT execution_spec_json FROM task_revisions WHERE task_id=? AND plan_digest=? "
                "ORDER BY revision DESC LIMIT 1",
                (grant["task_id"], grant["plan_digest"]),
            ).fetchone()
            spec = json.loads(rev[0])
            if (
                spec["profile"] != "ordinary-docker"
                or spec["network_enabled"]
                or spec["pinned_skill_digests"]
            ):
                raise Conflict("Plan is not the supported Docker profile.")
            target, bounds = self.validate_plan(
                connection, live, grant["project_id"], live["id"], spec, grant["pinned_image"]
            )
            attempt = connection.execute(
                "SELECT * FROM research_experiment_admitted_attempts WHERE id=? AND grant_id=?",
                (body["admitted_attempt_id"], grant["id"]),
            ).fetchone()
            agent = connection.execute(
                "SELECT c.principal_id FROM agent_authority_consumptions a "
                "JOIN credentials c ON c.id=a.credential_id "
                "WHERE a.execution_grant_id=? AND a.action='attempt:admit'",
                (grant["id"],),
            ).fetchone()
            if attempt is None or agent is None or attempt["outcome"] is not None:
                raise Conflict("Docker requires a real agent-authorized admitted attempt.")
            data = dict(
                version=1,
                operation_id=hashlib.sha256(
                    f"tessera-ordinary-docker-v1:{lease_id}".encode()
                ).hexdigest()[:32],
                org_id=live["org_id"],
                project_id=grant["project_id"],
                task_id=grant["task_id"],
                admitted_attempt_id=attempt["id"],
                agent_principal_id=agent[0],
                worker_principal_id=live["id"],
                grant_id=grant["id"],
                lease_id=lease_id,
                epoch=lease["epoch"],
                target_digest=target["target_digest"],
                plan_digest=grant["plan_digest"],
                hard_expires_at=lease["hard_expires_at"],
                image=grant["pinned_image"],
                argv=spec["argv"],
                output_paths=spec["output_paths"],
                max_output_bytes=spec["max_output_bytes"],
                **{k: spec["docker"][k] for k in bounds["resources"]},
            )
            package = DockerPackage.model_validate_json(json.dumps(data))
            package.envelope(DockerTarget.model_validate(bounds["target"]))
            return package, self.lease_response(lease), dict(attempt), target

        def command(connection, live):
            package, lease, attempt, target = prepare(connection, live)
            data = package.model_dump(mode="json")
            binding = {
                "admitted_attempt_id": attempt["id"],
                "experiment_id": attempt["experiment_id"],
                "design_revision": attempt["design_revision"],
                "variant_id": attempt["variant_id"],
                "task_id": attempt["task_id"],
                "plan_digest": attempt["plan_digest"],
                "grant_id": attempt["grant_id"],
            }
            control = data | {"docker_lookup_key": package.name, "experiment": binding}
            self._record_operation_intent(
                connection, live, control, package.package_digest, canonical_digest(body)
            )
            bind_admitted_attempt_package(
                connection, live, control, package.package_digest, attempt["id"]
            )
            connection.execute(
                "INSERT INTO execution_runtime_packages VALUES (?,?,?,?) "
                "ON CONFLICT(operation_id) DO NOTHING",
                (
                    package.operation_id,
                    package.package_digest,
                    json.dumps(data, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                live,
                package.project_id,
                package.task_id,
                "docker.package_prepared",
                package.epoch,
                {
                    "operation_id": package.operation_id,
                    "package_digest": package.package_digest,
                    "execution_dispatched": False,
                },
            )
            return 201, {
                "package": data,
                "package_digest": package.package_digest,
                "lease": lease,
                "target_id": target["id"],
            }

        def guard(connection, live, cached):
            package, _, _, _ = prepare(connection, live)
            if package.package_digest != cached["package_digest"]:
                raise Conflict("Immutable Docker package changed.")

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/docker/leases/{lease_id}/package",
            canonical_digest(body),
            command,
            audience="worker",
            replay_guard=guard,
        )

    def dispatch_transaction(self, connection, actor, attempt_id, body):
        attempt = connection.execute(
            "SELECT * FROM research_experiment_admitted_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise NotFound("Docker attempt not found.")
        self._project(
            connection, actor, self._task(connection, actor, attempt["task_id"])["project_id"]
        )
        if attempt["revision"] != body["expected_revision"] or attempt["outcome"] is not None:
            raise Conflict("Docker attempt revision/resolution changed.")
        stored = connection.execute(
            "SELECT * FROM execution_runtime_packages WHERE operation_id=?",
            (attempt["operation_id"],),
        ).fetchone()
        if stored is None:
            raise Conflict("Docker package must be issued before dispatch.")
        package = DockerPackage.model_validate_json(stored["package_json"])
        if (
            package.package_digest != stored["package_digest"]
            or package.admitted_attempt_id != attempt_id
        ):
            raise Conflict("Docker package checksum or admission binding changed.")
        from .experiments import admitted_attempt_package_identity

        admitted_attempt_package_identity(
            connection, actor, package.model_dump(mode="json"), attempt_id
        )
        worker = self._live_principal(
            connection,
            {"id": package.worker_principal_id, "org_id": actor["org_id"]},
            audience="worker",
        )
        grant = self._active_worker_grant(connection, worker, package.grant_id)
        from .execution import _parse_utc

        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id=?", (package.lease_id,)
        ).fetchone()
        if (
            lease is None
            or grant["epoch"] != package.epoch
            or lease["epoch"] != package.epoch
            or _parse_utc(lease["expires_at"]) <= self._now()
        ):
            raise Conflict("Docker dispatch lease expired/fenced.")
        revision = connection.execute(
            "SELECT execution_spec_json FROM task_revisions WHERE task_id=? AND plan_digest=? "
            "ORDER BY revision DESC LIMIT 1",
            (package.task_id, package.plan_digest),
        ).fetchone()
        target_id = json.loads(revision[0])["docker"]["target_id"]
        if body["target_digest"] != package.target_digest:
            raise Conflict("Docker target binding is ambiguous or changed.")
        self.target(connection, actor, target_id)
        if connection.execute(
            "SELECT 1 FROM ordinary_docker_dispatches WHERE operation_id=?", (package.operation_id,)
        ).fetchone():
            raise Conflict("Docker dispatch already exists; replay original command.")
        connection.execute(
            "INSERT INTO ordinary_docker_dispatches VALUES (?,?,?,?,?,'queued',NULL,NULL,NULL,1)",
            (
                package.operation_id,
                target_id,
                attempt_id,
                stored["package_json"],
                package.package_digest,
            ),
        )
        updated = _append_admitted_attempt_state(
            connection, dict(attempt), "preparing", actor["id"], operation_id=package.operation_id
        )
        self._event(
            connection,
            actor,
            package.project_id,
            package.task_id,
            "docker.dispatch_queued",
            updated["revision"],
            {
                "operation_id": package.operation_id,
                "attempt_id": attempt_id,
                "target_digest": package.target_digest,
            },
        )
        return 201, {
            "operation_id": package.operation_id,
            "attempt_id": attempt_id,
            "state": "queued",
            "package_digest": package.package_digest,
        }

    def authority(self, connection, worker, body):
        op = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id=?", (body["operation_id"],)
        ).fetchone()
        if op is None or op["worker_principal_id"] != worker["id"]:
            raise NotFound("Docker operation not owned by this worker.")
        grant = self._active_worker_grant(connection, worker, op["grant_id"])
        current_epoch = connection.execute(
            "SELECT epoch FROM execution_task_epochs WHERE task_id=?", (op["task_id"],)
        ).fetchone()
        if current_epoch is None or current_epoch[0] != grant["epoch"]:
            raise Conflict("Docker task epoch changed.")
        lease = connection.execute(
            "SELECT * FROM execution_leases WHERE id=?", (body["lease_id"],)
        ).fetchone()
        dispatch = connection.execute(
            "SELECT * FROM ordinary_docker_dispatches WHERE operation_id=?", (op["operation_id"],)
        ).fetchone()
        from .execution import _parse_utc

        if (
            lease is None
            or dispatch is None
            or lease["id"] != op["lease_id"]
            or lease["epoch"] != body["expected_epoch"]
            or grant["epoch"] != lease["epoch"]
            or op["epoch"] != lease["epoch"]
            or _parse_utc(lease["expires_at"]) <= self._now()
            or op["package_digest"] != body["package_digest"]
            or dispatch["attempt_id"] != body["admitted_attempt_id"]
        ):
            raise Conflict("Docker operation/attempt/package/lease fence changed.")
        package = DockerPackage.model_validate_json(dispatch["package_json"])
        if package.package_digest != dispatch["package_digest"]:
            raise Conflict("Stored Docker package integrity failure.")
        if package.target_digest != body["target_digest"]:
            raise Conflict("Docker target mismatch.")
        self.target(connection, worker, dispatch["target_id"])
        return self.lease_response(lease), dispatch, op

    def current(self, worker, body):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, worker, audience="worker")
            lease, _, _ = self.authority(connection, live, body)
            return {"lease": lease, "authority_current": True}
        finally:
            connection.close()

    def readback(self, worker, body):
        """Original valid worker can inspect/stop owned work, never revive authority."""
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, worker, audience="worker")
            op = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (body["operation_id"],)
            ).fetchone()
            dispatch = connection.execute(
                "SELECT * FROM ordinary_docker_dispatches WHERE operation_id=?",
                (body["operation_id"],),
            ).fetchone()
            if (
                op is None
                or dispatch is None
                or op["worker_principal_id"] != live["id"]
                or dispatch["claimed_by"] not in (None, live["id"])
            ):
                raise NotFound("Original Docker worker operation not found.")
            self._project(connection, live, op["project_id"])
            package = DockerPackage.model_validate_json(dispatch["package_json"])
            fence = {
                "operation_id": package.operation_id,
                "admitted_attempt_id": package.admitted_attempt_id,
                "lease_id": package.lease_id,
                "expected_epoch": package.epoch,
                "package_digest": package.package_digest,
                "target_digest": package.target_digest,
            }
            if (
                any(body[key] != value for key, value in fence.items())
                or op["operation_id"] != package.operation_id
                or op["org_id"] != live["org_id"]
                or package.org_id != live["org_id"]
                or op["project_id"] != package.project_id
                or op["task_id"] != package.task_id
                or op["grant_id"] != package.grant_id
                or op["lease_id"] != package.lease_id
                or op["epoch"] != package.epoch
                or op["worker_principal_id"] != package.worker_principal_id
                or dispatch["attempt_id"] != package.admitted_attempt_id
                or dispatch["package_digest"] != package.package_digest
                or op["package_digest"] != package.package_digest
                or op["provider_id"] != dispatch["container_id"]
            ):
                raise Conflict("Original Docker readback binding/integrity mismatch.")
            if (
                body.get("expected_container_id") is not None
                and body["expected_container_id"] != dispatch["container_id"]
            ):
                raise Conflict("Recorded Docker container identity mismatch.")
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (package.lease_id,)
            ).fetchone()
            if (
                lease is None
                or lease["worker_principal_id"] != live["id"]
                or lease["grant_id"] != package.grant_id
                or lease["epoch"] != package.epoch
            ):
                raise Conflict("Original Docker lease binding mismatch.")
            from .execution import _parse_utc

            if _parse_utc(lease["hard_expires_at"]) != package.hard_expires_at:
                raise Conflict("Original Docker hard expiry changed.")
            can_execute = False
            if (
                dispatch["state"] in ("queued", "claimed", "running", "unknown")
                and op["desired_state"] == "admitted"
            ):
                try:
                    self.authority(connection, live, fence)
                    can_execute = True
                except StoreError:
                    # Authentication/tenant/package proof above is not bypassed.
                    # Changed issuer/grant/target/lease authority permits only stop/read.
                    pass
            latest = json.loads(dispatch["report_json"]) if dispatch["report_json"] else None
            if latest is not None and (
                any(latest[key] != value for key, value in fence.items())
                or latest["container_id"] != dispatch["container_id"]
                or latest["state"] != dispatch["state"]
            ):
                raise Conflict("Native Docker report projection integrity mismatch.")

            def receipt(row):
                if row is None:
                    return None
                echoed = json.loads(row["response_json"])
                try:
                    DockerReportAck.model_validate(echoed)
                except ValidationError as error:
                    raise Conflict(
                        "Docker receipt is not a closed native report acknowledgement."
                    ) from error
                report = {
                    key: value
                    for key, value in echoed.items()
                    if key not in ("scientific_validity", "human_resolution_required")
                }
                if (
                    row["response_status"] != 200
                    or any(report.get(key) != value for key, value in fence.items())
                    or canonical_digest(report) != row["request_digest"]
                    or echoed.get("scientific_validity") != "not_assessed"
                    or echoed.get("human_resolution_required") is not True
                    or (
                        report.get("container_id") is not None
                        and report["container_id"] != dispatch["container_id"]
                    )
                ):
                    raise Conflict(
                        "Docker delivery receipt does not match this original operation."
                    )
                return {
                    "idempotency_key": row["idempotency_key"],
                    "payload_digest": row["request_digest"],
                    "response_status": 200,
                    "response": echoed,
                }

            delivery = None
            if body.get("delivery_key") is not None:
                delivery = receipt(
                    connection.execute(
                        "SELECT * FROM idempotency WHERE principal_id=? AND org_id=? "
                        "AND method='POST' AND path='/worker/docker/report' AND idempotency_key=?",
                        (live["id"], live["org_id"], body["delivery_key"]),
                    ).fetchone()
                )
            latest_delivery = None
            if latest is not None:
                latest_delivery = receipt(
                    connection.execute(
                        "SELECT * FROM idempotency WHERE principal_id=? AND org_id=? "
                        "AND method='POST' AND path='/worker/docker/report' AND request_digest=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (live["id"], live["org_id"], canonical_digest(latest)),
                    ).fetchone()
                )
                if latest_delivery is None:
                    raise Conflict("Native Docker report lacks a committed delivery receipt.")
            result = {
                "fence": fence,
                "task_id": package.task_id,
                "grant_id": package.grant_id,
                "worker_principal_id": live["id"],
                "claimed_by_principal_id": dispatch["claimed_by"],
                "container_id": dispatch["container_id"],
                "desired_state": op["desired_state"],
                "dispatch_state": dispatch["state"],
                "dispatch_revision": dispatch["revision"],
                "can_execute": can_execute,
                "authorization": "execute" if can_execute else "stop_only",
                "effective_action": "run" if can_execute else "stop",
                "lease": self.lease_response(lease),
                "latest_report": latest,
                "latest_delivery": latest_delivery,
                "delivery_key": body.get("delivery_key"),
                "delivery": delivery,
            }
            try:
                DockerReadbackReply.model_validate_json(json.dumps(result))
            except ValidationError as error:
                raise Conflict("Docker readback is not a closed native state snapshot.") from error
            return result
        finally:
            connection.close()

    def pending(self, worker, target_digest):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._live_principal(connection, worker, audience="worker")
            rows = connection.execute(
                "SELECT d.* FROM ordinary_docker_dispatches d JOIN external_operations o "
                "ON o.operation_id=d.operation_id WHERE o.worker_principal_id=? "
                "AND d.state IN ('queued','claimed','running','unknown') "
                "ORDER BY d.operation_id LIMIT 100",
                (live["id"],),
            ).fetchall()
            result = []
            for row in rows:
                package = DockerPackage.model_validate_json(row["package_json"])
                if package.target_digest != target_digest:
                    continue
                fence = {
                    "operation_id": package.operation_id,
                    "admitted_attempt_id": package.admitted_attempt_id,
                    "lease_id": package.lease_id,
                    "expected_epoch": package.epoch,
                    "package_digest": package.package_digest,
                    "target_digest": target_digest,
                }
                try:
                    lease, _, _ = self.authority(connection, live, fence)
                except Conflict:
                    # Expired/revoked work remains in the ledger, never rekeyed or revived.
                    continue
                result.append(
                    {
                        "fence": fence,
                        "package": package.model_dump(mode="json"),
                        "lease": lease,
                        "state": row["state"],
                    }
                )
            return {"dispatches": result}
        finally:
            connection.close()

    def claim(self, worker, key, body):
        def command(connection, live):
            lease, dispatch, op = self.authority(connection, live, body)
            if dispatch["claimed_by"] not in (None, live["id"]) or dispatch["state"] not in (
                "queued",
                "claimed",
                "running",
                "unknown",
            ):
                raise Conflict("Docker dispatch is not claimable.")
            connection.execute(
                "UPDATE ordinary_docker_dispatches SET claimed_by=?,state='claimed' "
                "WHERE operation_id=? AND state='queued'",
                (live["id"], op["operation_id"]),
            )
            if dispatch["state"] == "queued":
                self._event(
                    connection,
                    live,
                    op["project_id"],
                    op["task_id"],
                    "docker.dispatch_claimed",
                    1,
                    {
                        "operation_id": op["operation_id"],
                        "worker_principal_id": live["id"],
                        "package_digest": op["package_digest"],
                    },
                )
            return 200, {
                "package": json.loads(dispatch["package_json"]),
                "package_digest": dispatch["package_digest"],
                "lease": lease,
            }

        def guard(connection, live, cached):
            self.authority(connection, live, body)

        return self._mutate(
            worker,
            key,
            "POST",
            "/worker/docker/claim",
            canonical_digest(body),
            command,
            audience="worker",
            replay_guard=guard,
        )

    def report(self, worker, key, body):
        def command(connection, live):
            op = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (body["operation_id"],)
            ).fetchone()
            dispatch = connection.execute(
                "SELECT * FROM ordinary_docker_dispatches WHERE operation_id=?",
                (body["operation_id"],),
            ).fetchone()
            if (
                op is None
                or dispatch is None
                or op["worker_principal_id"] != live["id"]
                or dispatch["claimed_by"] != live["id"]
            ):
                raise NotFound("Claimed Docker operation not owned.")
            self._grant(connection, live, op["grant_id"])
            package = DockerPackage.model_validate_json(dispatch["package_json"])
            if (
                package.lease_id != body["lease_id"]
                or package.epoch != body["expected_epoch"]
                or package.admitted_attempt_id != body["admitted_attempt_id"]
                or package.package_digest != body["package_digest"]
                or package.target_digest != body["target_digest"]
                or dispatch["container_id"] not in (None, body["container_id"])
            ):
                raise Conflict("Docker report immutable identity changed.")
            terminal = body["state"] in ("succeeded", "failed", "cancelled")
            if body["state"] == "running" and not body["container_id"]:
                raise Conflict("Running Docker observation needs an exact container ID.")
            if terminal and (
                not body["container_id"]
                or not body["termination_confirmed"]
                or (
                    body["state"] == "succeeded"
                    and (body["exit_code"] != 0 or body["stop_requested"])
                )
                or (
                    body["state"] == "failed"
                    and (body["exit_code"] in (None, 0) or body["stop_requested"])
                )
                or (body["state"] == "cancelled" and not body["stop_requested"])
            ):
                raise Conflict("Terminal Docker report lacks exact observed termination evidence.")
            if not terminal and body["termination_confirmed"]:
                raise Conflict("Unknown/running Docker report cannot infer termination.")
            old = json.loads(dispatch["report_json"]) if dispatch["report_json"] else None
            if old and old["state"] in ("succeeded", "failed", "cancelled"):
                raise Conflict("Terminal Docker observation cannot be rewritten.")
            observed = "exited" if terminal else body["state"]
            now = utc_now()
            connection.execute(
                "INSERT INTO execution_observations "
                "(id,grant_id,lease_id,worker_principal_id,epoch,desired_state,observed_state,"
                "observed_at,provider_id,termination_confirmed,recorded_at,operation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "docker_observation_" + uuid.uuid4().hex,
                    op["grant_id"],
                    package.lease_id,
                    live["id"],
                    package.epoch,
                    op["desired_state"],
                    observed,
                    now,
                    body["container_id"],
                    int(body["termination_confirmed"]),
                    now,
                    op["operation_id"],
                ),
            )
            connection.execute(
                "UPDATE external_operations SET observed_state=?,"
                "provider_id=coalesce(provider_id,?),"
                "updated_at=? WHERE operation_id=?",
                (observed, body["container_id"], utc_now(), op["operation_id"]),
            )
            connection.execute(
                "UPDATE ordinary_docker_dispatches SET state=?,"
                "container_id=coalesce(container_id,?),"
                "report_json=?,revision=revision+1 WHERE operation_id=?",
                (body["state"], body["container_id"], json.dumps(body), op["operation_id"]),
            )
            attempt = dict(
                connection.execute(
                    "SELECT * FROM research_experiment_admitted_attempts WHERE id=?",
                    (dispatch["attempt_id"],),
                ).fetchone()
            )
            _append_admitted_attempt_state(
                connection,
                attempt,
                "interrupted"
                if body["state"] == "cancelled"
                else "running"
                if observed == "running"
                else "unknown",
                live["id"],
                observed_state=observed,
                operation_id=op["operation_id"],
                termination_confirmed=body["termination_confirmed"],
            )
            self._event(
                connection,
                live,
                op["project_id"],
                op["task_id"],
                "docker.operation_reported",
                dispatch["revision"] + 1,
                body | {"scientific_validity": "not_assessed", "human_resolution_required": True},
            )
            return 200, body | {
                "scientific_validity": "not_assessed",
                "human_resolution_required": True,
            }

        return self._mutate(
            worker,
            key,
            "POST",
            "/worker/docker/report",
            canonical_digest(body),
            command,
            audience="worker",
        )

    def revoke(self, actor, ident, key, body):
        def command(connection, live):
            self._require_human(live)
            row, _ = self.target(connection, live, ident)
            if row["revision"] != body["expected_revision"]:
                raise Conflict("Docker target revision changed.")
            connection.execute(
                "UPDATE ordinary_docker_targets SET revoked=1,revision=revision+1 WHERE id=?",
                (ident,),
            )
            self._event(
                connection,
                live,
                row["project_id"],
                None,
                "docker.target_revoked",
                row["revision"] + 1,
                {"target_id": ident},
            )
            return 200, {"id": ident, "revoked": True, "revision": row["revision"] + 1}

        return self._mutate(
            actor, key, "POST", f"/docker-targets/{ident}/revoke", canonical_digest(body), command
        )
