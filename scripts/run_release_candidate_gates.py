"""Exact-source local gates. Full execution requires an explicit parent freeze."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path, PurePosixPath

STANDARD_GATES = {
    "ruff-lint": ("-m", "ruff", "check", "."),
    "ruff-format": ("-m", "ruff", "format", "--check", "."),
    "python-tests": ("-m", "pytest", "-q"),
}
OBSERVER_FILES = {"README.md", "baseline.json", "LICENSE", "observer.patch", "baseline.tar.gz"}
RUNTIME_FILES = {
    "baseline.json",
    "baseline.tar.gz",
    "observer.patch",
    "LICENSE",
    "BASELINE.sha256",
    "TARGET.sha256",
    "origins.json",
    "README.md",
    "reconstruct.py",
    "link_runtime.py",
    "verify_runtime.mjs",
    "verify_python.py",
    "runtime-deps/package.json",
    "runtime-deps/bun.lock",
}
LIVE_REQUIREMENTS = (
    "installed-mcp-entrypoint",
    "installed-a2a-entrypoints",
    "native-picker",
    "offhost-execution",
    "remote-reasoning",
)
SOURCE_ROOTS = {
    "src",
    "tests",
    "scripts",
    "docs",
    "desktop",
    "skills",
    "examples",
    ".agents",
    "dev",
    "deploy",
    "plugins",
    ".github",
}
ROOT_FILES = {
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "DESIGN.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "LICENSE",
    "MANIFEST.in",
    "Dockerfile",
    "Dockerfile.guard",
    ".gitignore",
    ".dockerignore",
    "compose.yaml",
    "CHANGELOG.md",
    "SECURITY.md",
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "runtime",
    "state",
    ".state",
    "dist",
    "build",
}
AUTH_FILES = {
    "auth.json",
    "credentials.json",
    "token.json",
    "tokens.json",
    "owner.header",
    "worker.header",
    "credentials",
    "token",
}
HEX = re.compile(r"[a-f0-9]{64}\Z")
COUNT = r"\d+ (?:passed|failed|skipped|errors?|warnings?|deselected|xfailed|xpassed)"
SUMMARY = re.compile(rf"({COUNT}(?:, {COUNT})*) in \d+(?:\.\d+)?s(?: \([\d:]+\))?\Z")


class GateInputError(ValueError):
    """Finite category; never echoes input, command output or secret references."""


def source_path(value):
    if (
        not isinstance(value, str)
        or len(value) > 240
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
    ):
        raise GateInputError("invalid_source_path")
    p = PurePosixPath(value)
    if p.is_absolute() or value != str(p) or any(part in {".", ".."} for part in p.parts):
        raise GateInputError("invalid_source_path")
    if any(
        part in FORBIDDEN_PARTS
        or part in AUTH_FILES
        or part.startswith(".env")
        or part.endswith((".header", ".pem", ".key", ".sqlite3", ".db"))
        for part in p.parts
    ):
        raise GateInputError("excluded_source_path")
    if p.parts[0] == "release":
        if value.startswith("release/opencode-observer/runtime-v8/"):
            if value.removeprefix("release/opencode-observer/runtime-v8/") not in RUNTIME_FILES:
                raise GateInputError("unlisted_observer_distribution")
            return value
        if (
            len(p.parts) != 3
            or p.parts[1] != "opencode-observer"
            or p.parts[2] not in OBSERVER_FILES
        ):
            raise GateInputError("unlisted_observer_distribution")
        return value
    if len(p.parts) == 1:
        if value not in ROOT_FILES:
            raise GateInputError("unlisted_root_source")
    elif p.parts[0] not in SOURCE_ROOTS or any(part.startswith(".") for part in p.parts[1:]):
        raise GateInputError("unlisted_source_root")
    return value


def source_hash(root, paths):
    root = Path(root).resolve()
    if not isinstance(paths, list) or not 1 <= len(paths) <= 10000:
        raise GateInputError("invalid_source_scope")
    if paths != sorted(set(paths)):
        raise GateInputError("duplicate_or_unsorted_source")
    entries, total = {}, 0
    for value in paths:
        source_path(value)
        path = root / value
        if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
            raise GateInputError("source_missing_or_symlink")
        size = path.stat().st_size
        total += size
        if size > 64 * 1024 * 1024 or total > 512 * 1024 * 1024:
            raise GateInputError("source_byte_bound_exceeded")
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                h.update(chunk)
        entries[value] = h.hexdigest()
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_inventory(root):
    root = Path(root).resolve()
    if (root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "ls-files", "-co", "--exclude-standard", "-z"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise GateInputError("source_inventory_unavailable") from None
        if len(result.stdout) > 5 * 1024 * 1024:
            raise GateInputError("source_inventory_too_large")
        candidates = result.stdout.decode("utf-8").split("\0")
    else:
        candidates = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in FORBIDDEN_PARTS and not d.startswith(".env")]
            candidates.extend(str((Path(directory) / file).relative_to(root)) for file in files)
    accepted = set()
    # Closed intentional distribution files remain mandatory even if a broad
    # archive ignore rule hides them from Git. No other ignored input is adopted.
    for name in OBSERVER_FILES:
        candidate = "release/opencode-observer/" + name
        path = root / candidate
        if path.exists() or path.is_symlink():
            accepted.add(candidate)
    for name in RUNTIME_FILES:
        candidate = "release/opencode-observer/runtime-v8/" + name
        path = root / candidate
        if path.exists() or path.is_symlink():
            accepted.add(candidate)
    for candidate in candidates:
        try:
            accepted.add(source_path(candidate))
        except GateInputError:
            pass  # Excluded names are neither read nor included in receipts.
    return sorted(accepted)


def manifest(root, paths, artifacts=None):
    return validate_manifest(
        {
            "schema": 1,
            "source_paths": sorted(paths),
            "source_sha256": source_hash(root, sorted(paths)),
            "gates": list(STANDARD_GATES),
            "artifacts": artifacts or [],
        }
    )


def validate_manifest(value):
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "source_paths",
        "source_sha256",
        "gates",
        "artifacts",
    }:
        raise GateInputError("invalid_manifest_schema")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise GateInputError("unsupported_manifest_schema")
    if not isinstance(value["source_sha256"], str) or not HEX.fullmatch(value["source_sha256"]):
        raise GateInputError("invalid_source_revision")
    paths = value["source_paths"]
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 10000
        or (not all(isinstance(p, str) for p in paths) or paths != sorted(set(paths)))
    ):
        raise GateInputError("invalid_source_scope")
    for p in paths:
        source_path(p)
    if value["gates"] != list(STANDARD_GATES):
        raise GateInputError("required_standard_gates_missing")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise GateInputError("invalid_artifact_references")
    seen = set()
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"subject", "sha256"}
            or (
                not isinstance(artifact["subject"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", artifact["subject"])
                or not isinstance(artifact["sha256"], str)
                or not HEX.fullmatch(artifact["sha256"])
                or artifact["subject"] in seen
            )
        ):
            raise GateInputError("invalid_artifact_reference")
        seen.add(artifact["subject"])
    return value


def _consume(stream, counts):
    # Transient bounded parsing only: no raw output file, traceback, auth body or
    # source snippets retained in the report. Oversized output lines are dropped.
    pending, overflow = b"", False
    while chunk := stream.read(4096):
        for piece in chunk.splitlines(keepends=True):
            if not overflow:
                pending += piece
                if len(pending) > 65536:
                    pending, overflow = b"", True
            if piece.endswith(b"\n"):
                if not overflow:
                    text = pending.decode("utf-8", errors="replace").strip().strip("=").strip()
                    match = SUMMARY.fullmatch(text)
                    if match:
                        counts.clear()
                        for n, kind in re.findall(r"(\d+) (\w+)", match[1]):
                            counts[kind.rstrip("s") if kind in {"errors", "warnings"} else kind] = (
                                int(n)
                            )
                pending, overflow = b"", False


def native_gate(root, name, timeout_seconds):
    counts = {}
    try:
        process = subprocess.Popen(
            [sys.executable, *STANDARD_GATES[name]],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        return {"exit_code": None, "category": "command_unavailable", "counts": counts}
    reader = threading.Thread(target=_consume, args=(process.stdout, counts), daemon=True)
    reader.start()
    category = "completed"
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        category = "timeout"
        os.killpg(process.pid, signal.SIGKILL)  # our own newly created process group only
        process.wait(timeout=5)
    reader.join(timeout=5)
    if reader.is_alive():
        category = "output_not_closed"
    else:
        process.stdout.close()
    return {"exit_code": process.returncode, "category": category, "counts": dict(counts)}


def run_gates(root, value, *, parent_frozen=False, timeout_seconds=3600, execute=native_gate):
    value = validate_manifest(value)
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 3600:
        raise GateInputError("invalid_gate_timeout")
    before = source_hash(root, value["source_paths"])
    inventory_before = source_inventory(root)
    if inventory_before != value["source_paths"]:
        raise GateInputError("source_scope_incomplete")
    report = {
        "schema": 1,
        "source_sha256_expected": value["source_sha256"],
        "source_sha256_before": before,
        "source_sha256_after": None,
        "artifacts": value["artifacts"],
        "artifact_proof": "references_only",
        "source_gates": [],
        "required_integration": [
            {"id": name, "mode": "live", "status": "not_run"} for name in LIVE_REQUIREMENTS
        ],
        "full_acceptance": "not_run",
    }
    admitted = parent_frozen is True and before == value["source_sha256"]
    for name in STANDARD_GATES:
        row = {
            "id": name,
            "mode": "local" if execute is native_gate else "synthetic",
            "status": "not_run",
            "exit_code": None,
            "category": "not_frozen" if not parent_frozen else "source_revision_mismatch",
            "counts": {},
        }
        if admitted:
            result = execute(root, name, timeout_seconds)
            if not isinstance(result, dict) or set(result) != {"exit_code", "category", "counts"}:
                raise GateInputError("invalid_executor_receipt")
            if (
                result["category"]
                not in {"completed", "command_unavailable", "timeout", "output_not_closed"}
                or (result["exit_code"] is not None and type(result["exit_code"]) is not int)
                or not isinstance(result["counts"], dict)
                or any(
                    k
                    not in {
                        "passed",
                        "failed",
                        "skipped",
                        "error",
                        "warning",
                        "deselected",
                        "xfailed",
                        "xpassed",
                    }
                    or type(n) is not int
                    or n < 0
                    for k, n in result["counts"].items()
                )
            ):
                raise GateInputError("invalid_executor_receipt")
            row.update(result)
            row["status"] = (
                "passed"
                if result["exit_code"] == 0 and result["category"] == "completed"
                else "failed"
            )
            if (
                name == "python-tests"
                and row["status"] == "passed"
                and (
                    not result["counts"].get("passed")
                    or any(
                        result["counts"].get(k)
                        for k in ("failed", "error", "skipped", "deselected", "xfailed", "xpassed")
                    )
                )
            ):
                row.update(status="incomplete", category="pytest_incomplete")
        report["source_gates"].append(row)
    try:
        after = source_hash(root, value["source_paths"])
        if source_inventory(root) != inventory_before:
            after = None
    except GateInputError:
        after = None
    report["source_sha256_after"] = after
    report["source_unchanged"] = before == after == value["source_sha256"]
    if not report["source_unchanged"]:
        for row in report["source_gates"]:
            if row["status"] == "passed":
                row.update(status="invalidated", category="source_changed")
    report["local_gate_status"] = (
        "passed"
        if report["source_unchanged"]
        and all(row["status"] == "passed" for row in report["source_gates"])
        else "incomplete"
    )
    if report["local_gate_status"] == "passed" and execute is not native_gate:
        report["local_gate_status"] = "synthetic_only"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parent-frozen", action="store_true")
    args = parser.parse_args(argv)
    try:
        with args.manifest.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise GateInputError("manifest_too_large")
        report = run_gates(args.root, json.loads(data), parent_frozen=args.parent_frozen)
    except (ValueError, OSError, TypeError):
        print(json.dumps({"status": "blocked", "category": "invalid_gate_input"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["local_gate_status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
