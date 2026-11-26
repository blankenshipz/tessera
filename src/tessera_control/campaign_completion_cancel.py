"""A Salad completion shutdown cancel is the run's end, not a cancellation (ADR 0075 amendment).

The live-check driver always stops its own route when it ends. After a clean, verified run it
records that stop at control through its reduce-only worker cancel, which fences the grant and
its operation (``desired_state='cancel_requested'``), and writes ``cancel_cause.class ==
"execution_complete"`` on its receipt. ``resolve_attempt`` used to read that fence as a request
to abandon the run and refused the success, so the attempt failed as ``step_refused`` and none
of its outputs was indexed.

:func:`completion_evidence` decides, from control-plane records only, whether an operation's
cancel fence is exactly that completion stop of a verified pass. Every guard must hold:

* the operation is fenced ``cancel_requested`` and belongs to exactly one campaign Salad attempt
  of the same grant;
* the stored live receipt is ``passed`` / ``verified_journey`` with ``stop_confirmed``, the
  cancel was recorded at control (``cancel_recorded``), and ``cancel_cause.class`` is exactly the
  closed token ``execution_complete`` (not a substring, never missing);
* the route state names the stop as the route's own dispatcher worker's cancel
  (``stop_provenance.cancel_authority == "executor_worker"``, same worker principal), the
  provider's stop is confirmed and the route was never reallocated;
* the grant's one cancel transition was that worker's (an ``execution.grant_cancel_requested``
  event by the worker principal): a grant is fenced only while active, so no user, operator,
  campaign, deadline or revoke cancel came first;
* the attempt's manifest is the operation's own (grant, lease, operation, epoch), lists exactly
  the receipt's outputs, is not partial, is custody-released at its digest and retained
  ``retain_quarantined``, and every file's custody row is quarantined at the listed sha256/size.

Anything else returns a refusal code, and the caller keeps refusing ``Execution success cannot
override a requested cancellation.`` A real cancel is never overridden.

:class:`CompletionCancelRecovery` is the sanctioned, idempotent recovery of attempts that the old
refusal already failed (``tessera campaign reconcile-completion-cancels``). It re-hashes every
output from custody, resolves the admitted attempt ``succeeded``, writes the official
``campaign_stage_io`` output rows, moves the attempt and stage to ``succeeded`` (and the campaign
too when every stage then has), and appends one digest-sealed
``campaign.completion_cancel_recovered`` event bound to the campaign's immutable terminal
receipt. ``resolve_artifact`` reads that addendum only when the terminal receipt does not list
the output. Charges, holds and settlements are never touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .store import Conflict, Forbidden, StoreError, _connect, canonical_digest, utc_now

COMPLETION_CANCEL_CLASS = "execution_complete"
DRIVER_CANCEL_AUTHORITY = "executor_worker"
RECOVERED_EVENT = "campaign.completion_cancel_recovered"
RECOVERY_SCHEMA = "tessera.campaign.completion-cancel-recovery.v1"
# The exact refusal the old guard raised; only an attempt failed with it is recoverable.
REFUSED_DETAIL = "Execution success cannot override a requested cancellation."
# Campaign states the recovery may touch: the runner no longer advances a terminal campaign.
RECOVERABLE_CAMPAIGN = ("failed", "stopped", "succeeded")


class ReconcileCompletionCancels(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = Field(min_length=1, max_length=300)
    dry_run: bool = False


def _route_state(
    connection: sqlite3.Connection, org_id: str, project_id: str, route_id: str | None
) -> dict[str, Any] | None:
    if route_id is None:
        return None
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type='remote_execution.state'
        AND org_id=? AND project_id=? AND json_extract(payload_json,'$.id')=?
        ORDER BY cursor DESC LIMIT 1""",
        (org_id, project_id, route_id),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def _receipt_outputs(receipt: Mapping[str, Any]) -> dict[str, tuple[Any, Any]] | None:
    outputs = receipt.get("outputs")
    if isinstance(outputs, list):
        try:
            return {item["name"]: (item["sha256"], item["bytes"]) for item in outputs}
        except (KeyError, TypeError):
            return None
    if receipt.get("output_sha256") is not None:
        return {"output.json": (receipt["output_sha256"], receipt.get("output_bytes"))}
    return None


def completion_evidence(
    connection: sqlite3.Connection, operation_id: str
) -> tuple[dict[str, Any] | None, str]:
    """``(evidence, "")`` when the operation's cancel fence is the driver's completion stop of a
    verified pass, else ``(None, refusal_code)``. A pure read of control-plane records."""
    from .artifact_manifests import grant_manifest_is_partial
    from .external_operations import current_operation_reconciliation

    operation = connection.execute(
        """SELECT operation_id,grant_id,desired_state,worker_principal_id,org_id,project_id,
        package_digest FROM external_operations WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    if operation is None:
        return None, "operation_missing"
    if operation["desired_state"] != "cancel_requested":
        return None, "no_cancel_requested"
    rows = connection.execute(
        """SELECT sa.*, a.id AS campaign_attempt_id, a.campaign_id, a.grant_id AS attempt_grant_id
        FROM campaign_salad_attempts sa JOIN campaign_stage_attempts a ON a.id=sa.attempt_id
        WHERE sa.operation_id=?""",
        (operation_id,),
    ).fetchall()
    if len(rows) != 1 or rows[0]["attempt_grant_id"] != operation["grant_id"]:
        return None, "not_a_campaign_salad_attempt"
    row = rows[0]
    if row["live_receipt_json"] is None:
        return None, "live_receipt_missing"
    try:
        receipt = json.loads(row["live_receipt_json"])
    except ValueError:
        return None, "live_receipt_unreadable"
    if not isinstance(receipt, dict) or not (
        receipt.get("status") == "passed"
        and receipt.get("reason") == "verified_journey"
        and receipt.get("stop_confirmed") is True
    ):
        return None, "receipt_not_verified_pass"
    cause = receipt.get("cancel_cause")
    if (
        receipt.get("cancel_recorded") is not True
        or type(cause) is not dict
        or type(cause.get("class")) is not str
        or cause["class"] != COMPLETION_CANCEL_CLASS
    ):
        return None, "cancel_not_completion"
    reconciliation = current_operation_reconciliation(connection, operation_id)
    if (
        reconciliation is None
        or reconciliation["observed_state"] != "exited"
        or not reconciliation["termination_confirmed"]
    ):
        return None, "exit_not_confirmed"
    state = _route_state(connection, operation["org_id"], operation["project_id"], row["route_id"])
    provenance = None if state is None else state.get("stop_provenance")
    if (
        state is None
        or state.get("desired") != "stop"
        or state.get("termination_confirmed") is not True
        or state.get("provider_status") != "stopped"
        or state.get("reallocated") is not False
        or type(provenance) is not dict
        or provenance.get("cancel_authority") != DRIVER_CANCEL_AUTHORITY
        or provenance.get("worker_principal_id") != operation["worker_principal_id"]
    ):
        return None, "cancel_not_driver_completion"
    fenced_by = [
        event["actor_principal_id"]
        for event in connection.execute(
            """SELECT actor_principal_id FROM events
            WHERE event_type='execution.grant_cancel_requested' AND org_id=?
            AND json_extract(payload_json,'$.grant_id')=?""",
            (operation["org_id"], operation["grant_id"]),
        )
    ]
    if fenced_by != [operation["worker_principal_id"]]:
        return None, "cancel_not_driver_completion"
    manifest = (
        None
        if row["manifest_id"] is None
        else connection.execute(
            """SELECT * FROM artifact_manifests WHERE id=? AND grant_id=? AND lease_id=?
            AND operation_id=? AND epoch=?""",
            (
                row["manifest_id"],
                operation["grant_id"],
                row["lease_id"],
                operation_id,
                row["epoch"],
            ),
        ).fetchone()
    )
    if manifest is None:
        return None, "manifest_missing"
    artifacts = json.loads(manifest["artifacts_json"])
    listed = {item["relative_path"]: (item["sha256"], item["size"]) for item in artifacts}
    expected = _receipt_outputs(receipt)
    if not expected or listed != expected:
        return None, "manifest_does_not_match_receipt"
    grant = connection.execute(
        "SELECT * FROM execution_grants WHERE id=?", (operation["grant_id"],)
    ).fetchone()
    if grant is None or grant_manifest_is_partial(connection, grant, manifest["artifacts_json"]):
        return None, "manifest_partial"
    disposition = connection.execute(
        "SELECT disposition FROM artifact_dispositions WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    release = connection.execute(
        "SELECT state, manifest_digest FROM artifact_custody_releases WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    if (
        disposition is None
        or disposition["disposition"] != "retain_quarantined"
        or release is None
        or release["state"] != "released"
        or release["manifest_digest"] != manifest["manifest_digest"]
    ):
        return None, "custody_not_released"
    outputs = []
    for item in sorted(artifacts, key=lambda entry: entry["relative_path"]):
        custody = connection.execute(
            "SELECT sha256, size, status, project_id FROM artifacts WHERE id=?", (item["id"],)
        ).fetchone()
        if (
            custody is None
            or custody["project_id"] != operation["project_id"]
            or custody["status"] != "quarantined"
            or custody["sha256"] != item["sha256"]
            or custody["size"] != item["size"]
        ):
            return None, "custody_digest_mismatch"
        outputs.append(
            {
                "artifact_id": item["id"],
                "relative_path": item["relative_path"],
                "sha256": item["sha256"],
                "size": int(item["size"]),
            }
        )
    return {
        "operation_id": operation_id,
        "grant_id": operation["grant_id"],
        "campaign_attempt_id": row["campaign_attempt_id"],
        "route_id": row["route_id"],
        "live_receipt_digest": row["live_receipt_digest"],
        "cancel_cause_class": COMPLETION_CANCEL_CLASS,
        "cancel_authority": DRIVER_CANCEL_AUTHORITY,
        "manifest_id": manifest["id"],
        "manifest_digest": manifest["manifest_digest"],
        "outputs": outputs,
    }, ""


def verify_custody_bytes(connection: sqlite3.Connection, outputs: list[dict[str, Any]]) -> bool:
    """Re-hash every output's bytes as custody holds them (inline content or stored chunks)."""
    from .large_artifacts import is_large_artifact, verify_artifact_bytes

    for item in outputs:
        row = connection.execute(
            "SELECT id, relative_path, sha256, size, content FROM artifacts WHERE id=?",
            (item["artifact_id"],),
        ).fetchone()
        if row is None or row["sha256"] != item["sha256"] or row["size"] != item["size"]:
            return False
        if is_large_artifact(row):
            try:
                verify_artifact_bytes(
                    connection, row["id"], row["relative_path"], row["size"], row["sha256"]
                )
            except Exception:
                return False
            continue
        content = row["content"]
        if (
            type(content) is not bytes
            or len(content) != row["size"]
            or hashlib.sha256(content).hexdigest() != row["sha256"]
        ):
            return False
    return True


def _sealed(payload: Mapping[str, Any]) -> bool:
    body = {key: value for key, value in payload.items() if key != "recovery_digest"}
    return payload.get("recovery_digest") == canonical_digest(body)


def recovered_listing(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    campaign_id: str,
    receipt_digest: str,
    stage_key: str,
    attempt_number: int,
    name: str,
    sha256: str,
) -> bool:
    """True when a sealed recovery addendum to exactly this terminal receipt lists the output."""
    for row in connection.execute(
        """SELECT payload_json FROM events WHERE event_type=? AND org_id=? AND project_id=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (RECOVERED_EVENT, org_id, project_id, campaign_id),
    ):
        payload = json.loads(row["payload_json"])
        if (
            payload.get("schema") != RECOVERY_SCHEMA
            or payload.get("source_receipt_digest") != receipt_digest
            or not _sealed(payload)
        ):
            continue
        for item in payload.get("attempts", []):
            if item.get("stage_key") != stage_key or item.get("attempt") != attempt_number:
                continue
            if any(
                output.get("name") == name and output.get("sha256") == sha256
                for output in item.get("io_outputs", [])
            ):
                return True
    return False


def _recovered_attempts(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for row in connection.execute(
        """SELECT payload_json FROM events WHERE event_type=? AND org_id=? AND project_id=?
        AND json_extract(payload_json,'$.campaign_id')=?""",
        (RECOVERED_EVENT, campaign["org_id"], campaign["project_id"], campaign["id"]),
    ):
        payload = json.loads(row["payload_json"])
        if _sealed(payload):
            found.update(item["campaign_attempt_id"] for item in payload.get("attempts", []))
    return found


def assess_campaign(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], *, verify_bytes: bool
) -> list[dict[str, Any]]:
    """Each Salad attempt of the campaign with its recovery verdict (a pure read).

    ``verdict`` is ``recoverable``, ``already_recovered`` or ``refused`` (with ``refusal``)."""
    verdicts = []
    recovered = _recovered_attempts(connection, campaign)
    for attempt in connection.execute(
        """SELECT a.*, sa.operation_id AS salad_operation_id FROM campaign_stage_attempts a
        JOIN campaign_salad_attempts sa ON sa.attempt_id=a.id
        WHERE a.campaign_id=? ORDER BY a.stage_key, a.attempt""",
        (campaign["id"],),
    ).fetchall():
        entry: dict[str, Any] = {
            "campaign_attempt_id": attempt["id"],
            "stage_key": attempt["stage_key"],
            "attempt": attempt["attempt"],
            "status": attempt["status"],
            "failure_class": attempt["failure_class"],
        }
        io_rows = connection.execute(
            """SELECT 1 FROM campaign_stage_io WHERE campaign_id=? AND attempt_id=?
            AND direction='output' LIMIT 1""",
            (campaign["id"], attempt["id"]),
        ).fetchone()
        if attempt["id"] in recovered and attempt["status"] == "succeeded":
            verdicts.append({**entry, "verdict": "already_recovered"})
            continue
        refusal = ""
        if attempt["status"] == "succeeded":
            refusal = "attempt_already_succeeded"
        elif not (
            attempt["status"] == "failed_permanent"
            and attempt["failure_class"] == "step_refused"
            and attempt["failure_detail"] == REFUSED_DETAIL
            and attempt["salad_verdict"] == "ok"
        ):
            refusal = "not_a_completion_cancel_refusal"
        elif io_rows is not None:
            refusal = "output_rows_present"
        elif attempt["salad_operation_id"] is None or (
            attempt["operation_id"] not in (None, attempt["salad_operation_id"])
        ):
            refusal = "operation_mismatch"
        if refusal:
            verdicts.append({**entry, "verdict": "refused", "refusal": refusal})
            continue
        evidence, refusal = completion_evidence(connection, attempt["salad_operation_id"])
        if evidence is None:
            verdicts.append({**entry, "verdict": "refused", "refusal": refusal})
            continue
        if (
            evidence["campaign_attempt_id"] != attempt["id"]
            or evidence["manifest_id"] != attempt["manifest_id"]
            or evidence["manifest_digest"] != attempt["manifest_digest"]
        ):
            verdicts.append({**entry, "verdict": "refused", "refusal": "manifest_mismatch"})
            continue
        if verify_bytes and not verify_custody_bytes(connection, evidence["outputs"]):
            verdicts.append({**entry, "verdict": "refused", "refusal": "custody_digest_mismatch"})
            continue
        verdicts.append({**entry, "verdict": "recoverable", "evidence": evidence})
    return verdicts


class CompletionCancelRecovery:
    """``CampaignStore`` mixin: list and recover attempts failed by a completion cancel."""

    def _completion_actor(self, connection: sqlite3.Connection, live: dict[str, Any]) -> None:
        """The owner's own credential (an automation label allowed), like a campaign cancel; a
        scoped external agent, a worker or a service principal is refused."""
        if live.get("kind") == "worker" or self._external_scope(connection, live):
            raise Forbidden("Completion-cancel recovery needs the owner's control credential.")
        if live["role"] != "human":
            raise Forbidden("Completion-cancel recovery needs the owner's control credential.")

    def list_completion_cancels(self, principal, campaign_id: str) -> dict[str, Any]:
        """Dry run: every Salad attempt of the campaign with its verdict; changes nothing."""
        connection = _connect(self.database_path)
        try:
            live = self._actor(connection, principal)
            self._completion_actor(connection, live)
            campaign = self._readable_campaign(connection, live, campaign_id)
            verdicts = assess_campaign(connection, campaign, verify_bytes=True)
        finally:
            connection.close()
        return {
            "campaign_id": campaign_id,
            "campaign_status": campaign["status"],
            "dry_run": True,
            "attempts": [_public(item) for item in verdicts],
        }

    def reconcile_completion_cancels(
        self, principal, campaign_id: str, key: str, digest: str, payload
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = ReconcileCompletionCancels.model_validate(payload)
        except ValidationError as error:
            raise StoreError("Invalid completion-cancel recovery request.") from error
        if body.dry_run:
            return 200, self.list_completion_cancels(principal, campaign_id)
        # Bytes are re-hashed from custody first, outside the write transaction (a large output
        # streams its chunks); the transaction then re-checks every record and the chunk set.
        connection = _connect(self.database_path)
        try:
            live = self._actor(connection, principal)
            self._completion_actor(connection, live)
            campaign = self._readable_campaign(connection, live, campaign_id)
            hashed = {
                item["campaign_attempt_id"]: _fingerprints(connection, item["evidence"]["outputs"])
                for item in assess_campaign(connection, campaign, verify_bytes=True)
                if item["verdict"] == "recoverable"
            }
        finally:
            connection.close()

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._completion_actor(connection, live)
            campaign = dict(self._readable_campaign(connection, live, campaign_id))
            return 200, self._recover(connection, live, campaign, body.reason, hashed)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/campaigns/{campaign_id}/reconcile-completion-cancels",
            digest,
            command,
        )

    def _recover(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        campaign: dict[str, Any],
        reason: str,
        hashed: Mapping[str, list[str]],
    ) -> dict[str, Any]:
        from .campaign_io import output_entries, write_io
        from .campaign_models import normalize, parse_spec_json
        from .campaign_receipt_read import load_verified_receipt

        verdicts = assess_campaign(connection, campaign, verify_bytes=False)
        recoverable = [item for item in verdicts if item["verdict"] == "recoverable"]
        result: dict[str, Any] = {
            "campaign_id": campaign["id"],
            "dry_run": False,
            "attempts": [_public(item) for item in verdicts],
            "recovered": [],
        }
        if not recoverable:
            result["campaign_status"] = campaign["status"]
            if not any(item["verdict"] == "already_recovered" for item in verdicts):
                raise Conflict(
                    "No attempt of this campaign is a recoverable completion-cancel refusal: "
                    + ", ".join(
                        f"{item['stage_key']}#{item['attempt']}:{item.get('refusal', '')}"
                        for item in verdicts
                    )[:400]
                )
            return result
        if campaign["status"] not in RECOVERABLE_CAMPAIGN:
            raise Conflict("Only a finished campaign is recovered; the runner still owns it.")
        verified = load_verified_receipt(connection, campaign["id"])
        if verified is None:
            raise Conflict("The campaign has no terminal receipt to bind the recovery to.")
        for item in recoverable:
            outputs = item["evidence"]["outputs"]
            if item["campaign_attempt_id"] not in hashed:
                raise Conflict(
                    f"{item['stage_key']}#{item['attempt']}:custody_digest_mismatch: the bytes "
                    "custody holds do not hash to the recorded sha256; nothing was recovered."
                )
            if hashed[item["campaign_attempt_id"]] != _fingerprints(connection, outputs):
                raise Conflict("Custody changed while it was verified; nothing was recovered.")
        spec = normalize(parse_spec_json(campaign["spec_json"]))
        now = utc_now()
        stages_before = {
            row["stage_key"]: row["status"]
            for row in connection.execute(
                "SELECT stage_key, status FROM campaign_stages WHERE campaign_id=?",
                (campaign["id"],),
            )
        }
        recovered = []
        for item in recoverable:
            attempt = dict(
                connection.execute(
                    "SELECT * FROM campaign_stage_attempts WHERE id=?",
                    (item["campaign_attempt_id"],),
                ).fetchone()
            )
            resolution = self._resolve_admitted_success(connection, live, attempt, reason)
            updated = connection.execute(
                """UPDATE campaign_stage_attempts SET status='succeeded', failure_class=NULL,
                failure_detail=NULL, operation_id=COALESCE(operation_id, ?),
                revision=revision+1, updated_at=?
                WHERE id=? AND status='failed_permanent' AND failure_class='step_refused'""",
                (item["evidence"]["operation_id"], now, attempt["id"]),
            ).rowcount
            if updated != 1:
                raise Conflict("The attempt changed while it was recovered; nothing was recovered.")
            self._register_lineage(connection, live, campaign, attempt, item["evidence"])
            entries = output_entries(
                connection, campaign, spec.stage(attempt["stage_key"]), attempt["manifest_id"]
            )
            if not entries:
                raise Conflict("The stage's manifest holds none of its declared outputs.")
            write_io(
                connection, campaign, attempt["stage_key"], attempt["id"], "output", entries, now
            )
            connection.execute(
                """UPDATE campaign_stages SET status='succeeded', revision=revision+1, updated_at=?
                WHERE campaign_id=? AND stage_key=? AND status IN ('failed','blocked_unknown')""",
                (now, campaign["id"], attempt["stage_key"]),
            )
            recovered.append(
                {
                    "campaign_attempt_id": attempt["id"],
                    "admitted_attempt_id": attempt["admitted_attempt_id"],
                    "stage_key": attempt["stage_key"],
                    "attempt": attempt["attempt"],
                    "status_before": attempt["status"],
                    "failure_class_before": attempt["failure_class"],
                    "failure_detail_before": attempt["failure_detail"],
                    "status_after": "succeeded",
                    "admitted_resolution": resolution,
                    "settled_cents": attempt["settled_cents"],
                    "settlement_basis": attempt["settlement_basis"],
                    "evidence": item["evidence"],
                    "custody_bytes_verified": True,
                    "io_outputs": [
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "sha256": entry.sha256,
                            "size_bytes": entry.size_bytes,
                            "manifest_id": entry.manifest_id,
                        }
                        for entry in entries
                    ],
                }
            )
        stages_after = {
            row["stage_key"]: row["status"]
            for row in connection.execute(
                "SELECT stage_key, status FROM campaign_stages WHERE campaign_id=?",
                (campaign["id"],),
            )
        }
        status_after, stop_after = campaign["status"], campaign["stop_reason"]
        if all(status == "succeeded" for status in stages_after.values()):
            status_after, stop_after = "succeeded", None
        revision = campaign["revision"] + 1
        connection.execute(
            "UPDATE campaigns SET status=?, stop_reason=?, revision=?, updated_at=? WHERE id=?",
            (status_after, stop_after, revision, now, campaign["id"]),
        )
        audit: dict[str, Any] = {
            "schema": RECOVERY_SCHEMA,
            "campaign_id": campaign["id"],
            "source_receipt_digest": verified.digest,
            "reason": reason,
            "recovered_at": now,
            "recovered_by_principal_id": live["id"],
            "campaign_status_before": campaign["status"],
            "campaign_stop_reason_before": campaign["stop_reason"],
            "campaign_status_after": status_after,
            "campaign_stop_reason_after": stop_after,
            "stages_before": stages_before,
            "stages_after": stages_after,
            "attempts": recovered,
            "charges_unchanged": True,
        }
        audit["recovery_digest"] = canonical_digest(audit)
        self._event(
            connection, live, campaign["project_id"], None, RECOVERED_EVENT, revision, audit
        )
        result.update(
            campaign_status=status_after,
            recovered=[
                {
                    key: value
                    for key, value in entry.items()
                    if key in {"stage_key", "attempt", "status_before", "status_after"}
                }
                | {"outputs": [(o["name"], o["sha256"]) for o in entry["io_outputs"]]}
                for entry in recovered
            ],
            recovery_digest=audit["recovery_digest"],
            source_receipt_digest=verified.digest,
        )
        return result

    def _register_lineage(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        campaign: Mapping[str, Any],
        attempt: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> None:
        """Register what the forward ``_lineage`` step registers, in the recovery transaction:
        a ``tessera_attempt`` node for each manifest output (producing the admitted attempt)
        and a ``derived_from`` edge to each recorded input. Existing nodes and edges are
        reused, so a replay writes nothing and a node already registered is never rewritten."""
        from .artifact_lineage import _EDGE_COLUMNS, _NODE_COLUMNS, _created_at

        project = campaign["project_id"]
        operation_id = evidence["operation_id"]
        manifest = connection.execute(
            "SELECT artifacts_json FROM artifact_manifests WHERE id=? AND project_id=?",
            (attempt["manifest_id"], project),
        ).fetchone()
        outputs = sorted(
            json.loads(manifest["artifacts_json"]) if manifest else [],
            key=lambda entry: entry["relative_path"],
        )
        produced = connection.execute(
            """SELECT a.operation_id FROM research_experiment_admitted_attempts a
            JOIN tasks t ON t.id = a.task_id WHERE a.id=? AND t.project_id=?""",
            (attempt["admitted_attempt_id"], project),
        ).fetchone()
        if produced is None:
            raise Conflict("The admitted attempt was not found in the campaign's project.")
        if produced["operation_id"] not in (None, operation_id):
            raise Conflict("The admitted attempt names a different operation.")
        inputs = connection.execute(
            """SELECT name, sha256, snapshot_id FROM campaign_stage_io
            WHERE campaign_id=? AND stage_key=? AND attempt_id=? AND direction='input'
            ORDER BY name""",
            (campaign["id"], attempt["stage_key"], attempt["id"]),
        ).fetchall()

        def register(body: dict[str, Any]) -> str:
            existing = connection.execute(
                "SELECT id FROM artifact_lineage_nodes WHERE project_id=? AND sha256=?",
                (project, body["sha256"]),
            ).fetchone()
            if existing is not None:
                return existing["id"]
            row = {
                "framework": None,
                "format": None,
                "artifact_id": None,
                "result_checkpoint_id": None,
                "producing_attempt_id": None,
                "operation_id": None,
                "source_uri": None,
                **body,
                "id": "lineage_node_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project,
                "created_by_principal_id": live["id"],
                "created_at": _created_at(connection, "artifact_lineage_nodes", project),
            }
            if row["artifact_id"] is not None:
                artifact = connection.execute(
                    "SELECT sha256, operation_id FROM artifacts WHERE id=? AND project_id=?",
                    (row["artifact_id"], project),
                ).fetchone()
                if artifact is None or artifact["sha256"] != row["sha256"]:
                    raise Conflict("A recovered output does not match its custody artifact.")
                if artifact["operation_id"] not in (None, operation_id):
                    raise Conflict("A recovered output names a different operation.")
            columns = [name.strip() for name in _NODE_COLUMNS.split(",")]
            connection.execute(
                f"INSERT INTO artifact_lineage_nodes ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + name for name in columns)})",
                row,
            )
            self._event(
                connection,
                live,
                project,
                None,
                "human.artifact_lineage_node_registered",
                1,
                {"node_id": row["id"], "kind": row["kind"], "sha256": row["sha256"]},
            )
            return row["id"]

        parents = [
            register(
                {
                    "kind": "other" if entry["snapshot_id"] is None else "dataset",
                    "sha256": entry["sha256"],
                    "label": entry["name"][:200],
                    "origin": "external_import",
                }
            )
            for entry in inputs
        ]
        for item in outputs:
            child = register(
                {
                    "kind": "other",
                    "sha256": item["sha256"],
                    "label": f"{attempt['stage_key']}/{item['relative_path']}"[:200],
                    "origin": "tessera_attempt",
                    "artifact_id": item["id"],
                    "producing_attempt_id": attempt["admitted_attempt_id"],
                    "operation_id": operation_id,
                }
            )
            for parent in dict.fromkeys(parents):
                if (
                    child == parent
                    or connection.execute(
                        "SELECT 1 FROM artifact_lineage_edges WHERE child_node_id=? "
                        "AND parent_node_id=? AND relation='derived_from'",
                        (child, parent),
                    ).fetchone()
                ):
                    continue
                if connection.execute(
                    """WITH RECURSIVE up(node_id) AS (
                        SELECT ? UNION
                        SELECT e.parent_node_id FROM artifact_lineage_edges e
                        JOIN up ON e.child_node_id = up.node_id WHERE e.project_id = ?
                    ) SELECT 1 FROM up WHERE node_id = ?""",
                    (parent, project, child),
                ).fetchone():
                    raise Conflict("A recovered lineage edge would create a cycle.")
                edge = {
                    "id": "lineage_edge_" + uuid.uuid4().hex,
                    "org_id": live["org_id"],
                    "project_id": project,
                    "child_node_id": child,
                    "parent_node_id": parent,
                    "relation": "derived_from",
                    "rationale": f"Output of campaign {campaign['id']} stage "
                    f"{attempt['stage_key']} attempt {attempt['attempt']} (completion-cancel "
                    "recovery)",
                    "created_by_principal_id": live["id"],
                    "created_at": _created_at(connection, "artifact_lineage_edges", project),
                }
                columns = [name.strip() for name in _EDGE_COLUMNS.split(",")]
                connection.execute(
                    f"INSERT INTO artifact_lineage_edges ({', '.join(columns)}) "
                    f"VALUES ({', '.join(':' + name for name in columns)})",
                    edge,
                )
                self._event(
                    connection,
                    live,
                    project,
                    None,
                    "human.artifact_lineage_edge_recorded",
                    1,
                    {
                        "edge_id": edge["id"],
                        "child_node_id": child,
                        "parent_node_id": parent,
                        "relation": "derived_from",
                    },
                )

    def _resolve_admitted_success(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        attempt: Mapping[str, Any],
        reason: str,
    ) -> str:
        """Resolve the research attempt ``succeeded`` through the same evidence rule as
        ``resolve_attempt``; a human decision already recorded is never rewritten."""
        from .experiments import ExperimentStore

        admitted = connection.execute(
            "SELECT * FROM research_experiment_admitted_attempts WHERE id=?",
            (attempt["admitted_attempt_id"],),
        ).fetchone()
        if admitted is None:
            raise Conflict("The attempt has no admitted research attempt.")
        if admitted["outcome"] == "succeeded":
            return "already_succeeded"
        if admitted["outcome"] is not None:
            raise Conflict("The admitted attempt already carries another resolution.")
        ExperimentStore(self.database_path.parent).record_admitted_resolution(
            connection,
            live,
            dict(admitted),
            "succeeded",
            ("completion-cancel recovery: " + reason)[:500],
        )
        return "succeeded"


def _fingerprints(connection: sqlite3.Connection, outputs: list[dict[str, Any]]) -> list[str]:
    """What the write transaction re-checks after the bytes were hashed: each file's custody
    row and, for a chunked file, the digest over its stored chunk list."""
    from .large_artifacts import chunk_fingerprint

    found = []
    for item in outputs:
        row = connection.execute(
            "SELECT sha256, size FROM artifacts WHERE id=?", (item["artifact_id"],)
        ).fetchone()
        chunks = chunk_fingerprint(connection, item["artifact_id"])
        found.append(
            f"{item['artifact_id']}:{row and row['sha256']}:{row and row['size']}:{chunks}"
        )
    return found


def _public(item: Mapping[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in item.items() if key != "evidence"}
    evidence = item.get("evidence")
    if evidence is not None:
        out["outputs"] = [(o["relative_path"], o["sha256"], o["size"]) for o in evidence["outputs"]]
        out["manifest_id"] = evidence["manifest_id"]
    return out
