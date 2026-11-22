"""The opt-in CPU smoke for the deploy check (ADR 0098).

Runs one synthetic container that prints a random nonce: networkless, read-only, all
capabilities dropped. It spends nothing, contacts no provider, reads no secret and never pulls
an image; the pinned image must already exist locally. The container is always removed by its
exact generated name afterwards.
"""

from __future__ import annotations

import secrets
import subprocess
import time
from collections.abc import Callable

from tessera_control.deploy_check import Finding, Fix

# Copied from scripts/check_cpu_runtime.py (scripts are not packaged); a test keeps them equal.
PINNED_IMAGE = (
    "python:3.11-slim-bookworm@sha256:"
    "528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84"
)
CHECK_ID = "smoke.cpu"
AREA = "smoke"
MAX_OUTPUT_BYTES = 4096
DETAIL_OUTPUT_CHARS = 200

Runner = Callable[[list[str], int], tuple[int, str]]


def default_runner(argv: list[str], timeout: int) -> tuple[int, str]:
    """Run a fixed argv without a shell and with only PATH in the environment."""
    import os

    completed = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
        shell=False,
        env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
    )
    output = (completed.stdout or b"") + (completed.stderr or b"")
    return completed.returncode, output[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")


def smoke_skipped() -> Finding:
    return Finding(CHECK_ID, AREA, "skipped", "smoke not requested; pass --smoke")


def _pull_fix() -> Fix:
    return Fix(
        "smoke-pull-image",
        "Pull the pinned image once, then re-run the check; the smoke never pulls.",
        automatic=False,
        command=f"docker pull {PINNED_IMAGE}",
    )


def _failure(summary: str, code: int | None, output: str, **detail: object) -> Finding:
    info: dict[str, object] = {"image": PINNED_IMAGE, **detail}
    if code is not None:
        info["exit_code"] = code
    info["output"] = output[:DETAIL_OUTPUT_CHARS]
    return Finding(CHECK_ID, AREA, "fail", summary, info)


def run_smoke(runner: Runner | None = None, timeout_seconds: int = 120) -> Finding:
    run = runner or default_runner
    started = time.monotonic()

    def elapsed() -> float:
        return round(time.monotonic() - started, 3)

    try:
        code, output = run(["docker", "image", "inspect", PINNED_IMAGE], timeout_seconds)
    except Exception as error:  # includes a missing docker binary and timeouts
        finding = _failure(
            "The pinned CPU image must already exist locally (the smoke never pulls); "
            f"docker could not be queried: {type(error).__name__}.",
            None,
            "",
            duration_seconds=elapsed(),
            cleanup=True,
        )
        return Finding(CHECK_ID, AREA, "fail", finding.summary, finding.detail, _pull_fix())
    if code != 0:
        finding = _failure(
            "The pinned CPU image must already exist locally (the smoke never pulls); "
            "pull it once and re-run.",
            code,
            output,
            duration_seconds=elapsed(),
            cleanup=True,
        )
        return Finding(CHECK_ID, AREA, "fail", finding.summary, finding.detail, _pull_fix())

    nonce = secrets.token_hex(16)
    name = f"tessera-deploy-smoke-{secrets.token_hex(6)}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--name",
        name,
        PINNED_IMAGE,
        "python",
        "-c",
        f"print({nonce!r})",
    ]
    result: tuple[str, str, int | None, str] | None = None
    try:
        code, output = run(argv, timeout_seconds)
        if code == 0 and output.strip() == nonce:
            result = ("ok", "", code, "")
        elif code == 0:
            result = ("fail", "The smoke container did not print the expected nonce.", code, output)
        else:
            result = ("fail", "The smoke container exited non-zero.", code, output)
    except Exception as error:
        result = ("fail", f"The smoke container could not run: {type(error).__name__}.", None, "")
    cleanup = True
    try:
        removed_code, _ = run(["docker", "rm", "-f", name], 30)
        cleanup = removed_code == 0
    except Exception:
        cleanup = False
    duration = elapsed()

    status, summary, code, output = result
    if status == "ok":
        return Finding(
            CHECK_ID,
            AREA,
            "ok",
            "The synthetic networkless CPU container ran and returned its nonce.",
            {"image": PINNED_IMAGE, "duration_seconds": duration, "cleanup": cleanup},
        )
    finding = _failure(summary, code, output, duration_seconds=duration, cleanup=cleanup)
    return finding
