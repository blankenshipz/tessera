"""One-step interactive owner review of an agent research proposal.

The decision is always typed by a human at a TTY; no flag supplies it. Proposal text
is agent-authored and untrusted. The owner token is never printed, HTTP bodies are
never echoed, and only a loopback origin is contacted.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,200}")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
UNTRUSTED = "WARNING: the proposal text below is agent-authored, untrusted data."


class ReviewError(RuntimeError):
    pass


def control_origin(value):
    url = urlsplit(value)
    try:
        port = url.port
    except ValueError:
        raise ReviewError("Invalid control URL") from None
    if (
        url.scheme != "http"
        or url.hostname not in LOOPBACK
        or port is None
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ReviewError("Control URL must be a loopback http://HOST:PORT origin")
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    return f"http://{host}:{port}"


def read_owner_header(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 65536
            ):
                raise ReviewError("Owner header must be a private regular file owned by you")
            text = stream.read(65537).decode().strip()
    except (OSError, UnicodeDecodeError):
        raise ReviewError("Owner header file is unreadable") from None
    name, _, value = text.partition(": ")
    if name != "Authorization" or not value.startswith("Bearer ") or "\n" in value:
        raise ReviewError("Owner header file is malformed")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalTransport:
    """Callable (method, path, body, key) -> (status, json-or-None); never exposes bodies."""

    def __init__(self, origin, authorization):
        self.origin = origin
        self.authorization = authorization
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, method, path, body=None, key=None):
        headers = {"Authorization": self.authorization, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.origin + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, _json(response.read(1048577))
        except urllib.error.HTTPError as error:
            return error.code, None
        except Exception:
            raise ReviewError("Control service unreachable or refused") from None


def _json(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _identifier(value):
    if not IDENTIFIER.fullmatch(value or ""):
        raise ReviewError("Invalid identifier")
    return value


def _text(value):
    """Neutralize terminal control characters in agent-authored text."""
    return "".join(c if c in "\n " or c.isprintable() else "?" for c in str(value))


def format_proposal(record):
    payload = record.get("payload") or {}
    proposal = payload.get("proposal") or {}
    provenance = payload.get("provenance") or {}
    lines = [
        UNTRUSTED,
        f"Proposal:    {record.get('id')}",
        f"Kind:        {record.get('kind')} / {proposal.get('kind')}",
        f"Project:     {record.get('project_id')}",
        f"Credential:  {record.get('credential_id', '(not reported)')}",
        f"Proposer:    principal {record.get('actor_id')}",
        f"Provenance:  client={provenance.get('client')} model={provenance.get('model')} "
        f"session={provenance.get('session_id')} (caller-attested)",
        f"Digest:      {record.get('digest')}",
        f"Created:     {record.get('created_at')}",
        "Proposal body:",
        _text(json.dumps(proposal, indent=2, sort_keys=True)),
        f"Status:      {status_line(record)}",
    ]
    return "\n".join(lines)


def decided(record):
    return record.get("promotion") is not None or record.get("decline") is not None


def status_line(record):
    if record.get("promotion") is not None:
        resource = (record["promotion"].get("resource") or {}).get("id")
        return f"PROMOTED (resource {resource})"
    if record.get("decline") is not None:
        decline = record["decline"]
        return f"DECLINED at {decline.get('created_at')}: {_text(decline.get('rationale'))}"
    return "pending owner decision"


def _fetch(transport, proposal_id):
    status, record = transport("GET", f"/agent-research-proposals/{proposal_id}")
    if status != 200 or record is None:
        raise ReviewError(f"Could not read proposal (HTTP {status})")
    return record


def review(
    transport,
    proposal_id,
    *,
    input_fn=input,
    isatty=lambda: sys.stdin.isatty(),
    out=print,
    key_fn=lambda: secrets.token_hex(16),
):
    """Returns a process exit code: 0 done/skipped, 1 failure, 2 not interactive."""
    _identifier(proposal_id)
    record = _fetch(transport, proposal_id)
    out(format_proposal(record))
    if decided(record):
        return 0
    if not isatty():
        out("Refusing: an interactive terminal is required; no decision recorded.")
        return 2
    choice = input_fn("Decision [approve/decline/skip]: ").strip().lower()
    digest = record["digest"]
    if choice == "approve":
        typed = input_fn(f"Type the first 8 characters of the digest ({digest[:8]}) to confirm: ")
        if typed.strip() != digest[:8]:
            out("Digest confirmation did not match; nothing recorded.")
            return 1
        status, result = transport(
            "POST",
            f"/agent-research-proposals/{proposal_id}/promote",
            {"expected_revision": 1, "expected_digest": digest},
            key_fn(),
        )
        if status not in {200, 201} or result is None:
            out(f"Approval failed (HTTP {status}); nothing further recorded.")
            return 1
        resource = (result.get("resource") or {}).get("id")
        out(f"Approved: created {result.get('kind')} {resource}. Execution is not authorized.")
        return 0
    if choice == "decline":
        rationale = input_fn("Rationale for declining: ").strip()
        if not rationale:
            out("A non-empty rationale is required; nothing recorded.")
            return 1
        status, result = transport(
            "POST",
            f"/agent-research-proposals/{proposal_id}/decline",
            {"expected_revision": 1, "expected_digest": digest, "rationale": rationale},
            key_fn(),
        )
        if status not in {200, 201} or result is None:
            out(f"Decline failed (HTTP {status}); nothing recorded.")
            return 1
        out(f"Declined proposal {proposal_id} at {result.get('created_at')}.")
        return 0
    out("Skipped; nothing recorded.")
    return 0


def list_proposals(transport, project_id, *, out=print):
    _identifier(project_id)
    status, result = transport("GET", f"/projects/{project_id}/agent-workflow-proposals?limit=200")
    if status != 200 or result is None:
        raise ReviewError(f"Could not list proposals (HTTP {status})")
    for record in result.get("proposals", []):
        kind = (record.get("payload", {}).get("proposal") or {}).get("kind")
        out(f"{record['id']}  {kind}  {record['created_at']}  {status_line(record)}")
    return 0


def add_agent_proposal_commands(subcommands):
    command = subcommands.add_parser(
        "agent-proposal", help="human review of agent research proposals (interactive)"
    )
    actions = command.add_subparsers(dest="proposal_action", required=True)
    for name in ("review", "list"):
        sub = actions.add_parser(name)
        if name == "review":
            sub.add_argument("proposal_id")
        else:
            sub.add_argument("--project-id", required=True)
        sub.add_argument("--owner-header", type=Path, required=True)
        sub.add_argument("--control-url", default="http://127.0.0.1:8765")


def run_agent_proposal(arguments, transport_factory=LocalTransport, **io):
    try:
        origin = control_origin(arguments.control_url)
        transport = transport_factory(origin, read_owner_header(arguments.owner_header))
        if arguments.proposal_action == "review":
            return review(transport, arguments.proposal_id, **io)
        return list_proposals(transport, arguments.project_id)
    except ReviewError as error:
        print(str(error), file=sys.stderr)
        return 1
