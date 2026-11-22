"""ADR 0036 phase 2: guardian-held input delivery to a Salad workload (synthetic, no network).

The guardian service is real (real ES256 JWT verification, real SQLite state); control is a
fake HTTP opener or, for the trusted-server tests, the real provider-input store.
"""

import base64
import copy
import hashlib
import io
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

pytest.importorskip("cryptography")
import test_salad_safety_service as tss
from cryptography.hazmat.primitives.asymmetric import ec
from data_class_fixtures import declare_fixture_input_public
from test_salad_http import selection as base_selection
from test_salad_workload_jwt import GROUP, claims, signed

from tessera_control import salad_input_delivery
from tessera_control import salad_safety_service as tss_service
from tessera_control.provider_inputs import TicketInvalid
from tessera_control.salad_input_delivery import GuardianInputs
from tessera_control.salad_server import SaladSafetyClient, input_refresh_threshold
from tessera_control.store import Conflict, Forbidden
from tessera_worker import chunked_inputs
from tessera_worker.chunked_inputs import materialize_chunked_snapshot
from tessera_worker.provider_inputs import ProviderInputError

ORIGIN = "http://127.0.0.1:8765"
TOKEN_A = "ticket-A-" + "a" * 40
TOKEN_B = "ticket-B-" + "b" * 40
FILES = {"a.bin": bytes(range(20)), "z.txt": b"12345678"}


@pytest.fixture
def tiny_chunks(monkeypatch):
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", 8)


def build_snapshot(files=FILES, snapshot_id="snapshot_synthetic"):
    entries = [
        dict(
            relative_path=name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            provenance="imported_unverified",
            classification="unclassified",
        )
        for name, data in sorted(files.items())
    ]
    manifest = dict(
        root="synthetic",
        entries=entries,
        exclusions=[],
        total_bytes=sum(len(d) for d in files.values()),
    )
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    index, chunks = [], {}
    for entry in entries:
        data = files[entry["relative_path"]]
        for offset in range(0, len(data), 8):
            part = data[offset : offset + 8]
            item = dict(
                seq=len(index),
                relative_path=entry["relative_path"],
                offset=offset,
                size=len(part),
                sha256=hashlib.sha256(part).hexdigest(),
            )
            index.append(item)
            chunks[item["seq"]] = part
    return SimpleNamespace(
        id=snapshot_id, manifest=manifest, digest=digest, index=index, chunks=chunks, files=files
    )


class _Response:
    def __init__(self, value):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._body = io.BytesIO(json.dumps(value).encode())

    def read(self, limit=-1):
        return self._body.read(limit)

    def close(self):
        pass


class FakeControl:
    """Stands in for control's /provider-inputs/*: accepts only the currently valid token."""

    def __init__(self, snapshot, valid=(TOKEN_A,)):
        self.snapshot, self.valid, self.requests = snapshot, set(valid), []
        self.tamper_chunk = self.claimed_digest = self.tamper_manifest = None
        self.wrong_size = False

    def __call__(self, request, timeout):
        auth = dict(request.header_items())["Authorization"]
        path = "/" + request.full_url.split("://", 1)[1].split("/", 1)[1]
        self.requests.append((path, auth))
        if auth.removeprefix("Ticket ") not in self.valid:
            raise HTTPError(request.full_url, 401, "x", {}, io.BytesIO(b"{}"))
        snap = self.snapshot
        if path == "/provider-inputs/manifest":
            manifest = copy.deepcopy(snap.manifest)
            if self.tamper_manifest:
                manifest["entries"][0]["sha256"] = "0" * 64
            return _Response(
                dict(
                    snapshot_id=snap.id,
                    digest=self.claimed_digest or snap.digest,
                    manifest=manifest,
                    delivery="chunked",
                    chunks=snap.index,
                )
            )
        seq = int(path.rsplit("/", 1)[1])
        content = snap.chunks[seq]
        if self.tamper_chunk == seq:
            content = bytes(len(content))
        if self.wrong_size and seq == 0:
            content = content + b"x"
        return _Response(
            dict(
                snapshot_id=snap.id,
                **snap.index[seq],
                content_base64=base64.b64encode(content).decode(),
            )
        )


def expires(minutes=30, now=None):
    base = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)
    return (base + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def ticket_for(snapshot, token=TOKEN_A, minutes=30):
    """Ticket lifetimes are measured on the guardian fixture's fake clock (starts at 100)."""
    return dict(
        token=token,
        expires_at=expires(minutes, now=100.0),
        snapshot_id=snapshot.id,
        snapshot_digest=snapshot.digest,
    )


@pytest.fixture
def case(tmp_path, monkeypatch, tiny_chunks):
    """A real guardian, armed and running, whose route approves ``snapshot.digest`` as data."""
    tmp_path.chmod(0o700)
    snapshot = build_snapshot()
    control = FakeControl(snapshot)
    approved = base_selection().model_dump(mode="json")
    approved["manifests"]["data"] = snapshot.digest
    approved = type(base_selection()).model_validate(approved)
    monkeypatch.setattr(tss, "selection", lambda: approved)
    clock = [100.0]
    key = ec.generate_private_key(ec.SECP256R1())
    provider = tss.Provider()
    service = tss.make_service(
        tmp_path,
        lambda: clock[0],
        key,
        provider,
        control_origin=ORIGIN,
        control_opener=control,
    )
    body = tss.reservation()
    service.loop()
    service.admin(body)
    service.admin(
        dict(
            operation="bind", reservation=body["reservation"], provider_id=GROUP, allocation_id=None
        )
    )
    provider.state = "running"
    service.loop()
    request = service._request(body["proposal"]["selection"]["salad"]["execution"]["argv"])
    payload = claims()
    salad = body["proposal"]["selection"]["salad"]
    payload["salad_workload_name"] = f"{salad['organization']}-{salad['project']}-{salad['group']}"
    token = signed(key, payload)
    # Input is served only after the bound workload's begin-once (ADR 0036 phase 2).
    service.workload("/v1/workload/begin-once", {"binding": request}, token)
    yield SimpleNamespace(
        service=service,
        body=body,
        request=request,
        token=token,
        clock=clock,
        provider=provider,
        key=key,
        control=control,
        snapshot=snapshot,
        root=tmp_path,
        payload=payload,
    )
    service.close()


def give(case, ticket=None, **overrides):
    return case.service.admin(
        dict(
            operation="input_ticket",
            reservation=case.body["reservation"],
            input_ticket=ticket if ticket is not None else ticket_for(case.snapshot),
            **overrides,
        )
    )


def ask(case, kind="manifest", token=None, **extra):
    if kind == "chunk" and "offset" not in extra and "length" not in extra:
        # Tiny test chunks are one canonical range: offset 0, the whole stored chunk.
        seq, index = extra.get("seq"), case.snapshot.index
        size = index[seq]["size"] if type(seq) is int and 0 <= seq < len(index) else 1
        extra = dict(extra, offset=0, length=size)
    body = dict(binding=case.request, request=kind, **extra)
    return case.service.workload("/v1/workload/input", body, token or case.token)


def fetch_all(case):
    """Materialize through the guardian exactly as the workload would; return the tree."""
    manifest = ask(case)
    snapshot = {k: manifest[k] for k in ("digest", "manifest", "delivery", "chunks")}
    snapshot["id"] = manifest["snapshot_id"]

    def chunk(seq):
        reply = ask(case, "chunk", seq=seq)
        assert reply["schema"] == "tessera.input-range.v1"
        # One canonical range is the whole tiny chunk; rebuild the worker's chunk reply.
        return dict(
            snapshot_id=reply["snapshot_id"],
            **snapshot["chunks"][seq],
            content_base64=reply["content_base64"],
        )

    return materialize_chunked_snapshot(
        snapshot, chunk, case.root / "tree", expected_snapshot_digest=case.snapshot.digest
    )


def test_bound_workload_streams_a_verified_snapshot(case):
    case.root.joinpath("tree").mkdir(mode=0o700)
    assert give(case)["state"] == "ready"
    tree = fetch_all(case)
    for name, data in FILES.items():
        assert (tree.directory / name).read_bytes() == data
    status = case.service.admin(dict(operation="status", reservation=case.body["reservation"]))
    assert status["input_delivery"]["ticket_held"] is True
    assert status["input_delivery"]["chunks_served"] == len(case.snapshot.index)


def test_ticket_never_appears_in_profile_env_argv_status_database_or_replies(case):
    case.root.joinpath("tree").mkdir(mode=0o700)
    give(case)
    replies = [ask(case), *(ask(case, "chunk", seq=i) for i in range(len(case.snapshot.index)))]
    status = case.service.admin(dict(operation="status", reservation=case.body["reservation"]))
    execution = case.body["proposal"]["selection"]["salad"]["execution"]
    surfaces = [
        json.dumps(case.body, default=str),
        json.dumps(execution),
        json.dumps(execution["env"]),
        json.dumps(execution["argv"]),
        json.dumps(case.request),
        json.dumps(status, default=str),
        json.dumps(replies),
        json.dumps(case.service._row(case.body["reservation"]), default=str),
    ]
    assert not any(TOKEN_A in surface for surface in surfaces)
    # Held in guardian memory only: no durable file contains the bearer.
    case.service.db.commit()
    for path in case.root.rglob("*"):
        if path.is_file():
            assert TOKEN_A.encode() not in path.read_bytes(), path
    # The guardian authenticates to control with the ticket and never echoes it to the workload.
    assert all(auth == "Ticket " + TOKEN_A for _, auth in case.control.requests)


def test_only_the_bound_workload_with_a_valid_jwt_can_read_input(case):
    give(case)
    with pytest.raises(ValueError):
        ask(case, token="not-a-jwt")
    other_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError):
        ask(case, token=signed(other_key, case.payload))  # not signed by Salad's key
    with pytest.raises(ValueError):
        case.service.workload(
            "/v1/workload/input",
            dict(binding=dict(case.request, operation="f" * 32), request="manifest"),
            case.token,
        )
    with pytest.raises((ValueError, KeyError)):  # the HTTP handler maps both to 401
        case.service.workload("/v1/workload/input", dict(request="manifest"), case.token)
    assert case.control.requests == []  # nothing reached control for a refused caller
    assert ask(case)["digest"] == case.snapshot.digest


def test_workload_cannot_install_or_trigger_a_ticket(case):
    with pytest.raises(ValueError):
        case.service.workload(
            "/v1/workload/input",
            dict(binding=case.request, request="manifest", input_ticket=ticket_for(case.snapshot)),
            case.token,
        )
    with pytest.raises(ValueError):  # no ticket was ever handed over on the admin channel
        ask(case)
    assert case.control.requests == []


@pytest.mark.parametrize("kind", ["wrong_machine_token", "reallocated_machine"])
def test_wrong_machine_is_refused(case, kind):
    give(case)
    if kind == "wrong_machine_token":
        payload = dict(case.payload, sub="e" * 32, salad_machine_id="e" * 32)
        with pytest.raises(ValueError, match="workload_identity_rejected"):
            ask(case, token=signed(case.key, payload))
    else:
        case.provider.machine = "e" * 32  # provider now reports a different machine
        case.clock[0] += 5
        case.service.loop()
        with pytest.raises(ValueError):
            ask(case)
    assert case.control.requests == []


def test_expired_deadline_and_missing_heartbeat_are_refused(case):
    give(case)
    assert ask(case)["digest"]
    case.clock[0] += 31  # heartbeat stale (30 s), as for begin-once
    case.service.loop_at = case.clock[0]
    with pytest.raises(ValueError):
        ask(case)
    case.clock[0] = case.body["deadline"] + 1
    with pytest.raises(ValueError):
        ask(case)


def test_recovery_drops_the_ticket_and_refuses_input(case):
    give(case)
    assert ask(case)["digest"]
    case.clock[0] += 31  # stale heartbeat -> loop enters stop-only recovery
    case.service.loop()
    assert case.service._row(case.body["reservation"])["recovery"] == 1
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)
    case.service.loop_at = case.clock[0]
    with pytest.raises(ValueError):
        ask(case)
    with pytest.raises(ValueError, match="stop_only"):
        give(case)  # recovery also refuses a new ticket


def test_cancel_and_stop_drop_the_ticket_and_refuse_reads(case):
    give(case)
    assert ask(case)["digest"]
    case.service.admin(dict(operation="cancel", reservation=case.body["reservation"]))
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)
    with pytest.raises(ValueError):
        ask(case)
    requests_before = len(case.control.requests)
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=0)
    assert len(case.control.requests) == requests_before


def test_retained_output_stop_also_drops_the_ticket(case):
    give(case)  # the fixture already ran begin-once
    output = {"schema": "isolated-output", "result": 1}
    digest = hashlib.sha256(tss.encoded(output)).hexdigest()
    case.service.workload(
        "/v1/workload/output", dict(binding=case.request, output=output, sha256=digest), case.token
    )
    case.provider.state = "stopped"
    case.clock[0] += 5
    case.service.loop()
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)
    with pytest.raises(ValueError):
        ask(case)


def test_manifest_digest_must_equal_the_approved_route_digest(case):
    # A ticket for some other snapshot is refused at the admin channel.
    other = build_snapshot({"other.txt": b"other"}, "snapshot_other")
    with pytest.raises(ValueError, match="invalid_input_ticket"):
        give(case, ticket_for(other))
    # Control answering with a different claimed digest is refused before any chunk.
    case.control.claimed_digest = "f" * 64
    give(case)
    with pytest.raises(ValueError):
        ask(case)
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=0)
    assert [p for p, _ in case.control.requests] == ["/provider-inputs/manifest"] * 2


def test_manifest_content_is_rederived_not_trusted(case):
    # The claimed digest matches the approved one, but the manifest bytes do not hash to it.
    case.control.tamper_manifest = True
    give(case)
    with pytest.raises(ValueError):
        ask(case)
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=0)


@pytest.mark.parametrize("mode", ["sha", "size"])
def test_chunk_sha_and_size_mismatch_are_refused(case, mode):
    give(case)
    if mode == "sha":
        case.control.tamper_chunk = 1
        bad = 1
    else:
        case.control.wrong_size = True
        bad = 0
    assert ask(case)["digest"]
    for seq in range(len(case.snapshot.index)):
        if seq == bad:
            with pytest.raises(ValueError):
                ask(case, "chunk", seq=seq)
        else:
            assert ask(case, "chunk", seq=seq)["seq"] == seq
    assert case.service.inputs.status(case.request["operation"])["chunks_served"] == (
        len(case.snapshot.index) - 1
    )


def test_total_bytes_cap_is_enforced(case, monkeypatch):
    give(case)
    monkeypatch.setattr(
        salad_input_delivery, "MAX_BYTES", case.snapshot.manifest["total_bytes"] - 1
    )
    with pytest.raises(ValueError):
        ask(case)
    monkeypatch.setattr(salad_input_delivery, "MAX_BYTES", case.snapshot.manifest["total_bytes"])
    assert ask(case)["digest"]


def test_served_bytes_are_bounded_against_endless_rereads(case):
    give(case)
    ask(case)
    total = case.snapshot.manifest["total_bytes"]
    served = 0
    with pytest.raises(ValueError):
        while True:
            served += len(ask(case, "chunk", seq=0)["content_base64"])
            assert served < total * 100  # must refuse long before this
    assert case.service.inputs.status(case.request["operation"])["bytes_served"] <= total * 2


def test_chunk_sequence_must_be_in_the_index(case):
    give(case)
    for seq in (len(case.snapshot.index), -1, True, "0"):
        with pytest.raises(ValueError):
            ask(case, "chunk", seq=seq)


def test_expired_ticket_is_dropped_and_refused(case):
    give(case, ticket_for(case.snapshot, minutes=30))
    assert ask(case)["digest"]
    case.service.inputs.clock = lambda: case.clock[0] + 31 * 60
    with pytest.raises(ValueError):
        ask(case)
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)
    with pytest.raises(ValueError, match="invalid_input_ticket"):
        give(case, dict(ticket_for(case.snapshot), expires_at="2001-01-01T00:00:00Z"))


def test_guardian_switches_to_the_refreshed_ticket(case):
    case.root.joinpath("tree").mkdir(mode=0o700)
    give(case)
    assert ask(case)["digest"] and ask(case, "chunk", seq=0)
    served = case.service.inputs.status(case.request["operation"])["chunks_served"]
    # Control revokes A when it mints B; the guardian is then handed B on the admin channel.
    case.control.valid = {TOKEN_B}
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=1)  # still holding the revoked bearer
    give(case, ticket_for(case.snapshot, TOKEN_B, minutes=55))
    assert ask(case, "chunk", seq=1)["seq"] == 1
    assert case.control.requests[-1][1] == "Ticket " + TOKEN_B
    status = case.service.inputs.status(case.request["operation"])
    assert status["chunks_served"] == served + 1  # counters and verified index carry over
    assert [p for p, _ in case.control.requests].count("/provider-inputs/manifest") == 1
    assert fetch_all(case)


def test_admin_input_ticket_is_validated_and_needs_configuration(case, tmp_path):
    for bad in (
        dict(ticket_for(case.snapshot), extra="x"),
        dict(ticket_for(case.snapshot), token="short"),
        dict(ticket_for(case.snapshot), token="has space " * 4),
        dict(ticket_for(case.snapshot), snapshot_digest="0" * 64),
    ):
        with pytest.raises(ValueError):
            give(case, bad)
    case.service.inputs = None
    with pytest.raises(ValueError, match="input_unconfigured"):
        give(case)


def test_origin_follows_the_provider_input_client_rules():
    snapshot = build_snapshot()
    for bad in ("http://control.example.com", "ftp://127.0.0.1", "http://user@127.0.0.1"):
        with pytest.raises(ProviderInputError):
            GuardianInputs(bad, opener=FakeControl(snapshot))
    GuardianInputs("http://127.0.0.1:1", opener=FakeControl(snapshot))
    GuardianInputs("http://[::1]:1", opener=FakeControl(snapshot))
    GuardianInputs("https://control.example.com", opener=FakeControl(snapshot))


def test_client_sends_only_the_four_ticket_fields_on_the_admin_channel(monkeypatch):
    client = SaladSafetyClient("/not-contacted/guardian.sock", service_uid=0)
    sent = []
    monkeypatch.setattr(client, "_exchange", sent.append)
    state = dict(id="route", proposal_digest="d" * 64)
    ticket = dict(
        token=TOKEN_A,
        expires_at=expires(),
        snapshot_id="s",
        snapshot_digest="a" * 64,
        manifest_path="/provider-inputs/manifest",
        chunk_path_template="/provider-inputs/chunks/{seq}",
    )
    client.input_ticket(state, ticket)
    client.input_ticket(state, None)
    assert set(sent[0]["input_ticket"]) == {"token", "expires_at", "snapshot_id", "snapshot_digest"}
    assert sent[0]["operation"] == "input_ticket" and sent[1]["input_ticket"] is None


# ---- trusted server: mint, hand over, refresh, revoke (real control store) ---------------------


@pytest.fixture
def server_case(tmp_path, monkeypatch):
    from test_remote_execution_routing import dispatch
    from test_salad_server import fixture, preflight, wire

    h = fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    h.state = dispatch(h, "create")
    h.route_id = h.state["id"]
    declare_fixture_input_public(h.journey.state, h.state["proposal"]["operation_id"])
    return h


def rows(h):
    import sqlite3

    with sqlite3.connect(h.journey.state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r) for r in db.execute("SELECT * FROM provider_input_tickets ORDER BY created_at")
        ]


def tickets_sent(h):
    return [
        c["input_ticket"]
        for c in h.safety_calls
        if c["operation"] == "input_ticket" and c["input_ticket"] is not None
    ]


def control_opener(h):
    """The real provider-input store behind the guardian's fetch interface."""

    def opener(request, timeout):
        token = dict(request.header_items())["Authorization"].removeprefix("Ticket ")
        path = "/" + request.full_url.split("://", 1)[1].split("/", 1)[1]
        try:
            if path == "/provider-inputs/manifest":
                return _Response(h.server.inputs.read_manifest(token))
            return _Response(
                h.server.inputs.read_chunk_for_ticket(token, int(path.rsplit("/", 1)[1]))
            )
        except TicketInvalid:
            raise HTTPError(request.full_url, 401, "x", {}, io.BytesIO(b"{}")) from None

    return opener


def test_server_mints_and_hands_the_ticket_to_the_guardian_only(server_case):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    (sent,) = tickets_sent(h)
    (row,) = rows(h)
    assert row["token_hash"] == hashlib.sha256(sent["token"].encode()).hexdigest()
    assert row["revoked_at"] is None
    # The token reaches neither the stored route, the package, the profile nor the state.
    surfaces = [
        json.dumps(h.store.get(h.worker, h.route_id), default=str),
        json.dumps(h.server.policy.model_dump(mode="json"), default=str),
        json.dumps(h.server.store._operation_snapshot(h.route_id), default=str)
        if hasattr(h.server.store, "_operation_snapshot")
        else "",
        json.dumps([c for c in h.safety_calls if c["operation"] != "input_ticket"], default=str),
    ]
    assert not any(sent["token"] in s for s in surfaces)
    assert sent["snapshot_digest"] == h.body["selection"]["manifests"]["data"]


def test_refresh_reissues_below_ten_minutes_revokes_old_and_guardian_switches(server_case):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    first = tickets_sent(h)[0]
    expiry = h.server._input_state[h.route_id]["expires_at"]
    # Plenty of lifetime left: nothing happens.
    threshold = input_refresh_threshold(h.server._input_state[h.route_id]["lifetime"])
    assert h.server.refresh_inputs(h.route_id, now=expiry - threshold - 1) is False
    assert len(tickets_sent(h)) == 1 and len(rows(h)) == 1
    guard = GuardianInputs(ORIGIN, opener=control_opener(h))
    install = dict(
        expected_digest=first["snapshot_digest"], max_bytes=1 << 30, deadline=expiry + 3600
    )
    guard.install("op", first, **install)
    assert guard.manifest("op")["digest"] == first["snapshot_digest"]
    size0 = guard.manifest("op")["chunks"][0]["size"]
    # Under ten minutes: reissue; control revokes the old ticket in the same transaction.
    assert h.server.refresh_inputs(h.route_id, now=expiry - threshold + 1) is True
    second = tickets_sent(h)[1]
    assert second["token"] != first["token"]
    old, new = rows(h)
    assert old["revoked_at"] is not None and new["revoked_at"] is None
    with pytest.raises(ValueError):
        guard.chunk("op", 0, 0, size0)  # the guardian still holds the revoked bearer
    guard.install("op", second, **install)
    assert guard.chunk("op", 0, 0, size0)["seq"] == 0
    with pytest.raises(TicketInvalid):
        h.server.inputs.read_manifest(first["token"])
    assert h.server.inputs.read_manifest(second["token"])["digest"] == first["snapshot_digest"]


def test_failed_handoff_is_retried_without_minting_again(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    expiry = h.server._input_state[h.route_id]["expires_at"]
    real = h.server.safety._exchange
    monkeypatch.setattr(
        h.server.safety, "_exchange", lambda body: (_ for _ in ()).throw(ValueError("down"))
    )
    with pytest.raises(ValueError):
        h.server.refresh_inputs(h.route_id, now=expiry - 60)
    assert len(rows(h)) == 2 and h.server._input_state[h.route_id]["pending"] is not None
    monkeypatch.setattr(h.server.safety, "_exchange", real)
    assert h.server.refresh_inputs(h.route_id, now=expiry - 60) is True
    assert len(rows(h)) == 2  # the retry reused the minted ticket
    assert h.server._input_state[h.route_id]["pending"] is None
    assert tickets_sent(h)[-1]["token"] != tickets_sent(h)[0]["token"]


def test_the_workload_side_cannot_cause_issuance(server_case):
    h = server_case
    # No delivery was ever requested by the trusted server: refresh is a no-op.
    assert h.server.refresh_inputs(h.route_id, now=0) is False
    assert rows(h) == [] and tickets_sent(h) == []
    # The guardian's workload surface offers no way to mint: it only reads a held ticket.
    guard = GuardianInputs(ORIGIN, opener=control_opener(h))
    with pytest.raises(ValueError):
        guard.manifest("op")
    assert rows(h) == []


def test_heartbeat_refreshes_and_never_lets_upkeep_break_liveness(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    expiry = h.server._input_state[h.route_id]["expires_at"]
    h.server._input_state[h.route_id]["expires_at"] = expiry - 3600  # nearly expired
    h.server.heartbeat(h.route_id)
    assert len(tickets_sent(h)) == 2 and h.safety_calls[-2]["operation"] == "heartbeat"
    monkeypatch.setattr(
        h.server.inputs, "issue_ticket", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    h.server._input_state[h.route_id]["expires_at"] = expiry - 3600
    h.server.heartbeat(h.route_id)  # does not raise
    assert h.server.input_log[-1]["event"] == "refresh_failed"


def test_stop_revokes_the_ticket_at_control_and_records_it(server_case):
    from test_remote_execution_routing import command

    h = server_case
    h.server.deliver_inputs(h.route_id)
    command(h, "cancel")
    with pytest.raises((Conflict, Forbidden)):
        h.server.heartbeat(h.route_id)  # fenced: guardian cancel + ticket revocation
    assert h.safety_calls[-2]["operation"] == "cancel"
    assert h.safety_calls[-1] == dict(
        h.safety_calls[-1], operation="input_ticket", input_ticket=None
    )
    (row,) = rows(h)
    assert row["revoked_at"] is not None
    assert h.server.input_log[-1]["event"] == "revoke" and h.server.input_log[-1]["revoked"] == 1
    assert h.route_id not in h.server._input_state
    with pytest.raises(TicketInvalid):
        h.server.inputs.read_manifest(tickets_sent(h)[0]["token"])


def test_failed_revocation_is_recorded_not_raised(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    monkeypatch.setattr(
        h.server.inputs, "revoke_tickets", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    h.server._revoke_inputs(h.store.get(h.worker, h.route_id))
    assert h.server.input_log[-1] == dict(
        h.server.input_log[-1], event="revoke", revoked=None, error=True
    )


def test_ticket_outlives_the_30s_soft_lease_and_heartbeats_do_not_reissue(server_case, monkeypatch):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from tessera_control.execution import ExecutionStore, _utc_string

    h = server_case
    h.server.deliver_inputs(h.route_id)
    first = tickets_sent(h)[0]
    (row,) = rows(h)
    lease_id = h.body["lease_id"]
    db_path = h.journey.state / "tessera.sqlite3"
    with sqlite3.connect(db_path) as db:
        hard = db.execute(
            "SELECT hard_expires_at FROM execution_leases WHERE id=?", (lease_id,)
        ).fetchone()[0]
    issued = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    # Lifetime follows the hard expiry / one hour, not the 30 s soft lease.
    assert expiry - issued > timedelta(seconds=120)  # far beyond the 30 s soft lease
    assert expiry <= datetime.fromisoformat(hard.replace("Z", "+00:00"))
    guard = GuardianInputs(ORIGIN, opener=control_opener(h))
    install = dict(
        expected_digest=first["snapshot_digest"], max_bytes=1 << 30, deadline=expiry.timestamp()
    )
    guard.install("op", first, **install)
    size0 = guard.manifest("op")["chunks"][0]["size"]
    base = datetime.now(UTC)
    for step in range(1, 6):  # heartbeats 25 s apart, each renewing the 30 s soft lease
        at = base + timedelta(seconds=25 * step)
        monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda at=at: at))
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE execution_leases SET expires_at=? WHERE id=?",
                (_utc_string(at + timedelta(seconds=30)), lease_id),
            )
        assert h.server.refresh_inputs(h.route_id, now=at.timestamp()) is False
        assert guard.manifest("op")["digest"] == first["snapshot_digest"]
        if step in {1, 5}:  # the re-read budget is twice the (tiny) snapshot
            assert guard.chunk("op", 0, 0, size0)["seq"] == 0
    assert len(tickets_sent(h)) == 1 and len(rows(h)) == 1 and rows(h)[0]["revoked_at"] is None
    # Without a renewal the per-pull soft-lease fence still stops the pulls.
    late = base + timedelta(seconds=25 * 5 + 31)
    monkeypatch.setattr(ExecutionStore, "_now", staticmethod(lambda: late))
    with pytest.raises(TicketInvalid):
        h.server.inputs.read_chunk_for_ticket(first["token"], 0)


def test_refresh_threshold_scales_with_the_issued_lifetime():
    assert input_refresh_threshold(3600) == 600
    assert input_refresh_threshold(1200) == 300
    assert input_refresh_threshold(100) == 60
    assert input_refresh_threshold(0) == 60


def test_expired_pending_ticket_is_discarded_and_reissued(server_case, monkeypatch):
    h = server_case
    real = h.server.safety._exchange
    monkeypatch.setattr(
        h.server.safety, "_exchange", lambda body: (_ for _ in ()).throw(ValueError("down"))
    )
    with pytest.raises(ValueError):
        h.server.deliver_inputs(h.route_id)  # minted, never delivered
    pending = h.server._input_state[h.route_id]["pending"]
    monkeypatch.setattr(h.server.safety, "_exchange", real)
    from tessera_control.store import _parse_utc

    expiry = _parse_utc(pending["expires_at"]).timestamp()
    assert h.server.refresh_inputs(h.route_id, now=expiry + 1) is True
    sent = tickets_sent(h)[-1]
    assert sent["token"] != pending["token"] and len(rows(h)) == 2
    assert h.server._input_state[h.route_id]["pending"] is None
    assert _parse_utc(sent["expires_at"]).timestamp() > expiry - 3600  # a live ticket


def test_budget_fenced_reservation_refuses_input(case):
    give(case)
    assert ask(case)["digest"]
    with case.service.db:
        case.service.db.execute(
            "UPDATE reservations SET budget_fenced=1 WHERE reservation=?",
            (case.body["reservation"],),
        )
    before = len(case.control.requests)
    with pytest.raises(ValueError):
        ask(case)
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=0)
    with case.service.db:
        case.service.db.execute(
            "UPDATE reservations SET budget_fenced=0, rate_unknown=1 WHERE reservation=?",
            (case.body["reservation"],),
        )
    with pytest.raises(ValueError):
        ask(case)
    assert len(case.control.requests) == before


def test_failed_control_fetches_have_a_bounded_retry_budget(case):
    give(case)
    case.control.tamper_chunk = 0
    for _ in range(salad_input_delivery.MAX_FAILED_PER_ITEM):
        with pytest.raises(ValueError):
            ask(case, "chunk", seq=0)
    seen = len(case.control.requests)
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=0)
    assert len(case.control.requests) == seen  # refused without contacting control
    assert ask(case, "chunk", seq=1)["seq"] == 1  # other chunks are unaffected
    # A flapping control cannot be hammered past the per-ticket budget either.
    case.control.valid = set()
    refused = 0
    for _ in range(salad_input_delivery.MAX_FAILED_PER_TICKET * 2):
        before = len(case.control.requests)
        with pytest.raises(ValueError):
            ask(case, "chunk", seq=2)
        refused += len(case.control.requests) == before
    assert refused > 0


def test_refused_admin_ticket_holds_nothing(case):
    case.service.loop_at = None  # guardian loop not ready
    with pytest.raises(ValueError, match="guardian_loop_unready"):
        give(case)
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)
    case.service.loop()
    with pytest.raises(ValueError):
        ask(case)
    assert case.control.requests == []


def test_import_retained_revokes_the_ticket_and_refresh_cannot_remint(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    expiry = h.server._input_state[h.route_id]["expires_at"]
    terminal = dict(
        h.store.get(h.worker, h.route_id), termination_confirmed=True, reallocated=False
    )
    monkeypatch.setattr(h.server.store, "get", lambda *a, **k: terminal)
    monkeypatch.setattr(h.server.store, "_bound", lambda state: True)
    monkeypatch.setattr(h.server.safety, "import_retained", lambda state: dict(output={}))
    assert h.server.import_retained(h.route_id) == dict(output={})
    (row,) = rows(h)
    assert row["revoked_at"] is not None
    assert h.server.input_log[-1]["event"] == "revoke" and h.server.input_log[-1]["revoked"] == 1
    assert h.route_id not in h.server._input_state
    assert h.server.refresh_inputs(h.route_id, now=expiry - 1) is False
    assert len(rows(h)) == 1 and len(tickets_sent(h)) == 1


def test_control_fetch_timeout_is_bounded_by_the_stop_deadline(tiny_chunks):
    snapshot = build_snapshot()
    control = FakeControl(snapshot)
    timeouts = []

    def opener(request, timeout):
        timeouts.append(timeout)
        return control(request, timeout)

    clock = [100.0]
    guard = GuardianInputs(ORIGIN, opener=opener, clock=lambda: clock[0])
    guard.install(
        "op",
        ticket_for(snapshot),
        expected_digest=snapshot.digest,
        max_bytes=1 << 30,
        deadline=100.0 + 7,
    )
    guard.manifest("op")
    clock[0] = 104.0
    guard.chunk("op", 0, 0, 8)
    assert timeouts[0] == 7 and timeouts[1] == 3  # never beyond the remaining deadline
    clock[0] = 107.0  # deadline reached: refused with no control call
    with pytest.raises(ValueError):
        guard.chunk("op", 1, 0, 8)
    assert len(timeouts) == 2


def test_dropping_a_ticket_bypasses_stop_only_and_readiness(case):
    give(case)
    assert case.service.inputs.status(case.request["operation"])["ticket_held"]
    case.service.db.execute(
        "UPDATE reservations SET recovery=1 WHERE reservation=?", (case.body["reservation"],)
    )
    case.clock[0] = case.body["deadline"] + 1
    case.service.loop_at = None  # guardian loop unready too
    drop = dict(operation="input_ticket", reservation=case.body["reservation"], input_ticket=None)
    assert case.service.admin(drop)["state"] == "ready"
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)
    with pytest.raises(ValueError, match="stop_only"):
        give(case)  # installing is still refused


def test_fetch_timeout_uses_the_live_shortened_stop_deadline(case):
    reservation = case.body["reservation"]
    seen = []
    real = case.control

    def opener(request, timeout):
        seen.append(timeout)
        return real(request, timeout)

    case.service.inputs.opener = opener
    row = case.service._row(reservation)
    long_deadline = case.service._stop_deadline(row)
    give(case)
    # The job is now running with a short window: the stop deadline shrinks below the route's.
    case.service.db.execute(
        "UPDATE reservations SET seen_running_at=?, first_running_at=?, running_seconds=? "
        "WHERE reservation=?",
        (case.clock[0], case.clock[0], 5.0, reservation),
    )
    assert case.service._stop_deadline(case.service._row(reservation)) == case.clock[0] + 5.0
    assert case.clock[0] + 5.0 < long_deadline
    ask(case)  # manifest
    # Bounded by the guardian's per-request control-fetch budget and the live deadline.
    assert seen[-1] == pytest.approx(min(tss_service.INPUT_CONTROL_FETCH_SECONDS, 5.0))
    case.clock[0] += 3.0
    case.service.loop_at = case.clock[0]
    ask(case, "chunk", seq=0)
    assert seen[-1] == pytest.approx(2.0)  # never beyond the shortened deadline
    case.clock[0] += 2.0
    case.service.loop_at = case.clock[0]
    count = len(seen)
    with pytest.raises(ValueError):
        ask(case, "chunk", seq=1)
    assert len(seen) == count  # refused without calling control


def test_drop_without_inputs_still_raises_unconfigured(case):
    case.service.inputs = None
    with pytest.raises(ValueError, match="input_unconfigured"):
        case.service.admin(
            dict(operation="input_ticket", reservation=case.body["reservation"], input_ticket=None)
        )


@pytest.mark.parametrize("falsy", [{}, False, 0, ""])
def test_non_none_falsy_tickets_are_rejected(case, falsy):
    give(case)  # a held ticket must survive a rejected falsy "drop"
    with pytest.raises(ValueError, match="invalid_input_ticket"):
        case.service.admin(
            dict(operation="input_ticket", reservation=case.body["reservation"], input_ticket=falsy)
        )
    assert case.service.inputs.status(case.request["operation"])["ticket_held"]


def test_failed_revoke_blocks_remint_and_is_retried(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    expiry = h.server._input_state[h.route_id]["expires_at"]
    terminal = dict(
        h.store.get(h.worker, h.route_id), termination_confirmed=True, reallocated=False
    )
    monkeypatch.setattr(h.server.store, "get", lambda *a, **k: terminal)
    monkeypatch.setattr(h.server.store, "_bound", lambda state: True)
    monkeypatch.setattr(h.server.safety, "import_retained", lambda state: dict(output={}))
    real = h.server.inputs.revoke_tickets
    monkeypatch.setattr(
        h.server.inputs, "revoke_tickets", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    assert h.server.import_retained(h.route_id) == dict(output={})  # import still succeeds
    assert h.server.input_log[-1]["event"] == "revoke" and h.server.input_log[-1]["error"]
    (row,) = rows(h)
    assert row["revoked_at"] is None  # still live at control
    # Refresh does not re-mint (and its retry fails again while control is failing).
    assert h.server.refresh_inputs(h.route_id, now=expiry - 1) is False
    with pytest.raises(Conflict):
        h.server.deliver_inputs(h.route_id)
    assert len(rows(h)) == 1 and len(tickets_sent(h)) == 1
    monkeypatch.setattr(h.server.inputs, "revoke_tickets", real)
    assert h.server.refresh_inputs(h.route_id, now=expiry - 1) is False  # retries the revoke
    (row,) = rows(h)
    assert row["revoked_at"] is not None
    assert h.server.input_log[-1]["revoked"] == 1
    assert h.route_id not in h.server._revoke_pending


# ---- phase 2 correctives: deadline after fetch, guardian drop, pending-revoke upkeep ------------


def _guard_with(snapshot, clock, deadline, on_fetch=None):
    control = FakeControl(snapshot)

    def opener(request, timeout):
        if on_fetch:
            on_fetch()
        return control(request, timeout)

    guard = GuardianInputs(ORIGIN, opener=opener, clock=lambda: clock[0])
    guard.install(
        "op",
        ticket_for(snapshot),
        expected_digest=snapshot.digest,
        max_bytes=1 << 30,
        deadline=deadline,
    )
    return guard


def test_deadline_passing_during_a_fetch_discards_the_bytes(tiny_chunks):
    snapshot = build_snapshot()
    clock = [100.0]
    guard = _guard_with(snapshot, clock, 110.0)
    guard.manifest("op")
    clock_hit = []

    def slow():
        clock[0] = 111.0  # a slow drip: each socket op was fine, the whole took too long
        clock_hit.append(1)

    guard.opener = lambda r, t: (slow(), FakeControl(snapshot)(r, t))[1]
    with pytest.raises(ValueError, match="workload_rejected"):
        guard.chunk("op", 0, 0, 8)
    assert clock_hit and guard.status("op").get("bytes_served", 0) == 0


def test_deadline_passing_during_the_manifest_fetch_is_refused(tiny_chunks):
    snapshot = build_snapshot()
    clock = [100.0]
    guard = _guard_with(snapshot, clock, 110.0, on_fetch=lambda: clock.__setitem__(0, 111.0))
    with pytest.raises(ValueError, match="workload_rejected"):
        guard.manifest("op")
    assert guard._sessions["op"].snapshot is None


def test_a_failing_deadline_callable_fails_closed(tiny_chunks):
    snapshot = build_snapshot()
    clock = [100.0]
    state = dict(ok=True)

    def deadline():
        if not state["ok"]:
            raise KeyError("row gone")
        return 200.0

    guard = _guard_with(snapshot, clock, deadline)
    state["ok"] = False
    with pytest.raises(ValueError, match="workload_rejected") as caught:
        guard.manifest("op")
    assert type(caught.value) is ValueError


def test_completion_and_revoke_also_drop_the_guardian_ticket(server_case):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    h.server._revoke_inputs(h.store.get(h.worker, h.route_id))
    assert h.safety_calls[-1]["operation"] == "input_ticket"
    assert h.safety_calls[-1]["input_ticket"] is None


def test_guardian_drop_failure_never_blocks_import_or_revoke(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    terminal = dict(
        h.store.get(h.worker, h.route_id), termination_confirmed=True, reallocated=False
    )
    monkeypatch.setattr(h.server.store, "get", lambda *a, **k: terminal)
    monkeypatch.setattr(h.server.store, "_bound", lambda state: True)
    monkeypatch.setattr(h.server.safety, "import_retained", lambda state: dict(output={}))
    monkeypatch.setattr(
        h.server.safety,
        "input_ticket",
        lambda state, ticket: (_ for _ in ()).throw(ValueError("guardian down")),
    )
    assert h.server.import_retained(h.route_id) == dict(output={})
    assert any(r["event"] == "drop_failed" for r in h.server.input_log)
    (row,) = rows(h)
    assert row["revoked_at"] is not None  # control revoke still happened


def test_heartbeat_path_retries_pending_revokes_with_backoff(server_case, monkeypatch):
    h = server_case
    h.server.deliver_inputs(h.route_id)
    real = h.server.inputs.revoke_tickets
    calls = []

    def failing(*a, **k):
        calls.append(1)
        raise ValueError("x")

    monkeypatch.setattr(h.server.inputs, "revoke_tickets", failing)
    state = h.store.get(h.worker, h.route_id)
    h.server._revoke_inputs(state)
    assert h.route_id in h.server._revoke_pending and len(calls) == 1
    # Not yet due: the periodic retry does not hammer control.
    h.server.retry_pending_revokes(now=time.time())
    assert len(calls) == 1
    # Due, control recovered: the periodic retry (heartbeat) clears it.
    monkeypatch.setattr(h.server.inputs, "revoke_tickets", real)
    h.server.retry_pending_revokes(now=time.time() + 6)
    assert h.route_id not in h.server._revoke_pending
    assert h.route_id not in h.server._revoke_meta
    (row,) = rows(h)
    assert row["revoked_at"] is not None


def test_pending_revokes_are_capped_and_vanished_routes_cleared(server_case, monkeypatch):
    from tessera_control import salad_server as module

    h = server_case
    state = h.store.get(h.worker, h.route_id)
    monkeypatch.setattr(
        h.server.inputs, "revoke_tickets", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    monkeypatch.setattr(module, "MAX_REVOKE_PENDING", 3)
    for i in range(5):
        h.server._input_state[f"r{i}"] = {}
        h.server._revoke_inputs(dict(state, id=f"r{i}"))
    assert list(h.server._revoke_pending) == ["r2", "r3", "r4"]
    assert set(h.server._revoke_meta) == set(h.server._revoke_pending)

    def gone(*a, **k):
        raise module.NotFound("gone")

    monkeypatch.setattr(h.server.store, "get", gone)
    h.server.retry_pending_revokes(now=time.time() + 10_000)
    assert not h.server._revoke_pending and not h.server._revoke_meta
    assert h.server.input_log[-1]["event"] == "revoke_abandoned"


def test_revoke_retries_are_bounded(server_case, monkeypatch):
    from tessera_control import salad_server as module

    h = server_case
    h.server.deliver_inputs(h.route_id)
    monkeypatch.setattr(
        h.server.inputs, "revoke_tickets", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    state = h.store.get(h.worker, h.route_id)
    h.server._revoke_inputs(state)
    for _ in range(module.MAX_REVOKE_ATTEMPTS):
        h.server.retry_pending_revokes(now=time.time() + 10_000)
    assert h.route_id not in h.server._revoke_pending


def test_drop_reply_with_no_loop_tick_is_accepted_by_the_client(monkeypatch):
    client = SaladSafetyClient("/not-contacted/guardian.sock", service_uid=0)
    state = dict(id="route", proposal_digest="d" * 64)
    reservation = client._reservation(state)
    reply = dict(reservation=reservation, state="ready", observed_at=None)
    monkeypatch.setattr(client, "_call", lambda body: reply)
    client.input_ticket(state, None)  # no raise
    with pytest.raises(ValueError):
        client.input_ticket(
            state,
            dict(token=TOKEN_A, expires_at=expires(), snapshot_id="s", snapshot_digest="a" * 64),
        )


# ------------------------------------------------- review follow-ups (L3, L4)


def test_control_fetch_has_a_wall_clock_deadline_not_only_a_socket_timeout(tiny_chunks):
    """L3: a slow-drip control cannot hold a fetch past INPUT_CONTROL_FETCH_SECONDS."""
    snapshot = build_snapshot()
    control = FakeControl(snapshot)

    def drip(request, timeout):
        time.sleep(2)  # each socket read could stay under the per-recv timeout
        return control(request, timeout)

    guard = GuardianInputs(
        ORIGIN,
        opener=drip,
        clock=lambda: 100.0,
        fetch_timeout=0.2,
        fetch_budget=tss_service.alarm_budget,
    )
    guard.install(
        "op", ticket_for(snapshot), expected_digest=snapshot.digest, max_bytes=1 << 20,
        deadline=10**9,
    )  # fmt: skip
    started = time.monotonic()
    with pytest.raises(ValueError):
        guard.manifest("op")
    assert time.monotonic() - started < 1.5
    # Without a slow control the same budget admits the fetch.
    fast = GuardianInputs(
        ORIGIN, opener=control, clock=lambda: 100.0, fetch_timeout=0.2,
        fetch_budget=tss_service.alarm_budget,
    )  # fmt: skip
    fast.install(
        "op", ticket_for(snapshot), expected_digest=snapshot.digest, max_bytes=1 << 20,
        deadline=10**9,
    )  # fmt: skip
    assert fast.manifest("op")["digest"] == snapshot.digest


def test_guardian_wires_the_alarm_around_control_fetches(tmp_path):
    tmp_path.chmod(0o700)
    service = tss.make_service(
        tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), tss.Provider(),
        control_origin=ORIGIN, control_opener=lambda *a: None,
    )  # fmt: skip
    try:
        assert service.inputs.fetch_budget is tss_service.alarm_budget
        assert service.inputs.fetch_timeout == tss_service.INPUT_CONTROL_FETCH_SECONDS
    finally:
        service.close()


def test_admission_reply_size_equals_the_bytes_the_guardian_handler_sends():
    """L4: admission, guardian check and SafetyHandler._reply share one compact encoding."""
    from tessera_control.salad_safety_service import SafetyHandler
    from tessera_control.salad_workload_admission import (
        INPUT_CHUNK_BYTES,
        input_manifest_reply_bytes,
    )

    entries = [
        dict(relative_path=name, size=size, sha256=hashlib.sha256(name.encode()).hexdigest(),
             provenance="imported_unverified", classification="unclassified")
        for name, size in (("données/été ☃.bin", 2 * INPUT_CHUNK_BYTES + 5),
                           ("b.txt", 3), ("empty", 0))
    ]  # fmt: skip
    manifest = dict(
        root="r", entries=entries, exclusions=[], total_bytes=sum(e["size"] for e in entries)
    )
    index = []
    for entry in entries:
        for offset in range(0, entry["size"], INPUT_CHUNK_BYTES):
            size = min(INPUT_CHUNK_BYTES, entry["size"] - offset)
            index.append(dict(seq=len(index), relative_path=entry["relative_path"],
                              offset=offset, size=size,
                              sha256=hashlib.sha256(bytes([len(index)])).hexdigest()))  # fmt: skip
    reply = GuardianInputs._manifest_reply(
        dict(id="snapshot_x", digest="d" * 64, manifest=manifest, chunks=index)
    )
    handler = SafetyHandler.__new__(SafetyHandler)
    handler.connection = None
    handler.wfile = io.BytesIO()
    handler.send_response = handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler._reply(200, reply)
    sent = len(handler.wfile.getvalue())
    assert sent == len(salad_input_delivery._encoded(reply))
    assert sent == input_manifest_reply_bytes("snapshot_x", "d" * 64, manifest)


def test_input_install_tolerates_a_stored_legacy_route_without_execution(tmp_path):
    """L1: like deliver_inputs and route_input, install guards ``execution is None``."""
    from test_salad_input_path import _guardian
    from test_salad_workload_contract import contract as base_contract

    from tessera_control.remote_execution_models import RemoteProposal

    data = base_selection().manifests.data
    work = base_contract(input=dict(kind="staged_snapshot", snapshot_digest=data, max_bytes=77))
    service, body = _guardian(tmp_path, work)
    try:
        legacy = copy.deepcopy(body["proposal"])
        legacy["selection"]["salad"]["execution"] = None
        assert RemoteProposal.model_validate(legacy).selection.salad.execution is None
        service.db.execute(
            "UPDATE reservations SET proposal=? WHERE reservation=?",
            (json.dumps(legacy), body["reservation"]),
        )
        ticket = dict(token="t" * 40, expires_at=expires(now=100.0), snapshot_id="s",
                      snapshot_digest=data)  # fmt: skip
        service.admin(dict(operation="input_ticket", reservation=body["reservation"],
                           input_ticket=ticket))  # fmt: skip
        session = service.inputs._sessions[body["proposal"]["operation_id"]]
        # Legacy routes keep the disk bound only (no reviewed per-input cap exists).
        assert session.digest == data and session.max_bytes > 77
    finally:
        service.close()
