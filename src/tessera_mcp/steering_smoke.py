"""A $0 end-to-end smoke of steering a campaign through the ``tessera_mcp`` tools.

It drives one tiny local-docker campaign through submit, inspect, a person's approval, pause,
resume and cancel using only MCP tool calls, then reads the terminal receipt and checks that every
step left a record. Nothing here approves, raises a cap or reaches a provider other than the
local Docker provider: the campaign is paused right after approval and cancelled straight after
the resume, so at most a local zero-cost container is ever reserved, and the final check is that
nothing was settled.

Dry run by default: it prints the plan and the exact campaign spec and makes no network call and
reads no credential. ``--execute`` with the exact ``--confirm`` phrase runs it against a running
control plane. Two credentials are used on purpose, as in production: an enrolled agent credential
(submit, read, pause, resume, cancel) and a person's session credential used only for the
approval, which no agent credential can give.

``--smoke-credentials-dir DIR`` reads the owner-issued ``smoke-grant.json`` (and uses
``DIR/agent.token`` and ``DIR/approver.token``) so no credential has to be hand-issued; the grant is
local-docker only, $0 and short-lived, all enforced by the control plane. The grant id is recorded
as ``smoke_grant_id`` in the report. No token is ever printed or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONFIRM_PHRASE = "mcp-steering-smoke-local-docker"
PROVIDER = "local-docker"
RUNTIME = "docker-cpu-v1"
GRANT_SCHEMA = "tessera.smoke-credentials.v1"
GRANT_FILE = "smoke-grant.json"
TERMINAL = frozenset({"succeeded", "stopped", "failed", "cancelled"})
STEPS = (
    ("preflight", "tessera_status, tessera_list_snapshots, tessera_validate_campaign_spec"),
    ("submit", "tessera_submit_campaign"),
    ("inspect", "tessera_get_campaign"),
    ("agent_cannot_approve", "tessera_approve_campaign (agent credential, must be refused)"),
    ("delegated_decision", "tessera_approve_campaign (person's credential)"),
    ("pause", "tessera_pause_campaign"),
    ("resume", "tessera_resume_campaign"),
    ("cancel", "tessera_cancel_campaign"),
    ("terminal_receipt", "tessera_get_campaign_receipt, tessera_campaign_budget"),
)

Call = Callable[..., Any]


class SmokeFailure(AssertionError):
    """A step did not leave the record the smoke expects."""


class GrantRefused(ValueError):
    """The smoke-credentials directory or its grant file cannot be used."""


@dataclass(frozen=True)
class Grant:
    grant_id: str
    project_id: str
    destination: str
    expires_at: str
    agent_token_file: Path
    approver_token_file: Path
    snapshot_id: str | None = None
    snapshot_digest: str | None = None


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GrantRefused("the grant's expires_at is not a timestamp")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GrantRefused("the grant's expires_at is not a timestamp") from error
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def load_grant(directory: str, now: datetime | None = None) -> Grant:
    """Read and validate ``DIR/smoke-grant.json``; never reads a token."""
    root = Path(directory)
    if not root.is_absolute():
        raise GrantRefused("--smoke-credentials-dir must be an absolute path")
    try:
        if not stat.S_ISDIR(root.stat().st_mode):
            raise GrantRefused("--smoke-credentials-dir is not a directory")
    except OSError as error:
        raise GrantRefused("--smoke-credentials-dir cannot be read") from error
    if root.stat().st_mode & 0o077:
        raise GrantRefused("--smoke-credentials-dir must not be group or world accessible")
    files = {name: root / name for name in (GRANT_FILE, "agent.token", "approver.token")}
    for name, path in files.items():
        try:
            mode = path.stat().st_mode
        except OSError as error:
            raise GrantRefused(f"{name} is missing from the smoke-credentials directory") from error
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise GrantRefused(f"{name} must be a regular owner-only (0600) file")
    try:
        data = json.loads(files[GRANT_FILE].read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GrantRefused("smoke-grant.json is not valid JSON") from error
    if not isinstance(data, dict) or data.get("schema") != GRANT_SCHEMA:
        raise GrantRefused(f"smoke-grant.json is not a {GRANT_SCHEMA} document")
    if data.get("providers") != [PROVIDER]:
        raise GrantRefused(f"the grant must allow exactly the {PROVIDER} provider")
    budget = data.get("max_budget_cents")
    if isinstance(budget, bool) or budget != 0:
        raise GrantRefused("the grant's max_budget_cents must be 0")
    text = {}
    for key in ("grant_id", "project_id", "destination"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise GrantRefused(f"the grant has no {key}")
        text[key] = value
    expires = _parse_time(data.get("expires_at"))
    if expires <= (now or datetime.now(UTC)):
        raise GrantRefused("the smoke grant has expired: issue a new one")
    snapshot = {}
    for key in ("snapshot_id", "snapshot_digest"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise GrantRefused(f"the grant's {key} is not a string")
        snapshot[key] = value
    if (snapshot["snapshot_id"] is None) != (snapshot["snapshot_digest"] is None):
        raise GrantRefused("the grant needs both snapshot_id and snapshot_digest, or neither")
    return Grant(
        text["grant_id"],
        text["project_id"],
        text["destination"],
        data["expires_at"],
        files["agent.token"],
        files["approver.token"],
        snapshot["snapshot_id"],
        snapshot["snapshot_digest"],
    )


@dataclass
class Report:
    steps: list[dict[str, Any]] = field(default_factory=list)
    grant_id: str | None = None

    def record(self, step: str, tool: str, **evidence: Any) -> None:
        self.steps.append({"step": step, "tool": tool, "ok": True, "evidence": evidence})

    def document(self) -> dict[str, Any]:
        return {
            "schema": "tessera.mcp-steering-smoke.v1",
            "ok": True,
            "smoke_grant_id": self.grant_id,
            "steps": self.steps,
        }


def smoke_spec(image: str, snapshot: dict[str, Any], name: str = "mcp-steering-smoke") -> dict:
    """One tiny single-stage local-docker campaign; the stage prints a line and writes one file."""
    return {
        "version": 2,
        "name": name,
        "budget_cap_cents": 0,
        "deadline_seconds": 3600,
        "stages": [
            {
                "key": "smoke",
                "provider": PROVIDER,
                "runtime": RUNTIME,
                "image": image,
                "argv": ["python", "-c", "open('result.json','w').write('{\"ok\": true}')"],
                "outputs": [{"name": "result", "path": "result.json", "max_bytes": 1024}],
                "max_duration_seconds": 60,
                "max_exposure_cents": 0,
                "inputs": [
                    {
                        "kind": "snapshot",
                        "name": "code",
                        "snapshot_id": snapshot["id"],
                        "digest": snapshot["digest"],
                        "data_class": snapshot.get("data_class", "public"),
                    }
                ],
            }
        ],
    }


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _key(label: str) -> str:
    return f"smoke-{label}-{secrets.token_hex(6)}"


def _class_of(snapshot: dict[str, Any]) -> str | None:
    """The catalog row's class: a plain string, or a record with a ``data_class`` field."""
    value = snapshot.get("data_class")
    if isinstance(value, dict):
        value = value.get("data_class")
    return value if isinstance(value, str) else None


def pick_snapshot(listing: dict[str, Any], wanted: str | None) -> dict[str, Any]:
    items = listing.get("snapshots", [])
    if wanted:
        found = [s for s in items if s.get("id") == wanted]
        _need(bool(found), "the named snapshot is not in the project's catalog")
        chosen = found[0]
    else:
        public = [s for s in items if _class_of(s) == "public"]
        _need(
            bool(public),
            "no public snapshot in the project's catalog: pass --snapshot-id of a small one",
        )
        chosen = public[0]
    _need(
        _class_of(chosen) in {"public", "private_derived"},
        "the snapshot's recorded data class is not usable by a campaign stage",
    )
    return {"id": chosen["id"], "digest": chosen["digest"], "data_class": _class_of(chosen)}


def run_smoke(
    agent: Call,
    person: Call,
    agent_approve: Call,
    *,
    image: str,
    snapshot_id: str | None = None,
    snapshot_digest: str | None = None,
    grant_id: str | None = None,
    wait_seconds: float = 120.0,
    poll_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the steps. ``agent`` and ``agent_approve`` use the agent credential, ``person`` the
    person's; each is ``call(tool_name, **arguments) -> dict`` and raises on a refusal."""
    report = Report(grant_id=grant_id)

    status = agent("tessera_status")
    snapshot = pick_snapshot(agent("tessera_list_snapshots"), snapshot_id)
    if snapshot_digest is not None:
        # The grant's own snapshot: it must be listed as public with the digest the grant names.
        _need(
            snapshot["data_class"] == "public" and snapshot["digest"] == snapshot_digest,
            "the grant's snapshot is not listed as public with the digest the grant names",
        )
    spec = smoke_spec(image, snapshot)
    checked = agent("tessera_validate_campaign_spec", spec=spec)
    _need(bool(checked), "the control plane did not accept the spec")
    report.record(
        "preflight",
        "tessera_status",
        enrolled_actions=sorted(status.get("actions", status.get("scopes", []))),
        snapshot_id=snapshot["id"],
        provider=PROVIDER,
    )

    submitted = agent("tessera_submit_campaign", spec=spec, idempotency_key=_key("submit"))
    campaign_id = submitted["id"]
    _need(submitted.get("status") == "submitted", "a new campaign must start as submitted")
    report.record(
        "submit",
        "tessera_submit_campaign",
        campaign_id=campaign_id,
        spec_digest=submitted.get("spec_digest"),
        revision=submitted.get("revision"),
    )

    view = agent("tessera_get_campaign", campaign_id=campaign_id)
    _need(view.get("approval") is None, "a submitted campaign must have no approval yet")
    _need(view.get("spec_digest") == submitted.get("spec_digest"), "the stored digest changed")
    report.record("inspect", "tessera_get_campaign", status=view["status"])

    refused = False
    try:
        agent_approve(
            "tessera_approve_campaign",
            campaign_id=campaign_id,
            expected_spec_digest=view["spec_digest"],
        )
    except Exception as error:  # noqa: BLE001 - any refusal is the expected outcome
        refused = True
        refusal = type(error).__name__
    _need(refused, "an agent credential was allowed to approve a campaign")
    _need(
        agent("tessera_get_campaign", campaign_id=campaign_id).get("approval") is None,
        "the refused approval changed the campaign",
    )
    report.record("agent_cannot_approve", "tessera_approve_campaign", refused_with=refusal)

    approval = person(
        "tessera_approve_campaign",
        campaign_id=campaign_id,
        expected_spec_digest=view["spec_digest"],
    )
    approved = agent("tessera_get_campaign", campaign_id=campaign_id)
    _need(approved.get("approval") is not None, "the person's approval left no approval record")
    report.record(
        "delegated_decision",
        "tessera_approve_campaign",
        approval_recorded=True,
        approval_keys=sorted(approval) if isinstance(approval, dict) else [],
        status=approved["status"],
    )

    def revision() -> int:
        return agent("tessera_get_campaign", campaign_id=campaign_id)["revision"]

    before = revision()
    paused = agent(
        "tessera_pause_campaign",
        campaign_id=campaign_id,
        expected_revision=before,
        reason="smoke: pause before anything is reserved",
        idempotency_key=_key("pause"),
    )
    _need(revision() > before, "a pause did not advance the campaign revision")
    report.record(
        "pause", "tessera_pause_campaign", revision_after=revision(), result=_brief(paused)
    )

    before = revision()
    resumed = agent(
        "tessera_resume_campaign",
        campaign_id=campaign_id,
        expected_revision=before,
        reason="smoke: resume",
        idempotency_key=_key("resume"),
    )
    _need(revision() > before, "a resume did not advance the campaign revision")
    report.record(
        "resume", "tessera_resume_campaign", revision_after=revision(), result=_brief(resumed)
    )

    before = revision()
    cancelled = agent(
        "tessera_cancel_campaign",
        campaign_id=campaign_id,
        expected_revision=before,
        reason="smoke: cancel",
        idempotency_key=_key("cancel"),
    )
    report.record("cancel", "tessera_cancel_campaign", result=_brief(cancelled))

    deadline = clock() + wait_seconds
    final = agent("tessera_get_campaign", campaign_id=campaign_id)
    while final["status"] not in TERMINAL and clock() < deadline:
        sleep(poll_seconds)
        final = agent("tessera_get_campaign", campaign_id=campaign_id)
    _need(
        final["status"] in TERMINAL,
        f"the campaign did not reach a terminal state in {wait_seconds:.0f}s "
        f"(status {final['status']}; the runner may not be running)",
    )

    receipt = agent("tessera_get_campaign_receipt", campaign_id=campaign_id)["receipt"]
    steering = receipt.get("steering") or {}
    _need(steering.get("pause_count", 0) >= 1, "the receipt does not record the pause")
    _need(bool(receipt.get("approval") or final.get("approval")), "no approval on the record")
    budget = agent("tessera_campaign_budget", campaign_id=campaign_id)
    _need(budget.get("settled_cents") == 0, "the smoke settled money: it must cost nothing")
    report.record(
        "terminal_receipt",
        "tessera_get_campaign_receipt",
        status=final["status"],
        pause_count=steering.get("pause_count"),
        settled_cents=budget["settled_cents"],
        receipt_keys=sorted(receipt),
    )
    return report.document()


def _brief(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {k: value[k] for k in ("status", "revision", "id") if k in value}


def plan(arguments: argparse.Namespace, grant: Grant | None = None) -> dict[str, Any]:
    document = _plan(arguments)
    if grant is not None:
        document["smoke_grant"] = {"grant_id": grant.grant_id, "expires_at": grant.expires_at}
    return document


def _plan(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "provider": PROVIDER,
        "steps": [{"step": step, "tools": tools} for step, tools in STEPS],
        "spec": smoke_spec(
            arguments.image or "REGISTRY/IMAGE@sha256:<digest>",
            {
                "id": arguments.snapshot_id or "<a public snapshot in the catalog>",
                "digest": "<digest>",
            },
        ),
        "effects": "none in a dry run; a real run submits one local-docker campaign, cancels it, "
        "and settles nothing",
        "run": f"re-run with --execute --confirm {CONFIRM_PHRASE} and the connection options",
    }


def _build_caller(arguments: argparse.Namespace, token_file: Path, **flags: Any) -> Call:
    from tessera_mcp.client import ControlClient
    from tessera_mcp.config import Config
    from tessera_mcp.server import create_server

    config = Config(
        arguments.control_url,
        token_file,
        arguments.project_id,
        arguments.destination,
        Path(arguments.ca_file) if arguments.ca_file else None,
    )
    server = create_server(ControlClient(config), **flags)

    def call(name: str, **kwargs: Any) -> Any:
        result = asyncio.run(server.call_tool(name, kwargs))
        return result[1] if isinstance(result, tuple) else result

    return call


def _private_file(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise SystemExit(f"{label} must be an absolute path")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"{label} must be owner-only (0600)")
    return path


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--execute", action="store_true", help="run against a live control plane")
    p.add_argument("--confirm", help=f"required with --execute: {CONFIRM_PHRASE}")
    p.add_argument("--control-url", help="control plane origin (https, or loopback http)")
    p.add_argument(
        "--smoke-credentials-dir",
        help="absolute directory from `tessera dev smoke-credentials issue`: fills in the "
        "project, destination and token files unless given explicitly",
    )
    p.add_argument("--project-id")
    p.add_argument("--destination", help="the agent credential's enrolled destination")
    p.add_argument("--ca-file", help="CA bundle for the control plane's certificate")
    p.add_argument("--agent-token-file", help="owner-only file with the agent credential")
    p.add_argument("--person-token-file", help="owner-only file with a person's session credential")
    p.add_argument("--image", help="digest-pinned image the local docker provider allows")
    p.add_argument("--snapshot-id", help="a small public snapshot (default: first public one)")
    p.add_argument("--wait-seconds", type=float, default=120.0)
    p.add_argument("--receipt-file", help="write the step report here (0600)")
    return p


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    grant: Grant | None = None
    if arguments.smoke_credentials_dir:
        try:
            grant = load_grant(arguments.smoke_credentials_dir)
        except GrantRefused as refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2
        arguments.project_id = arguments.project_id or grant.project_id
        arguments.destination = arguments.destination or grant.destination
        arguments.agent_token_file = arguments.agent_token_file or str(grant.agent_token_file)
        arguments.person_token_file = arguments.person_token_file or str(grant.approver_token_file)
    if not arguments.execute:
        print(json.dumps(plan(arguments, grant), indent=2, sort_keys=True))
        return 0
    if arguments.confirm != CONFIRM_PHRASE:
        print(f"refused: --execute needs --confirm {CONFIRM_PHRASE}", file=sys.stderr)
        return 2
    missing = [
        flag
        for flag in (
            "control_url",
            "project_id",
            "destination",
            "agent_token_file",
            "person_token_file",
            "image",
        )
        if not getattr(arguments, flag)
    ]
    if missing:
        print(
            "refused: missing " + ", ".join("--" + m.replace("_", "-") for m in missing),
            file=sys.stderr,
        )
        return 2
    if "@sha256:" not in arguments.image:
        print("refused: --image must be digest-pinned (name@sha256:...)", file=sys.stderr)
        return 2
    agent_file = _private_file(arguments.agent_token_file, "--agent-token-file")
    person_file = _private_file(arguments.person_token_file, "--person-token-file")
    agent = _build_caller(arguments, agent_file, campaign_tools=True, campaign_actions=None)
    agent_approve = _build_caller(arguments, agent_file, approval_tool=True)
    person = _build_caller(arguments, person_file, approval_tool=True)
    # An explicit --snapshot-id wins; otherwise the grant's own synthetic public snapshot.
    from_grant = grant is not None and grant.snapshot_id and not arguments.snapshot_id
    try:
        document = run_smoke(
            agent,
            person,
            agent_approve,
            image=arguments.image,
            snapshot_id=grant.snapshot_id if from_grant else arguments.snapshot_id,
            snapshot_digest=grant.snapshot_digest if from_grant else None,
            grant_id=grant.grant_id if grant else None,
            wait_seconds=arguments.wait_seconds,
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
