"""Opt-in synthetic control-to-Docker integration over an actual loopback HTTP service.

Uses only temporary state and a locally available pinned image. No research
results, measured costs, automatic enrollment trust, or artifact collection are
claimed. Run with `.venv/bin/python scripts/check_control_runtime.py`.
"""

from __future__ import annotations

import json
import secrets
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from check_cpu_runtime import IMAGE, docker

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Store, canonical_digest, initialize_state
from tessera_control.workspace import capture_workspace, inventory_digest
from tessera_worker.control_channel import HttpControlChannel
from tessera_worker.cpu import DockerCpuAdapter
from tessera_worker.runtime import CpuRuntime

INPUT_PROBE = """
import errno
from pathlib import Path
assert Path('/inputs/input.txt').read_bytes() == b'public synthetic input'
try:
    Path('/inputs/input.txt').write_bytes(b'must fail')
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError('input mount is writable')
"""


@contextmanager
def serve(state: Path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    server = uvicorn.Server(uvicorn.Config(create_app(state), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Synthetic control service did not start.")
            time.sleep(0.02)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Synthetic control service did not stop.")


def prepare(root: Path, code: str):
    state = root / "control"
    initialize_state(state)
    store = Store(state)
    owner_token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    owner = store.authenticate(owner_token)
    project_body = {"name": "Synthetic runtime integration"}
    _, project = store.create_project(
        owner, "project", canonical_digest(project_body), project_body
    )
    token = secrets.token_urlsafe(48)
    credential = {
        "kind": "worker",
        "audience": "worker",
        "project_id": project["id"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    store.issue_credential(owner, "credential", canonical_digest(credential), credential, token)
    worker = store.authenticate_worker(token)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_bytes(b"public synthetic input")
    capture = capture_workspace(workspace)
    digest = inventory_digest(capture.inventory)
    _, snapshot = SnapshotStore(state).adopt_snapshot(
        owner, project["id"], "snapshot", digest, capture
    )
    release = {
        "expected_digest": digest,
        "worker_principal_id": worker["id"],
        "classification": "public",
        "permits_worker_transfer": True,
        "rationale": "Release only fixture-authored public synthetic bytes.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    SnapshotReleaseStore(state).release_snapshot(
        owner, snapshot["id"], "release", canonical_digest(release), release
    )
    draft = {
        "title": "Synthetic input and stop probe",
        "purpose": "Validate platform integration only.",
        "budget_cents": 0,
        "max_duration_seconds": 30,
    }
    _, task = store.create_task(owner, project["id"], "task", canonical_digest(draft), draft)
    revision = {
        **draft,
        "expected_revision": task["revision"],
        "execution_spec": {
            "argv": ["python", "-c", code],
            "input_snapshot_digest": digest,
            "pinned_skill_digests": [],
            "output_paths": [],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1,
        },
    }
    _, task = store.revise_task_plan(
        owner, task["id"], "plan", canonical_digest(revision), revision
    )
    approval = {
        "expected_revision": task["revision"],
        "expected_plan_digest": task["current_plan_digest"],
    }
    _, task = store.approve_task(
        owner,
        task["id"],
        "approve",
        canonical_digest(approval),
        task["revision"],
        task["current_plan_digest"],
    )
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 0,
        "max_concurrency": 1,
    }
    execution.configure_project_execution(
        owner, project["id"], "limits", canonical_digest(limits), limits
    )
    grant_body = {
        "task_id": task["id"],
        "expected_task_revision": task["revision"],
        "expected_plan_digest": task["current_plan_digest"],
        "worker_principal_id": worker["id"],
        "snapshot_id": snapshot["id"],
        "pinned_image": IMAGE,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "max_exposure_cents": 0,
        "currency": "USD",
        "acknowledges_unmeasured_infrastructure": True,
    }
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(grant_body), grant_body
    )
    return state, execution, owner, worker, token, grant


def run_fixture(*, revoke: bool) -> dict:
    code = INPUT_PROBE + ("\nimport time; time.sleep(120)" if revoke else "")
    with tempfile.TemporaryDirectory(prefix="tessera-control-fixture-") as temporary:
        root = Path(temporary).resolve()
        state, execution, owner, worker, token, grant = prepare(root, code)
        inputs = root / "inputs"
        inputs.mkdir(mode=0o700)
        adapter = DockerCpuAdapter(frozenset({IMAGE}), input_root=inputs)
        operation = None
        runtime = None
        try:
            with serve(state) as origin:
                channel = HttpControlChannel(origin, token, timeout_seconds=2)
                lease = channel.post("/worker/leases", {"grant_id": grant["id"]}, "lease")
                with CpuRuntime(
                    root / "runtime", inputs, channel, adapter, worker_id=worker["id"]
                ) as runtime:
                    result = runtime.start(lease["id"], lease["epoch"])
                    operation = runtime.operation
                    runtime.report(result)
                    if revoke:
                        if result.observation.state != "running":
                            raise RuntimeError("Revocation fixture did not reach running state.")
                        execution.revoke_execution_grant(owner, grant["id"], "stop", "stop", 1)
                    deadline = time.monotonic() + 15
                    while result.phase not in {"terminal", "denied"}:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Synthetic runtime exceeded its watchdog.")
                        time.sleep(0.1)
                        result = runtime.tick()
                    observed = adapter.observe(operation)
                    if not observed.termination_confirmed:
                        raise RuntimeError("Provider termination is not confirmed.")
                    if not revoke and observed.exit_code != 0:
                        raise RuntimeError("Live input isolation probe failed.")
                    if revoke and result.reason != "cancelled_lease_denied":
                        raise RuntimeError("Control revocation did not cause the stop.")
                    receipt = runtime.report(result)
                    if not receipt["termination_confirmed"]:
                        raise RuntimeError("Control did not retain the exit observation.")
                    return {
                        "fixture": "control-revocation" if revoke else "control-inputs",
                        "provider_terminated": True,
                        "exit_code": observed.exit_code,
                        "reason": result.reason,
                        "observation_recorded": True,
                    }
        finally:
            if runtime is not None:
                operation = runtime.operation
            if operation is not None:
                observed = adapter.observe(operation)
                if observed.container_id:
                    docker("rm", "--force", observed.container_id)
                elif observed.state != "absent":
                    raise RuntimeError(f"Cleanup unconfirmed for {operation.container_name}.")


def main():
    docker("image", "inspect", IMAGE)
    for revoke in (False, True):
        print(json.dumps(run_fixture(revoke=revoke), sort_keys=True))
    print(
        "Synthetic HTTP/control/Docker integration passed; "
        "output collection and research acceptance remain untested."
    )


if __name__ == "__main__":
    main()
