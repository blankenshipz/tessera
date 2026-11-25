"""``tessera reasoning install --plan|--apply``: the reasoning service's install checklist as one
checked, idempotent command (ADR 0119). ``tessera reasoning admit`` / ``revoke-admission``: a
person's recorded admission of the service and its marker (ADR 0163)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    command = subcommands.add_parser(
        "reasoning", help="the standalone reasoning service: install it as a checked command"
    )
    actions = command.add_subparsers(dest="reasoning_action", required=True)
    install = actions.add_parser(
        "install",
        help="plan (default) or apply the service user, directories, profile, unit and readiness",
        description=(
            "Idempotent. --plan (the default) prints what is missing and changes nothing; "
            "--apply creates exactly those pieces with their owners and modes, then runs the "
            "first-call readiness check. It never reads, asks for or writes an API key or an "
            "agent token, never enables or starts the service and never admits the host."
        ),
    )
    mode = install.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the plan only (the default)")
    mode.add_argument("--apply", action="store_true", help="make the changes (needs root)")
    install.add_argument("--root", type=Path, help="prefix every path (for staging and tests)")
    install.add_argument("--user", default="tessera-reasoning", help="service account name")
    install.add_argument("--config-dir", type=Path, default=Path("/etc/tessera"))
    install.add_argument("--credentials-directory", type=Path, default=None)
    install.add_argument(
        "--profile", type=Path, default=None, help="default CONFIG_DIR/reasoning.json"
    )
    install.add_argument("--state-dir", type=Path, default=Path("/var/lib/tessera-reasoning"))
    install.add_argument(
        "--unit-path", type=Path, default=Path("/etc/systemd/system/tessera-reasoning.service")
    )
    install.add_argument("--python", default="/opt/tessera/venv/bin/python")
    install.add_argument(
        "--profile-template",
        type=Path,
        help="operator-written profile JSON with every field except the caps (needed to create it)",
    )
    install.add_argument(
        "--prompt-source",
        "--prompt-file",
        dest="prompt_file",
        type=Path,
        help="the prompt text to install (at most 8192 bytes)",
    )
    install.add_argument(
        "--max-cost-cents-per-call", type=int, help="per-call spend cap written into the profile"
    )
    install.add_argument(
        "--daily-cost-cap-cents", type=int, help="daily spend cap written into the profile"
    )
    install.add_argument("--daily-token-cap", type=int)
    install.add_argument(
        "--replace", action="store_true", help="replace a managed file that differs"
    )
    install.add_argument(
        "--new-run",
        action="store_true",
        help="write a fresh generated run_id (and the installed prompt's digest) into the profile",
    )
    install.add_argument("--control-state-dir", type=Path)
    install.add_argument("--control-url")
    install.add_argument("--project-id")
    install.add_argument("--format", choices=("text", "json"), default="text")
    from .reasoning_admit_cli import add_admission_parsers

    add_admission_parsers(actions)


def _options(arguments: argparse.Namespace):
    from .reasoning_install import Options

    config = arguments.config_dir
    return Options(
        root=arguments.root,
        user=arguments.user,
        config_dir=config,
        credentials_dir=arguments.credentials_directory or config / "credentials",
        profile_path=arguments.profile or config / "reasoning.json",
        prompt_path=config / "reasoning-prompt.txt",
        state_dir=arguments.state_dir,
        unit_path=arguments.unit_path,
        python=arguments.python,
        profile_template=arguments.profile_template,
        prompt_source=arguments.prompt_file,
        max_cost_cents_per_call=arguments.max_cost_cents_per_call,
        daily_cost_cap_cents=arguments.daily_cost_cap_cents,
        daily_token_cap=arguments.daily_token_cap,
        replace=arguments.replace,
        new_run=arguments.new_run,
        control_state_dir=arguments.control_state_dir,
        control_url=arguments.control_url,
        project_id=arguments.project_id,
    )


def _text_plan(steps) -> str:
    marks = {"present": "ok     ", "missing": "missing", "differs": "DIFFERS"}
    return "\n".join(f"[{marks.get(s.state, s.state)}] {s.name}: {s.action}" for s in steps)


def run(arguments: argparse.Namespace) -> int:
    if arguments.reasoning_action == "admit":
        from .reasoning_admit_cli import run_admit

        return run_admit(arguments)
    if arguments.reasoning_action == "revoke-admission":
        from .reasoning_admit_cli import run_revoke

        return run_revoke(arguments)
    from . import reasoning_install as engine

    try:
        options = _options(arguments)
        if not arguments.apply:
            steps = engine.plan(options)
            if arguments.format == "json":
                print(
                    json.dumps(
                        {
                            "mode": "plan",
                            "steps": [
                                {"name": s.name, "state": s.state, "action": s.action}
                                for s in steps
                            ],
                        },
                        sort_keys=True,
                        indent=2,
                    )
                )
            else:
                print(_text_plan(steps))
                pending = [s.name for s in steps if s.state != "present"]
                print(
                    "nothing is changed; pass --apply to make these changes"
                    if pending
                    else "everything is installed; --apply would change nothing"
                )
            return 0
        result = engine.apply(options)
    except engine.InstallRefused as error:
        print(f"reasoning install: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"reasoning install: {type(error).__name__}", file=sys.stderr)
        return 2
    if arguments.format == "json":
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        for step in result["steps"]:
            print(f"[{step['result']}] {step['name']}")
        readiness = result.get("readiness") or {}
        print(f"readiness: {readiness.get('overall', 'unknown')}")
        for check in readiness.get("checks", []):
            if check.get("status") != "ok":
                note = check.get("fix") or check.get("detail")
                print(f"  {check['name']}: {check['status']}: {note}")
        for pending in result.get("pending_operator", []):
            print(f"operator step: {pending}")
    return int(result["exit_code"])
