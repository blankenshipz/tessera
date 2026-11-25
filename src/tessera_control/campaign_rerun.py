"""Re-run a finished campaign's failed stages, or resubmit its identical spec (ADR 0069).

Pure planning plus the reads it needs. A re-run is a NEW campaign: it is submitted like any
other, grants nothing, and a person (or the operator's own approval path) approves its exact
digest. The stages that already succeeded are not run again; a stage that consumed one of their
outputs gets a ``kind: artifact`` input pinning that output by campaign id and sha256, which the
platform resolves only from a succeeded attempt in a terminal campaign's receipt whose output
manifest is retained and custody-released. Nothing is invented: an output with no single
recorded digest, one that is not custody-released, a v1 spec (for the failed-stages mode) or an
unfinished campaign makes the plan refuse instead of guess. The new campaign may not ask for more
than the first campaign's cap less what the whole re-run tree has committed: settled spend of
finished campaigns plus the full cap of unfinished ones, siblings included (ADR 0074); the
program budget, trust, data-class and provider checks run at submit and approval as for any spec.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .campaign_models import TERMINAL_CAMPAIGN, normalize, parse_spec_json
from .campaign_seals import carried_seals

FINISHED = frozenset({"failed", "stopped", "cancelled"})
MAX_NAME = 200
RERUN_EVENT = "campaign.rerun_submitted"
MAX_TREE = 200  # members read per tree; a longer tree is reported truncated


class RerunRefused(ValueError):
    """The campaign cannot be re-run as asked; the message says why."""


RetryRefused = RerunRefused  # the name the offline MCP builder (ADR 0055) has always used


class RerunCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["failed_stages", "identical"] = "failed_stages"
    stage_keys: Annotated[list[str], Field(min_length=1, max_length=64)] | None = None
    budget_cap_cents: Annotated[int, Field(strict=True, ge=0, le=5000)] | None = None


def _dependencies(stage: dict[str, Any]) -> list[str]:
    return [i["stage"] for i in stage["inputs"] if i.get("kind") == "stage"]


def custody_released(connection: sqlite3.Connection, manifest_id: str | None) -> bool:
    """The condition a later campaign's artifact pin needs (campaign_io.resolve_artifact)."""
    if manifest_id is None:
        return False
    manifest = connection.execute(
        "SELECT manifest_digest FROM artifact_manifests WHERE id=?", (manifest_id,)
    ).fetchone()
    disposition = connection.execute(
        "SELECT disposition FROM artifact_dispositions WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest_id,),
    ).fetchone()
    release = connection.execute(
        "SELECT state, manifest_digest FROM artifact_custody_releases WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest_id,),
    ).fetchone()
    return bool(
        manifest is not None
        and disposition is not None
        and disposition["disposition"] == "retain_quarantined"
        and release is not None
        and release["state"] == "released"
        and release["manifest_digest"] == manifest["manifest_digest"]
    )


def load_source(connection: sqlite3.Connection, campaign: sqlite3.Row) -> dict[str, Any]:
    """The campaign as the planner reads it: spec, stage statuses and recorded outputs."""
    stages = [
        {"stage_key": row["stage_key"], "status": row["status"]}
        for row in connection.execute(
            "SELECT stage_key, status FROM campaign_stages WHERE campaign_id=? ORDER BY topo_index",
            (campaign["id"],),
        )
    ]
    outputs = []
    for row in connection.execute(
        """SELECT stage_key, name, sha256, manifest_id FROM campaign_stage_io
        WHERE campaign_id=? AND direction='output' ORDER BY created_at, stage_key, name""",
        (campaign["id"],),
    ):
        outputs.append(
            {
                "stage_key": row["stage_key"],
                "name": row["name"],
                "sha256": row["sha256"],
                "custody_released": custody_released(connection, row["manifest_id"]),
            }
        )
    tree = rerun_tree(connection, campaign)
    return {
        "id": campaign["id"],
        "status": campaign["status"],
        "spec": json.loads(campaign["spec_json"]),
        "spec_digest": campaign["spec_digest"],
        "settled_cents": campaign["settled_cents"],
        "lineage_remaining_cents": lineage_remaining(connection, campaign, tree),
        "lineage_holding": lineage_holding(connection, campaign, tree),
        "stages": stages,
        "outputs": outputs,
    }


def rerun_tree(connection: sqlite3.Connection, campaign: sqlite3.Row) -> dict[str, Any]:
    """The whole re-run tree this campaign belongs to and what it has committed (ADR 0074).

    The root is the first campaign (found through the recorded ``campaign.rerun_submitted``
    links); the tree is the root and every campaign re-run from it, at any depth, siblings
    included. A finished campaign commits what it settled; an unfinished one (submitted, approved
    or running) commits its whole cap, or what it already settled if that is more, so a link that
    has not settled yet still counts. Cancelling an unapproved re-run frees its cap. Read-only.
    """
    root = campaign
    seen = {campaign["id"]}
    while True:
        link = lineage_of(connection, root["id"], root["project_id"])
        if link is None or link["rerun_of"] in seen:
            break
        parent = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (link["rerun_of"],)
        ).fetchone()
        if parent is None:
            break
        seen.add(parent["id"])
        root = parent
    members: list[dict[str, Any]] = []
    queue = [root]
    visited = {root["id"]}
    while queue and len(members) < MAX_TREE:
        node = queue.pop(0)
        finished = node["status"] in TERMINAL_CAMPAIGN
        settled = int(node["settled_cents"])
        committed = settled if finished else max(int(node["budget_cap_cents"]), settled)
        members.append(
            {
                "campaign_id": node["id"],
                "status": node["status"],
                "cap_cents": int(node["budget_cap_cents"]),
                "settled_cents": settled,
                "committed_cents": committed,
            }
        )
        for child in reruns_of(connection, node["id"], node["project_id"]):
            if child["campaign_id"] in visited:
                continue
            visited.add(child["campaign_id"])
            row = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (child["campaign_id"],)
            ).fetchone()
            if row is not None:
                queue.append(row)
    root_cap = int(json.loads(root["spec_json"])["budget_cap_cents"])
    committed_total = sum(m["committed_cents"] for m in members)
    return {
        "root_campaign_id": root["id"],
        "root_cap_cents": root_cap,
        "committed_cents": committed_total,
        "remaining_cents": root_cap - committed_total,
        "members": members,
        "truncated": bool(queue),
    }


def lineage_holding(
    connection: sqlite3.Connection,
    campaign: sqlite3.Row,
    tree: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Unfinished members of the tree holding budget (never the first campaign), for messages.

    ``tree`` is an already computed ``rerun_tree`` result, to avoid walking the tree twice.
    """
    if tree is None:
        tree = rerun_tree(connection, campaign)
    return [
        {"campaign_id": m["campaign_id"], "committed_cents": m["committed_cents"]}
        for m in tree["members"]
        if m["status"] not in TERMINAL_CAMPAIGN
        and m["campaign_id"] != tree["root_campaign_id"]
        and m["committed_cents"] > 0
    ][:5]


def holding_hint(source: dict[str, Any]) -> str:
    """How budget held by unfinished re-runs is freed, named in a refusal."""
    held = source.get("lineage_holding") or []
    if not held:
        return ""
    names = ", ".join(f"{h['campaign_id']} ({h['committed_cents']} cents)" for h in held)
    return (
        f" Unfinished re-runs hold budget: {names}. A person can cancel one that is not wanted "
        "(a campaign that was never approved is cancelled at once and its cap is freed)."
    )


def lineage_remaining(
    connection: sqlite3.Connection,
    campaign: sqlite3.Row,
    tree: dict[str, Any] | None = None,
) -> int:
    """What is left of the tree's first cap for one more re-run (see ``rerun_tree``).

    ``tree`` is an already computed ``rerun_tree`` result, to avoid walking the tree twice.
    """
    if tree is None:
        tree = rerun_tree(connection, campaign)
    return 0 if tree["truncated"] else int(tree["remaining_cents"])  # unread members: fail closed


def build_retry_spec(
    campaign: dict[str, Any],
    outputs: dict[str, Any],
    stage_keys: list[str] | None = None,
    budget_cap_cents: int | None = None,
) -> dict[str, Any]:
    """The retry spec and a plain account of what it reuses and re-runs."""
    if campaign.get("status") not in FINISHED:
        raise RerunRefused("Only a failed, stopped or cancelled campaign can be retried.")
    spec = copy.deepcopy(campaign["spec"])
    if spec.get("version") != 2:
        raise RerunRefused("Only a v2 spec can pin earlier outputs; resubmit a v1 spec by hand.")
    status = {row["stage_key"]: row["status"] for row in campaign["stages"]}
    stages = {stage["key"]: stage for stage in spec["stages"]}
    if set(status) != set(stages):
        raise RerunRefused("The campaign's stage records do not match its spec.")
    if stage_keys is not None:
        unknown = sorted(set(stage_keys) - set(stages))
        if unknown:
            raise RerunRefused(f"Unknown stage keys: {unknown}")
        chosen = set(stage_keys)
    else:
        chosen = {key for key, state in status.items() if state != "succeeded"}
    if not chosen:
        raise RerunRefused("Every stage succeeded; there is nothing to retry.")
    rerun = set(chosen)
    changed = True
    while changed:  # a stage that consumed a re-run stage's output must run again too
        changed = False
        for key, stage in stages.items():
            if key not in rerun and any(dep in rerun for dep in _dependencies(stage)):
                rerun.add(key)
                changed = True
    digests: dict[tuple[str, str], set[str]] = {}
    unreleased: set[tuple[str, str]] = set()
    for row in outputs.get("outputs", []):
        digests.setdefault((row["stage_key"], row["name"]), set()).add(row["sha256"])
        if row.get("custody_released") is False:
            unreleased.add((row["stage_key"], row["name"]))
    reused: list[dict[str, str]] = []
    kept = []
    for stage in spec["stages"]:
        if stage["key"] not in rerun:
            continue
        inputs = []
        for source in stage["inputs"]:
            if source.get("kind") == "stage" and source["stage"] not in rerun:
                if status[source["stage"]] != "succeeded":
                    raise RerunRefused(
                        f"Stage {source['stage']!r} did not succeed and is not retried."
                    )
                if len(source["outputs"]) != 1:
                    raise RerunRefused(
                        f"Input {source['name']!r} reads several outputs of {source['stage']!r}; "
                        "pin them by hand so the mount layout stays yours to review."
                    )
                if (source["stage"], source["outputs"][0]) in unreleased:
                    raise RerunRefused(
                        f"Output {source['outputs'][0]!r} of {source['stage']!r} is not "
                        "custody-released and retained, so a later campaign cannot pin it; "
                        "a human restores custody first."
                    )
                found = digests.get((source["stage"], source["outputs"][0]), set())
                if len(found) != 1:
                    raise RerunRefused(
                        f"Output {source['outputs'][0]!r} of {source['stage']!r} has no single "
                        "recorded digest."
                    )
                sha256 = next(iter(found))
                inputs.append(
                    {
                        "kind": "artifact",
                        "name": source["name"],
                        "campaign_id": campaign["id"],
                        "stage": source["stage"],
                        "output": source["outputs"][0],
                        "sha256": sha256,
                    }
                )
                reused.append(
                    {"stage": source["stage"], "output": source["outputs"][0], "sha256": sha256}
                )
            else:
                inputs.append(source)
        stage["inputs"] = inputs
        kept.append(stage)
    original = int(spec["budget_cap_cents"])
    needed = sum(int(stage.get("max_exposure_cents", 0)) for stage in kept)
    # The ceiling is what is left of the chain's first cap; a source read without lineage (the
    # offline retry tool) has only its own cap and settled spend to go by.
    remaining = int(
        campaign.get("lineage_remaining_cents", original - int(campaign.get("settled_cents", 0)))
    )
    default = max(min(original - int(campaign.get("settled_cents", 0)), remaining), needed)
    cap = default if budget_cap_cents is None else budget_cap_cents
    if cap > original:
        raise RerunRefused("A retry may not ask for more than the original campaign's cap.")
    if cap > remaining:
        raise RerunRefused(
            f"A re-run may not ask for more than the {max(remaining, 0)} cents left of the "
            "first campaign's cap across its re-runs." + holding_hint(campaign)
        )
    if cap < needed:
        raise RerunRefused(f"The cap must cover the retried stages' exposure ({needed} cents).")
    spec["stages"] = kept
    if spec.get("seals"):
        # ADR 0164: a pinned output keeps the seals it carried (on itself or by lineage).
        source_spec = normalize(parse_spec_json(json.dumps(campaign["spec"])))
        carried = carried_seals(source_spec, rerun, reused)
        if carried:
            spec["seals"] = carried
        else:
            spec.pop("seals")
    spec["budget_cap_cents"] = cap
    spec["name"] = (spec["name"] + "-retry")[:MAX_NAME]
    return {
        "spec": spec,
        "source_campaign_id": campaign["id"],
        "retried_stages": [stage["key"] for stage in kept],
        "reused_outputs": reused,
        "note": (
            "A new spec for human approval; nothing was submitted. Validate it with "
            "tessera_validate_campaign_spec, then submit it with tessera_submit_campaign."
        ),
    }


def plan_rerun(source: dict[str, Any], request: RerunCampaign) -> dict[str, Any]:
    """The spec to submit and the lineage to record, or ``RerunRefused``."""
    if source["status"] not in FINISHED:
        raise RerunRefused("Only a failed, stopped or cancelled campaign can be re-run.")
    if request.mode == "identical":
        if request.stage_keys is not None or request.budget_cap_cents is not None:
            raise RerunRefused(
                "An identical re-run takes no stage keys or cap: it is the same spec."
            )
        spec = copy.deepcopy(source["spec"])
        left = source.get("lineage_remaining_cents")
        if left is not None and int(spec["budget_cap_cents"]) > int(left):
            raise RerunRefused(
                f"An identical re-run keeps the cap of {spec['budget_cap_cents']} cents, which is "
                f"more than the {max(int(left), 0)} cents left of the first campaign's cap across "
                "its re-runs; re-run the failed stages with a smaller cap instead."
                + holding_hint(source)
            )
        retried = [stage["key"] for stage in spec["stages"]]
        reused: list[dict[str, str]] = []
    else:
        plan = build_retry_spec(
            source,
            {"outputs": source["outputs"]},
            request.stage_keys,
            request.budget_cap_cents,
        )
        spec, retried, reused = plan["spec"], plan["retried_stages"], plan["reused_outputs"]
    lineage = {
        "rerun_of": source["id"],
        "mode": request.mode,
        "source_spec_digest": source["spec_digest"],
        "retried_stages": retried,
        "reused_outputs": reused,
        "budget_cap_cents": int(spec["budget_cap_cents"]),
    }
    return {"spec": spec, "lineage": lineage}


def lineage_of(
    connection: sqlite3.Connection, campaign_id: str, project_id: str
) -> dict[str, Any] | None:
    """The recorded re-run link of a campaign (it was re-run from another), if any.

    Read through the project's event range (the events index), not a scan of every event; a
    lineage column is the next step when the events of one project grow very large."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor LIMIT 1""",
        (project_id, RERUN_EVENT, campaign_id),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    # request_digest is the replay key; it stays in the stored submit response, not in a read.
    return {k: v for k, v in payload.items() if k not in {"campaign_id", "request_digest"}}


def reruns_of(
    connection: sqlite3.Connection, campaign_id: str, project_id: str
) -> list[dict[str, Any]]:
    """Campaigns re-run from this one, oldest first."""
    found = []
    for row in connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.rerun_of')=? ORDER BY cursor""",
        (project_id, RERUN_EVENT, campaign_id),
    ):
        payload = json.loads(row["payload_json"])
        found.append(
            {
                "campaign_id": payload["campaign_id"],
                "mode": payload["mode"],
                "retried_stages": payload["retried_stages"],
            }
        )
    return found
