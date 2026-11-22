"""``tessera admin provider-trust``: read the setting and reload its file (ADR 0040 section 4).

There is deliberately no command that edits the setting: it is a host file. ``reload`` asks
the running service to re-read that file and needs a human credential labelled as a
decision (``--automation-label`` for owner-operated automation).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def add_provider_trust_commands(subcommands: argparse._SubParsersAction) -> None:
    admin = subcommands.add_parser("admin", help="deployment administration over the control API")
    groups = admin.add_subparsers(dest="admin_group", required=True)
    trust = groups.add_parser("provider-trust", help="the provider data-class trust setting")
    actions = trust.add_subparsers(dest="provider_trust_action", required=True)
    show = actions.add_parser("show", help="the setting, its digest and its load time")
    reload = actions.add_parser("reload", help="human only: re-read the configured trust file")
    reload.add_argument("--idempotency-key", required=True)
    reload.add_argument(
        "--automation-label",
        help="marks this decision as owner-operated automation (X-Tessera-Automation)",
    )
    for sub in (show, reload):
        sub.add_argument("--owner-header", type=Path, required=True)
        sub.add_argument("--control-url", default="http://127.0.0.1:8765")


def run_provider_trust(arguments: argparse.Namespace, transport_factory=None) -> int:
    from .agent_research_review import (
        LocalTransport,
        ReviewError,
        control_origin,
        read_owner_header,
    )
    from .approval_trust import AUTOMATION_HEADER

    label = getattr(arguments, "automation_label", None)

    class Transport(LocalTransport):
        """Relays control denial bodies and the automation label."""

        def __call__(self, method, path, body=None, key=None):
            import urllib.error
            import urllib.request

            headers = {"Authorization": self.authorization, "Content-Type": "application/json"}
            if key:
                headers["Idempotency-Key"] = key
            if label:
                headers[AUTOMATION_HEADER] = label
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                self.origin + path, data=data, headers=headers, method=method
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    return response.status, json.loads(response.read(1024 * 1024))
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read(65536))
                except ValueError:
                    return error.code, None
            except Exception:
                raise ReviewError("Control service unreachable or refused") from None

    try:
        origin = control_origin(arguments.control_url)
        transport = (transport_factory or Transport)(
            origin, read_owner_header(arguments.owner_header)
        )
        if arguments.provider_trust_action == "show":
            call = ("GET", "/admin/provider-trust", None, None)
        else:
            call = ("POST", "/admin/provider-trust/reload", {}, arguments.idempotency_key)
        status, response = transport(*call)
    except ReviewError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 1
    ok = 200 <= status < 300
    print(json.dumps(response if ok else {"status": status, "body": response}, sort_keys=True))
    return 0 if ok else 1
