"""Bounded, sanitized tail of one container's log, for an operator receipt.

Log text is untrusted: every line is reduced to printable ASCII, capped, and scrubbed of
anything secret-, identity- or host-shaped before it can leave the transport. Fail closed:
anything malformed yields an empty or unavailable record, never an exception.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_LINES = 20
MAX_BYTES = 1024
MAX_LINE_CHARS = 200
MAX_ITEMS = 1000
# ``data_class_sealed``: a holdout run or a sealed partition; the log is never read or stored.
REASONS = (
    "no_trusted_log_source",
    "log_quota_exhausted",
    "read_failed",
    "not_read",
    "data_class_sealed",
)
_KEYS = {"outcome", "untrusted", "lines", "truncated", "sha256"}
_REDACTED = "[redacted]"

_KEYWORD = (
    r"(?:token|secret|password|passwd|authorization|bearer|api[-_ ]?key|access[-_ ]?key"
    r"|credential|cookie|session)"
)
# An auth scheme word is kept and the credential after it is redacted ("Basic <x>").
_SCHEME = r"(?:basic|bearer|token)"
# Well-known credential prefixes, redacted whatever their length (OpenAI/Anthropic-style
# ``sk-``, GitHub ``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``/``github_pat_``, Slack
# ``xox?-``, AWS access key ids ``AKIA``/``ASIA``).
_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[a-z]-)[A-Za-z0-9_\-]+"
    r"|(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]+"
)
_RULES = [
    (_PREFIXED, _REDACTED),
    (re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/@]*@[^\s]*"), _REDACTED),
    (re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+"), _REDACTED),
    (
        re.compile(r"\b[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{4,}\b"),
        lambda m: _REDACTED if _jwt_like(m.group(0)) else m.group(0),
    ),
    (
        # "<keyword>[:= ] [is|was|are] [<scheme>] <value>": the value after a connecting word
        # ("password is <x>") or an auth scheme ("Authorization: Basic <x>") is the secret.
        re.compile(
            rf"(?i)(\b[\w.\-]*{_KEYWORD}[\w.\-]*\b[\"']?\s*(?:[:=]|\s)\s*[\"']?"
            rf"(?:(?:is|was|are)\s+[\"']?)?(?:{_SCHEME}\s+)?)[^\s'\",;]+"
        ),
        lambda m: m.group(1) + _REDACTED,
    ),
    (
        # A bare scheme and its credential with no keyword before it ("Basic <x>").
        re.compile(rf"(?i)\b({_SCHEME})\s+(?!\[redacted\])[^\s'\",;]+"),
        lambda m: m.group(1) + " " + _REDACTED,
    ),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        _REDACTED,
    ),
    (re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])"), _REDACTED),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), _REDACTED),
    (re.compile(r"[A-Za-z0-9+/=_\-]{20,}"), _REDACTED),
    (re.compile(r"/(?:home|Users)/[^/\s]+"), "~"),
    (re.compile(r"/root\b"), "~"),
]


def _jwt_like(text: str) -> bool:
    return len(text) >= 16 and all(len(part) >= 4 for part in text.split("."))


def sanitize_line(line: str) -> str:
    """One log line reduced to bounded printable ASCII with sensitive shapes redacted."""
    text = "".join(c if " " <= c <= "~" else "?" for c in line[:2000])
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text.strip()[:MAX_LINE_CHARS]


# The GPU runtime's own last line (workloads/gpu_runtime_v1): one compact JSON object. Only these
# keys are kept, each a closed token or a small integer, so it never carries workload text.
RUNTIME_STATUS_SCHEMA = "tessera.gpu-runtime-status.v1"
RUNTIME_STATUS_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,39}")
RUNTIME_STEPS = frozenset({"claim", "manifest", "chunk", "output_plan", "upload"})


def _runtime_status_line(body: dict) -> str | None:
    """A ``json_log`` object that is the runtime's status line, as one closed compact JSON line
    (``schema``, ``status``, ``reason``, ``step`` and an integer ``exit_code``), else None.

    Salad may hand a JSON stdout line back parsed; without this its keys and its integer
    ``exit_code`` were dropped and the line could not be read again."""
    if body.get("schema") != RUNTIME_STATUS_SCHEMA:
        return None
    kept: dict[str, Any] = {"schema": RUNTIME_STATUS_SCHEMA}
    for key in ("status", "reason"):
        if type(body.get(key)) is str and RUNTIME_STATUS_TOKEN.fullmatch(body[key]):
            kept[key] = body[key]
    if body.get("step") in RUNTIME_STEPS:
        kept["step"] = body["step"]
    code = body.get("exit_code")
    if type(code) is int and -255 <= code <= 255:
        kept["exit_code"] = code
    return json.dumps(kept, sort_keys=True, separators=(",", ":"))


def runtime_status_lines(container_log: Any) -> list[dict[str, Any]]:
    """The runtime status lines a closed tail holds, in order, each reduced to closed tokens.

    The tail is untrusted (the workload writes to the same stream), so a caller may only use
    these to narrow a decision the guardian's own record already supports."""
    closed = closed_container_log(container_log)
    found: list[dict[str, Any]] = []
    for line in (closed or {}).get("lines") or []:
        if RUNTIME_STATUS_SCHEMA not in line:
            continue
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if type(body) is not dict or body.get("schema") != RUNTIME_STATUS_SCHEMA:
            continue
        status = {
            key: body[key]
            for key in ("status", "reason")
            if type(body.get(key)) is str and RUNTIME_STATUS_TOKEN.fullmatch(body[key])
        }
        if body.get("step") in RUNTIME_STEPS:
            status["step"] = body["step"]
        if type(body.get("exit_code")) is int:
            status["exit_code"] = body["exit_code"]
        found.append(status)
    return found


def _texts(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    found = []
    text = entry.get("text_log")
    if isinstance(text, str):
        found.append(text)
    body = entry.get("json_log")
    if isinstance(body, dict):
        status = _runtime_status_line(body)
        if status is not None:
            found.append(status)
        else:
            found.extend(v for v in body.values() if isinstance(v, str))
    return found


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def unavailable(reason: str) -> dict[str, Any]:
    return {"outcome": "unavailable", "reason": reason if reason in REASONS else "read_failed"}


def _empty() -> dict[str, Any]:
    return {
        "outcome": "empty",
        "untrusted": True,
        "lines": [],
        "truncated": False,
        "sha256": _digest([]),
    }


def summarize_container_log(items: Any) -> dict[str, Any]:
    """Closed tail record of one page of log entries (answered newest first)."""
    try:
        if not isinstance(items, list) or not items:
            return _empty()
        lines: list[str] = []
        for entry in reversed(items[:MAX_ITEMS]):  # chronological
            for text in _texts(entry):
                for raw in text.splitlines():
                    clean = sanitize_line(raw)
                    if clean:
                        lines.append(clean)
        truncated = len(lines) > MAX_LINES
        lines = lines[-MAX_LINES:]
        while lines and sum(len(x) + 1 for x in lines) - 1 > MAX_BYTES:
            lines.pop(0)
            truncated = True
        if not lines:
            return _empty()
        return {
            "outcome": "read",
            "untrusted": True,
            "lines": lines,
            "truncated": truncated,
            "sha256": _digest(lines),
        }
    except Exception:
        return unavailable("read_failed")


def closed_container_log(value: Any) -> dict[str, Any] | None:
    """The value if it has exactly the closed shape and bounds, else None."""
    try:
        if not isinstance(value, dict):
            return None
        if value.get("outcome") == "unavailable":
            if set(value) == {"outcome", "reason"} and value["reason"] in REASONS:
                return dict(value)
            return None
        if set(value) != _KEYS or value["outcome"] not in {"read", "empty"}:
            return None
        lines = value["lines"]
        if value["untrusted"] is not True or type(value["truncated"]) is not bool:
            return None
        if type(lines) is not list or len(lines) > MAX_LINES:
            return None
        if value["outcome"] == "empty" and lines:
            return None
        for line in lines:
            if type(line) is not str or len(line) > MAX_LINE_CHARS:
                return None
            if not all(" " <= c <= "~" for c in line):
                return None
        if sum(len(x) + 1 for x in lines) - 1 > MAX_BYTES:
            return None
        digest = value["sha256"]
        if type(digest) is not str or digest != _digest(lines):
            return None
        return {**value, "lines": list(lines)}
    except Exception:
        return None
