"""Campaign delegation (ADR 0038): one human approval authorizes exactly the spec's steps.

The runner acts as the approving principal. Each delegated step is an ordinary store
call whose principal dict carries ``CAMPAIGN_DELEGATION_KEY``. That marker exists only
in-process (``Store.authenticate`` strips it; HTTP code never sets it). ``Store._mutate``
hands it to :func:`admit` inside the command's own transaction, which requires a matching
pending (or, on replay, done) intent row, a live human approver, an active approval, a
campaign state that allows the step, a closed-table path whose resource ids this campaign
owns. :func:`complete` marks the intent done in that same transaction as the effect, its
events and the idempotency row, so a ``pending`` intent never has a committed effect.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from . import store as _store
from .approval_trust import CAMPAIGN_DELEGATION_KEY, DELEGATED_ACTOR_KEY
from .store import Forbidden, StoreError, canonical_digest

__all__ = [
    "CAMPAIGN_DELEGATION_KEY",
    "DELEGATED_ACTOR_KEY",
    "REDUCE_ONLY_STEPS",
    "STEP_TABLE",
    "admit",
    "approver_is_live",
    "assert_compose_sources_owned",
    "assert_gate_binding_matches",
    "complete",
    "delegated_principal",
    "intent_key",
    "write_intent",
]

REDUCE_ONLY_STEPS = frozenset(
    {
        "revoke_grant",
        "reconcile_grant",
        "reconcile_unattempted",
        "revoke_release",
        "resolve_attempt",
        "cancel_route",
        "reconcile_unclaimed_salad",
    }
)

# step -> (method, path template). Each template equals the path the named store method
# passes to Store._mutate (verified against the code, not assumed).
STEP_TABLE: dict[str, tuple[str, str]] = {
    "create_task": ("POST", "/projects/{project_id}/tasks"),
    "revise_plan": ("PUT", "/tasks/{task_id}/plan"),
    "approve_task": ("POST", "/tasks/{task_id}/approve"),
    "compose_snapshot": ("POST", "/projects/{project_id}/composed-snapshots"),
    "release_snapshot": ("POST", "/snapshots/{snapshot_id}/releases"),
    "revoke_release": ("POST", "/snapshot-releases/{release_id}/revoke"),
    "create_experiment": ("POST", "/projects/{project_id}/experiments"),
    "authorize_budget": ("POST", "/experiments/{experiment_id}/budget/authorization"),
    "issue_grant": ("POST", "/projects/{project_id}/execution-grants"),
    "admit_attempt": ("POST", "/experiment-variants/{variant_id}/admitted-attempts"),
    "revoke_grant": ("POST", "/execution-grants/{grant_id}/revoke"),
    "reconcile_grant": ("POST", "/execution-grants/{grant_id}/reconcile"),
    "reconcile_unattempted": (
        "POST",
        "/execution-grants/{grant_id}/reconcile-expired-unattempted",
    ),
    "record_disposition": ("POST", "/artifact-manifests/{manifest_id}/dispositions"),
    "custody_release": ("POST", "/artifact-manifests/{manifest_id}/custody-releases"),
    "resolve_attempt": ("POST", "/experiment-admitted-attempts/{attempt_id}/resolve"),
    "link_artifacts": ("POST", "/experiment-attempts/{attempt_id}/artifacts"),
    "derive_measurement": ("POST", "/experiment-attempts/{attempt_id}/derived-measurements"),
    # ADR 0049: pre-registered split binding and metric records, only for a stage whose
    # approved spec declares gate_evidence (checked in _resource_owned and the runner).
    "bind_split": ("POST", "/experiment-attempts/{attempt_id}/split-binding"),
    "record_gate_metric": ("POST", "/experiment-attempts/{attempt_id}/metric-records"),
    # ADR 0052: ingest a stage's typed evidence rows; delegation-only (no human route).
    "ingest_evidence_rows": ("POST", "/experiment-attempts/{attempt_id}/evidence-rows"),
    # ADR 0097: record a provider-run stage's public fetch rows, read by control from the
    # attempt's retained output.json and checked against the frozen pins; delegation-only.
    "record_provider_fetch": ("POST", "/experiment-attempts/{attempt_id}/provider-fetch"),
    "register_lineage_node": ("POST", "/projects/{project_id}/artifact-lineage/nodes"),
    "record_lineage_edge": ("POST", "/projects/{project_id}/artifact-lineage/edges"),
    # Salad GPU stages (ADR 0038 amendment). Never delegated: compute charges, budget bounds
    # and project execution limits. Source assessment and decision resolution are delegated
    # only under an exact-digest review pre-approval (ADR 0044), checked in the command.
    "create_study": ("POST", "/projects/{project_id}/studies"),
    "create_study_source": ("POST", "/studies/{study_id}/sources"),
    "register_study_variant": ("POST", "/studies/{study_id}/variants"),
    "create_split_lock": ("POST", "/projects/{project_id}/split-locks"),
    "propose_decision": ("POST", "/interaction/decisions"),
    "assess_source": ("POST", "/sources/{source_id}/assessment"),
    "resolve_decision": ("POST", "/interaction/decisions/{decision_id}/resolve"),
    "request_capability": ("POST", "/tasks/{task_id}/capabilities"),
    "approve_capability": ("POST", "/capabilities/{capability_id}/approve"),
    "propose_route": ("POST", "/tasks/{task_id}/remote-executions"),
    "approve_route": ("POST", "/remote-executions/{route_id}/commands"),
    "start_route": ("POST", "/remote-executions/{route_id}/commands"),
    "cancel_route": ("POST", "/remote-executions/{route_id}/commands"),
    "reconcile_unclaimed_salad": (
        "POST",
        "/execution-grants/{grant_id}/reconcile-unclaimed-salad",
    ),
}

# The three route command steps share one path; the ``command`` resource tells them apart.
_ROUTE_COMMANDS = {"approve_route": "approve", "start_route": "start", "cancel_route": "cancel"}
# Resources a step may declare beyond its path placeholders and the common extras.
_STEP_EXTRAS = {
    "propose_decision": frozenset({"project_id", "study_id", "decision_action"}),
    **{step: frozenset({"command"}) for step in _ROUTE_COMMANDS},
}
_DECISION_ACTIONS = frozenset({"plan", "source_use"})
_CLASSIFICATIONS = frozenset({"public", "private_derived"})

# Resource keys an intent may declare beyond its path placeholders.
_EXTRA_RESOURCES = frozenset(
    {
        "snapshot_id",
        "worker_principal_id",
        "disposition",
        "classification",
        "provider_id",
        "preapproval_id",
        "split_lock_id",
    }
)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_FORWARD_STATES = frozenset({"running"})
_REDUCE_STATES = frozenset({"running", "stopping", "cancel_requested", "blocked"})
_MAX_RESPONSE_BYTES = 256 * 1024
_DENIED = "Campaign delegation does not authorize this step."


def _now() -> datetime:
    return _store._parse_utc(_store.utc_now())


def _template_values(step: str, method: str, path: str) -> dict[str, str] | None:
    """Placeholder values when method/path equal the closed-table template, else None."""
    entry = STEP_TABLE.get(step)
    if entry is None or entry[0] != method:
        return None
    pattern = (
        "^"
        + "".join(
            "([^/]+)" if part.startswith("{") else re.escape(part)
            for part in re.split(r"(\{[a-z_]+\})", entry[1])
            if part
        )
        + "$"
    )
    match = re.match(pattern, path)
    if match is None:
        return None
    return dict(zip(_PLACEHOLDER.findall(entry[1]), match.groups(), strict=True))


def intent_key(
    campaign_id: str, stage_key: str | None, attempt: int | None, step: str, ordinal: int
) -> str:
    key = (
        f"cmp:{campaign_id[-32:]}:{stage_key or '-'}:{'-' if attempt is None else attempt}"
        f":{step}:{ordinal}"
    )
    if len(key) > 128:
        raise StoreError("Campaign step idempotency key is too long.")
    return key


def delegated_principal(approval: Mapping, campaign: Mapping, intent_id: str) -> dict[str, Any]:
    """In-process principal for one delegated call; never serializable from HTTP."""
    return {
        "id": approval["approving_principal_id"],
        "org_id": campaign["org_id"],
        CAMPAIGN_DELEGATION_KEY: {"campaign_id": campaign["id"], "intent_id": intent_id},
    }


def _check_resources(step: str, path_values: dict[str, str], resources: Mapping[str, str]) -> None:
    for name, value in path_values.items():
        if resources.get(name) != value:
            raise Forbidden(_DENIED)
    allowed = set(path_values) | _EXTRA_RESOURCES | _STEP_EXTRAS.get(step, frozenset())
    if any(
        name not in allowed or not isinstance(value, str) or not value
        for name, value in resources.items()
    ):
        raise Forbidden(_DENIED)
    if step == "record_disposition" and resources.get("disposition") != "retain_quarantined":
        raise Forbidden(_DENIED)
    if step in _ROUTE_COMMANDS and resources.get("command") != _ROUTE_COMMANDS[step]:
        raise Forbidden(_DENIED)
    if step == "propose_decision" and (
        resources.get("decision_action") not in _DECISION_ACTIONS
        or not resources.get("study_id")
        or not resources.get("project_id")
    ):
        raise Forbidden(_DENIED)
    if step == "bind_split" and not resources.get("split_lock_id"):
        raise Forbidden(_DENIED)
    if step == "release_snapshot" and resources.get("classification") not in _CLASSIFICATIONS:
        raise Forbidden(_DENIED)
    if step in {"assess_source", "resolve_decision"} and not resources.get("preapproval_id"):
        raise Forbidden(_DENIED)


def write_intent(
    connection: sqlite3.Connection,
    *,
    campaign: Mapping,
    approval: Mapping,
    stage_key: str | None,
    attempt: int | None,
    step: str,
    ordinal: int,
    method: str,
    path: str,
    request_digest: str,
    idempotency_key: str,
    resources: dict[str, str],
) -> dict[str, Any]:
    """Record the intent before the effect (runner transaction A); idempotent per slot."""
    values = _template_values(step, method, path)
    if values is None:
        raise StoreError("Campaign step is not in the delegation table.")
    _check_resources(step, values, resources)
    if approval["campaign_id"] != campaign["id"] or approval["status"] != "active":
        raise Forbidden(_DENIED)
    existing = connection.execute(
        """SELECT * FROM campaign_step_intents WHERE campaign_id=? AND approval_id=?
        AND stage_key IS ? AND attempt IS ? AND step=? AND ordinal=?""",
        (campaign["id"], approval["id"], stage_key, attempt, step, ordinal),
    ).fetchone()
    if existing is not None:
        if (
            existing["method"],
            existing["path"],
            existing["request_digest"],
            existing["idempotency_key"],
        ) != (method, path, request_digest, idempotency_key):
            raise _store.Conflict("Campaign step intent was already recorded differently.")
        return dict(existing)
    row = {
        "id": "intent_" + uuid.uuid4().hex,
        "campaign_id": campaign["id"],
        "approval_id": approval["id"],
        "stage_key": stage_key,
        "attempt": attempt,
        "step": step,
        "ordinal": ordinal,
        "principal_id": approval["approving_principal_id"],
        "method": method,
        "path": path,
        "request_digest": request_digest,
        "idempotency_key": idempotency_key,
        "resources_json": json.dumps(resources, sort_keys=True, separators=(",", ":")),
        "status": "pending",
        "response_status": None,
        "response_json": None,
        "error_class": None,
        "created_at": _store.utc_now(),
        "completed_at": None,
    }
    connection.execute(
        """INSERT INTO campaign_step_intents
        (id, campaign_id, approval_id, stage_key, attempt, step, ordinal, principal_id, method,
         path, request_digest, idempotency_key, resources_json, status, response_status,
         response_json, error_class, created_at, completed_at)
        VALUES (:id,:campaign_id,:approval_id,:stage_key,:attempt,:step,:ordinal,:principal_id,
        :method,:path,:request_digest,:idempotency_key,:resources_json,:status,:response_status,
        :response_json,:error_class,:created_at,:completed_at)""",
        row,
    )
    return row


def _spec_snapshot_ids(campaign: Mapping) -> set[str]:
    spec = json.loads(campaign["spec_json"])
    return {
        source["snapshot_id"]
        for stage in spec["stages"]
        for source in stage["inputs"]
        if source["kind"] == "snapshot"
    }


def _spec_artifact_sources(campaign: Mapping) -> list[dict[str, Any]]:
    spec = json.loads(campaign["spec_json"])
    return [
        source
        for stage in spec["stages"]
        for source in stage["inputs"]
        if source["kind"] == "artifact"
    ]


def _normal_spec(campaign: Mapping):
    from .campaign_models import normalize, parse_spec_json

    return normalize(parse_spec_json(campaign["spec_json"]))


def _stage_provider(
    connection: sqlite3.Connection, campaign: Mapping, stage_key: str | None
) -> str | None:
    """The provider of a campaign stage.

    A stage with alternates: the placement recorded on the stage (ADR 0060). A stage with an
    approved infrastructure reroute list and no alternates: the provider its latest attempt
    runs on, which a reroute may have moved (ADR 0058). Otherwise the recorded column, else the
    normalized spec's provider."""
    if stage_key is None:
        return None
    try:
        stage = _normal_spec(campaign).stage(stage_key)
    except (StopIteration, ValueError):
        stage = None
    if stage is not None and not stage.alternates and stage.retry.reroute_providers:
        latest = connection.execute(
            """SELECT provider_id FROM campaign_stage_attempts WHERE campaign_id=?
            AND stage_key=? ORDER BY attempt DESC LIMIT 1""",
            (campaign["id"], stage_key),
        ).fetchone()
        if latest is not None and latest["provider_id"]:
            return latest["provider_id"]
    row = connection.execute(
        "SELECT provider_id FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
        (campaign["id"], stage_key),
    ).fetchone()
    if row is not None and row["provider_id"]:
        return row["provider_id"]
    return None if stage is None else stage.provider


def _snapshot_owned(connection: sqlite3.Connection, campaign: Mapping, snapshot_id: str) -> bool:
    if snapshot_id in _spec_snapshot_ids(campaign):
        return True
    return (
        connection.execute(
            "SELECT 1 FROM campaign_stages WHERE campaign_id=? AND input_snapshot_id=?",
            (campaign["id"], snapshot_id),
        ).fetchone()
        is not None
    )


def _manifest_owned(
    connection: sqlite3.Connection,
    campaign: Mapping,
    manifest_id: str,
    pinned: bool = True,
) -> bool:
    if (
        connection.execute(
            "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND manifest_id=?",
            (campaign["id"], manifest_id),
        ).fetchone()
        is not None
    ):
        return True
    if not pinned:
        return False
    # An artifact input pins another campaign's output; the manifest is owned only through
    # the pinned index row (same project, same digest), never by id alone. Only the compose
    # step may take such a manifest (``pinned``); no other step owns another campaign's.
    for source in _spec_artifact_sources(campaign):
        row = connection.execute(
            """SELECT 1 FROM campaign_stage_io WHERE campaign_id=? AND stage_key=?
            AND direction='output' AND name=? AND sha256=? AND manifest_id=?
            AND org_id=? AND project_id=?""",
            (
                source["campaign_id"],
                source["stage"],
                source["output"],
                source["sha256"],
                manifest_id,
                campaign["org_id"],
                campaign["project_id"],
            ),
        ).fetchone()
        if row is not None:
            return True
    return False


_STAGE_COLUMNS = {
    "task_id": "task_id",
    "experiment_id": "experiment_id",
    "variant_id": "variant_id",
}
_ATTEMPT_COLUMNS = {
    "release_id": "release_id",
    "grant_id": "grant_id",
    "attempt_id": "admitted_attempt_id",
}
_SALAD_STAGE_COLUMNS = {
    "study_id": "study_id",
    "capability_id": "capability_id",
    "source_id": "source_id",
}


def _spec_workers(campaign: Mapping) -> set[str]:
    """Workers the campaign may use: v1 stage workers and each stage provider's instance."""
    import dataclasses

    from .campaign_models import stage_candidates, stage_provider

    workers: set[str] = set()
    # ADR 0060: each approved provider candidate of a stage, and ADR 0058: each approved
    # infrastructure reroute provider (as it would run the stage), never anything else.
    for stage in (
        c
        for s in _normal_spec(campaign).stages
        for c in (
            *stage_candidates(s),
            *(
                dataclasses.replace(s, provider=pid, worker_principal_id=None, provider_config={})
                for pid in s.retry.reroute_providers
            ),
        )
    ):
        if stage.worker_principal_id:
            workers.add(stage.worker_principal_id)
        try:
            provider = stage_provider(stage.provider)
        except KeyError:
            continue
        # A v2 Salad worker lives in the stage's provider_config, not on the descriptor or
        # the stage: ask the provider, as CampaignStore.stage_worker does.
        candidates = [provider.descriptor.worker_principal_id]
        try:
            candidates.append(provider.grant_request(None, None, stage).worker_principal_id)
        except Exception:  # noqa: BLE001 - a stage the provider cannot grant owns no worker
            pass
        workers.update(c for c in candidates if c)
    return workers


def _resource_owned(
    connection: sqlite3.Connection,
    campaign: Mapping,
    name: str,
    value: str,
    step: str | None = None,
) -> bool:
    cid = campaign["id"]
    if name == "project_id":
        return value == campaign["project_id"]
    if name in _STAGE_COLUMNS:
        column = _STAGE_COLUMNS[name]
        row = connection.execute(
            f"SELECT 1 FROM campaign_stages WHERE campaign_id=? AND {column}=?", (cid, value)
        ).fetchone()
        return row is not None
    if name in _ATTEMPT_COLUMNS:
        column = _ATTEMPT_COLUMNS[name]
        row = connection.execute(
            f"SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND {column}=?",
            (cid, value),
        ).fetchone()
        return row is not None
    if name == "manifest_id":
        return _manifest_owned(connection, campaign, value, pinned=step == "compose_snapshot")
    if name == "snapshot_id":
        return _snapshot_owned(connection, campaign, value)
    if name == "worker_principal_id":
        return value in _spec_workers(campaign)
    if name in _SALAD_STAGE_COLUMNS:
        column = _SALAD_STAGE_COLUMNS[name]
        return (
            connection.execute(
                f"SELECT 1 FROM campaign_salad_stages WHERE campaign_id=? AND {column}=?",
                (cid, value),
            ).fetchone()
            is not None
        )
    if name == "preapproval_id":
        return (
            connection.execute(
                """SELECT 1 FROM campaign_review_preapprovals p JOIN campaign_approvals a
                ON a.id = p.approval_id WHERE p.id=? AND p.campaign_id=? AND a.status='active'""",
                (value, cid),
            ).fetchone()
            is not None
        )
    if name == "decision_id":
        return (
            connection.execute(
                """SELECT 1 FROM campaign_salad_attempts sa JOIN campaign_stage_attempts a
                ON a.id = sa.attempt_id WHERE a.campaign_id=?
                AND (sa.plan_decision_id=? OR sa.source_decision_id=?)""",
                (cid, value, value),
            ).fetchone()
            is not None
        )
    if name == "route_id":
        return (
            connection.execute(
                """SELECT 1 FROM campaign_salad_attempts sa JOIN campaign_stage_attempts a
                ON a.id = sa.attempt_id WHERE a.campaign_id=? AND sa.route_id=?""",
                (cid, value),
            ).fetchone()
            is not None
        )
    if name == "split_lock_id":
        return any(
            stage.gate_evidence is not None and stage.gate_evidence.split_lock_id == value
            for stage in _normal_spec(campaign).stages
        )
    if name == "command":
        return value in {"approve", "start", "cancel"}
    if name == "decision_action":
        return value in _DECISION_ACTIONS
    if name == "classification":
        return value in _CLASSIFICATIONS
    return name == "disposition" and value == "retain_quarantined"


def _declared_gate_evidence(campaign: Mapping, stage_key: str | None):
    try:
        stage = _normal_spec(campaign).stage(stage_key)
    except (StopIteration, ValueError, TypeError):
        return None
    return stage.gate_evidence


def assert_gate_binding_matches(
    connection: sqlite3.Connection, live: Mapping, body: Mapping
) -> None:
    """In the bind_split command: the executed lock, seed and metrics equal the approved spec.

    The runner builds the body from the spec, but the human approved the spec, not the
    runner, so the delegation layer re-checks the body actually executed (ADR 0049).
    """
    delegated = live.get(DELEGATED_ACTOR_KEY)
    if not delegated:
        return
    campaign = connection.execute(
        "SELECT * FROM campaigns WHERE id=?", (delegated["campaign_id"],)
    ).fetchone()
    intent = connection.execute(
        "SELECT stage_key FROM campaign_step_intents WHERE id=? AND campaign_id=?",
        (delegated["intent_id"], delegated["campaign_id"]),
    ).fetchone()
    if campaign is None or intent is None:
        raise Forbidden(_DENIED)
    gate = _declared_gate_evidence(campaign, intent["stage_key"])
    if gate is None:
        raise Forbidden(_DENIED)
    declared = sorted(
        ({"name": m.name, "partition": m.partition, "unit": m.unit} for m in gate.metrics),
        key=lambda m: m["name"],
    )
    executed = sorted(
        (
            {"name": m["name"], "partition": m["partition"], "unit": m["unit"]}
            for m in body["metrics"]
        ),
        key=lambda m: m["name"],
    )
    if (
        body["split_lock_id"] != gate.split_lock_id
        or body["seed"] != gate.seed
        or executed != declared
    ):
        raise Forbidden(_DENIED)


def assert_compose_sources_owned(
    connection: sqlite3.Connection, live: Mapping, sources: list[tuple[str, str]]
) -> None:
    """In the compose command: every delegated source must belong to the campaign.

    ``sources`` are ``(kind, id)`` with kind ``snapshot`` or ``manifest``; the ids come
    from the request body actually executed, not from the intent's claim.
    """
    delegated = live.get(DELEGATED_ACTOR_KEY)
    if not delegated:
        return
    campaign = connection.execute(
        "SELECT * FROM campaigns WHERE id=?", (delegated["campaign_id"],)
    ).fetchone()
    if campaign is None:
        raise Forbidden(_DENIED)
    for kind, ident in sources:
        owned = (
            _snapshot_owned(connection, campaign, ident)
            if kind == "snapshot"
            else _manifest_owned(connection, campaign, ident)
        )
        if not owned:
            raise Forbidden(_DENIED)


_FINISHED_ATTEMPT = (
    "succeeded",
    "failed_transient",
    "failed_permanent",
    "unknown",
    "refused_budget",
    "cancelled",
)


def _attempt_in_flight(connection: sqlite3.Connection, campaign_id: str, intent: Mapping) -> bool:
    if intent["stage_key"] is None or intent["attempt"] is None:
        return False
    row = connection.execute(
        "SELECT status FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
        "AND attempt=?",
        (campaign_id, intent["stage_key"], intent["attempt"]),
    ).fetchone()
    return row is not None and row["status"] not in _FINISHED_ATTEMPT


def admit(
    connection: sqlite3.Connection,
    live: dict,
    marker: Mapping,
    method: str,
    path: str,
    digest: str,
    key: str,
) -> dict[str, Any]:
    """Authorize one delegated call inside its transaction; returns the intent row."""
    if not isinstance(marker, Mapping):
        raise Forbidden(_DENIED)
    intent = connection.execute(
        "SELECT * FROM campaign_step_intents WHERE id=? AND campaign_id=?",
        (marker.get("intent_id"), marker.get("campaign_id")),
    ).fetchone()
    if (
        intent is None
        or intent["status"] not in {"pending", "done"}
        or (intent["method"], intent["path"], intent["request_digest"], intent["idempotency_key"])
        != (method, path, digest, key)
        or intent["principal_id"] != live["id"]
    ):
        raise Forbidden(_DENIED)
    campaign = connection.execute(
        "SELECT * FROM campaigns WHERE id=?", (intent["campaign_id"],)
    ).fetchone()
    approval = connection.execute(
        "SELECT * FROM campaign_approvals WHERE id=? AND campaign_id=?",
        (intent["approval_id"], intent["campaign_id"]),
    ).fetchone()
    if (
        campaign is None
        or approval is None
        or campaign["org_id"] != live["org_id"]
        or approval["approving_principal_id"] != live["id"]
        or approval["status"] != "active"
        or live["role"] != "human"
        or live["project_id"] not in (None, campaign["project_id"])
    ):
        raise Forbidden(_DENIED)
    step = intent["step"]
    if step in REDUCE_ONLY_STEPS:
        allowed = campaign["status"] in _REDUCE_STATES
    else:
        in_window = _now() < _store._parse_utc(approval["expires_at"])
        allowed = campaign["status"] in _FORWARD_STATES and in_window
        if campaign["status"] == "stopping" and in_window:
            # A stop rule winds the campaign down: an attempt already reserved may finish its
            # own steps (its hold and grant exist), but no stage-level step and no step of an
            # attempt that was never reserved is admitted.
            allowed = _attempt_in_flight(connection, campaign["id"], intent)
    if not allowed:
        raise Forbidden(_DENIED)
    values = _template_values(step, method, path)
    if values is None:
        raise Forbidden(_DENIED)
    resources = json.loads(intent["resources_json"])
    _check_resources(step, values, resources)
    # Every delegated step belongs to a stage: no campaign-level (stage_key None) intent is
    # ever created, so one is refused here (provider_id None) rather than special-cased. A
    # future campaign-level reduce-only step would need an explicit allowance.
    if step in {"bind_split", "record_gate_metric"} and (
        _declared_gate_evidence(campaign, intent["stage_key"]) is None
    ):
        # Only a stage whose approved spec declares gate_evidence may bind or record.
        raise Forbidden(_DENIED)
    provider_id = resources.get("provider_id") or _stage_provider(
        connection, campaign, intent["stage_key"]
    )
    if provider_id is None or provider_id != _stage_provider(
        connection, campaign, intent["stage_key"]
    ):
        raise Forbidden(_DENIED)
    from .campaign_models import stage_provider

    try:
        delegable = stage_provider(provider_id).descriptor.delegable_steps
    except KeyError:
        raise Forbidden(_DENIED) from None
    if step not in delegable:
        raise Forbidden(_DENIED)
    if not all(
        _resource_owned(connection, campaign, n, v, step)
        for n, v in resources.items()
        if n != "provider_id"
    ):
        # create_task has no resource but the project; every other id must pre-exist.
        raise Forbidden(_DENIED)
    if step == "release_snapshot":
        # A snapshot the platform recorded private-derived is never released as public.
        from .data_class import snapshot_data_class

        if (
            resources["classification"] == "public"
            and snapshot_data_class(connection, resources["snapshot_id"])["data_class"] != "public"
        ):
            raise Forbidden(_DENIED)
    if intent["status"] == "pending" and step == "revise_plan":
        # Only the first plan is delegated: no earlier revise_plan may have completed.
        if connection.execute(
            """SELECT 1 FROM campaign_step_intents WHERE campaign_id=? AND stage_key IS ?
            AND step='revise_plan' AND status='done' AND id<>?""",
            (campaign["id"], intent["stage_key"], intent["id"]),
        ).fetchone():
            raise Forbidden(_DENIED)
    live["project_id"] = campaign["project_id"]
    if approval["automation_label"]:
        live[_store.AUTOMATION_KEY] = approval["automation_label"]
    live[DELEGATED_ACTOR_KEY] = {
        "campaign_id": campaign["id"],
        "approval_id": approval["id"],
        "intent_id": intent["id"],
        "step": step,
        "approval_decision_class": approval["decision_class"],
    }
    return dict(intent)


def complete(connection: sqlite3.Connection, intent: Mapping, status: int, response: dict) -> None:
    """Mark the intent done in the effect's transaction; no-op for an already-done replay."""
    encoded = json.dumps(response, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > _MAX_RESPONSE_BYTES:
        encoded = json.dumps(
            {"digest_only": canonical_digest(response), "bytes": len(encoded.encode())},
            sort_keys=True,
            separators=(",", ":"),
        )
    now = _store.utc_now()
    updated = connection.execute(
        """UPDATE campaign_step_intents SET status='done', response_status=?, response_json=?,
        completed_at=? WHERE id=? AND status='pending'""",
        (status, encoded, now, intent["id"]),
    )
    if updated.rowcount != 1:
        return
    campaign = connection.execute(
        "SELECT org_id, project_id, revision FROM campaigns WHERE id=?", (intent["campaign_id"],)
    ).fetchone()
    task_id = None
    if intent["stage_key"] is not None:
        row = connection.execute(
            "SELECT task_id FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            (intent["campaign_id"], intent["stage_key"]),
        ).fetchone()
        task_id = row["task_id"] if row is not None else None
    _store.Store._event(
        connection,
        {"id": intent["principal_id"], "org_id": campaign["org_id"]},
        campaign["project_id"],
        task_id,
        "campaign.step_completed",
        campaign["revision"],
        {
            "campaign_id": intent["campaign_id"],
            "intent_id": intent["id"],
            "stage_key": intent["stage_key"],
            "attempt": intent["attempt"],
            "step": intent["step"],
            "method": intent["method"],
            "path": intent["path"],
            "response_status": status,
        },
    )


def approver_is_live(connection: sqlite3.Connection, approval: Mapping) -> bool:
    """True while the approving human's credential is live and its principal unrevoked."""
    row = connection.execute(
        "SELECT org_id FROM campaigns WHERE id=?", (approval["campaign_id"],)
    ).fetchone()
    if row is None:
        return False
    try:
        live = _store.Store._live_principal(
            connection, {"id": approval["approving_principal_id"], "org_id": row["org_id"]}
        )
    except Forbidden:
        return False
    return live["role"] == "human"
