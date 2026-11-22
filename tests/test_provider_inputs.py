"""Provider input tickets (ADR 0037 s3), end to end against the real app."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest
from data_class_fixtures import declare_fixture_input_public
from fastapi.testclient import TestClient
from test_chunked_snapshots import FILES as CHUNKED_FILES
from test_chunked_snapshots import _lease as _chunked_lease
from test_input_delivery import _delivery
from test_runtime import ApiChannel

from tessera_control import snapshot_chunks
from tessera_control.api import create_app
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import _connect, canonical_digest
from tessera_worker import chunked_inputs
from tessera_worker.provider import FakeProvider, InputStagingProvider, ProviderDispatch
from tessera_worker.provider_inputs import ProviderInputError, fetch_snapshot, main
from tessera_worker.supervisor import Lease

ORIGIN = "https://control.test"
AUTH = {"Authorization": "Bearer worker-token"}


@pytest.fixture(autouse=True)
def tiny_chunks(monkeypatch):
    monkeypatch.setattr(snapshot_chunks, "CHUNK_BYTES", 8)
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", 8)


class _Response:
    def __init__(self, response):
        self.status = response.status_code
        self.headers = {"Content-Type": response.headers.get("content-type", "")}
        self._body = io.BytesIO(response.content)

    def read(self, limit=-1):
        return self._body.read(limit)

    def close(self):
        pass


def make_opener(client, log=None, hook=None):
    def opener(request, timeout):
        path = "/" + request.full_url.split("://", 1)[1].split("/", 1)[1]
        if log is not None:
            log.append(path)
        if hook is not None:
            hook(path)
        response = client.get(path, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "x", {}, io.BytesIO(b"{}"))
        return _Response(response)

    return opener


def _operation(state, worker, lease):
    payload = {"expected_epoch": lease["epoch"]}
    first = RuntimePackageStore(state).execution_package(
        worker, lease["id"], "package", canonical_digest(payload), lease["epoch"]
    )[1]
    operation_id = first["operation"]["operation_id"]
    # ADR 0039: a route-less provider operation is third-party compute (fail closed),
    # so these fixtures stage a public input; the refusal is tested separately.
    declare_fixture_input_public(state, operation_id)
    return operation_id


def _issue(client, operation_id, lease, headers=AUTH):
    return client.post(
        f"/worker/external-operations/{operation_id}/input-tickets",
        headers=headers,
        json={"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
    )


@pytest.fixture
def legacy(tmp_path):
    state, execution, owner, worker, grant, lease = _delivery(tmp_path)
    client = TestClient(create_app(state))
    op = _operation(state, worker, lease)
    return state, owner, worker, grant, lease, client, op


@pytest.fixture
def chunked(tmp_path):
    state, owner, project, worker, snapshot, grant, lease = _chunked_lease(tmp_path)
    client = TestClient(create_app(state))
    op = _operation(state, worker, lease)
    return state, owner, worker, grant, lease, client, op


def _staging(tmp_path):
    parent = tmp_path / "stage"
    parent.mkdir(mode=0o700)
    return parent


def _rows(state):
    with _connect(state / "tessera.sqlite3") as db:
        return [dict(r) for r in db.execute("SELECT * FROM provider_input_tickets")]


def test_legacy_snapshot_end_to_end(tmp_path, legacy):
    state, owner, worker, grant, lease, client, op = legacy
    issued = _issue(client, op, lease)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["snapshot_digest"] == grant["snapshot_digest"]
    assert body["manifest_path"] == "/provider-inputs/manifest"
    log: list[str] = []
    snap = fetch_snapshot(
        ORIGIN,
        body["token"],
        _staging(tmp_path),
        expected_snapshot_digest=grant["snapshot_digest"],
        opener=make_opener(client, log),
    )
    assert (snap.directory / "input.txt").read_bytes() == b"immutable input"
    assert stat.S_IMODE((snap.directory / "input.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE(snap.directory.stat().st_mode) == 0o555
    assert log[0] == "/provider-inputs/manifest" and len(log) == 3  # 15 bytes -> 2 chunks
    (row,) = _rows(state)
    assert row["chunks_served"] == 2 and row["last_served_at"]
    assert row["token_hash"] == hashlib.sha256(body["token"].encode()).hexdigest()


def test_chunked_snapshot_end_to_end(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    body = _issue(client, op, lease).json()
    snap = fetch_snapshot(
        ORIGIN,
        body["token"],
        _staging(tmp_path),
        expected_snapshot_digest=grant["snapshot_digest"],
        opener=make_opener(client),
    )
    for path, data in CHUNKED_FILES.items():
        assert (snap.directory / path).read_bytes() == data
    assert snap.file_count == len(CHUNKED_FILES)
    assert _rows(state)[0]["chunks_served"] == 4


def test_wrong_or_missing_token_is_the_uniform_401(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    _issue(client, op, lease)
    for headers in (
        {},
        {"Authorization": "Ticket nope-nope-nope-nope-nope"},
        {"Authorization": "Ticket " + "A" * 43},
        {"Authorization": "Bearer worker-token"},
        {"Authorization": "Ticket "},
    ):
        for path in ("/provider-inputs/manifest", "/provider-inputs/chunks/0"):
            r = client.get(path, headers=headers)
            assert r.status_code == 401 and r.json() == {
                "detail": "Provider input ticket is not valid."
            }


def _first_chunk(client, token):
    return client.get("/provider-inputs/chunks/0", headers={"Authorization": f"Ticket {token}"})


def _manifest(client, token):
    return client.get("/provider-inputs/manifest", headers={"Authorization": f"Ticket {token}"})


def test_reissue_revokes_old_token(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    old = _issue(client, op, lease).json()["token"]
    assert _first_chunk(client, old).status_code == 200
    new = _issue(client, op, lease).json()["token"]
    assert new != old
    assert _first_chunk(client, old).status_code == 401
    assert _first_chunk(client, new).status_code == 200
    with _connect(state / "tessera.sqlite3") as db:
        payloads = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='execution.provider_input_ticket_issued' ORDER BY cursor"
            )
        ]
    assert [p["revoked_previous"] for p in payloads] == [0, 1]


def test_explicit_revoke(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    r = client.post(
        f"/worker/external-operations/{op}/input-tickets/revoke",
        headers=AUTH,
        json={"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
    )
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert _manifest(client, token).status_code == 401
    with _connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM events "
                "WHERE event_type='execution.provider_input_ticket_revoked'"
            ).fetchone()[0]
            == 1
        )


def test_release_revoked_mid_fetch_stops_next_chunk(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    body = _issue(client, op, lease).json()

    def hook(path):
        if path.endswith("/chunks/1"):
            with _connect(state / "tessera.sqlite3") as db:
                db.execute("UPDATE snapshot_releases SET status='revoked'")

    parent = _staging(tmp_path)
    with pytest.raises(ProviderInputError, match="HTTP 401"):
        fetch_snapshot(
            ORIGIN,
            body["token"],
            parent,
            expected_snapshot_digest=grant["snapshot_digest"],
            opener=make_opener(client, hook=hook),
        )
    assert list(parent.iterdir()) == []


def _mutate_and_expect_401(client, state, token, sql, args=()):
    assert _manifest(client, token).status_code == 200
    with _connect(state / "tessera.sqlite3") as db:
        db.execute(sql, args)
    for r in (_manifest(client, token), _first_chunk(client, token)):
        assert r.status_code == 401
        assert r.json() == {"detail": "Provider input ticket is not valid."}


def test_expired_ticket(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    _mutate_and_expect_401(
        client,
        state,
        token,
        "UPDATE provider_input_tickets SET expires_at='2000-01-01T00:00:00Z'",
    )


def test_grant_revoked(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    _mutate_and_expect_401(
        client,
        state,
        token,
        "UPDATE execution_grants SET status='cancel_requested' WHERE id=?",
        (grant["id"],),
    )


def test_lease_epoch_changed(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    _mutate_and_expect_401(
        client, state, token, "UPDATE execution_leases SET epoch=epoch+1 WHERE id=?", (lease["id"],)
    )


def test_lease_expired(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    _mutate_and_expect_401(
        client,
        state,
        token,
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (lease["id"],),
    )


def test_operation_cancel_requested_and_exited(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    _mutate_and_expect_401(
        client,
        state,
        token,
        "UPDATE external_operations SET desired_state='cancel_requested' WHERE operation_id=?",
        (op,),
    )
    assert _issue(client, op, lease).status_code == 409
    with _connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE external_operations SET desired_state='admitted'")
    assert _issue(client, op, lease).status_code == 201
    with _connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE external_operations SET observed_state='exited'")
    assert _issue(client, op, lease).status_code == 409


def test_only_the_leaseholding_worker_can_issue(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    owner_token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    for headers in (
        {"Authorization": f"Bearer {owner_token}"},
        {"Authorization": "Bearer unknown-token"},
        {},
    ):
        assert _issue(client, op, lease, headers).status_code == 401
    from test_execution import _worker

    project_id = grant["project_id"]
    _worker(
        state,
        {"org_id": worker["org_id"], "credential_id": _owner_cred(state), "id": _owner_id(state)},
        project_id,
        "other-token",
    )
    other = _issue(client, op, lease, {"Authorization": "Bearer other-token"})
    assert other.status_code in {403, 404}
    wrong_lease = client.post(
        f"/worker/external-operations/{op}/input-tickets",
        headers=AUTH,
        json={"lease_id": "nope", "expected_epoch": lease["epoch"]},
    )
    assert wrong_lease.status_code == 409
    assert _rows(state) == []
    for bad in ({"lease_id": lease["id"]}, {"lease_id": lease["id"], "expected_epoch": True}):
        assert (
            client.post(
                f"/worker/external-operations/{op}/input-tickets", headers=AUTH, json=bad
            ).status_code
            == 422
        )


def _owner_id(state):
    with _connect(state / "tessera.sqlite3") as db:
        return db.execute("SELECT id FROM principals WHERE role='human' LIMIT 1").fetchone()[0]


def _owner_cred(state):
    with _connect(state / "tessera.sqlite3") as db:
        return db.execute(
            "SELECT id FROM credentials WHERE principal_id=? LIMIT 1", (_owner_id(state),)
        ).fetchone()[0]


def test_tampered_chunk_is_409_and_fetch_leaves_no_tree(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    body = _issue(client, op, lease).json()
    with _connect(state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE snapshot_chunks SET content=x'0000000000000000' WHERE seq=1 "
            "AND length(content)=8"
        )
    assert _first_chunk(client, body["token"]).status_code == 200
    r = client.get(
        "/provider-inputs/chunks/1", headers={"Authorization": f"Ticket {body['token']}"}
    )
    assert r.status_code == 409
    parent = _staging(tmp_path)
    with pytest.raises(ProviderInputError, match="HTTP 409"):
        fetch_snapshot(
            ORIGIN,
            body["token"],
            parent,
            expected_snapshot_digest=grant["snapshot_digest"],
            opener=make_opener(client),
        )
    assert list(parent.iterdir()) == []


def test_server_digest_differing_from_plan_is_refused_before_chunks(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    body = _issue(client, op, lease).json()
    log: list[str] = []
    with pytest.raises(ProviderInputError, match="approved plan"):
        fetch_snapshot(
            ORIGIN,
            body["token"],
            _staging(tmp_path),
            expected_snapshot_digest="0" * 64,
            opener=make_opener(client, log),
        )
    assert log == ["/provider-inputs/manifest"]


def test_client_transport_rules(tmp_path):
    parent = _staging(tmp_path)
    for origin in ("http://control.example", "ftp://x", "https://u:p@x", "https://x/path"):
        with pytest.raises(ProviderInputError):
            fetch_snapshot(origin, "t" * 20, parent, expected_snapshot_digest="0" * 64)

    class Redirecting:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self, n=-1):
            return b"{}"

        def close(self):
            pass

    with pytest.raises(ProviderInputError, match="unexpected"):
        fetch_snapshot(
            "http://127.0.0.1:1",
            "t" * 20,
            parent,
            expected_snapshot_digest="0" * 64,
            opener=lambda request, timeout: Redirecting(),
        )

    class Huge(Redirecting):
        headers = {"Content-Type": "application/json"}

        def read(self, n=-1):
            return b"x" * (n if n > 0 else 10)

    with pytest.raises(ProviderInputError, match="byte bound"):
        fetch_snapshot(
            "https://x.test",
            "t" * 20,
            parent,
            expected_snapshot_digest="0" * 64,
            opener=lambda request, timeout: Huge(),
        )


def test_cli_requires_private_token_file(tmp_path, capsys):
    token = tmp_path / "token"
    token.write_text("t" * 20)
    os.chmod(token, 0o644)
    args = [
        "--origin", ORIGIN, "--token-file", str(token),
        "--parent", str(_staging(tmp_path)), "--expected-digest", "0" * 64,
    ]  # fmt: skip
    assert main(args) == 1
    assert "0600" in capsys.readouterr().err


def test_secrecy_token_is_nowhere_in_the_database(tmp_path, chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    fetch_snapshot(
        ORIGIN,
        token,
        _staging(tmp_path),
        expected_snapshot_digest=grant["snapshot_digest"],
        opener=make_opener(client),
    )
    needle = token.encode()
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in db.execute(f'SELECT * FROM "{table}"'):
                for value in row:
                    if isinstance(value, str):
                        assert token not in value, table
                    elif isinstance(value, bytes):
                        assert needle not in value, table
        assert (
            db.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%input-tickets%'"
            ).fetchone()[0]
            == 0
        )
    raw = (state / "tessera.sqlite3").read_bytes()
    assert needle not in raw


def test_dispatch_with_fake_provider_stages_inputs_then_submits(tmp_path, legacy):
    state, owner, worker, grant, lease, client, op = legacy
    with _connect(state / "tessera.sqlite3") as db:
        manifest_json = db.execute(
            "SELECT manifest_json FROM source_snapshots WHERE id=?", (grant["snapshot_id"],)
        ).fetchone()[0]
    canonical = json.dumps(json.loads(manifest_json), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == grant["snapshot_digest"]
    from tessera_worker.cpu import CpuOperation

    operation = CpuOperation(
        operation_id=op,
        image="registry.test/x@sha256:" + "a" * 64,
        argv=("true",),
        plan_digest=grant["plan_digest"],
        max_duration_seconds=60,
        input_directory=str(tmp_path / "in"),
        input_manifest_json=canonical,
    )
    provider = FakeProvider(input_opener=make_opener(client), staging_parent=_staging(tmp_path))
    assert isinstance(provider, InputStagingProvider)
    directory = tmp_path / "dispatch"
    directory.mkdir(mode=0o700)
    dispatch = ProviderDispatch(directory, ApiChannel(state), provider, input_origin=ORIGIN)
    token_seen: list[str] = []
    original = provider.stage_inputs

    def spy(operation_id, lookup_key, origin, token, digest):
        token_seen.append(token)
        return original(operation_id, lookup_key, origin, token, digest)

    provider.stage_inputs = spy  # type: ignore[method-assign]
    lease_obj = Lease(
        lease["id"], op, grant["plan_digest"], lease["epoch"],
        datetime.now(UTC) + timedelta(minutes=5), 1, grant["project_id"],
    )  # fmt: skip
    dispatch.before_start(operation, lease_obj)
    assert provider.submit_calls == [op]
    staged = provider.staged[operation.container_name]
    assert staged["snapshot_digest"] == grant["snapshot_digest"] and staged["file_count"] == 1
    assert provider.operations[operation.container_name].payload["staged_inputs"] == {
        "snapshot_digest": grant["snapshot_digest"],
        "file_count": 1,
    }
    assert len(token_seen) == 1
    journal = (directory / "provider-dispatch.sqlite3").read_bytes()
    assert token_seen[0].encode() not in journal
    with sqlite3.connect(directory / "provider-dispatch.sqlite3") as db:
        events = [r[0] for r in db.execute("SELECT event FROM input_staging_journal ORDER BY id")]
    assert events == ["provider_input_ticket_requested", "provider_inputs_staged"]


def test_pull_reads_hold_no_write_lock_and_verify_metadata_once(tmp_path, chunked, monkeypatch):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    checks: list[int] = []
    real_check = snapshot_chunks.check_chunk_metadata
    monkeypatch.setattr(
        snapshot_chunks,
        "check_chunk_metadata",
        lambda *args, **kwargs: checks.append(1) or real_check(*args, **kwargs),
    )
    competing: list[str] = []
    from tessera_control import provider_inputs

    real_read = provider_inputs.read_chunk

    def read_while_a_writer_runs(connection, snapshot_id, seq):
        # A reader is mid-transaction; a second connection must still get the write lock.
        other = _connect(state / "tessera.sqlite3")
        try:
            other.execute("PRAGMA busy_timeout = 100")
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            competing.append("ok")
        finally:
            other.close()
        return real_read(connection, snapshot_id, seq)

    monkeypatch.setattr(provider_inputs, "read_chunk", read_while_a_writer_runs)
    fetch_snapshot(
        ORIGIN,
        token,
        _staging(tmp_path),
        expected_snapshot_digest=grant["snapshot_digest"],
        opener=make_opener(client),
    )
    (row,) = _rows(state)
    assert len(competing) == row["chunks_served"] > 1
    # One O(chunks) metadata verification for the manifest and every chunk of this ticket.
    assert len(checks) == 1


def test_a_chunk_is_not_served_if_the_ticket_is_revoked_before_it_is_counted(
    tmp_path, chunked, monkeypatch
):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    from tessera_control import provider_inputs

    real_record = provider_inputs.ProviderInputStore._record_served

    def revoke_then_record(self, token_hash, count=True):
        with _connect(state / "tessera.sqlite3") as db:
            db.execute("UPDATE provider_input_tickets SET revoked_at = '2000-01-01T00:00:00Z'")
        return real_record(self, token_hash, count)

    monkeypatch.setattr(provider_inputs.ProviderInputStore, "_record_served", revoke_then_record)
    response = client.get("/provider-inputs/chunks/0", headers={"Authorization": f"Ticket {token}"})
    assert response.status_code == 401
    assert _rows(state)[0]["chunks_served"] == 0


def test_manifest_pull_rechecks_revocation_without_counting(tmp_path, chunked, monkeypatch):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    from tessera_control import provider_inputs

    real_record = provider_inputs.ProviderInputStore._record_served

    def revoke_then_record(self, token_hash, count=True):
        with _connect(state / "tessera.sqlite3") as db:
            db.execute("UPDATE provider_input_tickets SET revoked_at = '2000-01-01T00:00:00Z'")
        return real_record(self, token_hash, count)

    ok = client.get("/provider-inputs/manifest", headers={"Authorization": f"Ticket {token}"})
    assert ok.status_code == 200 and _rows(state)[0]["chunks_served"] == 0
    monkeypatch.setattr(provider_inputs.ProviderInputStore, "_record_served", revoke_then_record)
    response = client.get("/provider-inputs/manifest", headers={"Authorization": f"Ticket {token}"})
    assert response.status_code == 401


def test_index_cache_is_a_locked_bounded_lru(tmp_path, chunked, monkeypatch):
    import threading

    from tessera_control import provider_inputs

    state, owner, worker, grant, lease, client, op = chunked
    store = client.app.state.provider_inputs
    monkeypatch.setattr(provider_inputs.ProviderInputStore, "_INDEX_CACHE_MAX", 2)
    tokens = [_issue(client, op, lease).json()["token"] for _ in range(4)]  # reissue revokes
    errors = []

    def pull(token):
        try:
            for _ in range(3):
                try:
                    store.read_manifest(token)
                except provider_inputs.TicketInvalid:
                    pass  # revoked by a later reissue
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=pull, args=(t,)) for t in tokens]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(store._ticket_index_cache) <= 2


def test_retention_purged_mid_ticket_stops_next_pull(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    assert _manifest(client, token).status_code == 200  # primes the verified-index cache
    with _connect(state / "tessera.sqlite3") as db:
        db.execute("DELETE FROM snapshot_chunks WHERE seq = (SELECT max(seq) FROM snapshot_chunks)")
    for r in (_manifest(client, token), _first_chunk(client, token)):
        assert r.status_code == 401
        assert r.json() == {"detail": "Provider input ticket is not valid."}


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00+00:00'",
        "UPDATE snapshot_releases SET expires_at='2000-01-01T00:00:00+00:00'",
    ],
)
def test_record_served_rechecks_lease_and_release_expiry(chunked, monkeypatch, sql):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    from tessera_control import provider_inputs

    real_record = provider_inputs.ProviderInputStore._record_served

    def expire_then_record(self, token_hash, count=True):
        with _connect(state / "tessera.sqlite3") as db:
            db.execute(sql)
        return real_record(self, token_hash, count)

    monkeypatch.setattr(provider_inputs.ProviderInputStore, "_record_served", expire_then_record)
    response = client.get("/provider-inputs/chunks/0", headers={"Authorization": f"Ticket {token}"})
    assert response.status_code == 401
    assert _rows(state)[0]["chunks_served"] == 0


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE external_operations SET observed_state='exited'",
        "UPDATE external_operations SET desired_state='cancel_requested'",
    ],
)
def test_record_served_rechecks_operation_state(chunked, monkeypatch, sql):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    from tessera_control import provider_inputs

    real_record = provider_inputs.ProviderInputStore._record_served

    def change_then_record(self, token_hash, count=True):
        with _connect(state / "tessera.sqlite3") as db:
            db.execute(sql)
        return real_record(self, token_hash, count)

    monkeypatch.setattr(provider_inputs.ProviderInputStore, "_record_served", change_then_record)
    response = client.get("/provider-inputs/chunks/0", headers={"Authorization": f"Ticket {token}"})
    assert response.status_code == 401
    assert _rows(state)[0]["chunks_served"] == 0


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM source_snapshots",
        "UPDATE source_snapshots SET digest='sha256:' || digest",
    ],
)
def test_cached_manifest_pull_refused_after_snapshot_row_changes(chunked, sql):
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    assert _manifest(client, token).status_code == 200  # primes the cache
    with _connect(state / "tessera.sqlite3") as db:
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute(sql)
    response = _manifest(client, token)
    assert response.status_code == 401
    assert response.json() == {"detail": "Provider input ticket is not valid."}


# ------------------------------------------------- ADR 0039 data class fences


def _make_private(state, snapshot_id, origin="default"):
    from data_class_fixtures import restore_class_guards, rewrite_class_row

    with sqlite3.connect(state / "tessera.sqlite3") as db:
        rewrite_class_row(db, snapshot_id)
        db.execute(
            "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,recorded_at) "
            "VALUES (?,'private_derived',?,'2026-01-01T00:00:00Z')",
            (snapshot_id, origin),
        )
        restore_class_guards(db)


def test_route_less_operation_with_a_private_input_is_refused_a_ticket(tmp_path, chunked):
    """No route target is treated as third-party compute (fail closed)."""
    state, owner, worker, grant, lease, client, op = chunked
    _make_private(state, grant["snapshot_id"])
    refused = _issue(client, op, lease)
    assert refused.status_code == 403 and "public" in refused.json()["detail"]
    assert _rows(state) == []


def test_every_pull_rechecks_the_input_data_class(tmp_path, chunked):
    """A ticket minted while the input was public stops working once it is not."""
    state, owner, worker, grant, lease, client, op = chunked
    token = _issue(client, op, lease).json()["token"]
    headers = {"Authorization": "Ticket " + token}
    assert client.get("/provider-inputs/manifest", headers=headers).status_code == 200
    assert client.get("/provider-inputs/chunks/0", headers=headers).status_code == 200
    _make_private(state, grant["snapshot_id"], origin="migrated")
    for path in ("/provider-inputs/manifest", "/provider-inputs/chunks/1"):
        r = client.get(path, headers=headers)
        assert r.status_code == 401 and r.json() == {
            "detail": "Provider input ticket is not valid."
        }


def test_v62_migration_revokes_tickets_minted_before_the_guard(tmp_path, chunked):
    from tessera_control.migrations import LATEST_SCHEMA_VERSION
    from tessera_control.store import migrate_state

    state, owner, worker, grant, lease, client, op = chunked
    _issue(client, op, lease)
    assert [r["revoked_at"] for r in _rows(state)] == [None]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        for trigger in ("no_update", "no_delete", "no_replace"):
            db.execute(f"DROP TRIGGER snapshot_data_classes_{trigger}")
        db.execute("DROP TABLE snapshot_data_classes")
        db.execute("UPDATE schema_meta SET schema_version=61")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    assert all(r["revoked_at"] is not None for r in _rows(state))
