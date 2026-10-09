"""Normal API client example; explicit human/worker roles, no automatic approval.

Credential is read from an operator-selected environment reference, never emitted.
Payloads/approval/release/grant IDs must already be independently reviewed. This
helper neither invents them nor impersonates the human in the worker process.
"""

import argparse
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit


def route(action, identifier):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
        raise ValueError("Invalid resource identifier")
    return {
        "upload": ("human", "POST", f"/projects/{identifier}/tabular-dataset-snapshots"),
        "plan": ("human", "PUT", f"/tasks/{identifier}/plan"),
        "grant": ("human", "POST", f"/projects/{identifier}/execution-grants"),
        "cancel": ("human", "POST", f"/execution-grants/{identifier}/revoke"),
        "status": ("human", "GET", f"/execution-grants/{identifier}"),
        "inputs": ("worker", "POST", f"/worker/leases/{identifier}/inputs"),
    }[action]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upload", "plan", "grant", "cancel", "status", "inputs"))
    parser.add_argument("identifier")
    parser.add_argument("--role", required=True, choices=("human", "worker"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--payload", type=argparse.FileType("rb"))
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    role, method, path = route(args.action, args.identifier)
    parsed = urlsplit(args.url)
    if role != args.role or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Role or endpoint mismatch")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("Use HTTPS or explicit loopback")
    body = None
    if method != "GET":
        if args.payload is None:
            raise ValueError("Explicit reviewed request payload required")
        body = args.payload.read(6 * 1024 * 1024 + 1)
        if len(body) > 6 * 1024 * 1024:
            raise ValueError("Request exceeds profile")
        json.loads(body)
    request = urllib.request.Request(
        args.url.rstrip("/") + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ[args.token_env],
            "Content-Type": "application/json",
            "Idempotency-Key": os.urandom(16).hex(),
        },
    )

    # No redirects: credentials must not cross an unreviewed destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None

    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("Response exceeds bound")
    fd = os.open(args.response, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(content)


if __name__ == "__main__":
    main()
