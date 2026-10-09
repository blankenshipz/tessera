#!/usr/bin/env python3
"""Opt-in acceptance against an installed control API and ordinary worker CLI.

Does not submit/approve work, create collector pods, or upload bytes itself.
Requires two already human-approved baseline/intervention dispatches and JSON
expectations: [{"operation_id": ..., "sha256": ..., "size": ..., "late": false}].
For interruption evidence, launch the ordinary installed worker from a JSON argv
file (credential *paths* only), and stop only that owned subprocess once immutable
bytes are retained but before the selected manifest exists. Restart uses the same
workspace. No Kubernetes reset, deployment rollout, or broad process cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from tessera_worker.service import read_private_credential


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--control-ca-file", type=Path, required=True)
    parser.add_argument("--owner-credential-file", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--expectations-file", type=Path, required=True)
    parser.add_argument("--worker-command-file", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--interrupt-operation")
    parser.add_argument("--human-release-fixture-outputs", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    origin = urllib.parse.urlsplit(args.control_url)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        parser.error("control URL must be an explicit HTTPS origin without credentials")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.project_id):
        parser.error("invalid project identity")
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("timeout must be 1..600 seconds")
    if args.interrupt_operation and (not args.worker_command_file or not args.workspace):
        parser.error("interruption requires an owned CLI command and its workspace")
    expected = json.loads(args.expectations_file.read_text())
    if len(expected) != 2 or len({item["operation_id"] for item in expected}) != 2:
        parser.error("requires two distinct baseline/intervention expectations")
    credential = read_private_credential(args.owner_credential_file)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(
            context=ssl.create_default_context(cafile=str(args.control_ca_file))
        ),
        NoRedirect(),
    )

    def request(path, body=None, key=None, raw=False):
        if not args.control_url.startswith("https://"):
            raise ValueError("acceptance requires explicit HTTPS control URL")
        headers = {"Authorization": "Bearer " + credential}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        req = urllib.request.Request(
            args.control_url.rstrip("/") + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        with opener.open(req, timeout=5) as response:
            data = response.read(24_000_001)
        if len(data) > 24_000_000:
            raise ValueError("acceptance response exceeds bound")
        return data if raw else json.loads(data)

    process = None
    interrupted = False
    command = json.loads(args.worker_command_file.read_text()) if args.worker_command_file else None
    if command and (
        not isinstance(command, list) or not all(isinstance(item, str) for item in command)
    ):
        parser.error(
            "worker command must be JSON argv containing credential references, not values"
        )
    try:
        if command:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline:
            manifests = request(f"/projects/{args.project_id}/artifact-manifests?limit=200")[
                "artifact_manifests"
            ]
            found = {item["operation_id"]: item for item in manifests}
            if args.interrupt_operation and not interrupted:
                if args.interrupt_operation in found:
                    raise ValueError(
                        "manifest completed before interruption; "
                        "interruption acceptance NOT verified"
                    )
                spool = args.workspace / "output-delivery.sqlite3"
                if spool.exists():
                    with sqlite3.connect(f"file:{spool}?mode=ro", uri=True) as db:
                        row = db.execute(
                            "SELECT outputs,receipt FROM deliveries WHERE operation=?",
                            (args.interrupt_operation,),
                        ).fetchone()
                    if row and row[0] is not None and row[1] is None:
                        process.terminate()
                        process.wait(timeout=10)
                        process = subprocess.Popen(
                            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        interrupted = True
            if all(item["operation_id"] in found for item in expected):
                break
            if process and process.poll() is not None:
                raise ValueError("ordinary worker exited before output retention")
            time.sleep(0.05)
        else:
            raise TimeoutError("ordinary-service output retention did not finish")
        observed = []
        for item in expected:
            manifest = found[item["operation_id"]]
            entries = manifest["artifacts"]
            if (
                manifest["status"] != "quarantined"
                or manifest["classification"] != "unclassified"
                or manifest["late"] is not item["late"]
                or len(entries) != 1
                or entries[0]["sha256"] != item["sha256"]
                or entries[0]["size"] != item["size"]
            ):
                raise ValueError(
                    "ordinary-service manifest does not match fixture provenance/bytes"
                )
            if not args.human_release_fixture_outputs:
                raise ValueError(
                    "byte retrieval needs explicit human fixture-output custody release flag"
                )
            request(
                f"/artifact-manifests/{manifest['id']}/custody-releases",
                {
                    "expected_manifest_digest": manifest["manifest_digest"],
                    "expected_revision": 0,
                    "action": "release",
                    "rationale": (
                        "Human-authorized synthetic baseline/intervention acceptance byte check; "
                        "not scientific acceptance"
                    ),
                },
                "ordinary-output-acceptance-" + manifest["id"],
            )
            content = request(
                f"/artifact-manifests/{manifest['id']}/artifacts/{entries[0]['id']}/content",
                raw=True,
            )
            if (
                hashlib.sha256(content).hexdigest() != item["sha256"]
                or len(content) != item["size"]
            ):
                raise ValueError("retained storage bytes do not match fixture output")
            observed.append(
                {
                    "operation_id": item["operation_id"],
                    "manifest_id": manifest["id"],
                    "sha256": item["sha256"],
                    "size": len(content),
                    "late": manifest["late"],
                }
            )
        print(
            json.dumps(
                {
                    "mode": "live",
                    "ordinary_service_outputs": observed,
                    "interruption_verified": interrupted,
                },
                sort_keys=True,
            )
        )
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
