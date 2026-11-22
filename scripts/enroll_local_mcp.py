"""Human-run local MCP enrollment. Never give owner.header to an agent.

Run with --help for references only; no secret values belong in arguments.
Retain the private directory. After any failure, reconcile or explicitly --resume
the SAME command/directory: requests replay exact durable bodies and keys, never
renew an identity. Expiry requires separate human revocation/new enrollment.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from tessera_control.models import CreateCredential, CreateProject, ExternalAgentScope


class EnrollmentError(RuntimeError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise EnrollmentError("Invalid response identity; retain state and reconcile")
    return value


def open_directory(path):
    """Walk absolute paths without following any symlink component."""
    if not path.is_absolute() or ".." in path.parts:
        raise EnrollmentError("Use absolute, non-symlink paths")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private(name, directory):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > 65536
        ):
            raise EnrollmentError("Credential/state file must be owned, single-link regular 0600")
        return stream.read(65537)


def write_private(name, value, directory):
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EnrollmentError("Redirect refused; retain state and reconcile")


class LocalHTTP:
    def __init__(self, origin, owner):
        self.origin = origin
        self.owner = owner
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, method, path, body=None, key=None):
        headers = {"Authorization": self.owner, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.origin + path, data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(65537)
                if len(raw) > 65536 or response.status not in {200, 201}:
                    raise EnrollmentError("Unexpected response")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise EnrollmentError("Unexpected response")
                return result
        except Exception:
            # Never print exception text or response bodies (may echo submitted secrets).
            raise EnrollmentError(
                "HTTP outcome uncertain/refused; retain state; reconcile or --resume"
            ) from None


def enroll(args, transport_factory=LocalHTTP):
    url = urlsplit(args.control_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise EnrollmentError("This local-only helper requires an http://127.0.0.1:PORT origin")
    if not 1 <= args.duration_minutes <= 1440:
        raise EnrollmentError("Duration must be 1..1440 minutes; no automatic renewal")
    if args.project_id:
        identifier(args.project_id)
    else:
        args.create_project = CreateProject(name=args.create_project).name
    # Validate scope before reading any owner reference.
    scope = {
        "actions": ["context:read", "proposal:read", "notebook:read"],
        "destination": args.destination,
    }
    if args.allow_proposals:
        scope["actions"].append("proposal:create")
    if getattr(args, "allow_results", False):
        scope["actions"].append("results:read")
    if getattr(args, "allow_budget", False):
        scope["actions"].append("budget:read")
    if getattr(args, "allow_campaigns", False):
        scope["actions"] += ["campaign:submit", "campaign:read", "campaign:cancel"]
    if getattr(args, "allow_campaign_submit_cleared", False):
        # Consuming a human's recorded approved submission (ADR 0093). Never part of
        # --allow-campaigns: the operator opts in separately.
        for action in ("campaign:submit_cleared", "campaign:read", "campaign:cancel"):
            if action not in scope["actions"]:
                scope["actions"].append(action)
    if getattr(args, "allow_campaign_evidence", False):
        scope["actions"] += ["snapshot:read", "artifact:read", "campaign:logs"]
    if getattr(args, "allow_review_jobs", False):
        scope["actions"] += ["review:submit", "review:read"]
    if getattr(args, "allow_review_findings", False):
        scope["actions"] += [
            a for a in ("review:findings", "review:read") if a not in scope["actions"]
        ]
    if getattr(args, "allow_loop_pause", False):
        scope["actions"].append("loop:pause")
    scope = ExternalAgentScope.model_validate(scope).model_dump(mode="json")
    options = {
        "origin": args.control_url.rstrip("/"),
        "project_id": args.project_id,
        "create_project": args.create_project,
        "scope": scope,
        "duration_minutes": args.duration_minutes,
    }
    parent = open_directory(args.private_dir.parent)
    try:
        if not args.resume:
            os.mkdir(args.private_dir.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        directory = os.open(
            args.private_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
    finally:
        os.close(parent)
    try:
        info = os.fstat(directory)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise EnrollmentError("Enrollment directory must be owned 0700")
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        allowed = {
            "intent.json",
            "project-request.json",
            "project.json",
            "credential-request.json",
            "result.json",
            "agent-token",
        }
        if set(os.listdir(directory)) - allowed:
            raise EnrollmentError("Unexpected enrollment files; reconcile manually")
        owner_dir = open_directory(args.owner_header.parent)
        try:
            owner = read_private(args.owner_header.name, owner_dir).decode().strip()
        finally:
            os.close(owner_dir)
        if not re.fullmatch(r"Authorization: Bearer [A-Za-z0-9_-]{43,256}", owner):
            raise EnrollmentError("Invalid private owner header")
        owner_token = owner.removeprefix("Authorization: Bearer ")
        http = transport_factory(options["origin"], owner.removeprefix("Authorization: "))
        if not args.resume:
            token = secrets.token_urlsafe(48)
            if secrets.compare_digest(token, owner_token):
                raise EnrollmentError("Agent identity must be distinct")
            intent = {
                "options": options,
                "token": token,
                "expires_at": (
                    datetime.now(UTC) + timedelta(minutes=args.duration_minutes)
                ).isoformat(),
                "project_key": secrets.token_hex(24),
                "credential_key": secrets.token_hex(24),
            }
            # Validate exact credential shape without contacting control.
            CreateCredential(
                token=token,
                kind="agent",
                audience="control",
                project_id="pending",
                expires_at=intent["expires_at"],
                external_agent=scope,
            )
            write_private("intent.json", encode(intent), directory)
        intent = json.loads(read_private("intent.json", directory))
        if intent["options"] != options or intent["token"] == owner_token:
            raise EnrollmentError("Existing intent differs; reconcile without replacing identity")
        if datetime.fromisoformat(intent["expires_at"]) <= datetime.now(UTC):
            raise EnrollmentError("Enrollment expired; human revocation/new enrollment required")

        def durable_post(name, path, body, key):
            expected = {"path": path, "body": encode(body).decode(), "key": key}
            if name not in os.listdir(directory):
                write_private(name, encode(expected), directory)
            saved = json.loads(read_private(name, directory))
            if saved != expected:
                raise EnrollmentError("Stored request differs; reconcile manually")
            return http("POST", saved["path"], saved["body"].encode(), saved["key"])

        if "project.json" not in os.listdir(directory):
            if args.project_id:
                project = http("GET", f"/projects/{args.project_id}")
                if project.get("id") != args.project_id:
                    raise EnrollmentError("Selected project mismatch")
            else:
                # Never choose a project by a possibly non-unique display name.
                # Skip discovery only when explicitly replaying an already persisted POST.
                if "project-request.json" not in os.listdir(directory):
                    offset = 0
                    while True:
                        page = http("GET", f"/projects?offset={offset}&limit=200")["projects"]
                        if any(p["name"] == args.create_project for p in page):
                            raise EnrollmentError(
                                "Name already exists; explicitly select --project-id"
                            )
                        if len(page) < 200:
                            break
                        offset += 200
                        if offset >= 10000:
                            raise EnrollmentError(
                                "Discovery bound reached; explicitly select project ID"
                            )
                project = durable_post(
                    "project-request.json",
                    "/projects",
                    {"name": args.create_project},
                    intent["project_key"],
                )
            write_private("project.json", encode({"id": identifier(project["id"])}), directory)
        project_id = identifier(json.loads(read_private("project.json", directory))["id"])
        body = {
            "token": intent["token"],
            "kind": "agent",
            "audience": "control",
            "project_id": project_id,
            "expires_at": intent["expires_at"],
            "external_agent": scope,
        }
        if "result.json" not in os.listdir(directory):
            response = durable_post(
                "credential-request.json", "/credentials", body, intent["credential_key"]
            )
            if (
                response.get("project_id") != project_id
                or response.get("kind") != "agent"
                or response.get("audience") != "control"
                or response.get("revoked_at") is not None
                or datetime.fromisoformat(response["expires_at"])
                != datetime.fromisoformat(intent["expires_at"])
            ):
                raise EnrollmentError("Credential response mismatch; reconcile manually")
            result = {
                "project_id": project_id,
                "principal_id": identifier(response["principal_id"]),
                "credential_id": identifier(response["credential_id"]),
                "expires_at": intent["expires_at"],
                "token_file": str(args.private_dir / "agent-token"),
            }
            write_private("result.json", encode(result), directory)
        if "agent-token" not in os.listdir(directory):
            write_private("agent-token", intent["token"].encode(), directory)
        if read_private("agent-token", directory).decode() != intent["token"]:
            raise EnrollmentError("Existing token mismatch; reconcile manually")
        return json.loads(read_private("result.json", directory))
    finally:
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-url", default="http://127.0.0.1:8765")
    parser.add_argument("--owner-header", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    project = parser.add_mutually_exclusive_group(required=True)
    project.add_argument("--project-id")
    project.add_argument("--create-project")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--allow-proposals", action="store_true")
    parser.add_argument("--allow-results", action="store_true")
    parser.add_argument("--allow-budget", action="store_true")
    parser.add_argument("--allow-campaigns", action="store_true")
    parser.add_argument("--allow-campaign-submit-cleared", action="store_true")
    parser.add_argument("--allow-campaign-evidence", action="store_true")
    parser.add_argument("--allow-review-jobs", action="store_true")
    parser.add_argument("--allow-review-findings", action="store_true")
    parser.add_argument("--allow-loop-pause", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(enroll(args), sort_keys=True))
        return 0
    except Exception:
        print(
            "Enrollment stopped. Retain private state; reconcile before explicit --resume. "
            "No identity renewal is automatic.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
