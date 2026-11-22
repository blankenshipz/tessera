"""Fail when tracked files contain private or machine-specific details.

Built-in patterns are generic: personal home directories, private-network IP
addresses outside tests, internal work-track names and branch-workflow wording.
Deployment-specific strings (an organization, host or user name) belong in a
deny-list that is never committed: set ``TESSERA_PRIVATE_DENYLIST`` to a file of
regular expressions (one per line, ``#`` comments), or create the git-ignored
``.private-denylist`` at the repository root. Usage: ``python dev/check_private_details.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_USERS = r"(?:user|username|example|you|name|runner|tessera|nonroot|me|youruser)"
HOME_PATH = re.compile(rf"/(?:Users|home)/(?!{PLACEHOLDER_USERS}\b)[A-Za-z_][A-Za-z0-9._-]*")
PRIVATE_IP = re.compile(
    r"(?<![0-9.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})(?![0-9]|/\d)"
)
WORKFLOW = re.compile(
    r"\b[Tt]rack [A-H]\b(?!\w)|\b(?:on|in|for|of) this branch\b|\bgrokbot\b"
    r"|\b(?:this|other|outside this) lanes?\b|\boutcome-?0\b|\bmixfm\b",
    re.IGNORECASE,
)
CGNAT_IP = re.compile(
    r"(?<![0-9.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![0-9]|/\d)"
)
TAILNET = re.compile(
    r"\b(?![\w.-]*(?:example-tailnet|tailnet)\.ts\.net|(?:h|custody)\.ts\.net)[\w.-]+\.ts\.net\b",
    re.IGNORECASE,
)
# An address is flagged unless its whole domain is a reserved/documentation one
# (``.test``, ``example.<tld>`` and their subdomains, or a known public no-reply
# domain) or its final label is a systemd unit type (``unit@host.service``).
# Allowed domains are matched to the end of the domain, so ``example.evil.io`` or
# ``anthropic.com.evil.io`` are still flagged; the final label must end the domain,
# so ``host.timer.io`` is an address while ``host.timer`` is a unit name.
EMAIL = re.compile(
    r"\b[\w.+-]+@"
    r"(?!(?:[\w-]+\.)*(?:test|example\.[a-z]+|anthropic\.com|users\.noreply\.github\.com)"
    r"(?![\w-]|\.\w))"
    r"(?:[\w-]+\.)+(?!(?:service|timer|socket|target|path|mount)\b)[a-z]{2,}(?![\w-]|\.\w)",
    re.IGNORECASE,
)
SKIP_PREFIXES = ("experiments/", "uv.lock")
SKIP_FILES = {"dev/check_private_details.py", "tests/test_private_details.py"}


def tracked_files(root: Path = ROOT) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        ).stdout.decode()
        return [name for name in out.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        return [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts
        ]


def load_denylist(root: Path = ROOT) -> list[re.Pattern[str]]:
    location = os.environ.get("TESSERA_PRIVATE_DENYLIST")
    path = Path(location) if location else root / ".private-denylist"
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text().splitlines())
    return [re.compile(line, re.IGNORECASE) for line in lines if line and not line.startswith("#")]


def scan_text(name: str, text: str, denylist: list[re.Pattern[str]] = ()) -> list[str]:
    findings = []
    is_test = name.startswith("tests/") or "/test/" in name or name.endswith(".test.mjs")
    for number, line in enumerate(text.splitlines(), 1):
        checks = [
            ("home path", HOME_PATH),
            ("workflow wording", WORKFLOW),
            ("tailnet name", TAILNET),
        ]
        if not is_test:
            checks += [("private IP", PRIVATE_IP), ("CGNAT IP", CGNAT_IP), ("email address", EMAIL)]
        checks += [("deny-list", pattern) for pattern in denylist]
        for label, pattern in checks:
            if pattern.search(line):
                findings.append(f"{name}:{number}: {label}")
    return findings


def scan(root: Path = ROOT, denylist: list[re.Pattern[str]] | None = None) -> list[str]:
    patterns = load_denylist(root) if denylist is None else denylist
    findings = []
    for name in tracked_files(root):
        if name in SKIP_FILES or name.startswith(SKIP_PREFIXES):
            continue
        path = root / name
        # Tracked file names are checked too, not only their contents.
        findings += scan_text(f"{name} (path)", name.replace("/", " /"), patterns)
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            text = data.decode() if b"\0" not in data else ""
        except (OSError, UnicodeDecodeError):
            continue
        findings += scan_text(name, text, patterns)
    return findings


def main() -> int:
    findings = scan()
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} private-detail finding(s)", file=sys.stderr)
        return 1
    print("No private details found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
