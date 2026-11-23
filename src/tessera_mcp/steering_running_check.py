"""End-to-end check: an agent steers a RUNNING campaign through ``tessera_mcp`` tools only.

Where ``steering_smoke`` cancels a campaign before it runs, this check lets a two-stage local-docker
campaign actually start: it submits it, has the person-side credential approve the digest (the
delegated decision no agent credential can make), waits until the first stage is live, asks for the
plain-words report while it runs, PAUSES while the stage runs, proves the running stage finishes but
no new stage starts while paused, resumes, lets the campaign finish, reads any gate decision, and
checks the terminal receipt records the pause and the approval and that nothing was settled.

Every agent act is an MCP tool call; the engine itself never touches the control database. How the
campaign makes progress is the caller's: live, ``advance`` just sleeps (the runner and the local
docker poller do the work); in a test it ticks a fake runner and worker. The check needs a stage
that lives long enough to be caught running: the live spec's first stage sleeps a few seconds.

Dry run by default, like the smoke: it prints the plan and the exact spec, makes no call and reads
no credential; ``--execute`` needs the exact confirmation phrase.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from tessera_mcp import steering_smoke as smoke
from tessera_mcp.steering_smoke import SmokeFailure, _need

CONFIRM_PHRASE = "mcp-steering-running-check-local-docker"
REPORT_SCHEMA = "tessera.mcp-steering-running-check.v1"
LIVE_ATTEMPT = frozenset({"reserved", "admitted", "running", "launching"})
DONE_ATTEMPT = frozenset({"succeeded", "failed_permanent", "failed_transient", "cancelled"})
STEPS = (
    ("preflight", "tessera_status, tessera_list_snapshots, tessera_validate_campaign_spec"),
    ("submit", "tessera_submit_campaign"),
    ("agent_cannot_approve", "tessera_approve_campaign (agent credential, must be refused)"),
    ("delegated_decision", "tessera_approve_campaign (person-side credential)"),
    ("running", "tessera_get_campaign (until stage one is live)"),
    ("report_while_running", "tessera_campaign_report"),
    ("pause_while_running", "tessera_pause_campaign"),
    ("held", "tessera_get_campaign (the running stage finishes, no new stage starts)"),
    ("resume_and_finish", "tessera_resume_campaign, tessera_get_campaign"),
    ("gate_decision", "tessera_campaign_gate_decisions"),
    ("terminal_receipt", "tessera_get_campaign_receipt, tessera_campaign_report, budget"),
)

Call = Callable[..., Any]
Advance = Callable[[str], None]  # phase: "running" | "held" | "finishing"


def running_spec(
    image: str, snapshot: dict[str, Any], *, stage_seconds: int = 8, metrics: bool = False
) -> dict[str, Any]:
    """Two $0 local-docker stages: ``one`` runs for a few seconds, ``two`` reads its output."""
    code = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": snapshot["id"],
        "digest": snapshot["digest"],
        "data_class": snapshot.get("data_class", "public"),
    }
    write = "open('result.json','w').write('{\"score\": 1}')"

    def stage(key: str, argv: list[str], inputs: list[dict[str, Any]]) -> dict[str, Any]:
        found: dict[str, Any] = {
            "key": key,
            "provider": smoke.PROVIDER,
            "runtime": smoke.RUNTIME,
            "image": image,
            "argv": argv,
            "outputs": [{"name": "result", "path": "result.json", "max_bytes": 1024}],
            "max_duration_seconds": 60,
            "max_exposure_cents": 0,
            "inputs": inputs,
        }
        if metrics:
            found["metrics"] = [{"name": "score", "path": "result.json", "json_pointer": "/score"}]
        return found

    return {
        "version": 2,
        "name": "mcp-steering-running-check",
        "budget_cap_cents": 0,
        "deadline_seconds": 3600,
        "stages": [
            stage(
                "one",
                ["python", "-c", f"import time; time.sleep({stage_seconds}); {write}"],
                [code],
            ),
            stage(
                "two",
                ["python", "-c", write],
                [code, {"kind": "stage", "name": "up", "stage": "one", "outputs": ["result"]}],
            ),
        ],
    }


def _attempts(view: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    return [a for a in view.get("attempts", []) if a.get("stage_key") == stage]


def _wait(
    agent: Call,
    campaign_id: str,
    done: Callable[[dict[str, Any]], bool],
    phase: str,
    advance: Advance,
    clock: Callable[[], float],
    deadline: float,
    failure: str,
) -> dict[str, Any]:
    view = agent("tessera_get_campaign", campaign_id=campaign_id)
    while not done(view):
        _need(clock() < deadline, failure)
        advance(phase)
        view = agent("tessera_get_campaign", campaign_id=campaign_id)
    return view


def run_running_check(
    agent: Call,
    person: Call,
    agent_approve: Call,
    *,
    image: str,
    snapshot_id: str | None = None,
    grant_id: str | None = None,
    spec: dict[str, Any] | None = None,
    gate: Callable[[str], str | None] | None = None,
    advance: Advance,
    wait_seconds: float = 180.0,
    held_polls: int = 6,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the steps. ``gate(campaign_id)`` performs the human acts of a gate (owner-signed
    standing approval and go) and may return the id of the campaign it started; without it the
    gate step only reads and records that this campaign has none."""
    report = smoke.Report(grant_id=grant_id)
    deadline = clock() + wait_seconds

    status = agent("tessera_status")
    snapshot = smoke.pick_snapshot(agent("tessera_list_snapshots"), snapshot_id)
    spec = spec or running_spec(image, snapshot)
    _need(bool(agent("tessera_validate_campaign_spec", spec=spec)), "the spec was not accepted")
    report.record(
        "preflight",
        "tessera_status",
        enrolled_actions=sorted(status.get("actions", status.get("scopes", []))),
        snapshot_id=snapshot["id"],
        stages=[s["key"] for s in spec["stages"]],
    )

    submitted = agent("tessera_submit_campaign", spec=spec, idempotency_key=smoke._key("submit"))
    campaign_id = submitted["id"]
    _need(submitted.get("status") == "submitted", "a new campaign must start as submitted")
    report.record("submit", "tessera_submit_campaign", campaign_id=campaign_id)

    view = agent("tessera_get_campaign", campaign_id=campaign_id)
    refused = False
    try:
        agent_approve(
            "tessera_approve_campaign",
            campaign_id=campaign_id,
            expected_spec_digest=view["spec_digest"],
        )
    except Exception as error:  # noqa: BLE001 - any refusal is the expected outcome
        refused, refusal = True, type(error).__name__
    _need(refused, "an agent credential was allowed to approve a campaign")
    report.record("agent_cannot_approve", "tessera_approve_campaign", refused_with=refusal)

    person(
        "tessera_approve_campaign",
        campaign_id=campaign_id,
        expected_spec_digest=view["spec_digest"],
    )
    approved = agent("tessera_get_campaign", campaign_id=campaign_id)
    _need(approved.get("approval") is not None, "the person's approval left no record")
    report.record("delegated_decision", "tessera_approve_campaign", approval_recorded=True)

    first, second = spec["stages"][0]["key"], spec["stages"][1]["key"]
    view = _wait(
        agent,
        campaign_id,
        lambda v: (
            any(a["status"] in LIVE_ATTEMPT for a in _attempts(v, first))
            or v["status"] in smoke.TERMINAL
        ),
        "running",
        advance,
        clock,
        deadline,
        "the first stage never became live (is the campaign runner and the poller running?)",
    )
    live = [a for a in _attempts(view, first) if a["status"] in LIVE_ATTEMPT]
    _need(
        bool(live),
        "the first stage finished before it could be paused while running: use a longer "
        "first stage (the live spec sleeps a few seconds; raise it for a slow control loop)",
    )
    report.record("running", "tessera_get_campaign", stage=first, attempt_status=live[0]["status"])

    plain = agent("tessera_campaign_report", campaign_id=campaign_id)
    text = plain.get("text") if isinstance(plain, dict) else None
    _need(isinstance(text, str) and bool(text.strip()), "the plain-words report was empty")
    report.record(
        "report_while_running",
        "tessera_campaign_report",
        text_length=len(text),
        mentions_stage=first in text,
    )

    before = view["revision"]
    agent(
        "tessera_pause_campaign",
        campaign_id=campaign_id,
        expected_revision=before,
        reason="running check: pause while a stage runs",
        idempotency_key=smoke._key("pause"),
    )
    paused = agent("tessera_get_campaign", campaign_id=campaign_id)
    _need(paused["revision"] > before, "the pause did not advance the campaign revision")
    report.record(
        "pause_while_running", "tessera_pause_campaign", revision_after=paused["revision"]
    )

    # The running stage is not interrupted; no new stage may start while paused.
    view = paused
    quiet = 0
    while True:
        first_done = all(a["status"] in DONE_ATTEMPT for a in _attempts(view, first)) and bool(
            _attempts(view, first)
        )
        _need(
            not _attempts(view, second),
            "a new stage started while the campaign was paused",
        )
        if first_done:
            quiet += 1
            if quiet >= held_polls:
                break
        _need(clock() < deadline, "the running stage did not finish while paused")
        advance("held")
        view = agent("tessera_get_campaign", campaign_id=campaign_id)
    _need(
        all(a["status"] == "succeeded" for a in _attempts(view, first)),
        "the running stage did not finish while the campaign was paused",
    )
    report.record(
        "held",
        "tessera_get_campaign",
        first_stage_attempts=[a["status"] for a in _attempts(view, first)],
        second_stage_attempts=0,
        polls_after_first_finished=quiet,
    )

    before = view["revision"]
    agent(
        "tessera_resume_campaign",
        campaign_id=campaign_id,
        expected_revision=before,
        reason="running check: resume",
        idempotency_key=smoke._key("resume"),
    )
    final = _wait(
        agent,
        campaign_id,
        lambda v: v["status"] in smoke.TERMINAL,
        "finishing",
        advance,
        clock,
        deadline,
        "the campaign did not finish after the resume",
    )
    _need(final["status"] == "succeeded", f"the campaign ended {final['status']}, not succeeded")
    report.record(
        "resume_and_finish",
        "tessera_resume_campaign",
        status=final["status"],
        second_stage_attempts=[a["status"] for a in _attempts(final, second)],
    )

    started = gate(campaign_id) if gate is not None else None
    decisions = agent("tessera_campaign_gate_decisions", campaign_id=campaign_id)
    listed = decisions.get("gate_decisions", [])
    evidence: dict[str, Any] = {"decisions": len(listed), "gate_configured": gate is not None}
    if gate is not None:
        _need(bool(listed), "the gate's go left no decision on the record")
        _need(started is not None, "the gate did not start the follow-on campaign")
        follow = agent("tessera_get_campaign", campaign_id=started)
        evidence.update(
            decision_class=listed[0].get("decision_class"),
            follow_on_status=follow["status"],
            follow_on_started=True,
        )
    report.record("gate_decision", "tessera_campaign_gate_decisions", **evidence)

    receipt = agent("tessera_get_campaign_receipt", campaign_id=campaign_id)["receipt"]
    steering = receipt.get("steering") or {}
    _need(steering.get("pause_count", 0) >= 1, "the receipt does not record the pause")
    _need(bool(receipt.get("approval") or final.get("approval")), "no approval on the record")
    budget = agent("tessera_campaign_budget", campaign_id=campaign_id)
    _need(budget.get("settled_cents") == 0, "the check settled money: it must cost nothing")
    after = agent("tessera_campaign_report", campaign_id=campaign_id)
    _need(bool((after.get("text") or "").strip()), "the final report was empty")
    report.record(
        "terminal_receipt",
        "tessera_get_campaign_receipt",
        status=final["status"],
        pause_count=steering.get("pause_count"),
        settled_cents=budget["settled_cents"],
        report_text_length=len(after["text"]),
    )
    document = report.document()
    document["schema"] = REPORT_SCHEMA
    return document


def plan(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "provider": smoke.PROVIDER,
        "steps": [{"step": step, "tools": tools} for step, tools in STEPS],
        "spec": running_spec(
            arguments.image or "REGISTRY/IMAGE@sha256:<digest>",
            {"id": arguments.snapshot_id or "<a public snapshot>", "digest": "<digest>"},
            stage_seconds=arguments.stage_seconds,
        ),
        "effects": "none in a dry run; a real run submits one two-stage local-docker campaign, "
        "lets it run, pauses it while a stage runs, and settles nothing",
        "run": f"re-run with --execute --confirm {CONFIRM_PHRASE} and the connection options",
    }


def parser() -> argparse.ArgumentParser:
    p = smoke.parser()
    p.description = __doc__.splitlines()[0]
    p.add_argument("--stage-seconds", type=int, default=8, help="how long stage one runs")
    p.add_argument("--held-polls", type=int, default=6, help="polls to watch a paused campaign")
    return p


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    grant = None
    if arguments.smoke_credentials_dir:
        try:
            grant = smoke.load_grant(arguments.smoke_credentials_dir)
        except smoke.GrantRefused as refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2
        arguments.project_id = arguments.project_id or grant.project_id
        arguments.destination = arguments.destination or grant.destination
        arguments.agent_token_file = arguments.agent_token_file or str(grant.agent_token_file)
        arguments.person_token_file = arguments.person_token_file or str(grant.approver_token_file)
        arguments.snapshot_id = arguments.snapshot_id or getattr(grant, "snapshot_id", None)
    if not arguments.execute:
        print(json.dumps(plan(arguments), indent=2, sort_keys=True))
        return 0
    if arguments.confirm != CONFIRM_PHRASE:
        print(f"refused: --execute needs --confirm {CONFIRM_PHRASE}", file=sys.stderr)
        return 2
    missing = [
        flag
        for flag in (
            "control_url", "project_id", "destination", "agent_token_file",
            "person_token_file", "image",
        )
        if not getattr(arguments, flag)
    ]  # fmt: skip
    if missing:
        print(
            "refused: missing " + ", ".join("--" + m.replace("_", "-") for m in missing),
            file=sys.stderr,
        )
        return 2
    if "@sha256:" not in arguments.image:
        print("refused: --image must be digest-pinned (name@sha256:...)", file=sys.stderr)
        return 2
    agent_file = smoke._private_file(arguments.agent_token_file, "--agent-token-file")
    person_file = smoke._private_file(arguments.person_token_file, "--person-token-file")
    agent = smoke._build_caller(arguments, agent_file, campaign_tools=True, campaign_actions=None)
    agent_approve = smoke._build_caller(arguments, agent_file, approval_tool=True)
    person = smoke._build_caller(arguments, person_file, approval_tool=True)
    spec_holder: dict[str, Any] = {}

    def sleeper(_phase: str) -> None:
        time.sleep(2.0)

    try:
        snapshot = smoke.pick_snapshot(agent("tessera_list_snapshots"), arguments.snapshot_id)
        spec_holder["spec"] = running_spec(
            arguments.image, snapshot, stage_seconds=arguments.stage_seconds
        )
        document = run_running_check(
            agent,
            person,
            agent_approve,
            image=arguments.image,
            snapshot_id=arguments.snapshot_id,
            grant_id=grant.grant_id if grant else None,
            spec=spec_holder["spec"],
            advance=sleeper,
            wait_seconds=arguments.wait_seconds,
            held_polls=arguments.held_polls,
        )
    except SmokeFailure as failure:
        print(json.dumps({"ok": False, "failed": str(failure)}, indent=2), file=sys.stderr)
        return 1
    text = json.dumps(document, indent=2, sort_keys=True)
    if arguments.receipt_file:
        descriptor = os.open(arguments.receipt_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
