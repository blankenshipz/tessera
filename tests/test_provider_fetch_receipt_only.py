"""ADR 0097/0116: after a provider-side fetch only a receipt returns; the bytes never reach control.

Fakes only (no network). Covers the local worker (supervisor fetch, report endpoint, recorded event,
campaign receipt) and the Salad in-container runtime (custody uploads). The receipt is the
platform's: its pins come from the approved spec and its numbers only from an authenticated,
plan-checked provider report, never from stage code.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_execution as fx
from fastapi.testclient import TestClient
from test_public_fetch_local_worker import RecordingChannel
from test_sandbox_egress_control import _world
from test_supervisor import FakeAdapter

from tessera_control.api import create_app
from tessera_control.campaign_models import NormalInput
from tessera_control.campaign_receipts import _local_fetched, public_fetch_entry
from tessera_control.public_fetch_report import public_fetch_for_operation
from tessera_control.store import _connect
from tessera_worker.public_fetch import fetch_public_files
from tessera_worker.runtime import CpuRuntime
from tessera_worker.supervisor import SupervisorError

# Distinctive bytes: any copy of them under control state would be found by a byte scan.
BODY = b"PINNED-PUBLIC-WEIGHTS-" + hashlib.sha256(b"receipt-only").hexdigest().encode()
DIGEST = hashlib.sha256(BODY).hexdigest()
URL = "https://example.org/models/w.bin"
PLAN = {
    "hosts": ["example.org"],
    "files": [
        {"input": "weights", "name": "w.bin", "url": URL, "sha256": DIGEST, "max_bytes": 1000}
    ],
    "max_bytes_total": 1000,
    "cache": False,
}
ROW_KEYS = {"input", "name", "source_host", "sha256", "bytes", "seconds", "cached"}


class BodyRecordingChannel(RecordingChannel):
    """Records every body the worker posts to control, as sent."""

    def __init__(self, state, log):
        super().__init__(state, log)
        self.bodies: list[str] = []

    def post(self, path, body, key):
        self.bodies.append(json.dumps(body, sort_keys=True))
        return super().post(path, body, key)


def _fetcher(body: bytes = BODY, rows=None):
    def opener(url):
        return 200, {}, iter([body])

    def fetch(files, **kwargs):
        got = fetch_public_files(files, opener=opener, **kwargs)
        return got if rows is None else rows(got)

    return fetch


def _runtime(tmp_path, state, worker, log, **fetch):
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700, exist_ok=True)
    public = tmp_path / "public"
    public.mkdir(mode=0o700, exist_ok=True)
    channel = BodyRecordingChannel(state, log)
    runtime = CpuRuntime(
        tmp_path / "runtime",
        inputs,
        channel,
        FakeAdapter(),
        worker_id=worker["id"],
        public_root=public,
        public_fetcher=_fetcher(**fetch),
    )
    return runtime, channel, public


def _all_bytes_under(root: Path) -> list[tuple[Path, bytes]]:
    return [(p, p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()]


# -- local worker: bytes stay on the worker, only rows reach control ---------------------------


def test_local_fetch_sends_only_rows_and_control_state_holds_no_file_bytes(tmp_path):
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", {"public_fetch": PLAN})
    log: list[str] = []
    runtime, channel, public = _runtime(tmp_path, state, worker, log)
    with runtime:
        started = runtime.start(lease["id"], lease["epoch"])
        runtime.report(started)
        operation_id = runtime.operation.operation_id
    # The bytes exist on the worker (mounted read-only into the container) ...
    assert (public / f"tessera-public-{operation_id}" / "weights" / "w.bin").read_bytes() == BODY
    # ... but no body the worker posted carries them, nor the URL's path.
    assert channel.bodies and "public-fetch" in log
    for sent in channel.bodies:
        assert BODY.decode() not in sent and "/models/w.bin" not in sent
    # Nothing under the control plane's state (database, WAL, artifact store) holds them.
    for path, data in _all_bytes_under(state):
        assert BODY not in data, path
    with _connect(state / "tessera.sqlite3") as connection:
        event = public_fetch_for_operation(connection, operation_id)
    assert set(event) == {"operation_id", "files", "hosts", "via_control"}
    assert event["via_control"] is False and event["hosts"] == ["example.org"]
    (row,) = event["files"]
    assert set(row) == ROW_KEYS
    assert (row["source_host"], row["sha256"], row["bytes"]) == ("example.org", DIGEST, len(BODY))


def _report_body(lease, **changes):
    row = {
        "input": "weights",
        "name": "w.bin",
        "source_host": "example.org",
        "sha256": DIGEST,
        "bytes": len(BODY),
        "seconds": 0.1,
        "cached": False,
    } | changes
    return {"lease_id": lease["id"], "expected_epoch": lease["epoch"], "files": [row]}


def _post(state, path, body, key, token="worker-token"):
    client = TestClient(create_app(state))
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    return client.post(path, json=body, headers=headers)


def test_the_report_endpoint_admits_no_extra_field_so_content_cannot_ride_along(tmp_path):
    state, _, _, _, _, lease, op = _world(tmp_path / "w", {"public_fetch": PLAN})
    path = f"/worker/operations/{op['package']['operation_id']}/public-fetch"
    for index, extra in enumerate(
        [{"content": BODY.decode()}, {"url": URL}, {"error": BODY.decode()}]
    ):
        response = _post(state, path, _report_body(lease, **extra), f"extra-{index}")
        assert response.status_code == 422
        assert BODY.decode() not in response.text
    extra_top = _report_body(lease) | {"content": BODY.decode()}
    assert _post(state, path, extra_top, "extra-top").status_code == 422
    for p, data in _all_bytes_under(state):
        assert BODY not in data, p


def test_a_report_cannot_be_forged_by_another_worker_a_stale_epoch_or_a_partial_set(tmp_path):
    state, owner, _, project, _, lease, op = _world(tmp_path / "w", {"public_fetch": PLAN})
    path = f"/worker/operations/{op['package']['operation_id']}/public-fetch"
    fx._worker(state, owner, project["id"], "other-worker-token")
    assert _post(state, path, _report_body(lease), "o", "other-worker-token").status_code == 404
    stale = _report_body(lease) | {"expected_epoch": lease["epoch"] + 1}
    assert _post(state, path, stale, "stale").status_code == 409
    other_lease = _report_body(lease) | {"lease_id": "lease-not-this-one"}
    assert _post(state, path, other_lease, "lease").status_code == 409
    extra = _report_body(lease)
    extra["files"].append(extra["files"][0] | {"name": "x.bin"})
    assert _post(state, path, extra, "extra-file").status_code == 409
    renamed = _report_body(lease, name="other.bin")
    assert _post(state, path, renamed, "renamed").status_code == 409
    with _connect(state / "tessera.sqlite3") as connection:
        assert public_fetch_for_operation(connection, op["package"]["operation_id"]) is None
    assert _post(state, path, _report_body(lease), "good").status_code == 201


# -- local worker: a skipped or incomplete fetch fails closed before anything runs -------------


def test_a_fetch_that_returns_fewer_rows_than_pinned_fails_before_inputs(tmp_path):
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", {"public_fetch": PLAN})
    log: list[str] = []
    runtime, channel, public = _runtime(tmp_path, state, worker, log, rows=lambda got: [])
    with runtime, pytest.raises(SupervisorError, match="incomplete result") as error:
        runtime.start(lease["id"], lease["epoch"])
    assert "inputs" not in log and "public-fetch" not in log
    assert list(public.iterdir()) == []
    assert BODY.decode() not in str(error.value) and URL not in str(error.value)


def test_a_failed_fetch_names_only_a_code_never_the_bytes_or_the_url(tmp_path):
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", {"public_fetch": PLAN})
    log: list[str] = []
    runtime, _, public = _runtime(tmp_path, state, worker, log, body=BODY + b"-tampered")
    with runtime, pytest.raises(SupervisorError) as error:
        runtime.start(lease["id"], lease["epoch"])
    assert str(error.value) == "Public fetch failed: digest_mismatch"
    assert list(public.iterdir()) == []
    for _, data in _all_bytes_under(state):
        assert BODY not in data


def test_a_descriptor_that_skips_or_contradicts_the_fetch_is_refused(tmp_path):
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", {"public_fetch": PLAN})
    log: list[str] = []
    runtime, _, _ = _runtime(tmp_path, state, worker, log)
    with runtime:
        runtime.start(lease["id"], lease["epoch"])
        descriptor = json.loads(json.dumps(runtime.descriptor))
        skipped = {k: v for k, v in descriptor.items() if k != "public_fetch"}
        with pytest.raises(SupervisorError, match="does not match its package"):
            runtime._operation(skipped)
        other = json.loads(json.dumps(descriptor))
        other["public_fetch"]["rows"][0]["sha256"] = "f" * 64
        with pytest.raises(SupervisorError, match="contradicts its plan"):
            runtime._operation(other)
        dropped = json.loads(json.dumps(descriptor))
        dropped["public_fetch"]["rows"] = []
        with pytest.raises(SupervisorError, match="contradicts its plan"):
            runtime._operation(dropped)


# -- the campaign receipt: platform-built, exact shape, no URL or content ----------------------


def _stage(redirect_hosts=()):
    source = NormalInput(
        "public_artifact",
        "weights",
        public_files=(("w.bin", URL, DIGEST, 1000),),
        redirect_hosts=tuple(redirect_hosts),
    )
    return SimpleNamespace(inputs=(source,))


def test_the_receipt_has_exactly_provider_hosts_files_and_via_control_false():
    rows = [{"url": URL, "sha256": DIGEST, "bytes": len(BODY), "seconds": 0.5}]
    entry = public_fetch_entry("local-docker", _stage(["cdn.example.net"]), rows)
    assert set(entry) == {"provider", "placement", "hosts", "files", "via_control"}
    assert entry["provider"] == "local-docker" and entry["placement"] == "provider_side"
    assert entry["via_control"] is False
    # Egress hosts: the pinned host plus the approved redirect hosts (the allowlist, from the spec).
    assert entry["hosts"] == ["cdn.example.net", "example.org"]
    (file,) = entry["files"]
    assert set(file) == {"name", "source_host", "sha256", "bytes", "seconds", "evidence"}
    assert file == {
        "name": "w.bin",
        "source_host": "example.org",
        "sha256": DIGEST,
        "bytes": len(BODY),
        "seconds": 0.5,
        "evidence": "provider_reported",
    }
    assert URL not in json.dumps(entry)


@pytest.mark.parametrize(
    "row",
    [
        {"url": URL, "sha256": DIGEST, "bytes": "640", "seconds": 1},
        {"url": URL, "sha256": DIGEST, "bytes": True, "seconds": 1},
        {"url": URL, "sha256": DIGEST, "seconds": 1},
        {"url": "https://example.org/other.bin", "sha256": DIGEST, "bytes": 640},
        {"url": URL, "sha256": "f" * 64, "bytes": 640},
        "not a row",
    ],
)
def test_a_malformed_or_unpinned_row_cannot_make_a_file_look_fetched(row):
    entry = public_fetch_entry("salad", _stage(), [row])
    (file,) = entry["files"]
    assert file["evidence"] == "declared_only"
    assert file["bytes"] is None and file["seconds"] is None
    assert file["sha256"] == DIGEST and file["source_host"] == "example.org"  # from the spec


def test_a_row_cannot_override_the_receipts_own_fields_or_add_content():
    row = {
        "url": URL,
        "sha256": DIGEST,
        "bytes": 5,
        "seconds": 1,
        "via_control": True,
        "provider": "elsewhere",
        "source_host": "evil.example",
        "content": BODY.decode(),
    }
    entry = public_fetch_entry("salad", _stage(), [row])
    assert entry["via_control"] is False and entry["provider"] == "salad"
    assert entry["files"][0]["source_host"] == "example.org"
    assert BODY.decode() not in json.dumps(entry)


def test_a_recorded_event_with_extra_content_reaches_the_receipt_as_numbers_only():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (event_type TEXT, payload_json TEXT)")
    payload = {
        "operation_id": "op1",
        "files": [
            {
                "input": "weights",
                "name": "w.bin",
                "source_host": "example.org",
                "sha256": DIGEST,
                "bytes": len(BODY),
                "seconds": 0.5,
                "cached": False,
                "content": BODY.decode(),
            }
        ],
    }
    connection.execute(
        "INSERT INTO events VALUES ('execution.public_fetch_recorded', ?)", (json.dumps(payload),)
    )
    rows = _local_fetched(connection, {"operation_id": "op1"}, _stage())
    assert rows == [
        {"url": URL, "sha256": DIGEST, "bytes": len(BODY), "seconds": 0.5, "cached": False}
    ]
    entry = public_fetch_entry("local-docker", _stage(), rows)
    assert BODY.decode() not in json.dumps(entry) and URL not in json.dumps(entry)


def test_a_missing_report_is_declared_only_never_reported_numbers():
    """Today's behaviour: with no recorded report the receipt is honest (``declared_only``, null
    numbers); it does not fail the attempt. That gap is ADR 0097's listed follow-up."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (event_type TEXT, payload_json TEXT)")
    assert _local_fetched(connection, {"operation_id": "op1"}, _stage()) is None
    (file,) = public_fetch_entry("local-docker", _stage(), None)["files"]
    assert (file["evidence"], file["bytes"], file["seconds"]) == ("declared_only", None, None)


# -- Salad runtime: the fetched files are never uploaded to custody ----------------------------


def test_the_salad_runtime_uploads_rows_but_never_the_fetched_bytes(tmp_path):
    import test_gpu_runtime_v1 as gpu

    request, _ = gpu.make_request()
    custody = gpu.FakeCustody([])

    def entry_runner(entry, work, site, out_dir, dl):
        (Path(out_dir) / "output.json").write_text('{"answer":1}')

    gpu.rt.execute(
        request,
        gpu.deadline(),
        str(tmp_path),
        custody_factory=lambda b, d: custody,
        opener=gpu.opener_for(gpu.BLOBS),
        installer=lambda *a: "site",
        entry_runner=entry_runner,
    )
    assert set(custody.parts) == {"output.json"}
    uploaded = b"".join(data for parts in custody.parts.values() for data in parts.values())
    for (_url, kind, _dest), data in gpu.BLOBS.items():
        if kind != "entry":
            assert data not in uploaded
    rows = json.loads(custody.file("output.json"))["fetched"]
    assert all(set(row) == {"url", "sha256", "bytes", "seconds", "cache"} for row in rows)
