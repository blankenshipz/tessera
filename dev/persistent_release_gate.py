"""Private complete native gate receipts; no inferred live coverage or output disclosure."""

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from release_input_parity import assert_unchanged, inventory


def run_gate(
    root: Path,
    receipts: Path,
    argv: list[str],
    *,
    label: str,
    candidate_revision: str,
    timeout: float = 3600,
    env: dict | None = None,
) -> dict:
    if not argv or not candidate_revision or not 0 < timeout <= 3600:
        raise ValueError("Explicit command/candidate and bounded timeout required")
    before = inventory(root)
    receipts.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(receipts, 0o700)
    start = time.monotonic()
    result = {
        "schema": 1,
        "label": label,
        "argv": argv,
        "mode": "local",
        "candidate_revision": candidate_revision,
        "source_before": before,
        "exit_code": None,
        "category": "starting",
    }
    receipt = receipts / "result.json"
    receipt.write_text(json.dumps(result, sort_keys=True))
    os.chmod(receipt, 0o600)
    with (
        (receipts / "stdout.log").open("xb") as stdout,
        (receipts / "stderr.log").open("xb") as stderr,
    ):
        os.chmod(receipts / "stdout.log", 0o600)
        os.chmod(receipts / "stderr.log", 0o600)
        try:
            process = subprocess.Popen(
                argv, cwd=root, env=env, stdout=stdout, stderr=stderr, start_new_session=True
            )
            result["pid"] = process.pid
            try:
                result["exit_code"] = process.wait(timeout=timeout)
                result["category"] = "completed"
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                result["exit_code"] = process.wait(timeout=10)
                result["category"] = "timeout"
        except OSError:
            result["category"] = "command_unavailable"
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    try:
        result["source_after"] = inventory(root)
        assert_unchanged(root, before)
        result["source_unchanged"] = True
    except (ValueError, OSError):
        result["source_unchanged"] = False
    for name in ("stdout", "stderr"):
        path = receipts / (name + ".log")
        h = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(65536):
                h.update(block)
        result[name + "_sha256"] = h.hexdigest()
        result[name + "_bytes"] = path.stat().st_size
    result["status"] = (
        "passed"
        if (
            result["exit_code"] == 0
            and result["category"] == "completed"
            and result["source_unchanged"]
        )
        else "incomplete"
    )
    receipt.write_text(json.dumps(result, sort_keys=True))
    return result
