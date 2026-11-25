"""``tessera deploy-request``: deploy a gated release once its blockers clear (ADR 0165)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import deploy_request

MIN_INTERVAL = 10
MAX_INTERVAL = 3600


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser(
        "deploy-request",
        help="record a deploy that runs deploy-upgrade once nothing blocks it",
        description=(
            "A deploy request names a release, the requester, the gate evidence and an expiry. "
            "'tick' evaluates the open request once and runs the checked upgrade (deploy-upgrade "
            "--execute with the request's plan) exactly once when no remote route is in flight "
            "and the upgrade preflight passes; run it from a timer, or use 'watch'. Every "
            "request, wait, fire, refusal and cancellation writes a receipt."
        ),
    )
    actions = command.add_subparsers(dest="deploy_request_action", required=True)

    create = actions.add_parser("create", help="record a deploy request")
    create.add_argument("--plan", type=Path, required=True, help="the deploy-upgrade plan file")
    create.add_argument("--release", required=True, help="the 40-hex revision to deploy")
    create.add_argument("--requester", required=True, help="who asks (a short label)")
    evidence = create.add_mutually_exclusive_group(required=True)
    evidence.add_argument(
        "--gate-evidence", type=Path, help="absolute gate evidence file (rechecked at fire time)"
    )
    evidence.add_argument("--gate-evidence-sha256", help="digest of the gate evidence")
    create.add_argument("--main-ref", default="main", help="git ref that must stay at the release")
    create.add_argument(
        "--ttl-hours",
        type=int,
        default=deploy_request.DEFAULT_TTL_HOURS,
        help=f"expiry, 1 to {deploy_request.MAX_TTL_HOURS} hours",
    )

    for name, text in (
        ("list", "list deploy requests, newest first"),
        ("show", "show one request and its receipts"),
        ("cancel", "cancel a pending request"),
        ("tick", "evaluate the open request once; fire it when nothing blocks"),
        ("watch", "tick on an interval until no request is open"),
    ):
        action = actions.add_parser(name, help=text)
        action.add_argument("--state-dir", type=Path, required=True, help="control state dir")
        if name in ("show", "cancel"):
            action.add_argument("request_id")
        if name == "cancel":
            action.add_argument("--by", required=True, help="who cancels (a short label)")
        if name == "watch":
            action.add_argument("--interval", type=int, default=60, help="seconds between ticks")
            action.add_argument(
                "--max-seconds", type=int, default=None, help="stop watching after this long"
            )


def _print(document: Any) -> None:
    print(json.dumps(document, sort_keys=True, indent=2))


def _exit_for(outcome: str) -> int:
    return {"fired": 0, "idle": 0, "waiting": 0, "busy": 0, "expired": 1}.get(outcome, 1)


def run(
    arguments: argparse.Namespace,
    *,
    sleep=time.sleep,
    clock=time.monotonic,
) -> int:
    action = arguments.deploy_request_action
    try:
        if action == "create":
            plan = arguments.plan
            if not plan.is_absolute():
                raise deploy_request.RequestRefused("--plan must be an absolute path.")
            from .deploy_upgrade_plan import PlanError, load_plan

            try:
                state_dir = Path(load_plan(plan).state_dir)
            except PlanError as error:
                raise deploy_request.RequestRefused(str(error)) from None
            record = deploy_request.create(
                state_dir,
                plan_path=plan,
                release=arguments.release,
                requester=arguments.requester,
                gate_evidence=arguments.gate_evidence,
                gate_evidence_sha256=arguments.gate_evidence_sha256,
                main_ref=arguments.main_ref,
                ttl_hours=arguments.ttl_hours,
            )
            _print(deploy_request.public_view(record))
            return 0
        state_dir = arguments.state_dir
        if not state_dir.is_absolute():
            raise deploy_request.RequestRefused("--state-dir must be an absolute path.")
        if action == "list":
            _print([deploy_request.public_view(r) for r in deploy_request.list_requests(state_dir)])
            return 0
        if action == "show":
            record = deploy_request.show(state_dir, arguments.request_id)
            view = deploy_request.public_view(record)
            view["receipt_log"] = deploy_request.receipts_for(state_dir, arguments.request_id)
            _print(view)
            return 0
        if action == "cancel":
            record = deploy_request.cancel(state_dir, arguments.request_id, by=arguments.by)
            _print(deploy_request.public_view(record))
            return 0
        if action == "tick":
            result = deploy_request.tick(state_dir)
            _print(result)
            return _exit_for(result["outcome"])
        if action == "watch":
            interval = arguments.interval
            if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
                raise deploy_request.RequestRefused(
                    f"--interval must be {MIN_INTERVAL} to {MAX_INTERVAL} seconds."
                )
            began = clock()
            while True:
                result = deploy_request.tick(state_dir)
                _print(result)
                if result["outcome"] not in ("waiting", "busy"):
                    return _exit_for(result["outcome"])
                if arguments.max_seconds is not None and clock() - began >= arguments.max_seconds:
                    return 0
                sleep(interval)
    except (deploy_request.RequestRefused, OSError) as error:
        message = (
            str(error)
            if isinstance(error, deploy_request.RequestRefused)
            else (f"failed ({type(error).__name__})")
        )
        print(f"deploy-request: {message}", file=sys.stderr)
        return 2
    return 2
