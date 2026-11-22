"""ADR 0036 phase 2 end to end: declared input, runtime fetch, delivery and setup (synthetic).

No Salad, Docker or network call: the guardian is the real service with a fake control
opener, the runtime is driven with fake custody or the real guardian handler, and the
live driver runs against local or concretely composed synthetic sessions.
"""

import base64
import copy
import io
import json
import os
import stat
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
import test_salad_input_delivery as tid
from pydantic import ValidationError
from test_gpu_runtime_v1 import BLOBS, OP, WATCHDOG, FakeCustody, mf, opener_for, rt, sha
from test_salad_input_delivery import case, server_case, tiny_chunks  # noqa: F401
from test_salad_live_check import LocalSession, concrete_session, drive
from test_salad_live_setup import inputs, private_file, setup_script  # noqa: F401
from test_salad_workload_contract import contract as base_contract
from test_salad_workload_contract import execution, workload_reservation

from tessera_control import salad_server
from tessera_control.remote_execution_models import ExecutionSelection
from tessera_control.salad_workload_admission import check_workload_admission
from tessera_control.salad_workload_contract import WorkloadContractV1, fetches_digest
from tessera_control.salad_workload_input import staged_input_manifest
from tessera_control.store import Conflict
from tessera_worker import chunked_inputs
from tessera_worker.chunked_inputs import materialize_chunked_snapshot
from tessera_worker.inputs import InputIntegrityError

# ADR 0039: Salad admits only a staged input declared public with evidence.
PUBLIC_INPUT = [
    "--data-class", "public", "--license", "CC0-1.0",
    "--source", "https://example.org/synthetic-fixture",
]  # fmt: skip


def input_args(directory):
    return ["--input-dir", str(directory), *PUBLIC_INPUT]


CHUNK = 8
# Real runtime constants, read before the autouse fixture shrinks the chunk size.
RT_DEFAULTS = dict(
    INPUT_CHUNK_BYTES=rt.INPUT_CHUNK_BYTES,
    INPUT_RANGE_BYTES=rt.INPUT_RANGE_BYTES,
    INPUT_REPLY_BYTES=rt.INPUT_REPLY_BYTES,
    MAX_INPUT_BYTES=rt.MAX_INPUT_BYTES,
    MAX_INPUT_FILES=rt.MAX_INPUT_FILES,
    MAX_INPUT_MANIFEST_BYTES=rt.MAX_INPUT_MANIFEST_BYTES,
)
FILES = {"data/train.jsonl": b'{"x":1}\n{"x":2}\n{"x":3}\n', "z.bin": bytes(range(19))}


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(rt, "INPUT_CHUNK_BYTES", CHUNK)
    monkeypatch.setattr(chunked_inputs, "CHUNK_BYTES", CHUNK)


def snapshot_of(files=FILES, entries=None, snapshot_id="snapshot_x"):
    """A chunked snapshot as the guardian relays it: manifest, digest, index, chunk bodies."""
    entries = entries or [
        dict(relative_path=p, size=len(d), sha256=sha(d), provenance="imported_unverified",
             classification="unclassified")
        for p, d in files.items()
    ]  # fmt: skip
    manifest = dict(
        root="input", entries=entries, exclusions=[], total_bytes=sum(e["size"] for e in entries)
    )
    digest = sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    index, bodies = [], {}
    for entry in entries:
        data = files.get(entry["relative_path"], b"\0" * entry["size"])
        for offset in range(0, entry["size"], CHUNK):
            part = data[offset : offset + CHUNK]
            item = dict(seq=len(index), relative_path=entry["relative_path"], offset=offset,
                        size=len(part), sha256=sha(part))  # fmt: skip
            index.append(item)
            bodies[item["seq"]] = dict(
                item, snapshot_id=snapshot_id, content_base64=base64.b64encode(part).decode()
            )
    return dict(id=snapshot_id, digest=digest, manifest=manifest, chunks=index, bodies=bodies)


class InputCustody(FakeCustody):
    """FakeCustody plus the guardian's /v1/workload/input replies, with tamper hooks."""

    def __init__(self, log, snap, *, fail=None, tamper=None):
        super().__init__(log)
        self.snap, self.fail, self.tamper = snap, dict(fail or {}), tamper
        self.input_calls = []
        self.ranges = []

    def input_request(self, body):
        key = body["request"] if body["request"] == "manifest" else body["seq"]
        self.input_calls.append(key)
        self.log.append(("input", key))
        if self.fail.get(key):
            self.fail[key] -= 1
            raise rt.Failure("custody")
        if key == "manifest":
            reply = dict(schema="tessera.input-manifest.v1", snapshot_id=self.snap["id"],
                         digest=self.snap["digest"], manifest=self.snap["manifest"],
                         delivery="chunked", chunks=self.snap["chunks"])  # fmt: skip
        else:
            # The guardian's range reply, cut from the stored chunk body.
            stored = self.snap["bodies"][key]
            content = base64.b64decode(stored["content_base64"])
            offset, length = body["offset"], body["length"]
            part = content[offset : offset + length]
            reply = dict(schema="tessera.input-range.v1", snapshot_id=stored["snapshot_id"],
                         seq=key, offset=offset, length=length,
                         content_base64=base64.b64encode(part).decode())  # fmt: skip
            self.ranges.append((key, offset, length))
        reply = copy.deepcopy(reply)
        if self.tamper:
            self.tamper(key, reply)
        return reply


def argv_for(snap=None, *, digest=None, max_bytes=10_000, manifest=None, from_input=None):
    raw = mf.canonical(manifest or rt_manifest())
    tail = []
    if snap is not None or digest is not None:
        tail.append(f"input:{digest or snap['digest']}:{max_bytes}")
    if from_input:
        tail.append(f"input-manifest:{from_input[0]}:{from_input[1]}")
    else:
        tail += mf.refs(raw)
    return ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", "4096", "1", sha(raw), *tail]


def rt_manifest(blobs=BLOBS):
    rows = [{"url": u, "sha256": sha(d), "max_bytes": 1000, "kind": k, "dest": dst}
            for (u, k, dst), d in blobs.items()]  # fmt: skip
    return {"schema": rt.MANIFEST_SCHEMA, "fetches": rows}


def run(tmp_path, argv, custody, entry=None, opener=None):
    seen = {}

    def entry_runner(path, work, site, out_dir, dl):
        custody.log.append("entry")
        root = Path(work) / "input"
        seen["files"] = {
            str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
        } if root.exists() else None  # fmt: skip
        seen["modes"] = {p.name: stat.S_IMODE(p.stat().st_mode) for p in root.rglob("*")} if (
            root.exists()) else None  # fmt: skip
        (Path(out_dir) / "output.json").write_text('{"ok":1}')
        if entry:
            entry(Path(work))

    result = rt.execute(
        rt.parse_argv(argv), time.monotonic() + 120, str(tmp_path / "work"),
        custody_factory=lambda b, d: custody, opener=opener or opener_for(BLOBS),
        installer=lambda *a: custody.log.append("install") or "site", entry_runner=entry_runner,
    )  # fmt: skip
    return result, seen


def kinds(log):
    out = []
    for e in log:
        name = e if isinstance(e, str) else e[0]
        if not out or out[-1] != name:
            out.append(name)
    return out


# ---------------------------------------------------------------- runtime: argv


def test_argv_input_forms_and_backward_compatibility():
    snap = snapshot_of()
    old = argv_for()
    assert rt.parse_argv(old)["input"] is None  # existing workload_v1 argv unchanged
    request = rt.parse_argv(argv_for(snap))
    assert request["input"] == {"digest": snap["digest"], "max_bytes": 10_000}
    good = argv_for(snap, from_input=("f" * 64, "runtime/manifest.json"))
    assert rt.parse_argv(good)["manifest_in_input"] == {
        "fetches_sha256": "f" * 64, "path": "runtime/manifest.json"}  # fmt: skip
    for bad in (
        [*old[:9], f"input:{'A' * 64}:1", *old[9:]],
        [*old[:9], f"input:{'a' * 64}:0", *old[9:]],
        [*old[:9], f"input:{'a' * 64}:{rt.MAX_INPUT_BYTES + 1}", *old[9:]],
        [*old[:9], f"input:{'a' * 64}", *old[9:]],
        [*old[:9], "input-manifest:" + "f" * 64 + ":m.json"],  # needs a declared input
        [*good[:10], "input-manifest:" + "f" * 64 + ":../m.json"],
        [*good[:10], "input-manifest:" + "f" * 64 + ":/m.json"],
        [*good[:10], "input-manifest:" + "f" * 64 + ":a/../m.json"],
        [*good, "b64:abc"],  # one manifest source only
    ):
        with pytest.raises(rt.Failure):
            rt.parse_argv(bad)


# ---------------------------------------------------------- runtime: fetch input


def test_declared_input_is_fetched_after_begin_and_materialized_read_only(tmp_path):
    snap = snapshot_of()
    custody = InputCustody([], snap)
    result, seen = run(tmp_path, argv_for(snap), custody)
    assert kinds(custody.log) == ["install", "begin", "plan", "input", "entry", "chunk"]
    assert seen["files"] == FILES
    assert {m for n, m in seen["modes"].items() if n in {"train.jsonl", "z.bin"}} == {0o444}
    assert seen["modes"]["data"] == 0o555
    assert custody.input_calls == ["manifest", *range(len(snap["chunks"]))]
    assert result[0]["name"] == "output.json"


def test_no_declared_input_never_asks_for_input(tmp_path):
    custody = InputCustody([], snapshot_of())
    _, seen = run(tmp_path, argv_for(), custody)
    assert custody.input_calls == [] and seen["files"] is None


def _refused(tmp_path, argv, custody, code):
    with pytest.raises(rt.Failure) as err:
        run(tmp_path, argv, custody)
    assert err.value.code == code
    assert "entry" not in custody.log and not any(
        isinstance(e, tuple) and e[0] == "chunk" for e in custody.log
    )
    assert not (tmp_path / "work" / "input").exists()  # the partial tree is removed


def test_tampered_chunk_is_refused_without_retry(tmp_path):
    snap = snapshot_of()

    def tamper(key, reply):
        if key == 2:
            data = bytearray(base64.b64decode(reply["content_base64"]))
            data[0] ^= 1
            reply["content_base64"] = base64.b64encode(bytes(data)).decode()

    custody = InputCustody([], snap, tamper=tamper)
    _refused(tmp_path, argv_for(snap), custody, "input_chunk")
    assert custody.input_calls.count(2) == 1  # verification failures are never retried


def test_consistently_relabelled_chunk_still_fails_the_file_hash(tmp_path):
    """Chunk and index rewritten together (guardian bug or lie) still fail the file sha256."""
    snap = snapshot_of()
    evil = b"EVIL!!!!"
    body = snap["bodies"][0]
    body.update(sha256=sha(evil), content_base64=base64.b64encode(evil).decode())
    snap["chunks"][0]["sha256"] = sha(evil)
    _refused(tmp_path, argv_for(snap), InputCustody([], snap), "input_file")


@pytest.mark.parametrize("change", ["entry_sha", "entry_size", "extra_entry", "root"])
def test_tampered_manifest_fails_the_bound_digest(tmp_path, change):
    snap = snapshot_of()

    def tamper(key, reply):
        if key != "manifest":
            return
        entries = reply["manifest"]["entries"]
        if change == "entry_sha":
            entries[0]["sha256"] = "0" * 64
        elif change == "entry_size":
            entries[0]["size"] += 1
            reply["manifest"]["total_bytes"] += 1
        elif change == "extra_entry":
            entries.append(dict(entries[0], relative_path="extra"))
        else:
            reply["manifest"]["root"] = "elsewhere"

    _refused(tmp_path, argv_for(snap), InputCustody([], snap, tamper=tamper), "input_digest")


def test_wrong_snapshot_digest_is_refused_before_any_chunk(tmp_path):
    snap, other = snapshot_of(), snapshot_of({"o.txt": b"other"})
    custody = InputCustody([], other)  # guardian serves a different (self-consistent) snapshot
    _refused(tmp_path, argv_for(digest=snap["digest"]), custody, "input_digest")
    assert custody.input_calls == ["manifest"]


def test_oversize_input_is_refused_before_any_chunk(tmp_path):
    snap = snapshot_of()
    total = snap["manifest"]["total_bytes"]
    custody = InputCustody([], snap)
    _refused(tmp_path, argv_for(snap, max_bytes=total - 1), custody, "input_size")
    assert custody.input_calls == ["manifest"]
    run(tmp_path / "ok", argv_for(snap, max_bytes=total), InputCustody([], snap))


@pytest.mark.parametrize(
    "name",
    ["../escape", "/abs", "a/../b", "a/./b", "a//b", "a\\b", "C:x", ".", "", "a/\0b",
     "x/" * 64 + "y", "\ud800"],
)  # fmt: skip
def test_path_traversal_names_are_refused(tmp_path, name):
    entries = [dict(relative_path=name, size=0, sha256=sha(b""), provenance="imported_unverified",
                    classification="unclassified")]  # fmt: skip
    snap = snapshot_of({}, entries=entries)
    _refused(tmp_path, argv_for(snap), InputCustody([], snap), "input")
    assert not list(tmp_path.glob("**/escape"))


@pytest.mark.parametrize("pair", [("a", "a/b"), ("Data/x", "data/y"), ("café", "café")])
def test_aliasing_and_file_directory_collisions_are_refused(tmp_path, pair):
    files = {p: b"1" for p in pair}
    snap = snapshot_of(files)
    _refused(tmp_path, argv_for(snap), InputCustody([], snap), "input")


def test_expected_input_not_delivered_fails_with_bounded_retries(tmp_path):
    snap = snapshot_of()
    custody = InputCustody([], snap, fail={"manifest": 99})  # guardian holds no ticket
    _refused(tmp_path, argv_for(snap), custody, "input_fetch")
    assert custody.input_calls == ["manifest"] * rt.INPUT_CHUNK_ATTEMPTS
    # Transient failures within the per-item and overall budget are retried.
    flaky = InputCustody([], snap, fail={"manifest": 1, 0: 2, 1: 2})
    run(tmp_path / "flaky", argv_for(snap), flaky)
    exhausted = InputCustody([], snap, fail={i: 2 for i in range(5)})  # 10 > budget 8
    _refused(tmp_path / "exhausted", argv_for(snap), exhausted, "input_fetch")


def test_malformed_guardian_replies_are_refused(tmp_path):
    snap = snapshot_of()
    for tamper in (
        lambda k, r: k == "manifest" and r.update(delivery="inline"),
        lambda k, r: k == "manifest" and r.update(extra=1),
        lambda k, r: k == 0 and r.update(schema="other"),
        lambda k, r: k == 0 and r.update(seq=1),
        lambda k, r: k == 0 and r.update(snapshot_id="other"),
        lambda k, r: k == "manifest" and r["chunks"].pop(),
    ):
        custody = InputCustody([], snap, tamper=tamper)
        with pytest.raises(rt.Failure):
            run(tmp_path / str(id(tamper)), argv_for(snap), custody)
        assert "entry" not in custody.log


def test_restart_wipes_a_read_only_input_tree(tmp_path):
    snap = snapshot_of()
    run(tmp_path, argv_for(snap), InputCustody([], snap))
    work = tmp_path / "work"
    assert stat.S_IMODE((work / "input").stat().st_mode) == 0o555
    rt.prepare_work(str(work))
    assert sorted(os.listdir(work)) == ["out"]


# ----------------------------------------------------- runtime: manifest in input


def manifest_snapshot(manifest_bytes, extra=None):
    files = dict(FILES, **{"runtime/manifest.json": manifest_bytes}, **(extra or {}))
    return snapshot_of(files)


def test_fetch_manifest_carried_in_the_input(tmp_path):
    raw = mf.canonical(rt_manifest())
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in rt_manifest()["fetches"]]
    snap = manifest_snapshot(raw)
    calls = []
    custody = InputCustody([], snap)
    argv = argv_for(snap, from_input=(fetches_digest(rows), "runtime/manifest.json"))
    result, seen = run(tmp_path, argv, custody, opener=opener_for(BLOBS, calls))
    # Fetches and install now follow the verified input (the claim is already spent).
    assert kinds(custody.log) == ["begin", "plan", "input", "install", "entry", "chunk"]
    assert len(calls) == len(BLOBS)
    assert json.loads(custody.file("output.json"))["fetched"][0]["url"] == calls[0]


def test_manifest_from_input_with_sha_mismatch_is_refused_before_any_fetch(tmp_path):
    raw = mf.canonical(rt_manifest())
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in rt_manifest()["fetches"]]
    other = mf.canonical(rt_manifest({k: v + b"!" for k, v in BLOBS.items()}))
    snap = manifest_snapshot(other)  # snapshot carries a different manifest than argv pins
    argv = argv_for(snap, manifest=rt_manifest(), from_input=(fetches_digest(rows),
                    "runtime/manifest.json"))  # fmt: skip
    assert argv[8] == sha(raw)
    calls = []
    custody = InputCustody([], snap)
    with pytest.raises(rt.Failure) as err:
        run(tmp_path, argv, custody, opener=opener_for(BLOBS, calls))
    assert err.value.code == "manifest_digest" and calls == [] and "entry" not in custody.log


def test_manifest_from_input_rows_must_hash_to_the_reviewed_fetches(tmp_path):
    raw = mf.canonical(rt_manifest())
    snap = manifest_snapshot(raw)
    argv = argv_for(snap, from_input=("0" * 64, "runtime/manifest.json"))
    calls = []
    custody = InputCustody([], snap)
    with pytest.raises(rt.Failure) as err:
        run(tmp_path, argv, custody, opener=opener_for(BLOBS, calls))
    assert err.value.code == "manifest_fetches" and calls == []


def test_manifest_from_input_missing_or_symlinked_is_refused(tmp_path):
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in rt_manifest()["fetches"]]
    snap = snapshot_of()  # no runtime/manifest.json inside
    argv = argv_for(snap, from_input=(fetches_digest(rows), "runtime/manifest.json"))
    with pytest.raises(rt.Failure) as err:
        run(tmp_path, argv, InputCustody([], snap))
    assert err.value.code == "manifest"
    root = tmp_path / "tree"
    root.mkdir()
    (root / "real.json").write_bytes(b"{}")
    (root / "link.json").symlink_to(root / "real.json")
    request = rt.parse_argv(argv_for(snap, from_input=("0" * 64, "link.json")))
    with pytest.raises(rt.Failure):
        rt.load_input_manifest(request, str(root))


def test_file_manifest_raises_the_fetch_count_but_keeps_byte_caps():
    many = {(f"https://w.example.com/p{i}-1-py3-none-any.whl", "wheel",
             f"p{i}-1-py3-none-any.whl"): b"w" for i in range(127)}  # fmt: skip
    many[("https://d.example.com/e.py", "entry", "e.py")] = b"e"
    manifest = rt_manifest(many)
    raw = mf.canonical(manifest)
    assert len(raw) > rt.MAX_MANIFEST_BYTES // 3  # would not fit 32 argv tokens of 256 chars
    assert len(rt.validate_manifest(manifest)["fetches"]) == 128
    too_many = rt_manifest({**many, ("https://w.example.com/x.whl", "wheel", "x.whl"): b"x"})
    with pytest.raises(rt.Failure):
        rt.validate_manifest(too_many)
    big = rt_manifest()
    for row in big["fetches"]:
        row["max_bytes"] = 8589934592
    rt.validate_manifest(big)
    big["fetches"][0]["max_bytes"] += 1
    with pytest.raises(rt.Failure):
        rt.validate_manifest(big)


# ------------------------------------------- runtime and worker verifiers agree


MUTATIONS = {
    "valid": lambda s: None,
    "index_short": lambda s: s["chunks"].pop(),
    "index_seq": lambda s: s["chunks"][0].update(seq=5),
    "index_offset": lambda s: s["chunks"][1].update(offset=0),
    "index_extra_key": lambda s: s["chunks"][0].update(x=1),
    "chunk_bytes": lambda s: s["bodies"][1].update(
        content_base64=base64.b64encode(b"x" * 8).decode()
    ),
    "manifest_digest": lambda s: s.update(digest="0" * 64),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_runtime_verifier_matches_the_worker_materializer(tmp_path, mutation):
    snap = snapshot_of()
    expected = snap["digest"]
    MUTATIONS[mutation](snap)
    worker_snapshot = dict(snap, delivery="chunked")
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    try:
        materialize_chunked_snapshot(
            worker_snapshot, lambda seq: snap["bodies"][seq], parent,
            expected_snapshot_digest=expected,
        )  # fmt: skip
        worker_ok = True
    except (InputIntegrityError, KeyError):
        worker_ok = False
    custody = InputCustody([], snap)
    try:
        run(tmp_path / "rt", argv_for(digest=expected), custody)
        runtime_ok = True
    except rt.Failure:
        runtime_ok = False
    assert runtime_ok == worker_ok == (mutation == "valid")


# ------------------------------------------------ runtime against the real guardian


def test_runtime_wire_format_pulls_through_the_real_guardian(case, tmp_path):  # noqa: F811
    tid.give(case)
    custody = rt.HTTPSCustody.__new__(rt.HTTPSCustody)
    custody.binding, custody.token_expires = case.request, float("inf")
    custody.post = lambda path, body, cap=4096, timeout=10: case.service.workload(
        path, json.loads(rt.encode(body)), case.token
    )
    expected = {"digest": case.snapshot.digest, "max_bytes": 1000}
    work = tmp_path / "work"
    work.mkdir()
    root = rt.fetch_input(custody, expected, str(work), time.monotonic() + 60)
    got = {p.name: p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}
    assert got == tid.FILES
    status = case.service.inputs.status(case.request["operation"])
    assert status["bytes_served"] == sum(len(v) for v in tid.FILES.values())
    with pytest.raises(rt.Failure) as err:
        rt.fetch_input(custody, dict(expected, digest="0" * 64), str(tmp_path / "w2"),
                       time.monotonic() + 60)  # fmt: skip
    assert err.value.code == "input_digest"


# ------------------------------------------------------------------- contract


def input_contract(digest, *, manifest_in_input=False, max_bytes=5000, **over):
    fetches = [
        dict(url="https://w.example.com/p.whl", sha256="1" * 64, max_bytes=10),
        dict(url="https://d.example.com/e.py", sha256="2" * 64, max_bytes=20),
    ]
    manifest = {"schema": rt.MANIFEST_SCHEMA, "fetches": [
        {**f, "kind": k, "dest": d}
        for f, k, d in zip(fetches, ("wheel", "entry"), ("p-1-py3-none-any.whl", "e.py"),
                           strict=True)
    ]}  # fmt: skip
    raw = mf.canonical(manifest)
    tail = [f"input:{digest}:{max_bytes}"]
    if manifest_in_input:
        rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in fetches]
        tail.append(f"input-manifest:{fetches_digest(rows)}:runtime/manifest.json")
    else:
        tail += mf.refs(raw)
    value = dict(
        contract="tessera.salad.workload.v1", runtime="gpu_runtime_v1",
        argv_template=["v1", "{operation}", "{epoch}", "{custody_host}", WATCHDOG, "120",
                       "4096", "1", sha(raw), *tail],
        env={}, workload_seconds=120, outputs=[dict(name="output.json", max_bytes=4096)],
        egress=dict(kind="provider_broad", fetches=fetches), custody_host="localhost",
        input=dict(kind="staged_snapshot", snapshot_digest=digest, max_bytes=max_bytes),
    )  # fmt: skip
    value.update(over)
    return value, raw


def test_contract_input_declaration_binds_the_argv_token():
    for inline in (False, True):
        value, _ = input_contract("d" * 64, manifest_in_input=inline)
        model = WorkloadContractV1.model_validate(value)
        assert model.input.snapshot_digest == "d" * 64
        request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
        assert request["input"] == {"digest": "d" * 64, "max_bytes": 5000}


@pytest.mark.parametrize(
    "edit",
    [
        lambda v: v["argv_template"].__setitem__(9, f"input:{'e' * 64}:5000"),
        lambda v: v["argv_template"].__setitem__(9, f"input:{'d' * 64}:5001"),
        lambda v: v["argv_template"].__setitem__(9, f"input:{'d' * 64}"),
        lambda v: v["argv_template"].pop(9),  # declared but no token
        lambda v: v.pop("input"),  # token but no declaration
        lambda v: v.update(
            input=dict(kind="staged_snapshot", snapshot_digest="d" * 64, max_bytes=2**31 + 1)
        ),  # fmt: skip
        lambda v: v["argv_template"].__setitem__(
            10, "input-manifest:" + "0" * 64 + ":runtime/manifest.json"
        ),
        lambda v: v["argv_template"].__setitem__(
            10, v["argv_template"][10].rsplit(":", 1)[0] + ":../m.json"
        ),
        lambda v: v["argv_template"].append("b64:abc"),
    ],
)
def test_contract_input_mismatches_are_refused(edit):
    value, _ = input_contract("d" * 64, manifest_in_input=True)
    edit(value)
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(value)


def test_input_manifest_ref_requires_a_declared_input():
    value, _ = input_contract("d" * 64, manifest_in_input=True)
    value.pop("input")
    value["argv_template"].pop(9)
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(value)


def test_contract_without_input_keeps_its_serialized_shape():
    model = WorkloadContractV1.model_validate(base_contract())
    assert "input" not in model.model_dump(mode="json")
    assert model.input is None


def test_selection_binds_input_to_manifests_data_and_bounds_disk():
    from test_salad_http import selection

    value = selection().model_dump(mode="json")
    data = value["manifests"]["data"]
    work = base_contract(input=dict(kind="staged_snapshot", snapshot_digest=data, max_bytes=100))
    value["salad"]["execution"] = execution(work)
    ExecutionSelection.model_validate(value)
    wrong = copy.deepcopy(value)
    wrong["salad"]["execution"]["workload"]["input"]["snapshot_digest"] = "f" * 64
    with pytest.raises(ValidationError, match="manifests.data"):
        ExecutionSelection.model_validate(wrong)
    # Disk is an admission bound, not a parse bound: an over-disk stored route still parses.
    big = copy.deepcopy(value)
    big["salad"]["execution"]["workload"]["input"]["max_bytes"] = 2048 * 1024 * 1024
    ExecutionSelection.model_validate(big)
    with pytest.raises(ValueError, match="disk_mb"):
        check_workload_admission(None, "project", big)


# ------------------------------------------------------------------- guardian


def _guardian(tmp_path, work):
    from cryptography.hazmat.primitives.asymmetric import ec
    from test_salad_safety_service import Provider, make_service

    tmp_path.chmod(0o700)
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), Provider(),
                     control_origin=tid.ORIGIN, control_opener=lambda *a: None)  # fmt: skip
    s.loop()
    body = workload_reservation(work)
    s.admin(body)
    return s, body


def test_guardian_refuses_a_ticket_for_a_workload_without_declared_input(tmp_path):
    s, body = _guardian(tmp_path, base_contract())
    try:
        snap = body["proposal"]["selection"]["manifests"]["data"]
        ticket = dict(token="t" * 40, expires_at=tid.expires(), snapshot_id="s",
                      snapshot_digest=snap)  # fmt: skip
        with pytest.raises(ValueError, match="input_not_declared"):
            s.admin(dict(operation="input_ticket", reservation=body["reservation"],
                         input_ticket=ticket))  # fmt: skip
        assert s.inputs.status(body["proposal"]["operation_id"]) == dict(ticket_held=False)
    finally:
        s.close()


def test_guardian_caps_a_declared_input_at_its_reviewed_max_bytes(tmp_path):
    from test_salad_http import selection

    data = selection().manifests.data
    work = base_contract(input=dict(kind="staged_snapshot", snapshot_digest=data, max_bytes=77))
    s, body = _guardian(tmp_path, work)
    try:
        ticket = dict(token="t" * 40, expires_at=tid.expires(), snapshot_id="s",
                      snapshot_digest=data)  # fmt: skip
        s.admin(dict(operation="input_ticket", reservation=body["reservation"],
                     input_ticket=ticket))  # fmt: skip
        session = s.inputs._sessions[body["proposal"]["operation_id"]]
        assert session.max_bytes == 77 and session.digest == data
    finally:
        s.close()


# -------------------------------------------------------------- trusted server


def test_server_revoke_inputs_forces_control_revoke_and_skips_routes_without_input(
    server_case,  # noqa: F811
):
    h = server_case
    calls = []
    original = h.server.inputs.revoke_tickets
    h.server.inputs.revoke_tickets = lambda *a, **k: calls.append(a) or original(*a, **k)
    # Smoke route, nothing held: untouched.
    assert h.server.revoke_inputs(h.route_id) is False and calls == []
    h.server.deliver_inputs(h.route_id)
    assert h.server.revoke_inputs(h.route_id) is True
    assert [r["revoked_at"] is not None for r in tid.rows(h)] == [True]
    # After a reconnect (no in-memory state) a route that declares an input still revokes.
    h.server._input_state.clear()
    h.server.deliver_inputs(h.route_id)
    h.server._input_state.clear()
    original_route_input = salad_server.route_input
    salad_server.route_input = lambda proposal: object()
    try:
        assert h.server.revoke_inputs(h.route_id) is True
    finally:
        salad_server.route_input = original_route_input
    assert all(r["revoked_at"] is not None for r in tid.rows(h))
    assert len(calls) == 2


def test_server_refuses_delivery_for_a_workload_route_without_declared_input(
    server_case,  # noqa: F811
    monkeypatch,
):
    h = server_case
    state = h.store.get(h.worker, h.route_id)
    work_execution = execution(base_contract())
    proposal = copy.deepcopy(state["proposal"])
    proposal["selection"]["salad"]["execution"] = work_execution
    monkeypatch.setattr(h.server.store, "get", lambda *a: dict(state, proposal=proposal))
    monkeypatch.setattr(h.server.store, "_bound", lambda state: True)
    with pytest.raises(Conflict, match="declares no input"):
        h.server.deliver_inputs(h.route_id)
    assert tid.rows(h) == [] and tid.tickets_sent(h) == []


# ----------------------------------------------------------------- live driver


def test_driver_delivers_input_before_start_and_revokes_on_completion():
    session = LocalSession()
    session.expects_input = True
    receipt = drive(session)
    assert receipt["status"] == "passed"
    assert receipt["input_expected"] and receipt["input_delivered"] and receipt["input_revoked"]
    # deliver happens while the created group is stopped, strictly before start.
    assert session.actions.index("start") > 0 and session.input_calls[0] == "deliver"
    assert session.input_calls[-2:] == ["cancel", "revoke"]


def test_delivery_failure_before_start_never_starts_and_still_stops():
    session = LocalSession()
    session.expects_input = True
    session.deliver_error = RuntimeError("guardian refused the ticket")
    receipt = drive(session)
    assert "start" not in session.actions and "stop" in session.actions
    assert receipt["status"] == "inconclusive" and not receipt["input_delivered"]
    assert session.input_calls == ["deliver", "cancel", "revoke"]


def test_routes_without_input_are_never_delivered():
    session = LocalSession()
    receipt = drive(session)
    assert receipt["status"] == "passed" and "deliver" not in session.input_calls
    assert receipt["input_expected"] is False and receipt["input_revoked"] is False


def test_cancel_path_and_failed_revoke_still_stop():
    session = LocalSession()
    session.expects_input = True
    session.fail = "observe"
    session.revoke_error = RuntimeError("control unreachable")
    receipt = drive(session)
    assert receipt["status"] == "inconclusive" and receipt["input_revoked"] is False
    assert session.input_calls[-2:] == ["cancel", "revoke"] and "stop" in session.actions


def test_reconnect_never_redelivers_but_revokes():
    session = LocalSession(prior=True)
    session.expects_input = True
    drive(session)
    assert session.input_calls == ["cancel", "revoke"]


def test_concrete_keepalive_refreshes_the_input_ticket(tmp_path, monkeypatch):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    route = h.route["id"]
    server = session.server
    real = server.store.preflight_authority
    monkeypatch.setattr(
        server.store, "preflight_authority",
        lambda *a, **k: dict(real(*a, **k), effects=[{"action": "create"}]),
    )  # fmt: skip
    monkeypatch.setattr(server.safety, "heartbeat", lambda state: None)
    minted = []
    monkeypatch.setattr(server, "deliver_inputs", lambda route_id: minted.append(route_id))
    server._input_state[route] = dict(expires_at=time.time() + 30, lifetime=3600.0)
    session.keepalive(route)
    assert minted == [route]
    server._input_state[route] = dict(expires_at=time.time() + 3000, lifetime=3600.0)
    session.keepalive(route)
    assert minted == [route]  # not yet due
    session.close()
    h.server.resolver.close()


def test_concrete_delivery_requires_guardian_confirmation_of_the_bound_digest(
    tmp_path, monkeypatch
):
    session, h, _ = concrete_session(tmp_path, monkeypatch)
    route = h.route["id"]
    with pytest.raises(ValueError, match="declares no input"):
        session.deliver_inputs(route)  # the fixture route is a smoke route
    data = session.state(route)["proposal"]["selection"]["manifests"]["data"]
    declared = type("D", (), {"snapshot_digest": data})()
    monkeypatch.setattr(salad_server, "route_input", lambda proposal: declared)
    monkeypatch.setattr(session.server, "deliver_inputs", lambda route_id: None)
    for held in (None, dict(ticket_held=False), dict(ticket_held=True, snapshot_digest="0" * 64)):
        monkeypatch.setattr(session.custody, "exchange", lambda body, held=held: dict(
            input_delivery=held))  # fmt: skip
        with pytest.raises(ValueError, match="unconfirmed"):
            session.deliver_inputs(route)
    monkeypatch.setattr(session.custody, "exchange", lambda body: dict(
        input_delivery=dict(ticket_held=True, snapshot_digest=data)))  # fmt: skip
    session.deliver_inputs(route)
    assert session.input_expected(route) is True
    session.close()
    h.server.resolver.close()


# ----------------------------------------------------------------- setup tooling


def staged_dir(root, manifest_raw, extra=None):
    path = root / "staged-input"
    path.mkdir(mode=0o700)
    (path / "runtime").mkdir()
    (path / "runtime" / "manifest.json").write_bytes(manifest_raw)
    (path / "data.jsonl").write_bytes(b'{"x":1}\n' * 1000)
    for name, data in (extra or {}).items():
        (path / name).write_bytes(data)
    return path


def setup_contract(directory, raw, **over):
    _, digest = staged_input_manifest(directory)
    value, contract_raw = input_contract(
        digest, manifest_in_input=True, max_bytes=1 << 20,
        custody_host="custody.example-tailnet.ts.net", **over,
    )  # fmt: skip
    assert contract_raw == raw
    return value, digest


def test_setup_approves_a_route_whose_contract_declares_a_staged_input(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    _, raw = input_contract("0" * 64)
    directory = staged_dir(inputs["root"], raw)
    value, digest = setup_contract(directory, raw)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path), *input_args(directory)]
    assert setup_script.main(argv) == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    config = json.loads(Path(summary["config_file"]).read_text())
    selection = config["policy"]["target"]["selection"]
    assert selection["manifests"]["data"] == digest
    assert selection["salad"]["execution"]["workload"]["input"]["snapshot_digest"] == digest
    data = next(r for r in summary["review_required"] if r["field"] == "selection.manifests.data")
    assert data["value"] == digest and "--input-dir" in data["basis"]
    import sqlite3

    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        (stored,) = db.execute(
            "SELECT digest FROM source_snapshots WHERE digest=?", (digest,)
        ).fetchall()
        chunks = db.execute(
            "SELECT relative_path, content FROM snapshot_chunks ORDER BY seq"
        ).fetchall()
    rebuilt = {}
    for rel, content in chunks:
        rebuilt[rel] = rebuilt.get(rel, b"") + content
    assert rebuilt["runtime/manifest.json"] == raw
    # The route's own use-time authority admits it; the live session sees the input.
    from test_salad_live_setup import open_session

    session = open_session(Path(summary["config_file"]), monkeypatch)
    try:
        assert session.input_expected(summary["records"]["route_id"]) is True
    finally:
        session.close()


def test_setup_refuses_an_input_dir_that_differs_from_the_declared_digest(
    inputs,  # noqa: F811
    capsys,
):
    _, raw = input_contract("0" * 64)
    directory = staged_dir(inputs["root"], raw)
    value, _ = setup_contract(directory, raw)
    (directory / "data.jsonl").write_bytes(b"changed")
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path), *input_args(directory)]
    assert setup_script.main(argv) == 1
    assert "input.snapshot_digest" in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_setup_requires_input_dir_iff_the_contract_declares_input(inputs, capsys):  # noqa: F811
    _, raw = input_contract("0" * 64)
    directory = staged_dir(inputs["root"], raw)
    value, _ = setup_contract(directory, raw)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    assert setup_script.main(inputs["argv"] + ["--workload-contract", str(path)]) == 1
    assert "--input-dir is required" in capsys.readouterr().err
    plain = private_file(
        inputs["root"] / "plain.json",
        json.dumps(base_contract(custody_host="custody.example-tailnet.ts.net")).encode(),
    )
    bad = inputs["argv"] + ["--workload-contract", str(plain), *input_args(directory)]
    assert setup_script.main(bad) == 1
    assert "declares no input" in capsys.readouterr().err
    assert setup_script.main(inputs["argv"] + [*input_args(directory)]) == 1
    capsys.readouterr()
    directory.chmod(0o755)
    argv = inputs["argv"] + ["--workload-contract", str(path), *input_args(directory)]
    assert setup_script.main(argv) == 1
    assert "owner-only" in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_setup_refuses_a_staged_manifest_whose_rows_differ_from_egress(
    inputs,  # noqa: F811
    capsys,
):
    _, raw = input_contract("0" * 64)
    manifest = json.loads(raw)
    manifest["fetches"][0]["url"] = "https://evil.example.net/p.whl"
    evil = mf.canonical(manifest)
    directory = staged_dir(inputs["root"], evil)
    _, digest = staged_input_manifest(directory)
    value, _ = input_contract(digest, manifest_in_input=True, max_bytes=1 << 20,
                              custody_host="custody.example-tailnet.ts.net")  # fmt: skip
    value["argv_template"][8] = sha(evil)  # argv pins the evil bytes, egress does not list them
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path), *input_args(directory)]
    assert setup_script.main(argv) == 1
    assert "egress.fetches" in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_staged_input_refuses_symlinks_and_credentials(tmp_path):
    root = tmp_path / "in"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    (root / "link").symlink_to(root / "a.txt")
    with pytest.raises(ValueError):
        staged_input_manifest(root)
    (root / "link").unlink()
    (root / ".env").write_bytes(b"X=1")
    with pytest.raises(ValueError, match="excluded"):
        staged_input_manifest(root)


def test_manifest_helper_prints_input_tokens(tmp_path, capsys):
    value, raw = input_contract("0" * 64)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(value))
    staged = tmp_path / "in"
    (staged / "runtime").mkdir(parents=True)
    out = staged / "runtime" / "manifest.json"
    argv = [str(path), "--item", "wheel:p-1-py3-none-any.whl", "--item", "entry:e.py",
            "--out", str(out), "--input-ref", "runtime/manifest.json"]  # fmt: skip
    assert mf.main(argv) == 0
    lines = capsys.readouterr().out.split()
    assert out.read_bytes() == raw and lines[0] == sha(raw)
    assert lines[1] == input_contract("0" * 64, manifest_in_input=True)[0]["argv_template"][10]
    assert mf.main([*argv, "--input-dir", str(staged)]) == 0
    printed = capsys.readouterr().out.strip().split("\n")
    _, digest = staged_input_manifest(staged)
    assert json.loads(printed[2])["snapshot_digest"] == digest
    assert printed[3] == f"input:{digest}:{len(raw)}"


# ------------------------------------------------- review fixes: F1 input ranges


def test_range_replies_are_checked_field_by_field(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "INPUT_RANGE_BYTES", 3)
    snap = snapshot_of()
    ok = InputCustody([], snap)
    _, seen = run(tmp_path / "ok", argv_for(snap), ok)
    assert seen["files"] == FILES
    # Every stored chunk is read as ranges of at most 3 bytes, in order.
    assert all(length <= 3 for _, _, length in ok.ranges)
    assert len(ok.ranges) == sum(-(-c["size"] // 3) for c in snap["chunks"])
    for i, tamper in enumerate(
        (
            lambda k, r: k == 0 and r.update(offset=r["offset"] + 1),
            lambda k, r: k == 0 and r.update(length=True),
            lambda k, r: k == 0 and r.update(content_base64=r["content_base64"] + "="),
            lambda k, r: k == 0 and r.update(content_base64=base64.b64encode(b"xx").decode()),
            lambda k, r: k == 0 and r.update(extra=1),
            lambda k, r: k == 1 and r.update(seq=0),
        )
    ):
        custody = InputCustody([], snap, tamper=tamper)
        with pytest.raises(rt.Failure) as err:
            run(tmp_path / str(i), argv_for(snap), custody)
        assert err.value.code == "input_chunk" and "entry" not in custody.log


def test_a_tampered_range_fails_the_whole_chunk_before_any_byte_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "INPUT_RANGE_BYTES", 3)
    snap = snapshot_of()

    def tamper(key, reply):
        if key == 0 and reply["offset"] == 6:
            data = bytearray(base64.b64decode(reply["content_base64"]))
            data[-1] ^= 1
            reply["content_base64"] = base64.b64encode(bytes(data)).decode()

    _refused(tmp_path, argv_for(snap), InputCustody([], snap, tamper=tamper), "input_chunk")


def test_real_reply_sizes_fit_the_rate_derived_request_budget():
    from tessera_control import salad_safety_service as svc
    from tessera_control.salad_workload_contract import INPUT_RANGE_BYTES, INPUT_REPLY_BYTES

    assert (RT_DEFAULTS["INPUT_RANGE_BYTES"], RT_DEFAULTS["INPUT_REPLY_BYTES"]) == (
        INPUT_RANGE_BYTES, INPUT_REPLY_BYTES)  # fmt: skip
    content = base64.b64encode(os.urandom(INPUT_RANGE_BYTES)).decode()
    worst = dict(schema="tessera.input-range.v1", snapshot_id="snapshot_" + "f" * 32,
                 seq=511, offset=3 * INPUT_RANGE_BYTES, length=INPUT_RANGE_BYTES,
                 content_base64=content)  # fmt: skip
    size = len(svc.encoded(worst))
    assert size <= INPUT_REPLY_BYTES
    # Slow-floor client: 1.5 Mbit/s for the whole reply, plus every pre-reply phase.
    total = (
        svc.HEADER_READ_SECONDS
        + svc.BODY_READ_SECONDS
        + svc.INPUT_AUTH_WORK_SECONDS
        + svc.INPUT_CONTROL_FETCH_SECONDS
        + svc.body_read_seconds(size)
    )
    assert total < svc.CHUNK_REQUEST_SECONDS
    assert svc.CHUNK_REQUEST_SECONDS + svc.LOOP_PASS_ALLOWANCE_SECONDS <= (
        svc.UNSEEN_RUN_GAP_SECONDS
    )
    # A 4 MiB stored chunk needs four such replies, never one ~5.6 MiB reply.
    assert -(-RT_DEFAULTS["INPUT_CHUNK_BYTES"] // INPUT_RANGE_BYTES) == 4


def test_guardian_serves_ranges_from_one_verified_cached_chunk(case, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tid.salad_input_delivery, "INPUT_RANGE_BYTES", 3)
    tid.give(case)
    tid.ask(case)
    fetched = lambda: [p for p, _ in case.control.requests if "/chunks/" in p]  # noqa: E731
    parts = [tid.ask(case, "chunk", seq=0, offset=o, length=min(3, 8 - o)) for o in range(0, 8, 3)]
    assert fetched() == ["/provider-inputs/chunks/0"]  # one control fetch for three ranges
    data = b"".join(base64.b64decode(p["content_base64"]) for p in parts)
    assert data == tid.FILES["a.bin"][:8]
    assert {p["schema"] for p in parts} == {"tessera.input-range.v1"}
    for bad in (dict(offset=0, length=4), dict(offset=7, length=2), dict(offset=-1, length=1),
                dict(offset=0, length=0), dict(offset=True, length=1), dict(offset=0)):  # fmt: skip
        with pytest.raises(ValueError):
            tid.ask(case, "chunk", seq=0, **bad)
    # A whole stored chunk larger than one range never goes out as a single reply,
    # and whole-chunk (non-ranged) requests no longer exist.
    with pytest.raises(ValueError):
        tid.ask(case, "chunk", seq=1)
    with pytest.raises(ValueError):
        case.service.workload(
            "/v1/workload/input", dict(binding=case.request, request="chunk", seq=1), case.token
        )
    status = case.service.inputs.status(case.request["operation"])
    assert status["bytes_served"] == 8


def test_guardian_refuses_a_manifest_reply_that_would_not_fit(case, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tid.salad_input_delivery, "INPUT_REPLY_BYTES", 200)
    tid.give(case)
    with pytest.raises(ValueError):
        tid.ask(case)


class _Wire:
    def __init__(self, cut_after=None):
        self.buf, self.cut_after = io.BytesIO(), cut_after

    def write(self, data):
        if self.cut_after is not None and self.buf.tell() + len(data) > self.cut_after:
            raise ConnectionResetError("client went away")
        self.buf.write(data)

    def flush(self):
        pass


class _Sock:
    def settimeout(self, value):
        pass


def _http(service, path, payload, token, cut_after=None, extended=None):
    """One request through the real SafetyHandler.do_POST (no socket, no TLS)."""
    import email.message

    from tessera_control.salad_safety_service import SafetyHandler

    raw = json.dumps(payload).encode()
    handler = SafetyHandler.__new__(SafetyHandler)
    headers = email.message.Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(raw))
    headers["Authorization"] = "Bearer " + token
    handler.headers, handler.path = headers, path
    handler.rfile, handler.wfile, handler.connection = io.BytesIO(raw), _Wire(cut_after), _Sock()
    handler.request_version, handler.command, handler.requestline = "HTTP/1.1", "POST", "POST"
    handler.client_address, handler.close_connection = ("127.0.0.1", 0), False
    handler.server = type(
        "Server", (),
        dict(service=service, accepted_at=service.clock(), admin_token=b"a" * 40,
             extend_deadline=staticmethod((extended if extended is not None else []).append)),
    )()  # fmt: skip
    handler.do_POST()
    head, _, body = handler.wfile.buf.getvalue().partition(b"\r\n\r\n")
    status = int(head.split()[1]) if head else None
    return status, body, handler.close_connection


def test_slow_client_multi_chunk_pull_and_a_cut_off_reply_does_not_burn_the_budget(
    case,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(tid.salad_input_delivery, "INPUT_RANGE_BYTES", 3)
    monkeypatch.setattr(rt, "INPUT_RANGE_BYTES", 3)
    tid.give(case)
    cuts, extended, replies = {(0, 3), (2, 0)}, [], []

    def post(path, body, cap=4096, timeout=10):
        key = (body.get("seq"), body.get("offset"))
        cut = 40 if key in cuts else None
        cuts.discard(key)
        payload = json.loads(rt.encode(body))
        status, raw, _ = _http(case.service, path, payload, case.token, cut, extended)
        if status != 200 or len(raw) > cap:
            raise rt.Failure("custody")
        replies.append(len(raw))
        return json.loads(raw)

    custody = rt.HTTPSCustody.__new__(rt.HTTPSCustody)
    custody.binding, custody.token_expires, custody.post = case.request, float("inf"), post
    expected = {"digest": case.snapshot.digest, "max_bytes": 1000}
    work = tmp_path / "work"
    work.mkdir()
    root = rt.fetch_input(custody, expected, str(work), time.monotonic() + 60)
    got = {p.name: p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}
    assert got == tid.FILES and cuts == set()
    from tessera_control import salad_safety_service as svc

    # Every input request ran under the chunk alarm, never the 10 s small-path one.
    assert extended and set(extended) == {svc.CHUNK_REQUEST_SECONDS}
    status = case.service.inputs.status(case.request["operation"])
    total = sum(len(v) for v in tid.FILES.values())
    assert status["bytes_served"] == total  # the two cut-off replies were refunded
    chunk_fetches = [p for p, _ in case.control.requests if "/chunks/" in p]
    assert len(chunk_fetches) == len(case.snapshot.index)  # cached across ranges and retries


def test_refunds_are_bounded_so_endless_cut_offs_still_hit_the_budget(case):  # noqa: F811
    from tessera_control.salad_input_delivery import MAX_REFUNDS_PER_TICKET

    tid.give(case)
    tid.ask(case)
    operation = case.request["operation"]
    for _ in range(MAX_REFUNDS_PER_TICKET + 5):
        reply = tid.ask(case, "chunk", seq=0)
        case.service.input_reply_failed(dict(binding=case.request), reply)
    status = case.service.inputs.status(operation)
    assert status["bytes_served"] == 5 * 8  # refunds stop after MAX_REFUNDS_PER_TICKET
    # Non-input replies and malformed calls never refund.
    case.service.input_reply_failed(dict(binding=case.request), dict(schema="other", size=8))
    case.service.input_reply_failed("x", None)
    assert case.service.inputs.status(operation)["bytes_served"] == 40


# ------------------------------------------------- review fixes: F5 begun, F3 drop


def test_input_requires_begin_once(case):  # noqa: F811
    tid.give(case)
    case.service.db.execute(
        "UPDATE reservations SET begun=0 WHERE reservation=?", (case.body["reservation"],)
    )
    case.service.db.commit()
    with pytest.raises(ValueError, match="workload_rejected"):
        tid.ask(case)
    with pytest.raises(ValueError, match="workload_rejected"):
        tid.ask(case, "chunk", seq=0)


@pytest.mark.parametrize("gate", ["cancel", "deadline"])
def test_ticket_drop_bypasses_recovery_deadline_and_stop_only(case, gate):  # noqa: F811
    tid.give(case)
    reservation = case.body["reservation"]
    if gate == "cancel":
        case.service.admin(dict(operation="cancel", reservation=reservation))
        assert case.service._row(reservation)["recovery"] == 1
    else:
        case.clock[0] = case.service._stop_deadline(case.service._row(reservation)) + 1
    with pytest.raises(ValueError, match="stop_only"):
        tid.give(case)  # installing is refused ...
    reply = case.service.admin(
        dict(operation="input_ticket", reservation=reservation, input_ticket=None)
    )
    assert reply["state"] == "ready"  # ... but dropping always succeeds
    assert case.service.inputs.status(case.request["operation"]) == dict(ticket_held=False)


# ------------------------------------------------- review fixes: parity guard


PINNED_WORKER_VERIFIER = "70579e2d1a640ed8bc38c796cf891fabdad1551c35e4f978769781d490ae6e38"


def test_runtime_mirror_is_pinned_to_the_worker_verifier_source():
    """Fails whenever the mirrored worker verifier changes.

    If this fails, re-check workloads/gpu_runtime_v1/runtime.py (_input_path,
    input_chunk_plan, verify_input_manifest, verify_input_index, the range and chunk
    checks) against the changed functions, extend the parity cases, then update the pin.
    """
    import inspect

    from tessera_worker import inputs as worker_inputs

    source = "".join(
        inspect.getsource(f)
        for f in (
            chunked_inputs.chunk_plan,
            chunked_inputs._verify_manifest,
            chunked_inputs._verify_index,
            chunked_inputs._decode_chunk,
            worker_inputs._path,
        )
    )
    assert sha(source.encode()) == PINNED_WORKER_VERIFIER
    assert RT_DEFAULTS["INPUT_CHUNK_BYTES"] == 4 * 1024 * 1024  # chunked_inputs.CHUNK_BYTES
    assert RT_DEFAULTS["MAX_INPUT_BYTES"] == chunked_inputs.MAX_BYTES
    assert RT_DEFAULTS["MAX_INPUT_FILES"] == chunked_inputs.MAX_FILES
    assert RT_DEFAULTS["MAX_INPUT_MANIFEST_BYTES"] == chunked_inputs.MAX_MANIFEST_BYTES


# ----------------------------------- review fixes: F2 admission, F4 staged manifest


def _approved_input_route(inputs, capsys, monkeypatch):  # noqa: F811
    _, raw = input_contract("0" * 64)
    directory = staged_dir(inputs["root"], raw)
    value, _ = setup_contract(directory, raw)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path), *input_args(directory)]
    assert setup_script.main(argv) == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    from test_salad_live_setup import open_session

    return open_session(Path(summary["config_file"]), monkeypatch), summary


def test_stored_routes_parse_and_stop_even_if_admission_would_now_refuse(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    from tessera_control import salad_workload_admission
    from tessera_control.remote_execution_models import RemoteProposal

    session, summary = _approved_input_route(inputs, capsys, monkeypatch)
    route = summary["records"]["route_id"]
    try:
        state = session.state(route)
        proposal = RemoteProposal.model_validate(state["proposal"])
        server = session.server
        server.store.preflight_authority(server.worker, route, proposal, "create")

        def tightened(*a, **k):
            raise ValueError("declared fetch and input bytes exceed the route's disk_mb")

        monkeypatch.setattr(salad_workload_admission, "check_workload_admission", tightened)
        with pytest.raises(Conflict, match="disk_mb"):
            server.store.preflight_authority(server.worker, route, proposal, "create")
        # Stop authority and the stored proposal are unaffected.
        server.store.preflight_authority(server.worker, route, proposal, "stop")
        assert RemoteProposal.model_validate(session.state(route)["proposal"]) == proposal
    finally:
        session.close()


def test_setup_refuses_a_contract_that_exceeds_disk_before_any_write(inputs, capsys):  # noqa: F811
    big = base_contract(
        custody_host="custody.example-tailnet.ts.net",
        egress=dict(kind="provider_broad", fetches=[
            dict(url="https://m.example.com/w", sha256="c" * 64, max_bytes=2 * 1024**3)]),
    )  # fmt: skip
    path = private_file(inputs["root"] / "contract.json", json.dumps(big).encode())
    assert setup_script.main(inputs["argv"] + ["--workload-contract", str(path)]) == 1
    assert "--disk-mb" in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_control_refuses_a_staged_manifest_that_mismatches_argv_before_approval(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    """Even if the operator-side check is bypassed, control refuses at proposal time."""
    from tessera_control import salad_workload_input

    _, raw = input_contract("0" * 64)
    manifest = json.loads(raw)
    manifest["fetches"][0]["url"] = "https://evil.example.net/p.whl"
    evil = mf.canonical(manifest)
    directory = staged_dir(inputs["root"], evil)
    _, digest = staged_input_manifest(directory)
    value, _ = input_contract(digest, manifest_in_input=True, max_bytes=1 << 20,
                              custody_host="custody.example-tailnet.ts.net")  # fmt: skip
    value["argv_template"][8] = sha(evil)
    monkeypatch.setattr(salad_workload_input, "check_gpu_runtime_manifest", lambda *a: None)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path), *input_args(directory)]
    assert setup_script.main(argv) == 1
    err = capsys.readouterr().err
    assert "egress.fetches" in err and "refused" in err
    import sqlite3

    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        routes = db.execute(
            "SELECT count(*) FROM events WHERE event_type='remote_execution.state'"
        ).fetchone()[0]
    assert routes == 0  # no route was proposed, so none can be approved or begun


def test_admission_checks_the_staged_manifest_bytes_against_argv(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    import sqlite3

    session, summary = _approved_input_route(inputs, capsys, monkeypatch)
    route = summary["records"]["route_id"]
    try:
        state = session.state(route)
        selection = state["proposal"]["selection"]
        with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
            check_workload_admission(db, state["project_id"], selection)
            wrong = copy.deepcopy(selection)
            wrong["salad"]["execution"]["workload"]["argv_template"][8] = "e" * 64
            with pytest.raises(ValueError, match="sha256"):
                check_workload_admission(db, state["project_id"], wrong)
            with pytest.raises(ValueError, match="not staged"):
                check_workload_admission(db, "other-project", selection)
            small = copy.deepcopy(selection)
            small["salad"]["execution"]["workload"]["input"]["max_bytes"] = 10
            small["salad"]["execution"]["workload"]["argv_template"][9] = (
                f"input:{selection['manifests']['data']}:10")  # fmt: skip
            with pytest.raises(ValueError, match="max_bytes"):
                check_workload_admission(db, state["project_id"], small)
    finally:
        session.close()


# ------------------------------------- second review: F-A request budget, F-E carry


def test_non_canonical_ranges_are_refused(case):  # noqa: F811
    tid.give(case)
    tid.ask(case)
    for bad in (dict(offset=0, length=1), dict(offset=1, length=7), dict(offset=0, length=9)):
        with pytest.raises(ValueError):
            tid.ask(case, "chunk", seq=0, **bad)
    assert tid.ask(case, "chunk", seq=0, offset=0, length=8)["length"] == 8


def test_input_request_budget_is_charged_before_any_provider_read(case):  # noqa: F811
    from tessera_control.salad_safety_service import INPUT_REQUEST_SLACK

    tid.give(case)
    tid.ask(case)  # verifies the manifest: 4 tiny chunks, so 5 planned reads
    planned = 1 + len(case.snapshot.index)
    assert case.service.inputs.planned_requests(case.request["operation"]) == planned
    refused = 0
    with pytest.raises(ValueError, match="input_request_budget_exhausted"):
        for _ in range(1000):  # a workload looping on 1-byte ranges
            try:
                tid.ask(case, "chunk", seq=0, offset=0, length=1)
            except ValueError as error:
                if "budget" in str(error):
                    raise
                refused += 1
    # Refused requests counted: manifest + refused ranges == the whole budget.
    assert 1 + refused == 2 * planned + INPUT_REQUEST_SLACK
    reads = len(case.provider.calls)
    for _ in range(3):
        with pytest.raises(ValueError, match="input_request_budget_exhausted"):
            tid.ask(case)
    assert len(case.provider.calls) == reads  # exhausted: no further provider read
    row = case.service._row(case.body["reservation"])
    assert row["input_requests"] == 2 * planned + INPUT_REQUEST_SLACK


def test_an_unauthenticated_caller_cannot_burn_the_input_budget(case):  # noqa: F811
    from test_salad_workload_jwt import signed

    tid.give(case)
    other = dict(case.payload, salad_machine_id="00000000-0000-0000-0000-000000000099")
    for token in ("not-a-jwt", signed(case.key, other)):
        with pytest.raises(ValueError):
            tid.ask(case, token=token)
    assert case.service._row(case.body["reservation"])["input_requests"] == 0


def test_served_and_refund_counters_survive_a_drop_and_reinstall(case):  # noqa: F811
    tid.give(case)
    tid.ask(case)
    reply = tid.ask(case, "chunk", seq=0)
    case.service.input_reply_failed(dict(binding=case.request), reply)  # one refund
    tid.ask(case, "chunk", seq=0)
    operation = case.request["operation"]
    before = case.service.inputs.status(operation)
    session = case.service.inputs._sessions[operation]
    refunds = session.refunds
    case.service.admin(
        dict(operation="input_ticket", reservation=case.body["reservation"], input_ticket=None)
    )
    tid.give(case, tid.ticket_for(case.snapshot, token=tid.TOKEN_B))
    after = case.service.inputs.status(operation)
    assert (after["bytes_served"], after["chunks_served"]) == (
        before["bytes_served"], before["chunks_served"])  # fmt: skip
    assert case.service.inputs._sessions[operation].refunds == refunds == 1


# ------------------------------------- second review: F-B heartbeat skips admission


def test_running_route_keeps_heartbeating_when_admission_would_now_refuse(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    from tessera_control import salad_workload_admission
    from tessera_control.remote_execution_models import RemoteProposal

    session, summary = _approved_input_route(inputs, capsys, monkeypatch)
    route = summary["records"]["route_id"]
    try:
        server = session.server
        proposal = RemoteProposal.model_validate(session.state(route)["proposal"])
        calls = []
        real_admit = salad_workload_admission.check_workload_admission

        def counting(*a, **k):
            calls.append(1)
            return real_admit(*a, **k)

        monkeypatch.setattr(salad_workload_admission, "check_workload_admission", counting)
        real = server.store.preflight_authority
        launched = lambda *a, **k: dict(  # noqa: E731
            real(*a, **k), effects=[{"action": "create"}, {"action": "start"}]
        )
        server.store.preflight_authority(server.worker, route, proposal, "create")
        assert calls == [1]  # admission runs for a new create

        def tightened(*a, **k):
            raise ValueError("declared fetch and input bytes exceed the route's disk_mb")

        monkeypatch.setattr(salad_workload_admission, "check_workload_admission", tightened)
        with pytest.raises(Conflict, match="disk_mb"):
            server.store.preflight_authority(server.worker, route, proposal, "create")
        # The heartbeat path never runs admission, so a running job is not cancelled.
        cancelled, beats = [], []
        monkeypatch.setattr(server.safety, "cancel", lambda state: cancelled.append(1))
        monkeypatch.setattr(server.safety, "heartbeat", lambda state: beats.append(1))
        monkeypatch.setattr(server.store, "preflight_authority", launched)
        server.heartbeat(route)
        assert beats == [1] and cancelled == []
    finally:
        session.close()
