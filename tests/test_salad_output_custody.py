"""ADR 0036 phase 3: declared multi-file chunked output custody (fake provider, synthetic JWT)."""

import base64
import email.message
import hashlib
import io
import json
import sqlite3

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import Provider, make_service
from test_salad_workload_contract import contract, workload_reservation
from test_salad_workload_jwt import GROUP, claims, signed

from tessera_control.remote_execution_models import RemoteProposal
from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_output_import import (
    OutputImportError,
    expected_binding,
    stream_files,
    verify_manifest,
)
from tessera_control.salad_safety_service import (
    OUTPUT_CHUNK_BODY_BYTES,
    REQUEST_BODY_BYTES,
    SafetyHandler,
    SaladSafetyService,
    encoded,
)
from tessera_control.salad_server import SaladSafetyClient
from tessera_control.salad_workload_contract import OUTPUT_CHUNK_BYTES

C = OUTPUT_CHUNK_BYTES
CHUNK = "/v1/workload/output-chunk"
PLAN = "/v1/workload/outputs"
WEIGHTS = bytes(range(256)) * (C * 2 // 256) + b"tail-bytes"  # three chunks, last one short
OUTPUTS = [dict(name="output.json", max_bytes=4096), dict(name="weights.bin", max_bytes=3 * C)]


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Case:
    def __init__(self, root, outputs=OUTPUTS, seconds=300):
        root.chmod(0o700)
        self.root, self.clock, self.provider = root, [100.0], Provider()
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.service = make_service(root, lambda: self.clock[0], self.key, self.provider)
        self.body = workload_reservation(contract(outputs=outputs), seconds=seconds)
        s = self.service
        s.loop()
        s.admin(self.body)
        s.admin(
            dict(
                operation="bind",
                reservation=self.body["reservation"],
                provider_id=GROUP,
                allocation_id=None,
            )
        )
        self.provider.state = "running"
        s.loop()
        self.proposal = RemoteProposal.model_validate(self.body["proposal"])
        self.request = SaladSafetyService._request_for(self.proposal)
        payload = claims()
        salad = self.body["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = (
            f"{salad['organization']}-{salad['project']}-{salad['group']}"
        )
        self.token = signed(self.key, payload)
        self.operation = self.request["operation"]

    def begin(self):
        return self.service.workload(
            "/v1/workload/begin-once", dict(binding=self.request), self.token
        )

    def chunk(self, name, data, seq, *, declared_sha=None, size=None, part=None, token=None):
        part = data[seq * C : (seq + 1) * C] if part is None else part
        return self.service.workload(
            CHUNK,
            dict(
                binding=self.request,
                name=name,
                sha256=declared_sha or sha(data),
                bytes=len(data) if size is None else size,
                seq=seq,
                chunk_sha256=sha(part),
                content_base64=base64.b64encode(part).decode(),
            ),
            token or self.token,
        )

    def upload(self, name, data, order=None):
        seqs = order or range(max(1, -(-len(data) // C)))
        return [self.chunk(name, data, seq) for seq in seqs][-1]

    def admin(self, operation, **fields):
        return self.service.admin(
            dict(operation=operation, reservation=self.body["reservation"], **fields)
        )

    def guardian(self):
        return self.service.guardian.status(self.operation)

    def stop(self):
        self.provider.state = "stopped"
        self.clock[0] += 5
        self.service.loop()

    def binding(self):
        return CustodyBinding(**json.loads(self.service._row(self.body["reservation"])["binding"]))

    def state(self):
        return dict(
            id="route",
            proposal_digest=self.body["route_digest"],
            org_id="tenant",
            project_id="project",
            proposal=self.body["proposal"],
        )

    def client(self):
        client = SaladSafetyClient("/nonexistent", service_uid=0)
        client._call = lambda body, cap=16384: _bounded(self.service.admin(body), cap)
        return client


def _bounded(reply, cap):
    assert len(encoded(reply)) <= cap, "reply exceeds the client's IPC cap"
    return reply


@pytest.fixture
def case(tmp_path):
    value = Case(tmp_path)
    yield value
    value.service.close()


OUTPUT = encoded({"result": 1, "fetched": []})


def stored_bytes(case):
    db = case.service.custody.db
    parts = db.execute("SELECT COALESCE(SUM(length(data)),0) FROM parts").fetchone()[0]
    files = db.execute("SELECT COALESCE(SUM(length(data)),0) FROM files").fetchone()[0]
    return parts + files


# --- the happy path -----------------------------------------------------------------------


def test_plan_out_of_order_duplicate_chunks_complete_stop_seal_and_verified_import(case):
    assert case.begin()["granted"]
    plan = case.service.workload(PLAN, dict(binding=case.request), case.token)
    assert plan == dict(
        schema="tessera.output-plan.v1",
        operation=case.operation,
        epoch="1",
        chunk_bytes=C,
        outputs=OUTPUTS,
    )
    # Out of order, with a duplicate chunk resent after a lost response.
    first = case.chunk("weights.bin", WEIGHTS, 2)
    assert first["stored"] and not first["file_complete"] and not first["outputs_complete"]
    case.chunk("weights.bin", WEIGHTS, 0)
    case.chunk("weights.bin", WEIGHTS, 0)
    done = case.chunk("weights.bin", WEIGHTS, 1)
    assert done["file_complete"] and not done["outputs_complete"]
    # A complete file alone never stops the job: one declared output is missing.
    guardian = case.guardian()
    assert guardian["status"] == "armed" and guardian["reason"] == "armed"
    progress = case.admin("status")["retained_outputs"]
    assert progress["complete"] is False
    assert progress["files"] == [dict(name="weights.bin", sha256=sha(WEIGHTS), bytes=len(WEIGHTS))]
    last = case.upload("output.json", OUTPUT)
    assert last["outputs_complete"] and last["file_complete"]
    assert case.guardian()["reason"] == "output_retained"
    assert case.service.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
    assert case.admin("status")["retained_outputs"]["complete"] is True
    # Stored bytes never exceed the declared sizes (duplicates store nothing).
    assert stored_bytes(case) == len(WEIGHTS) + len(OUTPUT)
    case.stop()
    assert case.guardian()["status"] == "stopped"
    reply = case.admin("import")
    assert set(reply) == {"manifest", "chunk_bytes"} and reply["chunk_bytes"] == C
    files = verify_manifest(
        reply["manifest"],
        binding=expected_binding(case.state()),
        declared={o["name"]: o["max_bytes"] for o in OUTPUTS},
        max_total=sum(o["max_bytes"] for o in OUTPUTS),
    )
    pulled = {}

    def sink(name):
        pulled[name] = io.BytesIO()
        return pulled[name]

    verified = stream_files(
        lambda name, seq: case.admin("import_chunk", name=name, seq=seq), files, sink
    )
    assert verified == [
        dict(name="output.json", sha256=sha(OUTPUT), bytes=len(OUTPUT)),
        dict(name="weights.bin", sha256=sha(WEIGHTS), bytes=len(WEIGHTS)),
    ]
    assert pulled["weights.bin"].getvalue() == WEIGHTS


def test_control_import_retained_verifies_per_file_digests_and_writes_destination(case, tmp_path):
    case.begin()
    case.upload("weights.bin", WEIGHTS, order=[1, 2, 0])
    case.upload("output.json", OUTPUT)
    case.stop()
    destination = tmp_path / "imported"
    destination.mkdir(mode=0o700)
    result = case.client().import_retained(case.state(), destination=destination)
    assert result["files"] == [
        dict(name="output.json", sha256=sha(OUTPUT), bytes=len(OUTPUT)),
        dict(name="weights.bin", sha256=sha(WEIGHTS), bytes=len(WEIGHTS)),
    ]
    assert (destination / "weights.bin").read_bytes() == WEIGHTS
    assert (destination / "output.json").read_bytes() == OUTPUT
    assert sorted(p.name for p in destination.iterdir()) == ["output.json", "weights.bin"]
    # A repeated import resumes: matching published files are accepted, not rewritten.
    inode = (destination / "weights.bin").stat().st_ino
    assert case.client().import_retained(case.state(), destination=destination) == result
    assert (destination / "weights.bin").stat().st_ino == inode
    assert not any(p.name.endswith(".part") for p in destination.iterdir())
    # Hash-only import without a destination returns the same verified digests.
    assert case.client().import_retained(case.state())["files"] == result["files"]


@pytest.mark.parametrize("tamper", ["chunk", "manifest", "missing", "binding"])
def test_control_rejects_tampered_import(case, tamper):
    case.begin()
    case.upload("weights.bin", WEIGHTS)
    case.upload("output.json", OUTPUT)
    case.stop()
    client = case.client()
    real = client._call

    def call(body, cap=16384):
        reply = real(body, cap)
        if tamper == "chunk" and body["operation"] == "import_chunk" and body["seq"] == 1:
            data = bytearray(base64.b64decode(reply["content_base64"]))
            data[0] ^= 1
            reply = dict(reply, content_base64=base64.b64encode(bytes(data)).decode())
            reply["sha256"] = sha(bytes(data))  # consistent chunk, wrong file
        if tamper == "manifest" and body["operation"] == "import":
            reply["manifest"]["inventories"][0]["files"][1]["sha256"] = "0" * 64
        if tamper == "missing" and body["operation"] == "import":
            inventory = reply["manifest"]["inventories"][0]
            inventory["files"] = inventory["files"][1:]
        if tamper == "binding" and body["operation"] == "import":
            reply["manifest"]["inventories"][0]["binding"]["epoch"] = 2
        return reply

    client._call = call
    with pytest.raises(ValueError, match="invalid retained output"):
        client.import_retained(case.state())


# --- adversarial uploads ------------------------------------------------------------------


def test_legacy_single_object_endpoint_refused_for_declared_multi_outputs(case):
    case.begin()
    output = {"result": 1}
    with pytest.raises(ValueError, match="workload_rejected"):
        case.service.workload(
            "/v1/workload/output",
            dict(binding=case.request, output=output, sha256=sha(encoded(output))),
            case.token,
        )
    assert stored_bytes(case) == 0


def test_chunks_and_plan_require_begin_once_and_the_bound_identity(case):
    with pytest.raises(ValueError):
        case.chunk("output.json", OUTPUT, 0)
    with pytest.raises(ValueError):
        case.service.workload(PLAN, dict(binding=case.request), case.token)
    case.begin()
    other = ec.generate_private_key(ec.SECP256R1())
    forged = signed(other, claims())
    with pytest.raises(ValueError):
        case.chunk("output.json", OUTPUT, 0, token=forged)
    wrong = dict(case.request, epoch="2")
    with pytest.raises(ValueError):
        case.service.workload(PLAN, dict(binding=wrong), case.token)
    assert stored_bytes(case) == 0


def test_undeclared_name_is_refused_and_nothing_stored(case):
    case.begin()
    with pytest.raises(ValueError, match="undeclared"):
        case.chunk("extra.bin", b"x" * 10, 0)
    with pytest.raises(ValueError, match="undeclared"):
        case.chunk("../output.json", OUTPUT, 0)
    assert stored_bytes(case) == 0
    assert case.service.custody.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_oversize_file_and_oversize_chunk_are_refused(case):
    case.begin()
    big = b"x" * 4097  # output.json is declared at 4096 bytes
    with pytest.raises(ValueError, match="oversize"):
        case.chunk("output.json", big, 0)
    # A chunk longer than its declared slice, or longer than a chunk, never lands.
    with pytest.raises(ValueError, match="chunk_integrity"):
        case.chunk("output.json", OUTPUT, 0, part=OUTPUT + b"x")
    with pytest.raises(ValueError, match="workload_rejected"):
        case.chunk("weights.bin", WEIGHTS, 0, part=WEIGHTS[: C + 1])
    with pytest.raises(ValueError, match="workload_rejected"):
        case.chunk("weights.bin", WEIGHTS, 0, part=WEIGHTS[: 2 * C])
    with pytest.raises(ValueError, match="chunk_out_of_range"):
        case.chunk("weights.bin", WEIGHTS, 3, part=b"")
    assert stored_bytes(case) == 0


def test_declaration_is_fixed_by_the_first_chunk(case):
    case.begin()
    case.chunk("weights.bin", WEIGHTS, 0)
    with pytest.raises(ValueError, match="upload_mismatch"):
        case.chunk("weights.bin", WEIGHTS + b"x", 1, part=WEIGHTS[C : 2 * C])
    with pytest.raises(ValueError, match="upload_mismatch"):
        case.chunk("weights.bin", WEIGHTS, 1, declared_sha="0" * 64)


def test_conflicting_duplicate_chunk_is_refused(case):
    case.begin()
    case.chunk("weights.bin", WEIGHTS, 0)
    with pytest.raises(ValueError, match="chunk_conflict"):
        case.chunk("weights.bin", WEIGHTS, 0, part=bytes(reversed(WEIGHTS[:C])))
    assert stored_bytes(case) == C


def test_whole_file_digest_mismatch_discards_parts_and_never_completes(case):
    case.begin()
    case.upload("output.json", OUTPUT)
    wrong = "f" * 64
    case.chunk("weights.bin", WEIGHTS, 0, declared_sha=wrong)
    case.chunk("weights.bin", WEIGHTS, 1, declared_sha=wrong)
    with pytest.raises(ValueError, match="integrity_mismatch"):
        case.chunk("weights.bin", WEIGHTS, 2, declared_sha=wrong)
    db = case.service.custody.db
    assert db.execute("SELECT COUNT(*) FROM parts WHERE name='weights.bin'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM files WHERE name='weights.bin'").fetchone()[0] == 0
    guardian = case.guardian()
    assert guardian["status"] == "armed" and guardian["reason"] == "armed"
    assert case.admin("status")["retained_outputs"]["complete"] is False


def test_completion_with_a_missing_file_never_stops_or_seals_complete(case):
    case.begin()
    case.upload("weights.bin", WEIGHTS)
    assert case.guardian()["reason"] == "armed"
    case.admin("cancel")
    case.stop()
    assert case.guardian()["status"] == "stopped"
    reply = case.admin("import")
    assert reply["manifest"]["complete"] is False
    with pytest.raises(OutputImportError):
        verify_manifest(
            reply["manifest"],
            binding=expected_binding(case.state()),
            declared={o["name"]: o["max_bytes"] for o in OUTPUTS},
            max_total=10 * C,
        )
    with pytest.raises(ValueError, match="invalid retained output"):
        case.client().import_retained(case.state())


def test_partial_upload_never_keeps_the_job_alive_past_its_deadline(case):
    case.begin()
    case.chunk("weights.bin", WEIGHTS, 0)
    row = case.service._row(case.body["reservation"])
    deadline = case.service._stop_deadline(row)
    # Keep the control heartbeat fresh so only the deadline can stop the job.
    while case.clock[0] < deadline:
        case.clock[0] = min(deadline, case.clock[0] + 20)
        case.service.loop()
        if case.clock[0] < deadline:
            case.admin("heartbeat")
            case.chunk("weights.bin", WEIGHTS, 0)  # a retried chunk does not extend anything
    case.service.loop()
    assert case.service._row(case.body["reservation"])["recovery"] == 1
    assert case.guardian()["reason"] not in {"armed", "output_retained"}
    assert "stop" in case.provider.calls
    with pytest.raises(ValueError):
        case.chunk("weights.bin", WEIGHTS, 1)
    assert stored_bytes(case) == C


def test_upload_after_completion_and_after_seal_is_refused(case):
    case.begin()
    case.upload("output.json", OUTPUT)
    case.upload("weights.bin", WEIGHTS)
    # After completion only an exact replay of a stored chunk is acknowledged.
    with pytest.raises(ValueError):
        case.chunk("weights.bin", WEIGHTS, 0, part=bytes(reversed(WEIGHTS[:C])))
    with pytest.raises(ValueError):
        case.chunk("weights.bin", WEIGHTS + b"x", 0)
    with pytest.raises(ValueError):
        case.service.workload(PLAN, dict(binding=case.request), case.token)
    case.stop()
    assert case.guardian()["status"] == "stopped"
    case.admin("import")
    with pytest.raises(ValueError):
        case.chunk("weights.bin", WEIGHTS, 0)
    with pytest.raises(ValueError):
        case.service.custody.put_chunk(
            case.binding(),
            name="weights.bin",
            checksum=sha(WEIGHTS),
            size=len(WEIGHTS),
            seq=0,
            chunk_sha256=sha(WEIGHTS[:C]),
            data=WEIGHTS[:C],
            expires=1e12,
        )
    assert stored_bytes(case) == len(WEIGHTS) + len(OUTPUT)


def test_chunk_retry_budget_is_bounded(case):
    case.begin()
    # Declared caps plan 1 + 3 chunks: budget 2 * 4 + 16 = 24 calls, refused ones included.
    for _ in range(12):
        with pytest.raises(ValueError, match="undeclared"):
            case.chunk("extra.bin", b"x", 0)
    for _ in range(12):
        case.chunk("weights.bin", WEIGHTS, 0)  # idempotent retries still count
    with pytest.raises(ValueError, match="chunk_budget_exhausted"):
        case.chunk("weights.bin", WEIGHTS, 1)
    with pytest.raises(ValueError, match="chunk_budget_exhausted"):
        case.upload("output.json", OUTPUT)
    assert stored_bytes(case) == C


def test_import_chunk_requires_confirmed_stop(case):
    case.begin()
    case.upload("output.json", OUTPUT)
    case.upload("weights.bin", WEIGHTS)
    with pytest.raises(ValueError, match="writers_not_stopped"):
        case.admin("import_chunk", name="weights.bin", seq=0)
    with pytest.raises(ValueError, match="invalid_admin_request"):
        case.admin("import_chunk", name="weights.bin")
    case.stop()
    with pytest.raises(ValueError):
        case.admin("import_chunk", name="weights.bin", seq=3)
    with pytest.raises(ValueError):
        case.admin("import_chunk", name="absent.bin", seq=0)


# --- backward compatibility -----------------------------------------------------------------


@pytest.fixture
def single(tmp_path):
    value = Case(tmp_path, outputs=[dict(name="output.json", max_bytes=4096)])
    yield value
    value.service.close()


def test_single_output_json_legacy_endpoint_keeps_the_original_import_shape(single):
    single.begin()
    output = {"result": 1}
    reply = single.service.workload(
        "/v1/workload/output",
        dict(binding=single.request, output=output, sha256=sha(encoded(output))),
        single.token,
    )
    assert reply["retained"] and single.guardian()["reason"] == "output_retained"
    single.stop()
    imported = single.client().import_retained(single.state())
    assert imported["output"] == output and set(imported) == {"manifest", "output"}


def test_single_output_json_can_also_use_chunked_custody(single):
    single.begin()
    assert single.upload("output.json", OUTPUT)["outputs_complete"]
    single.stop()
    imported = single.client().import_retained(single.state())
    assert imported["files"] == [dict(name="output.json", sha256=sha(OUTPUT), bytes=len(OUTPUT))]
    # The original status field reports a chunked output.json, verified from its parts.
    status = single.admin("status")
    assert status["retained_output"]["sha256"] == sha(OUTPUT)


def test_smoke_binding_has_no_chunked_endpoints(tmp_path):
    from test_salad_safety_service import reservation

    tmp_path.chmod(0o700)
    clock, provider = [100.0], Provider()
    key = ec.generate_private_key(ec.SECP256R1())
    s = make_service(tmp_path, lambda: clock[0], key, provider)
    try:
        body = reservation()
        s.loop()
        s.admin(body)
        s.admin(
            dict(operation="bind", reservation=body["reservation"], provider_id=GROUP,
                 allocation_id=None)
        )  # fmt: skip
        provider.state = "running"
        s.loop()
        request = s._request(body["proposal"]["selection"]["salad"]["execution"]["argv"])
        payload = claims()
        salad = body["proposal"]["selection"]["salad"]
        payload["salad_workload_name"] = "-".join(
            salad[k] for k in ("organization", "project", "group")
        )
        token = signed(key, payload)
        s.workload("/v1/workload/begin-once", dict(binding=request), token)
        with pytest.raises(ValueError, match="workload_rejected"):
            s.workload(PLAN, dict(binding=request), token)
        assert (
            s.custody.retained_outputs(
                CustodyBinding(**json.loads(s._row(body["reservation"])["binding"]))
            )
            is None
        )
    finally:
        s.close()


# --- custody store unit and schema convergence -----------------------------------------------


def private_root(tmp_path):
    root = tmp_path / "custody"
    root.mkdir(mode=0o700)
    return root


BINDING = CustodyBinding("t", "p", "op", 1, "artifact-ref:output")


def test_register_refuses_declared_outputs_beyond_the_total_or_file_cap(tmp_path):
    custody = SaladCustody(private_root(tmp_path) / "c.db")
    declared = [dict(name="a.bin", max_bytes=10), dict(name="b.bin", max_bytes=10)]
    with pytest.raises(ValueError, match="invalid_declared"):
        custody.register(BINDING, byte_cap=19, file_cap=2, expected_files=2, outputs=declared)
    with pytest.raises(ValueError, match="invalid_declared"):
        custody.register(BINDING, byte_cap=20, file_cap=3, expected_files=2, outputs=declared)
    with pytest.raises(ValueError, match="invalid_declared"):
        custody.register(
            BINDING, byte_cap=20, file_cap=2, expected_files=2, outputs=declared + declared[:1]
        )
    custody.register(BINDING, byte_cap=20, file_cap=2, expected_files=2, outputs=declared)
    custody.register(BINDING, byte_cap=20, file_cap=2, expected_files=2, outputs=declared[::-1])
    changed = [declared[0], dict(name="c.bin", max_bytes=10)]
    with pytest.raises(ValueError, match="inventory_mismatch"):
        custody.register(BINDING, byte_cap=20, file_cap=2, expected_files=2, outputs=changed)
    # The original ticket path is bound to the declared names and per-file caps too.
    expires = custody.clock() + 60
    with pytest.raises(ValueError, match="undeclared_or_oversize"):
        custody.issue(BINDING, name="c.bin", checksum="a" * 64, size=1, expires=expires)
    with pytest.raises(ValueError, match="undeclared_or_oversize"):
        custody.issue(BINDING, name="a.bin", checksum="a" * 64, size=11, expires=expires)


def test_partial_upload_resumes_after_custody_restart(tmp_path):
    path = private_root(tmp_path) / "c.db"
    custody = SaladCustody(path)
    custody.register(
        BINDING, byte_cap=3 * C, file_cap=1, expected_files=1,
        outputs=[dict(name="weights.bin", max_bytes=3 * C)],
    )  # fmt: skip

    def put(store, seq):
        part = WEIGHTS[seq * C : (seq + 1) * C]
        return store.put_chunk(
            BINDING, name="weights.bin", checksum=sha(WEIGHTS), size=len(WEIGHTS), seq=seq,
            chunk_sha256=sha(part), data=part, expires=1e12,
        )  # fmt: skip

    put(custody, 1)
    custody.db.close()
    resumed = SaladCustody(path)
    put(resumed, 0)
    assert put(resumed, 2) == dict(file_complete=True, outputs_complete=True)
    manifest = resumed.seal_operation(BINDING)
    assert manifest["complete"]
    assert manifest["inventories"][0]["files"] == [
        dict(name="weights.bin", sha256=sha(WEIGHTS), bytes=len(WEIGHTS))
    ]
    # Stored part corruption is caught on read, never served.
    resumed.db.execute("UPDATE parts SET data=? WHERE seq=1", (b"x" * C,))
    resumed.db.commit()
    with pytest.raises(ValueError, match="stored_integrity"):
        resumed.read_chunk(BINDING, "weights.bin", 1)


OLD_SCHEMA = """
CREATE TABLE inventories (
    binding TEXT PRIMARY KEY, byte_cap INTEGER, file_cap INTEGER,
    closed INTEGER NOT NULL DEFAULT 0, manifest TEXT, expected_files INTEGER DEFAULT 0);
CREATE TABLE tickets (
    digest TEXT PRIMARY KEY, binding TEXT, name TEXT, checksum TEXT,
    size INTEGER, expires REAL, consumed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(binding, name));
CREATE TABLE files (
    binding TEXT, name TEXT, checksum TEXT, size INTEGER, data BLOB,
    PRIMARY KEY(binding, name));
CREATE TABLE closed_scopes (scope TEXT PRIMARY KEY);
"""


def test_custody_store_upgrade_converges_and_keeps_legacy_retention(tmp_path):
    """The guardian-private store upgrades additively; the control schema is unchanged."""
    path = private_root(tmp_path) / "c.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    data = encoded({"result": 1})
    old.execute(
        "INSERT INTO inventories(binding,byte_cap,file_cap,expected_files) VALUES(?,?,?,?)",
        (BINDING.key(), 1024, 1, 1),
    )
    old.execute(
        "INSERT INTO tickets VALUES(?,?,?,?,?,?,1)",
        ("d" * 64, BINDING.key(), "output.json", sha(data), len(data), 1e12),
    )
    old.execute(
        "INSERT INTO files VALUES(?,?,?,?,?)",
        (BINDING.key(), "output.json", sha(data), len(data), data),
    )
    old.commit()
    old.close()
    path.chmod(0o600)
    first = SaladCustody(path)

    def shape(db):
        return {
            table: [tuple(r[1:3]) for r in db.execute(f"PRAGMA table_info({table})")]
            for table in ("inventories", "tickets", "files", "parts", "closed_scopes")
        }

    upgraded = shape(first.db)
    first.db.close()
    again = SaladCustody(path)
    assert shape(again.db) == upgraded  # idempotent
    (tmp_path / "fresh").mkdir()
    fresh = SaladCustody(private_root(tmp_path / "fresh") / "c.db")
    assert shape(fresh.db) == upgraded  # converges with a fresh store
    # Legacy retention (smoke shape) still registers, verifies, seals and reads.
    again.register(BINDING, byte_cap=1024, file_cap=1, expected_files=1)
    assert again.retained_metadata(BINDING, "output.json")["sha256"] == sha(data)
    assert again.retained_outputs(BINDING) is None
    manifest = again.seal_operation(BINDING)
    assert manifest["complete"] and again.read_retained(BINDING, "output.json") == data
    assert base64.b64decode(again.read_chunk(BINDING, "output.json", 0)["content_base64"]) == data


# --- HTTP body bounds -------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def workload(self, path, body, token, accepted_at=None):
        self.calls.append(path)
        return {"ok": True}


class _Conn:
    def settimeout(self, value):
        pass


def _post(path, length, body=b"{}"):
    handler = SafetyHandler.__new__(SafetyHandler)
    headers = email.message.Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(length)
    headers["Authorization"] = "Bearer token"
    handler.headers, handler.path = headers, path
    handler.rfile = io.BytesIO(body)
    handler.connection = _Conn()
    handler.server = type("Server", (), {"service": _Recorder(), "accepted_at": None})()
    replies = []
    handler._reply = lambda status, value: replies.append(status)
    handler.do_POST()
    return replies[0], handler.server.service.calls, handler.rfile.tell()


def test_body_caps_are_enforced_before_any_byte_is_read():
    assert OUTPUT_CHUNK_BODY_BYTES < 2 * C and REQUEST_BODY_BYTES == 131072
    status, calls, read = _post(CHUNK, OUTPUT_CHUNK_BODY_BYTES + 1)
    assert (status, calls, read) == (400, [], 0)
    # The larger chunk cap applies to the chunk path only.
    status, calls, read = _post(PLAN, REQUEST_BODY_BYTES + 1)
    assert (status, calls, read) == (400, [], 0)
    assert _post(CHUNK, 2)[:2] == (200, [CHUNK])
    assert _post(PLAN, 2)[:2] == (200, [PLAN])


# --- the real runtime upload code against the real guardian ----------------------------------


def _runtime():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "workloads/gpu_runtime_v1/runtime.py"
    spec = importlib.util.spec_from_file_location("gpu_runtime_v1_for_custody", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_wire_format_completes_guardian_custody(case, tmp_path):
    rt = _runtime()
    assert rt.OUTPUT_CHUNK_BYTES == C
    case.begin()
    custody = rt.HTTPSCustody.__new__(rt.HTTPSCustody)
    custody.binding, custody.token_expires = case.request, float("inf")
    # The runtime's exact JSON bodies, delivered to the guardian's workload handler.
    custody.post = lambda path, body: case.service.workload(
        path, json.loads(rt.encode(body)), case.token
    )
    request = dict(case.request, max_output_bytes=sum(o["max_bytes"] for o in OUTPUTS))
    plan = rt.validate_plan(custody.output_plan(), request)
    out = tmp_path / "out"
    out.mkdir()
    (out / "output.json").write_text('{"answer":42}')
    (out / "weights.bin").write_bytes(WEIGHTS)
    staged = rt.stage_outputs(str(out), str(tmp_path / "staged"), plan, [])
    result = rt.upload_outputs(custody, staged)
    assert case.guardian()["reason"] == "output_retained"
    case.stop()
    imported = case.client().import_retained(case.state())
    assert imported["files"] == sorted(result, key=lambda f: f["name"])


# --- review fixes: liveness, crash-safe completion, request budget, replay ack, import ---------


def test_slow_body_upload_does_not_age_loop_freshness(case):
    """Freshness is judged at acceptance; a body read slower than 2 s is still served."""
    case.begin()
    accepted = case.clock[0]
    case.clock[0] += 3.5  # the 1.34 MiB body took 3.5 s over a slow uplink
    reply = case.service.workload(
        CHUNK, _chunk_body(case, "output.json", OUTPUT, 0), case.token, accepted_at=accepted
    )
    assert reply["stored"] and reply["file_complete"]
    # A loop that was already stale when the request was accepted is still refused.
    case.clock[0] += 3
    with pytest.raises(ValueError, match="workload_rejected"):
        case.service.workload(
            CHUNK, _chunk_body(case, "weights.bin", WEIGHTS, 0), case.token,
            accepted_at=case.clock[0],
        )  # fmt: skip
    # An acceptance time in the future is not trusted either.
    case.service.loop()
    with pytest.raises(ValueError, match="workload_rejected"):
        case.service.workload(
            CHUNK, _chunk_body(case, "weights.bin", WEIGHTS, 0), case.token,
            accepted_at=case.clock[0] + 1,
        )  # fmt: skip


def test_http_listener_stamps_acceptance_before_the_body_is_read(case):
    case.begin()
    body = encoded(_chunk_body(case, "output.json", OUTPUT, 0))

    class SlowBody(io.BytesIO):
        def read1(self, n=-1):
            case.clock[0] += 3  # reading the body advances the clock past 2 s
            return super().read1(n)

    handler = SafetyHandler.__new__(SafetyHandler)
    headers = email.message.Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(body))
    headers["Authorization"] = "Bearer " + case.token
    handler.headers, handler.path, handler.rfile = headers, CHUNK, SlowBody(body)
    handler.connection = _Conn()
    handler.server = type("Server", (), {"service": case.service, "accepted_at": case.clock[0]})()
    replies = []
    handler._reply = lambda status, value: replies.append((status, value))
    handler.do_POST()
    assert replies[0][0] == 200 and replies[0][1]["outputs_complete"] is False
    # The same slow body with an acceptance stamp from a stale loop is refused.
    handler.rfile, handler.server.accepted_at = SlowBody(body), case.clock[0]
    case.clock[0] += 2.5
    handler.do_POST()
    assert replies[1][0] == 401


def _chunk_body(case, name, data, seq, part=None):
    part = data[seq * C : (seq + 1) * C] if part is None else part
    return dict(
        binding=case.request, name=name, sha256=sha(data), bytes=len(data), seq=seq,
        chunk_sha256=sha(part), content_base64=base64.b64encode(part).decode(),
    )  # fmt: skip


def test_interrupted_completion_after_stop_intent_still_stops_and_retry_closes(case, monkeypatch):
    case.begin()
    case.upload("output.json", OUTPUT)
    case.chunk("weights.bin", WEIGHTS, 0)
    case.chunk("weights.bin", WEIGHTS, 1)
    original = case.service.custody.close_writers

    def interrupted(binding):
        raise TimeoutError("service_deadline")  # the request alarm fires here

    monkeypatch.setattr(case.service.custody, "close_writers", interrupted)
    with pytest.raises(TimeoutError):
        case.chunk("weights.bin", WEIGHTS, 2)
    # The durable stop intent was recorded before the interruption.
    assert case.guardian()["reason"] == "output_retained"
    assert case.service._row(case.body["reservation"])["output"] == 1
    monkeypatch.setattr(case.service.custody, "close_writers", original)
    # The runtime's retry is acknowledged and closes writers.
    retry = case.chunk("weights.bin", WEIGHTS, 2)
    assert retry["outputs_complete"] and retry["file_complete"]
    assert case.service.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1


def test_interrupted_completion_before_stop_intent_is_repaired_by_retry(case, monkeypatch):
    case.begin()
    case.upload("output.json", OUTPUT)
    case.chunk("weights.bin", WEIGHTS, 0)
    case.chunk("weights.bin", WEIGHTS, 1)
    original = case.service.guardian.complete

    def interrupted(operation):
        raise TimeoutError("service_deadline")

    monkeypatch.setattr(case.service.guardian, "complete", interrupted)
    with pytest.raises(TimeoutError):
        case.chunk("weights.bin", WEIGHTS, 2)
    assert case.guardian()["reason"] == "armed"
    assert case.service._row(case.body["reservation"])["output"] == 0
    monkeypatch.setattr(case.service.guardian, "complete", original)
    retry = case.chunk("weights.bin", WEIGHTS, 2)  # idempotent duplicate re-completes
    assert retry["outputs_complete"]
    assert case.guardian()["reason"] == "output_retained"
    assert case.service.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
    assert stored_bytes(case) == len(WEIGHTS) + len(OUTPUT)


def test_lost_final_response_replay_is_acknowledged_without_reopening(case):
    case.begin()
    case.upload("output.json", OUTPUT)
    final = case.upload("weights.bin", WEIGHTS)
    assert final["outputs_complete"]
    # The guardian loop has already begun stopping the group.
    case.service.loop()
    assert case.guardian()["status"] == "unknown"
    assert case.guardian()["reason"] == "output_retained"
    replay = case.chunk("weights.bin", WEIGHTS, 2)
    assert replay == final
    # Nothing reopened, nothing stored, the stop intent stands.
    db = case.service.custody.db
    assert db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
    assert stored_bytes(case) == len(WEIGHTS) + len(OUTPUT)
    assert case.guardian()["reason"] == "output_retained"
    deadline = case.service._stop_deadline(case.service._row(case.body["reservation"]))
    assert case.service._row(case.body["reservation"])["recovery"] == 0
    assert deadline == case.service._stop_deadline(case.service._row(case.body["reservation"]))
    # Once confirmed stopped and sealed, even an exact replay is refused.
    case.stop()
    assert case.guardian()["status"] == "stopped"
    with pytest.raises(ValueError):
        case.chunk("weights.bin", WEIGHTS, 2)


def test_output_request_budget_is_charged_after_authentication_before_provider_read(case):
    case.begin()
    # Declared caps plan 1 + 3 chunks: 2 * 4 + 32 = 40 authenticated requests.
    bad = dict(_chunk_body(case, "output.json", OUTPUT, 0), content_base64="!!not base64!!")
    reads = []
    original = case.service._provider
    case.service._provider = lambda row: reads.append(1) or original(row)
    # An anonymous or forged caller is refused without charging the operation.
    forged = signed(ec.generate_private_key(ec.SECP256R1()), claims())
    for _ in range(50):
        with pytest.raises(ValueError):
            case.service.workload(CHUNK, bad, forged)
    assert case.service._row(case.body["reservation"])["output_requests"] == 0
    assert reads == []
    # Invalid bodies from the bound workload are charged.
    for _ in range(40):
        with pytest.raises(ValueError, match="workload_rejected"):
            case.service.workload(CHUNK, bad, case.token)
    assert case.service._row(case.body["reservation"])["output_requests"] == 40
    assert len(reads) == 40
    with pytest.raises(ValueError, match="output_request_budget_exhausted"):
        case.chunk("output.json", OUTPUT, 0)
    with pytest.raises(ValueError, match="output_request_budget_exhausted"):
        case.service.workload(PLAN, dict(binding=case.request), case.token)
    assert len(reads) == 40  # no provider read once exhausted
    assert stored_bytes(case) == 0


def _imported(case, tmp_path):
    case.begin()
    case.upload("weights.bin", WEIGHTS)
    case.upload("output.json", OUTPUT)
    case.stop()
    destination = tmp_path / "imported"
    destination.mkdir(mode=0o700)
    return destination


def test_import_resumes_after_a_partial_import(case, tmp_path, monkeypatch):
    import os

    destination = _imported(case, tmp_path)
    real_link = os.link
    calls = []

    def crash_on_second(src, dst, **kw):
        calls.append(dst)
        if len(calls) == 2:
            raise KeyboardInterrupt  # process dies before publishing the second file
        return real_link(src, dst, **kw)

    monkeypatch.setattr(os, "link", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        case.client().import_retained(case.state(), destination=destination)
    monkeypatch.setattr(os, "link", real_link)
    assert sorted(p.name for p in destination.iterdir()) == ["output.json"]
    # A crash can also leave a temporary file behind; it is never published as is.
    (destination / ".weights.bin.part").write_bytes(b"partial")
    result = case.client().import_retained(case.state(), destination=destination)
    assert [f["name"] for f in result["files"]] == ["output.json", "weights.bin"]
    assert (destination / "weights.bin").read_bytes() == WEIGHTS
    assert sorted(p.name for p in destination.iterdir()) == ["output.json", "weights.bin"]


def test_import_resumes_after_crash_between_link_and_part_unlink(case, tmp_path, monkeypatch):
    import os

    destination = _imported(case, tmp_path)
    real_unlink, armed = os.unlink, [True]

    def crash(path, *a, **kw):
        if armed[0] and str(path).endswith(".part"):
            armed[0] = False
            raise KeyboardInterrupt  # dies after os.link published, before unlink
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", crash)
    with pytest.raises(KeyboardInterrupt):
        case.client().import_retained(case.state(), destination=destination)
    monkeypatch.setattr(os, "unlink", real_unlink)
    names = sorted(p.name for p in destination.iterdir())
    assert names == [".output.json.part", "output.json"]
    assert (destination / "output.json").stat().st_nlink == 2
    result = case.client().import_retained(case.state(), destination=destination)
    assert [f["name"] for f in result["files"]] == ["output.json", "weights.bin"]
    assert sorted(p.name for p in destination.iterdir()) == ["output.json", "weights.bin"]
    assert (destination / "output.json").stat().st_nlink == 1


def test_import_does_not_block_on_a_fifo_destination(case, tmp_path):
    import os

    destination = _imported(case, tmp_path)
    os.mkfifo(destination / "weights.bin")
    with pytest.raises(ValueError, match="invalid retained output"):
        case.client().import_retained(case.state(), destination=destination)


@pytest.mark.parametrize("problem", ["differs", "symlink", "directory-part", "raced", "missing"])
def test_import_never_replaces_and_maps_filesystem_errors(case, tmp_path, monkeypatch, problem):
    import os

    destination = _imported(case, tmp_path)
    if problem == "differs":
        (destination / "weights.bin").write_bytes(b"other bytes")
    elif problem == "symlink":
        (destination / "weights.bin").symlink_to(tmp_path / "elsewhere")
    elif problem == "directory-part":
        (destination / ".weights.bin.part").mkdir()
    elif problem == "raced":
        real_link = os.link

        def raced(src, dst, **kw):
            raise FileExistsError(dst)  # the name appeared between check and publish

        monkeypatch.setattr(os, "link", raced)
    else:
        destination.rmdir()
    with pytest.raises(ValueError, match="invalid retained output"):
        case.client().import_retained(case.state(), destination=destination)
    if problem == "differs":
        assert (destination / "weights.bin").read_bytes() == b"other bytes"
    if problem == "raced":
        monkeypatch.setattr(os, "link", real_link)
        assert not any(p.name.endswith(".part") for p in destination.iterdir())


# --- second review: crash windows, body deadline, shapes, names --------------------------------


def test_crash_between_guardian_complete_and_output_flag_is_repaired_by_retry(case, monkeypatch):
    case.begin()
    case.upload("output.json", OUTPUT)
    case.chunk("weights.bin", WEIGHTS, 0)
    case.chunk("weights.bin", WEIGHTS, 1)
    real_db = case.service.db

    class DieOnFlag:
        def __getattr__(self, name):
            return getattr(real_db, name)

        def __enter__(self):
            return real_db.__enter__()

        def __exit__(self, *args):
            return real_db.__exit__(*args)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE reservations SET output=1"):
                raise KeyboardInterrupt  # process dies after guardian.complete
            return real_db.execute(sql, *args)

    monkeypatch.setattr(case.service, "db", DieOnFlag())
    with pytest.raises(KeyboardInterrupt):
        case.chunk("weights.bin", WEIGHTS, 2)
    monkeypatch.setattr(case.service, "db", real_db)
    case.service.next_poll[case.body["reservation"]] = 0
    case.service.loop()  # the guardian is now stopping: no longer armed
    assert case.guardian()["status"] == "unknown"
    assert case.guardian()["reason"] == "output_retained"
    assert case.service._row(case.body["reservation"])["output"] == 0
    retry = case.chunk("weights.bin", WEIGHTS, 2)
    assert retry["outputs_complete"] and retry["file_complete"]
    assert case.service._row(case.body["reservation"])["output"] == 1
    assert case.service.custody.db.execute("SELECT closed FROM inventories").fetchone()[0] == 1
    assert stored_bytes(case) == len(WEIGHTS) + len(OUTPUT)


def test_slow_trickled_body_is_cut_off_by_a_total_deadline(case, monkeypatch):
    from tessera_control import salad_safety_service as svc

    now = [0.0]
    monkeypatch.setattr(svc.time, "monotonic", lambda: now[0])
    body = b"x" * 1000

    class Trickle:
        reads = 0

        def read1(self, n):
            Trickle.reads += 1
            now[0] += 1.0  # one byte per second, each read within the socket timeout
            return b"x"

    class Conn:
        def settimeout(self, value):
            assert value <= 2

    handler = SafetyHandler.__new__(SafetyHandler)
    handler.rfile, handler.connection = Trickle(), Conn()
    with pytest.raises(ValueError, match="invalid_request"):
        handler._read_body(len(body))
    assert Trickle.reads <= svc.BODY_READ_SECONDS + 1 and svc.BODY_READ_SECONDS < 10
    assert handler.close_connection is True

    # A prompt body is read whole, in bounded pieces.
    class Fast:
        def __init__(self, data):
            self.data, self.sizes = data, []

        def read1(self, n):
            self.sizes.append(n)
            out, self.data = self.data[:n], self.data[n:]
            return out

    handler.rfile = Fast(b"y" * 200000)
    assert handler._read_body(200000) == b"y" * 200000
    assert max(handler.rfile.sizes) <= 65536


def test_single_output_json_chunked_returns_output_too(single, tmp_path):
    single.begin()
    assert single.upload("output.json", OUTPUT)["outputs_complete"]
    single.stop()
    imported = single.client().import_retained(single.state())
    assert imported["output"] == json.loads(OUTPUT)
    assert imported["files"][0]["name"] == "output.json"
    destination = tmp_path / "dest"
    destination.mkdir(mode=0o700)
    again = single.client().import_retained(single.state(), destination=destination)
    assert again["output"] == json.loads(OUTPUT)


def test_one_output_name_rule_everywhere():
    from tessera_control import salad_output_import
    from tessera_control.salad_custody import SaladCustody as CustodyStore
    from tessera_control.salad_workload_contract import OUTPUT_NAME_PATTERN, OutputFile

    rt = _runtime()
    assert rt._OUTPUT_NAME.pattern == OUTPUT_NAME_PATTERN
    assert salad_output_import._NAME.pattern == OUTPUT_NAME_PATTERN
    for bad in ["Weights.bin", "a b", "a\\b", "a\x01", "sub/dir", "..", ".x", "x" * 65, "a\n"]:
        with pytest.raises(ValueError):
            CustodyStore._declared([dict(name=bad, max_bytes=10)])
        with pytest.raises(ValueError):
            OutputFile(name=bad, max_bytes=10)
    assert CustodyStore._declared([dict(name="a-b_c.1", max_bytes=10)]) == {"a-b_c.1": 10}


def test_empty_file_with_non_empty_digest_fails_fast(case):
    case.begin()
    body = _chunk_body(case, "output.json", b"", 0, part=b"")
    body["sha256"] = sha(b"not empty")
    with pytest.raises(ValueError):
        case.service.workload(CHUNK, body, case.token)
    assert stored_bytes(case) == 0


# --- request time budget, pre-auth, reply, strict output ----------------------------------------


def _fake_clock_handler(svc, monkeypatch, rate, length):
    """A handler whose body arrives at ``rate`` bytes/s on a fake clock."""
    now = [0.0]
    monkeypatch.setattr(svc.time, "monotonic", lambda: now[0])

    class Paced:
        def __init__(self):
            self.left = length

        def read1(self, n):
            n = min(n, self.left)
            self.left -= n
            now[0] += n / rate
            return b"z" * n

    class Conn:
        def settimeout(self, value):
            assert value <= 2

    handler = SafetyHandler.__new__(SafetyHandler)
    handler.rfile, handler.connection = Paced(), Conn()
    return handler


def test_body_deadline_derives_from_the_documented_floor_rate(monkeypatch):
    from tessera_control import salad_safety_service as svc

    assert svc.BODY_FLOOR_BYTES_PER_SECOND == 187_500  # 1.5 Mbit/s
    # A maximal chunk envelope needs ~7.5 s at the floor; the deadline sits just above.
    assert 7.4 < OUTPUT_CHUNK_BODY_BYTES / svc.BODY_FLOOR_BYTES_PER_SECOND < 7.6
    assert svc.OUTPUT_CHUNK_BODY_SECONDS == pytest.approx(8.48, abs=0.01)
    assert svc.BODY_READ_SECONDS == pytest.approx(1.70, abs=0.01)
    assert (
        svc.HEADER_READ_SECONDS + svc.OUTPUT_CHUNK_BODY_SECONDS + svc.POST_BODY_WORK_SECONDS
        < svc.CHUNK_REQUEST_SECONDS
    )
    assert (
        svc.HEADER_READ_SECONDS + svc.BODY_READ_SECONDS + svc.POST_BODY_WORK_SECONDS
        < svc.REQUEST_SECONDS
    )
    n = OUTPUT_CHUNK_BODY_BYTES
    # At (just above) the floor rate a full chunk body is accepted ...
    handler = _fake_clock_handler(svc, monkeypatch, svc.BODY_FLOOR_BYTES_PER_SECOND * 1.05, n)
    assert len(handler._read_body(n)) == n
    # ... and a sender at 1.0 Mbit/s (2.2 Mbit/s was the old requirement) is cut off.
    slow = _fake_clock_handler(svc, monkeypatch, 125_000, n)
    with pytest.raises(ValueError, match="invalid_request"):
        slow._read_body(n)
    assert slow.close_connection is True
    # Small bodies stay tight: 128 KiB at 0.25 Mbit/s is refused.
    small = _fake_clock_handler(svc, monkeypatch, 31_250, REQUEST_BODY_BYTES)
    with pytest.raises(ValueError, match="invalid_request"):
        small._read_body(REQUEST_BODY_BYTES)


def _post_with(service, path, length, body=b"{}", auth="Bearer token"):
    handler = SafetyHandler.__new__(SafetyHandler)
    headers = email.message.Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(length)
    headers["Authorization"] = auth
    handler.headers, handler.path = headers, path
    handler.rfile = io.BytesIO(body)
    handler.connection = _Conn()
    extended = []
    handler.server = type(
        "Server",
        (),
        dict(
            service=service,
            accepted_at=None,
            admin_token=b"a" * 40,
            extend_deadline=staticmethod(extended.append),
        ),
    )()
    replies = []
    handler._reply = lambda status, value: replies.append(status)
    handler.do_POST()
    return replies[0], handler.rfile.tell(), extended


def test_pre_auth_runs_before_the_body_is_read_or_the_deadline_extended():
    class Jwt:
        def __init__(self, ok):
            self.ok = ok

        def precheck(self, token):
            if not self.ok:
                raise ValueError("workload_identity_rejected")

    class Service(_Recorder):
        def __init__(self, ok):
            super().__init__()
            self.jwt = Jwt(ok)

    bad = Service(False)
    assert _post_with(bad, CHUNK, 2) == (401, 0, [])  # no body byte read, no extension
    assert bad.calls == []
    good = Service(True)
    status, read, extended = _post_with(good, CHUNK, 2)
    assert (status, read, good.calls) == (200, 2, [CHUNK])
    from tessera_control import salad_safety_service as svc

    assert extended == [svc.CHUNK_REQUEST_SECONDS]
    assert _post_with(Service(True), PLAN, 2)[2] == [svc.REQUEST_SECONDS]
    # Admin: the token is compared before the body; a wrong one reads nothing.
    assert _post_with(good, "/v1/admin", 2, auth="Bearer wrong") == (401, 0, [])
    # Unknown paths are refused before any body byte is read.
    assert _post_with(good, "/v1/nope", 2) == (400, 0, [])


def test_real_jwt_precheck_needs_no_network_and_rejects_unsigned_or_expired(case):
    jwt = case.service.jwt
    assert jwt.precheck(case.token) is True
    head, claims_part, sig = case.token.split(".")
    for bad in (
        "garbage",
        f"{head}.{claims_part}.{'A' * len(sig)}",
        f"{head}.{claims_part[:-2]}AA.{sig}",
    ):
        with pytest.raises(ValueError, match="workload_identity_rejected"):
            jwt.precheck(bad)
    case.clock[0] += 400  # past exp; stale JWKS also fails closed
    with pytest.raises(ValueError, match="workload_identity_rejected"):
        jwt.precheck(case.token)


def test_reply_resets_the_socket_timeout_left_by_the_body_reader():
    class Conn:
        timeouts = []

        def settimeout(self, value):
            Conn.timeouts.append(value)

    handler = SafetyHandler.__new__(SafetyHandler)
    handler.connection = Conn()
    handler.wfile = io.BytesIO()
    handler.request_version, handler.requestline, handler.command = "HTTP/1.1", "POST /", "POST"
    handler._headers_buffer = []
    handler.client_address = ("127.0.0.1", 0)
    handler.server = type("S", (), dict(server_version="x", sys_version="y"))()
    handler.request_version = "HTTP/1.0"
    handler._reply(200, {"ok": True})
    assert Conn.timeouts == [2]
    assert b'{"ok":true}' in handler.wfile.getvalue()


def test_header_trickle_reconnects_cannot_starve_the_guardian_loop(tmp_path, monkeypatch):
    import socket
    import ssl
    import threading
    import time as real_time

    from test_salad_safety_service import provision_tls

    from tessera_control import salad_safety_service as svc

    tmp_path.chmod(0o700)
    provision_tls(tmp_path)
    monkeypatch.setattr(svc, "HEADER_READ_SECONDS", 0.4)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")

    class Service:
        def __init__(self):
            self.loops = []

        def clock(self):
            return real_time.time()

        def loop(self):
            self.loops.append(real_time.monotonic())

    service = Service()
    server = svc.SafetyHTTPServer(("127.0.0.1", 0), service, b"a" * 40, context=context)
    port = server.server_port
    stop = threading.Event()
    connects = []
    client_context = ssl.create_default_context(cafile=tmp_path / "cert.pem")

    def attacker():
        while not stop.is_set():
            try:
                raw = socket.create_connection(("127.0.0.1", port), timeout=2)
                tls = client_context.wrap_socket(raw, server_hostname="localhost")
                connects.append(1)
                for byte in b"POST /v1/admin HTTP/1.1\r\nX-A: " + b"a" * 200:
                    if stop.is_set():
                        break
                    tls.sendall(bytes([byte]))
                    real_time.sleep(0.05)
                tls.close()
            except OSError:
                real_time.sleep(0.01)

    thread = threading.Thread(target=attacker)
    thread.start()
    end = real_time.monotonic() + 3
    try:
        svc.serve(service, server, shutdown=lambda: real_time.monotonic() > end)
    finally:
        stop.set()
        thread.join(5)
        server.server_close()
    assert len(connects) >= 3
    assert len(service.loops) >= 6
    gaps = [b - a for a, b in zip(service.loops, service.loops[1:], strict=False)]
    # Each trickling connection is dropped near the 0.4 s header deadline (not 10 s).
    assert max(gaps) < 1.5


def test_empty_file_digest_is_checked_in_custody_and_manifest(case):
    case.begin()
    empty = sha(b"")
    ok = _chunk_body(case, "output.json", b"", 0, part=b"")
    assert ok["sha256"] == empty
    case.service.workload(CHUNK, ok, case.token)
    assert stored_bytes(case) == 0
    bad = _chunk_body(case, "weights.bin", b"", 0, part=b"")
    bad["sha256"] = sha(b"x")
    with pytest.raises(ValueError):
        case.service.workload(CHUNK, bad, case.token)
    # The importer refuses a manifest entry of zero bytes with a non-empty digest.
    from tessera_control.store import canonical_digest

    def manifest(entry_sha):
        inventory = dict(
            binding=expected_binding(case.state()),
            writers_closed=True,
            quarantine=True,
            complete=True,
            files=[dict(name="output.json", sha256=entry_sha, bytes=0)],
        )
        inventory["digest"] = canonical_digest(inventory)
        outer = dict(inventories=[inventory], writers_closed=True, quarantine=True, complete=True)
        outer["digest"] = canonical_digest(outer)
        return outer

    kwargs = dict(
        binding=expected_binding(case.state()), declared={"output.json": 10}, max_total=10
    )
    assert verify_manifest(manifest(empty), **kwargs)[0]["bytes"] == 0
    with pytest.raises(OutputImportError):
        verify_manifest(manifest(sha(b"x")), **kwargs)


@pytest.mark.parametrize("raw", [b"NaN", b"[Infinity]", b'{"a":1,"a":2}', b"\xff\xfe"])
def test_import_retained_output_parse_is_strict(single, raw):
    single.begin()
    single.upload("output.json", raw)
    single.stop()
    with pytest.raises(ValueError, match="invalid retained output"):
        single.client().import_retained(single.state())
