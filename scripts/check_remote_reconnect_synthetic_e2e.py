"""Opt-in two-process synthetic remote-workflow/reconnect acceptance fixture.

It starts temporary loopback control and a separate Python worker process.  It
does not start Electron, provision a target, call a provider, or establish
laptop independence; it verifies only the durable control/worker protocol.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from check_control_runtime import prepare, serve

from tessera_control.store import canonical_digest
from tessera_worker.control_channel import HttpControlChannel


def worker(config: dict[str, object]) -> dict[str, object]:
    channel = HttpControlChannel(str(config["origin"]), str(config["token"]), timeout_seconds=2)
    phase = str(config["phase"])
    if phase == "prepare":
        channel.post("/worker/heartbeat", {}, "synthetic-remote-worker-heartbeat")
        lease = channel.post(
            "/worker/leases", {"grant_id": config["grant_id"]}, "synthetic-remote-worker-lease"
        )
        package = channel.post(
            f"/worker/leases/{lease['id']}/package",
            {"expected_epoch": lease["epoch"], "execution_adapter": "local-cpu"},
            "synthetic-remote-worker-package",
        )
        operation = package["operation"]
        heartbeat = {
            "lease_id": lease["id"],
            "expected_epoch": lease["epoch"],
            "observed_state": "running",
        }
        channel.post(
            f"/worker/remote-workflows/{operation['operation_id']}/heartbeat",
            heartbeat,
            "synthetic-remote-worker-running",
        )
        channel.post(
            f"/worker/remote-workflows/{operation['operation_id']}/checkpoints",
            {
                "lease_id": lease["id"],
                "expected_epoch": lease["epoch"],
                "sequence": 1,
                "checkpoint_cursor": "synthetic-1",
                "checkpoint_digest": "a" * 64,
            },
            "synthetic-remote-worker-checkpoint",
        )
        return {
            "operation_id": operation["operation_id"],
            "lease_id": lease["id"],
            "epoch": lease["epoch"],
        }
    identity = dict(config["identity"])
    return channel.post(
        f"/worker/remote-workflows/{identity['operation_id']}/reconnect",
        {
            "lease_id": identity["lease_id"],
            "expected_epoch": identity["epoch"],
            "last_checkpoint_sequence": 1,
            "control_outage": True,
        },
        "synthetic-remote-worker-reconnect",
    )


def _child(config: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(config)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        print(json.dumps(worker(json.loads(sys.argv[2])), sort_keys=True))
        return
    with tempfile.TemporaryDirectory(prefix="tessera-remote-reconnect-") as temporary:
        root = Path(temporary).resolve()
        state, execution, owner, _worker, token, grant = prepare(root, "print('synthetic')")
        with serve(state) as origin:
            identity = _child(
                {"phase": "prepare", "origin": origin, "token": token, "grant_id": grant["id"]}
            )
            reconnect = _child(
                {"phase": "reconnect", "origin": origin, "token": token, "identity": identity}
            )
            if reconnect["state"] != "unknown" or not reconnect["reconciliation_required"]:
                raise RuntimeError("Control outage did not retain unknown reconciliation state.")
            cancel = {"expected_revision": 1}
            execution.revoke_execution_grant(
                owner, grant["id"], "synthetic-cancel", canonical_digest(cancel), 1
            )
            print(
                json.dumps(
                    {
                        "simulation": True,
                        "success_failure": "provider-unverified",
                        "cancel": "requested",
                        "unknown_reconnect": "retained",
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    main()
