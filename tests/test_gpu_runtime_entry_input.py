"""gpu_runtime_v1 entry served from the staged input, and the generic GPU cold-start smoke.

Offline: fake custody or the real setup/admission against temporary state. No Salad,
Docker, GPU or network call; the smoke entry runs as a real subprocess with no GPU.
"""

import copy
import hashlib
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from pydantic import ValidationError
from test_gpu_runtime_v1 import BLOBS, OP, WATCHDOG, mf, opener_for, rt, runtime_contract, sha
from test_salad_input_path import (
    InputCustody,
    kinds,
    rt_manifest,
    small_chunks,  # noqa: F401 (autouse: 8-byte input chunks)
    snapshot_of,
)
from test_salad_live_setup import inputs, open_session, private_file, setup_script  # noqa: F401

from tessera_control.salad_workload_admission import check_workload_admission
from tessera_control.salad_workload_contract import (
    WorkloadContractV1,
    check_gpu_runtime_inline_rules,
    fetches_digest,
)
from tessera_control.salad_workload_input import check_staged_input, staged_input_manifest

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "workloads/gpu_runtime_v1/smoke"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke_entry = _load("gpu_runtime_smoke_entry", SMOKE / "entry.py")
smoke_stage = _load("gpu_runtime_smoke_stage", SMOKE / "stage.py")

ENTRY = {"kind": "entry", "source": "input", "path": "main.py"}
WHEEL_ROW = next((u, k, d) for (u, k, d) in BLOBS if k == "wheel")
FILES = {"main.py": b"print('x')\n", "data/train.jsonl": b'{"x":1}\n'}


def manifest_of(*items, blobs=None):
    """URL rows for ``blobs`` (default: the wheel only) plus the given input items."""
    base = rt_manifest(blobs if blobs is not None else {WHEEL_ROW: BLOBS[WHEEL_ROW]})
    return {"schema": rt.MANIFEST_SCHEMA, "fetches": [*base["fetches"], *items]}


def url_rows(manifest):
    return [
        dict(url=f["url"], sha256=f["sha256"], max_bytes=f["max_bytes"])
        for f in manifest["fetches"]
        if "source" not in f
    ]


def contract_value(manifest, *, digest="d" * 64, declared=True, carried=None, fetches=None):
    """A gpu_runtime_v1 contract whose egress equals the manifest's URL rows."""
    raw = mf.canonical(manifest)
    fetches = url_rows(manifest) if fetches is None else fetches
    tail = [f"input:{digest}:5000"] if declared else []
    if carried:
        rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in fetches]
        tail.append(f"input-manifest:{fetches_digest(rows)}:{carried}")
    else:
        tail += mf.refs(raw)
    value = dict(
        contract="tessera.salad.workload.v1", runtime="gpu_runtime_v1",
        argv_template=["v1", "{operation}", "{epoch}", "{custody_host}", WATCHDOG, "120",
                       "4096", "1", sha(raw), *tail],
        env={}, workload_seconds=120, outputs=[dict(name="output.json", max_bytes=4096)],
        egress=dict(kind="provider_broad", fetches=fetches), custody_host="localhost",
    )  # fmt: skip
    if declared:
        value["input"] = dict(kind="staged_snapshot", snapshot_digest=digest, max_bytes=5000)
    return value


def run(tmp_path, argv, custody, opener=None, calls=None):
    seen = {}

    def entry_runner(path, work, site, out_dir, dl):
        custody.log.append("entry")
        seen["entry"] = path
        seen["entry_bytes"] = Path(path).read_bytes()
        seen["entry_mode"] = stat.S_IMODE(os.lstat(path).st_mode)
        (Path(out_dir) / "output.json").write_text('{"ok":1}')

    result = rt.execute(
        rt.parse_argv(argv), time.monotonic() + 120, str(tmp_path / "work"),
        custody_factory=lambda b, d: custody, opener=opener or opener_for(BLOBS, calls),
        installer=lambda *a: custody.log.append("install") or "site", entry_runner=entry_runner,
    )  # fmt: skip
    return result, seen


def argv_of(value):
    return WorkloadContractV1.model_validate(value).derived_argv(OP, 7)[1:]


# ------------------------------------------------------------- contract validation


def test_inline_input_entry_validates_and_is_not_an_egress_fetch():
    manifest = manifest_of(ENTRY)
    model = WorkloadContractV1.model_validate(contract_value(manifest))
    assert [f.url for f in model.egress.fetches] == [WHEEL_ROW[0]]
    assert model.capability_network() == [{"host": "w.example.com", "port": 443}]
    request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
    assert rt.load_manifest(request)["fetches"][-1] == ENTRY
    # With no URL fetch at all, egress and the capability network are empty.
    only = WorkloadContractV1.model_validate(contract_value(manifest_of(ENTRY, blobs={})))
    assert only.egress.fetches == [] and only.capability_network() == []


def test_carried_manifest_binds_only_url_rows_into_the_fetches_digest():
    manifest = manifest_of(ENTRY)
    model = WorkloadContractV1.model_validate(contract_value(manifest, carried="rt/m.json"))
    token = model.argv_template[10]
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in url_rows(manifest)]
    assert token.split(":")[1] == fetches_digest(rows)
    request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
    assert request["manifest_in_input"]["fetches_sha256"] == fetches_digest(rows)


def _refused(manifest, **kw):
    """Refused for a new route: by the model validator, else by the strict new-route rules."""
    try:
        model = WorkloadContractV1.model_validate(contract_value(manifest, **kw))
    except ValidationError:
        return
    with pytest.raises(ValueError):
        check_gpu_runtime_inline_rules(model)


@pytest.mark.parametrize(
    "items",
    [
        [dict(ENTRY, path="main.sh")],  # not .py
        [dict(ENTRY, path="../main.py")],  # traversal
        [dict(ENTRY, path="a/../main.py")],
        [dict(ENTRY, path="/main.py")],  # absolute
        [dict(ENTRY, path="")],
        [dict(ENTRY, source="url")],
        [dict(ENTRY, kind="wheel")],
        [ENTRY, dict(ENTRY, path="second.py")],  # two entries
        [ENTRY, dict(ENTRY, kind="file")],  # duplicate path
        [dict(ENTRY, url="https://d.example.com/main.py", sha256="2" * 64, max_bytes=20)],
        [],  # no entry at all
    ],
)
def test_malformed_input_items_are_refused(items):
    _refused(manifest_of(*items))


def test_input_entry_plus_url_entry_is_two_entries():
    blobs = {k: v for k, v in BLOBS.items() if k[1] in {"wheel", "entry"}}
    _refused(manifest_of(ENTRY, blobs=blobs))


def test_input_entry_needs_a_declared_input():
    # Enforced by the model validator itself (no stored contract can hold an input item).
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract_value(manifest_of(ENTRY), declared=False))


def test_input_entry_listed_in_egress_fetches_is_refused():
    manifest = manifest_of(ENTRY)
    listed = [*url_rows(manifest), dict(url="https://d.example.com/main.py", sha256="2" * 64,
                                         max_bytes=20)]  # fmt: skip
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract_value(manifest, fetches=listed))


def _all_rows(manifest):
    return [dict(url=f["url"], sha256=f["sha256"], max_bytes=f["max_bytes"])
            for f in manifest["fetches"]]  # fmt: skip


def _stray(source):
    manifest = manifest_of(blobs={k: v for k, v in BLOBS.items() if k[1] in {"wheel", "entry"}})
    manifest["fetches"][0]["source"] = source
    return manifest


# Earlier contract shapes that must keep parsing as stored proposals.
LEGACY = {
    "zero_entries": lambda: manifest_of(),
    "stray_source_key": lambda: _stray("mirror"),
    "url_row_with_source_input": lambda: _stray("input"),
}


@pytest.mark.parametrize("name", LEGACY)
def test_legacy_contracts_keep_parsing_and_stopping_but_new_routes_refuse(
    name,
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    from tessera_control import salad_workload_admission, salad_workload_contract
    from tessera_control.remote_execution_models import RemoteProposal
    from tessera_control.store import Conflict

    manifest = LEGACY[name]()
    value = contract_value(manifest, declared=False, fetches=_all_rows(manifest))
    value["custody_host"] = "custody.example-tailnet.ts.net"
    model = WorkloadContractV1.model_validate(value)  # the model validator is unchanged
    with pytest.raises(ValueError):
        check_gpu_runtime_inline_rules(model)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path)]
    assert setup_script.main(argv) == 1  # a new route is refused before any write
    assert "--workload-contract refused" in capsys.readouterr().err
    assert not inputs["output"].exists()
    # A route stored before the new-route rules existed (simulated by bypassing them).
    with monkeypatch.context() as patch:
        patch.setattr(salad_workload_admission, "check_workload_admission", lambda *a, **k: None)
        patch.setattr(salad_workload_contract, "check_gpu_runtime_inline_rules", lambda c: c)
        assert setup_script.main(argv) == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    session = open_session(Path(summary["config_file"]), monkeypatch)
    route = summary["records"]["route_id"]
    try:
        state = session.state(route)
        proposal = RemoteProposal.model_validate(state["proposal"])  # still parses
        server = session.server
        with pytest.raises(Conflict):  # admission refuses it for create/start
            server.store.preflight_authority(server.worker, route, proposal, "create")
        server.store.preflight_authority(server.worker, route, proposal, "stop")
        assert RemoteProposal.model_validate(session.state(route)["proposal"]) == proposal
    finally:
        session.close()


def test_url_entry_contracts_serialize_and_validate_unchanged():
    cls, base, manifest = runtime_contract()
    model = cls.model_validate(base)
    dumped = model.model_dump(mode="json")
    assert dumped == {k: v for k, v in base.items() if k != "manifest"}
    raw = json.dumps(dumped, sort_keys=True, separators=(",", ":")).encode()
    # Pinned before input items existed: an URL-entry contract serializes byte for byte.
    assert sha(raw) == "50416e95f9c5f76a86acdca85c3fa5edb7840990785a2977efb488f252a94769"
    assert cls.model_validate_json(raw) == model
    assert "source" not in json.dumps(manifest)
    request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
    assert rt.load_manifest(request) == manifest


# --------------------------------------------------------------------- runtime


def test_runtime_runs_an_inline_input_entry_from_the_read_only_tree(tmp_path):
    snap = snapshot_of(FILES)
    calls = []
    custody = InputCustody([], snap)
    argv = argv_of(contract_value(manifest_of(ENTRY), digest=snap["digest"]))
    argv[9] = f"input:{snap['digest']}:10000"
    result, seen = run(tmp_path, argv, custody, calls=calls)
    assert kinds(custody.log) == ["install", "begin", "plan", "input", "entry", "chunk"]
    assert seen["entry"] == str((tmp_path / "work").resolve() / "input" / "main.py")
    assert seen["entry_bytes"] == FILES["main.py"] and seen["entry_mode"] == 0o444
    assert calls == [WHEEL_ROW[0]]  # the input entry is never fetched
    fetched = json.loads(custody.file("output.json"))["fetched"]
    assert [f["url"] for f in fetched] == [WHEEL_ROW[0]] and result[0]["name"] == "output.json"


def test_runtime_runs_an_input_entry_named_by_a_carried_manifest(tmp_path):
    manifest = manifest_of(ENTRY)
    raw = mf.canonical(manifest)
    snap = snapshot_of(dict(FILES, **{"rt/m.json": raw}))
    custody = InputCustody([], snap)
    argv = argv_of(contract_value(manifest, digest=snap["digest"], carried="rt/m.json"))
    argv[9] = f"input:{snap['digest']}:10000"
    calls = []
    _, seen = run(tmp_path, argv, custody, calls=calls)
    assert kinds(custody.log) == ["begin", "plan", "input", "install", "entry", "chunk"]
    assert seen["entry_bytes"] == FILES["main.py"] and calls == [WHEEL_ROW[0]]


def test_runtime_refuses_a_missing_input_entry_before_running(tmp_path):
    snap = snapshot_of({"data/train.jsonl": FILES["data/train.jsonl"]})
    custody = InputCustody([], snap)
    argv = argv_of(contract_value(manifest_of(ENTRY), digest=snap["digest"]))
    argv[9] = f"input:{snap['digest']}:10000"
    with pytest.raises(rt.Failure) as err:
        run(tmp_path, argv, custody)
    assert err.value.code == "input_item" and "entry" not in custody.log


def test_runtime_refuses_a_directory_or_symlink_as_the_input_entry(tmp_path):
    root = tmp_path / "tree"
    (root / "dir.py").mkdir(parents=True)
    (root / "real.py").write_bytes(b"x")
    (root / "link.py").symlink_to(root / "real.py")
    (root / "sub").symlink_to(root, target_is_directory=True)
    for path in ("dir.py", "link.py", "sub/real.py", "absent.py"):
        manifest = manifest_of(dict(ENTRY, path=path))
        with pytest.raises(rt.Failure) as err:
            rt.resolve_input_items(manifest, str(root))
        assert err.value.code == "input_item"
    assert rt.resolve_input_items(manifest_of(dict(ENTRY, path="real.py")), str(root)) == str(
        root / "real.py"
    )


def test_runtime_refuses_input_items_without_a_declared_input_before_the_claim(tmp_path):
    raw = mf.canonical(manifest_of(ENTRY))
    argv = ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", "4096", "1", sha(raw),
            *mf.refs(raw)]  # fmt: skip
    custody = InputCustody([], snapshot_of(FILES))
    with pytest.raises(rt.Failure) as err:
        run(tmp_path, argv, custody)
    assert err.value.code == "manifest" and custody.log == []


@pytest.mark.parametrize(
    "items",
    [
        [dict(ENTRY, path="main.sh")],
        [dict(ENTRY, path="../main.py")],
        [dict(ENTRY, path="/main.py")],
        [ENTRY, dict(ENTRY, path="b.py")],
        [ENTRY, ENTRY],
        [dict(ENTRY, url="https://d.example.com/main.py")],
        [dict(ENTRY, source="url")],
    ],
)
def test_runtime_manifest_refuses_malformed_input_items(items):
    with pytest.raises(rt.Failure):
        rt.validate_manifest(manifest_of(*items))


def test_carried_manifest_rows_exclude_input_items_from_the_fetches_digest(tmp_path):
    manifest = manifest_of(ENTRY)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "m.json").write_bytes(mf.canonical(manifest))
    # A reviewed FETCHES_SHA256 that also lists the input entry as if it were an egress row.
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in url_rows(manifest)]
    every = [*rows, ("https://d.example.com/main.py", "2" * 64, 20)]
    wrong = rt.parse_argv(
        [
            "v1",
            OP,
            "7",
            "custody.example.com",
            WATCHDOG,
            "120",
            "4096",
            "1",
            sha(mf.canonical(manifest)),
            f"input:{'d' * 64}:10",
            f"input-manifest:{rt.fetches_digest(every)}:m.json",
        ]  # fmt: skip
    )
    with pytest.raises(rt.Failure) as err:
        rt.load_input_manifest(wrong, str(root))
    assert err.value.code == "manifest_fetches"
    right = dict(wrong, manifest_in_input=dict(wrong["manifest_in_input"],
                                               fetches_sha256=rt.fetches_digest(rows)))  # fmt: skip
    assert rt.load_input_manifest(right, str(root)) == manifest


# --------------------------------------------------------- manifest helper script


def test_manifest_helper_builds_input_items(tmp_path, capsys):
    manifest = manifest_of(ENTRY)
    value = contract_value(manifest)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(value))
    argv = [str(path), "--item", f"wheel:{WHEEL_ROW[2]}", "--item", "entry:input:main.py",
            "--input-ref", "rt/m.json"]  # fmt: skip
    assert mf.main(argv) == 0
    lines = capsys.readouterr().out.split()
    assert lines[0] == sha(mf.canonical(manifest))
    rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in url_rows(manifest)]
    assert lines[1] == f"input-manifest:{fetches_digest(rows)}:rt/m.json"
    model = WorkloadContractV1.model_validate(value)
    for bad in (["entry:input:main.py"], [f"wheel:{WHEEL_ROW[2]}", "entry:input:main.sh"],
                [f"wheel:{WHEEL_ROW[2]}", "entry:input:a.py", "entry:input:b.py"],
                [f"wheel:{WHEEL_ROW[2]}", "wheel:input:x.whl"]):  # fmt: skip
        with pytest.raises(ValueError):
            mf.build_manifest(model, bad)


# ------------------------------------------------- setup and admission (staged snapshot)


def test_setup_check_refuses_a_staged_input_without_the_entry(tmp_path):
    directory = tmp_path / "in"
    (directory / "data").mkdir(parents=True)
    (directory / "data" / "train.jsonl").write_bytes(b"x")
    _, digest = staged_input_manifest(directory)
    model = WorkloadContractV1.model_validate(contract_value(manifest_of(ENTRY), digest=digest))
    with pytest.raises(ValueError, match="not a file of the staged input"):
        check_staged_input(model, directory)
    (directory / "main.py").write_bytes(b"print(1)\n")
    _, digest = staged_input_manifest(directory)
    model = WorkloadContractV1.model_validate(contract_value(manifest_of(ENTRY), digest=digest))
    check_staged_input(model, directory)


def _stage_smoke(root):
    summary = smoke_stage.stage(root / "smoke-input", root / "smoke-contract.json",
                                "custody.example-tailnet.ts.net")  # fmt: skip
    return summary, root / "smoke-input", root / "smoke-contract.json"


def test_setup_approves_the_smoke_and_admission_refuses_a_snapshot_without_the_entry(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    import sqlite3

    summary, directory, contract = _stage_smoke(inputs["root"])
    argv = inputs["argv"] + [
        "--workload-contract", str(contract), "--input-dir", str(directory),
        "--data-class", "public", "--license", "CC0-1.0",
        "--source", "tessera:workloads/gpu_runtime_v1/smoke/stage.py",
    ]  # fmt: skip
    assert setup_script.main(argv) == 0, capsys.readouterr().err
    setup = json.loads(capsys.readouterr().out)
    config = json.loads(Path(setup["config_file"]).read_text())
    selection = config["policy"]["target"]["selection"]
    assert selection["manifests"]["data"] == summary["snapshot_digest"]
    session = open_session(Path(setup["config_file"]), monkeypatch)
    try:
        state = session.state(setup["records"]["route_id"])
        selection = state["proposal"]["selection"]
        with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
            check_workload_admission(db, state["project_id"], selection)
            # Same snapshot, but the reviewed manifest names an entry it does not hold.
            wrong = copy.deepcopy(selection)
            work = wrong["salad"]["execution"]["workload"]
            raw = mf.canonical(manifest_of(dict(ENTRY, path="absent.py"), blobs={}))
            work["argv_template"][8:] = [sha(raw), work["argv_template"][9], *mf.refs(raw)]
            WorkloadContractV1.model_validate(work)  # the contract alone cannot tell
            with pytest.raises(ValueError, match="not a file of the staged input"):
                check_workload_admission(db, state["project_id"], wrong)
    finally:
        session.close()


# ------------------------------------------------------------------ smoke workload


def test_smoke_stage_writes_a_valid_private_contract_and_input(tmp_path):
    root = tmp_path.resolve()
    summary, directory, contract = _stage_smoke(root)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(contract.stat().st_mode) == 0o600
    model = WorkloadContractV1.model_validate_json(contract.read_bytes())
    assert model.runtime == "gpu_runtime_v1" and model.workload_seconds == 300
    assert model.egress.fetches == [] and model.capability_network() == []
    assert model.output_plan() == [dict(name="result.json", max_bytes=65536),
                                   dict(name="probe.txt", max_bytes=65536)]  # fmt: skip
    assert model.input.snapshot_digest == summary["snapshot_digest"]
    assert not any("b64:" not in t and "https" in t for t in model.argv_template)
    manifest, digest = check_staged_input(model, directory)
    assert digest == summary["snapshot_digest"]
    assert {e["relative_path"] for e in manifest["entries"]} == {
        "entry.py", "data/sample.bin", "expected.json"}  # fmt: skip
    assert (directory / "entry.py").read_bytes() == (SMOKE / "entry.py").read_bytes()
    request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
    assert rt.load_manifest(request)["fetches"] == [
        {"kind": "entry", "source": "input", "path": "entry.py"}]  # fmt: skip
    # Refuses to overwrite; the CLI reports it without a traceback.
    again = ["--input-dir", str(directory), "--contract-out", str(root / "other.json")]
    assert smoke_stage.main([*again, "--custody-host", "h.ts.net"]) == 1


def test_smoke_stage_removes_only_a_directory_it_created(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(smoke_stage, "ENTRY_SOURCE", root / "absent-entry.py")
    with pytest.raises(OSError):
        smoke_stage.stage(root / "in", root / "c.json", "custody.example-tailnet.ts.net")
    assert not (root / "in").exists() and not (root / "c.json").exists()
    # A pre-existing directory makes mkdir fail and is left exactly as it was.
    (root / "mine").mkdir()
    (root / "mine" / "keep.txt").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        smoke_stage.write_input_dir(root / "mine")
    assert (root / "mine" / "keep.txt").read_bytes() == b"keep"
    # A failure after the input is written (contract path taken meanwhile) removes it.
    monkeypatch.setattr(smoke_stage, "ENTRY_SOURCE", SMOKE / "entry.py")
    monkeypatch.setattr(smoke_stage, "build_contract", lambda *a: {"bad": 1})
    with pytest.raises(ValueError):
        smoke_stage.stage(root / "in2", root / "c2.json", "custody.example-tailnet.ts.net")
    assert not (root / "in2").exists() and not (root / "c2.json").exists()


def _fake_tree(tmp_path):
    work = tmp_path / "work"
    (work / "input" / "data").mkdir(parents=True)
    (work / "out").mkdir()
    data = smoke_stage.sample_data()
    (work / "input" / "data" / "sample.bin").write_bytes(data)
    (work / "input" / "expected.json").write_bytes(
        smoke_stage.canonical({"data_sha256": sha(data)})
    )
    return work, data


def test_smoke_entry_without_a_gpu_records_unavailable_and_still_writes_outputs(tmp_path):
    work, data = _fake_tree(tmp_path)
    entry = tmp_path / "entry.py"
    entry.write_bytes((SMOKE / "entry.py").read_bytes())
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if sys.platform == "linux":
        env["LD_LIBRARY_PATH"] = str(tmp_path)  # nothing named libcuda.so.1 there
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(entry), str(work), str(work / "out")],
        capture_output=True, env=env, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    result = json.loads((work / "out" / "result.json").read_bytes())
    assert result["schema"] == "tessera.gpu-runtime-smoke.v1"
    assert result["input"]["sha256"] == sha(data) and result["input"]["matches_expected"] is True
    if not result["gpu"]["available"]:  # a developer machine may have a real driver
        assert result["gpu"]["error"] in {"library_unavailable", "no_devices"} or (
            result["gpu"]["error"].startswith("cu")
        )
    assert set(result["timings"]) == {"started_unix", "input_seconds", "probe_seconds",
                                      "total_seconds"}  # fmt: skip
    probe = (work / "out" / "probe.txt").read_text()
    assert sha(data) in probe and "gpu available" in probe
    for name in ("result.json", "probe.txt"):
        assert (work / "out" / name).stat().st_size <= 65536


class FakeCuda:
    """ctypes-shaped stand-in for libcuda.so.1 with configurable return codes."""

    def __init__(self, init=0, count=2):
        self.init, self.count = init, count

    def cuInit(self, flags):
        return self.init

    def cuDriverGetVersion(self, out):
        out._obj.value = 12040
        return 0

    def cuDeviceGetCount(self, out):
        out._obj.value = self.count
        return 0

    def cuDeviceGet(self, out, ordinal):
        out._obj.value = ordinal
        return 0

    def cuDeviceGetName(self, buffer, size, device):
        buffer.value = f"Generic GPU {device.value}".encode()
        return 0

    def cuDeviceTotalMem_v2(self, out, device):
        out._obj.value = (device.value + 1) * 1024**3
        return 0


def test_smoke_gpu_probe_records_devices_and_driver_errors(tmp_path):
    facts = smoke_entry.probe_gpu(lambda name: FakeCuda())
    assert facts["available"] is True and facts["error"] is None
    assert facts["driver_version"] == 12040 and facts["device_count"] == 2
    assert facts["devices"][1] == {"ordinal": 1, "name": "Generic GPU 1",
                                   "total_memory_bytes": 2 * 1024**3}  # fmt: skip
    failed = smoke_entry.probe_gpu(lambda name: FakeCuda(init=100))
    assert failed["available"] is False and failed["error"] == "cuInit:100"
    assert smoke_entry.probe_gpu(lambda name: FakeCuda(count=0))["error"] == "no_devices"

    def missing(name):
        raise OSError("not found")

    assert smoke_entry.probe_gpu(missing)["error"] == "library_unavailable"
    assert smoke_entry.probe_gpu(lambda name: object())["error"] == "symbol_missing"
    work, _ = _fake_tree(tmp_path)
    assert smoke_entry.main([str(work), str(work / "out")], lambda n: FakeCuda()) == 0
    result = json.loads((work / "out" / "result.json").read_bytes())
    assert result["gpu"]["devices"][0]["name"] == "Generic GPU 0"
    assert "Generic GPU 1" in (work / "out" / "probe.txt").read_text()


def test_smoke_runs_end_to_end_through_the_runtime_with_fake_custody(tmp_path):
    """Staged input -> runtime pull and verify -> real entry subprocess -> both outputs."""
    root = tmp_path.resolve()
    summary, directory, contract = _stage_smoke(root)
    model = WorkloadContractV1.model_validate_json(contract.read_bytes())
    manifest, digest = staged_input_manifest(directory)
    files = {e["relative_path"]: (directory / e["relative_path"]).read_bytes()
             for e in manifest["entries"]}  # fmt: skip
    snap = snapshot_of(files, entries=manifest["entries"])
    assert snap["digest"] == digest == summary["snapshot_digest"]
    custody = InputCustody([], snap)
    custody.outputs = model.output_plan()
    calls = []
    result = rt.execute(
        rt.parse_argv(model.derived_argv(OP, 7)[1:]), time.monotonic() + 120,
        str(root / "work"), custody_factory=lambda b, d: custody,
        opener=lambda url, timeout: calls.append(url),
    )  # fmt: skip
    assert calls == []  # no URL fetch at all
    assert [r["name"] for r in result] == ["result.json", "probe.txt"]
    retained = json.loads(custody.file("result.json"))
    assert retained["input"]["sha256"] == summary["data_sha256"]
    assert retained["input"]["matches_expected"] is True
    assert "fetched" not in retained  # only output.json gets the runtime's fetched record
    assert hashlib.sha256(custody.file("probe.txt")).hexdigest() == result[1]["sha256"]


def test_smoke_sample_data_is_fresh_per_staging():
    first, second = smoke_stage.sample_data(), smoke_stage.sample_data()
    assert len(first) == len(second) == 4096 and first != second
