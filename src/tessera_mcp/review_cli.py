"""``tessera-review``: submit a code review of a platform git branch (ADR 0108).

Uses the same runtime references as the MCP server (TESSERA_CONTROL_URL,
TESSERA_AGENT_TOKEN_FILE, TESSERA_PROJECT_ID, TESSERA_AGENT_DESTINATION). Credential
values are never printed. Exit 0 on submit, 2 on invalid arguments or configuration, 1 when
the control API refuses or is unavailable.
"""

import argparse
import asyncio
import json
import sys

from tessera_mcp.client import ControlClient, ControlError, segment
from tessera_mcp.config import Config
from tessera_mcp.review_tools import build_code_review_body, default_code_review_key


def _parser():
    parser = argparse.ArgumentParser(
        prog="tessera-review", description="Request an advisory code review job."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser(
        "submit",
        help="submit a branch at an exact head commit for review (max 3 rounds)",
        description=(
            "Name focused test files only (repeat --test). A re-review checks only the "
            "listed findings (--previous JOB and repeated --recheck N). Advisory only."
        ),
    )
    submit.add_argument("--repo", required=True)
    submit.add_argument("--branch", required=True)
    submit.add_argument("--head", required=True, help="40-hex head commit")
    submit.add_argument("--base-branch", default="main")
    submit.add_argument("--tier", required=True, choices=["haiku", "sonnet", "opus", "fable"])
    submit.add_argument("--budget-cents", required=True, type=int)
    submit.add_argument("--destination-id", default="anthropic")
    submit.add_argument("--test", action="append", default=[], metavar="PATH")
    submit.add_argument("--previous", metavar="JOB")
    submit.add_argument("--recheck", action="append", type=int, default=None, metavar="N")
    submit.add_argument("--idempotency-key")
    return parser


def main(argv=None, client_factory=None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        body = build_code_review_body(
            repo=arguments.repo,
            branch=arguments.branch,
            head_sha=arguments.head,
            tier=arguments.tier,
            budget_cents=arguments.budget_cents,
            focused_tests=arguments.test,
            base_branch=arguments.base_branch,
            destination_id=arguments.destination_id,
            previous_job_id=arguments.previous,
            recheck_findings=arguments.recheck,
        )
        if client_factory is None:
            config = Config.from_env()
            config.token()
            client = ControlClient(config)
        else:
            client = client_factory()
        path = f"/projects/{segment(client.config.project_id)}/code-reviews"
    except (ValueError, ControlError) as exc:
        message = str(exc)
        if "credential" in message or "configuration" in message:
            message = "Tessera configuration/credential reference invalid"
        print(message, file=sys.stderr)
        return 2
    key = arguments.idempotency_key or default_code_review_key(body)
    try:
        job = asyncio.run(client._request("POST", path, body=body, key=key))
    except ControlError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    code_review = job.get("code_review") if isinstance(job, dict) else None
    source = code_review.get("source") if isinstance(code_review, dict) else None
    round_ = source.get("round") if isinstance(source, dict) else job.get("round")
    print(
        json.dumps(
            {
                "job_id": job.get("id") or job.get("job_id"),
                "round": round_,
                "state": job.get("state"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
