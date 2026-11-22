"""ADR 0036 phase 5: generic gpu_runtime_v1 runtime (offline, stubbed network)."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = _load("gpu_runtime_v1_runtime", ROOT / "workloads/gpu_runtime_v1/runtime.py")
mf = _load("workload_manifest_script", ROOT / "scripts/workload_manifest.py")

OP, WATCHDOG = "a" * 32, "b" * 64
ENTRY_SRC = b"print('x')\n"
WHEEL = b"wheel-bytes"
FILE = b"data-bytes"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def manifest_for(blobs):
    rows = []
    for (url, kind, dest), data in blobs.items():
        rows.append(
            {"url": url, "sha256": sha(data), "max_bytes": 1000, "kind": kind, "dest": dest}
        )
    return {"schema": rt.MANIFEST_SCHEMA, "fetches": rows}


BLOBS = {
    ("https://w.example.com/p-1-py3-none-any.whl", "wheel", "p-1-py3-none-any.whl"): WHEEL,
    ("https://d.example.com/data.bin", "file", "data.bin"): FILE,
    ("https://d.example.com/entry.py", "entry", "entry.py"): ENTRY_SRC,
}


def make_request(manifest=None, ref="b64", digest=None, tmp=None):
    raw = mf.canonical(manifest or manifest_for(BLOBS))
    if ref == "file":
        path = tmp / "manifest.json"
        path.write_bytes(raw)
        refs = [f"file:{path}"]
    else:
        refs = mf.refs(raw)
    argv = ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", "4096", "1",
            digest or sha(raw), *refs]  # fmt: skip
    return rt.parse_argv(argv, allow_file=True), raw


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status, self.headers = io.BytesIO(data), status, {}

    def read(self, n=-1):
        return self.data.read(n)

    def close(self):
        pass


def opener_for(blobs, calls=None):
    by_url = {u: d for (u, _, _), d in blobs.items()}

    def opener(url, timeout):
        if calls is not None:
            calls.append(url)
        return Resp(by_url[url])

    return opener


class FakeCustody:
    """Guardian stand-in: declared plan, idempotent chunks, per-file sha256 completion."""

    def __init__(self, log, outputs=None, fail=None):
        self.log = log
        self.outputs = outputs or [{"name": "output.json", "max_bytes": 4096}]
        self.parts, self.done = {}, set()
        self.fail = dict(fail or {})  # (name, seq) -> remaining failures

    def begin_once(self):
        self.log.append("begin")
        return True

    def output_plan(self):
        self.log.append("plan")
        return {
            "schema": "tessera.output-plan.v1", "operation": OP, "epoch": "7",
            "chunk_bytes": rt.OUTPUT_CHUNK_BYTES, "outputs": self.outputs,
        }  # fmt: skip

    def put_chunk(self, name, size, digest, seq, data):
        self.log.append(("chunk", name, seq))
        if self.fail.get((name, seq)):
            self.fail[(name, seq)] -= 1
            raise rt.Failure("custody")
        self.parts.setdefault(name, {})[seq] = data
        chunks = max(1, -(-size // rt.OUTPUT_CHUNK_BYTES))
        whole = b"".join(self.parts[name].get(i, b"") for i in range(chunks))
        file_complete = len(self.parts[name]) == chunks and sha(whole) == digest
        if file_complete:
            self.done.add(name)
        return file_complete, self.done == {o["name"] for o in self.outputs}

    def file(self, name):
        parts = self.parts[name]
        return b"".join(parts[i] for i in sorted(parts))


def deadline(seconds=120):
    return time.monotonic() + seconds


# ----------------------------------------------------------------------- argv


def test_argv_roundtrip_inline_and_file(tmp_path):
    for ref in ("b64", "file"):
        request, raw = make_request(ref=ref, tmp=tmp_path)
        assert rt.binding_of(request) == {
            "operation": OP, "epoch": "7", "destination": "custody.example.com",
            "watchdog": WATCHDOG, "max_seconds": 120, "max_output_bytes": 4096,
            "max_output_files": 1,
        }  # fmt: skip
        assert rt.load_manifest(request)["schema"] == rt.MANIFEST_SCHEMA


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a[:5],
        lambda a: ["v2", *a[1:]],
        lambda a: [a[0], "Bad_Op", *a[2:]],
        lambda a: [*a[:2], "0", *a[3:]],
        lambda a: [*a[:3], "-bad..host", *a[4:]],
        lambda a: [*a[:4], "z" * 64, *a[5:]],
        lambda a: [*a[:5], "3601", *a[6:]],
        lambda a: [*a[:5], "59", *a[6:]],
        lambda a: [*a[:6], "16777217", *a[7:]],
        lambda a: [*a[:6], "0", *a[7:]],
        lambda a: [*a[:7], "17", *a[8:]],
        lambda a: [*a[:7], "0", *a[8:]],
        lambda a: [*a[:8], "nothex", *a[9:]],
        lambda a: [*a[:9], "nope"],
        lambda a: [*a[:9], "file:/x"],  # file: refused by default
        lambda a: [*a[:9], "file:relative"],
        lambda a: [*a[:9], "file:/x", "file:/y"],
        lambda a: [*a[:9], "b64:", "b64:abc"],
        lambda a: [*a, 5],
    ],
)
def test_argv_rejects_malformed(mutate):
    good = ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", "4096", "1", "c" * 64, "b64:abc"]
    rt.parse_argv(good)
    with pytest.raises(rt.Failure):
        rt.parse_argv(mutate(good))


# ------------------------------------------------------------------- manifest


def test_manifest_digest_mismatch_refused_before_parse():
    request, _ = make_request(digest="0" * 64)
    with pytest.raises(rt.Failure) as err:
        rt.load_manifest(request)
    assert err.value.code == "manifest_digest"


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m["fetches"][0].update(url="http://w.example.com/x.whl"),
        lambda m: m["fetches"][0].update(kind="script"),
        lambda m: m["fetches"][1].update(dest="../escape"),
        lambda m: m["fetches"][1].update(dest="/abs"),
        lambda m: m["fetches"][0].update(dest="sub/p.whl"),
        lambda m: m["fetches"][2].update(dest="entry.sh"),
        lambda m: m["fetches"][2].update(kind="file"),  # no entry
        lambda m: m["fetches"].append(dict(m["fetches"][1])),  # duplicate
        lambda m: m["fetches"][1].update(extra=1),
        lambda m: m.update(extra=1),
    ],
)
def test_manifest_shape_refused(edit):
    manifest = manifest_for(BLOBS)
    edit(manifest)
    request, _ = make_request(manifest)
    with pytest.raises(rt.Failure):
        rt.load_manifest(request)


def test_manifest_helper_matches_contract(tmp_path):
    from tessera_control.salad_workload_contract import WorkloadContractV1

    contract = WorkloadContractV1.model_validate(
        dict(
            contract="tessera.salad.workload.v1",
            argv_template=["v1", "{operation}", "{epoch}", "{custody_host}"],
            env={},
            workload_seconds=30,
            outputs=[dict(name="output.json", max_bytes=4096)],
            egress=dict(
                kind="provider_broad",
                fetches=[
                    dict(url="https://w.example.com/p.whl", sha256="1" * 64, max_bytes=10),
                    dict(url="https://d.example.com/e.py", sha256="2" * 64, max_bytes=20),
                ],
            ),
            custody_host="custody.example.com",
        )
    )
    manifest = mf.build_manifest(contract, ["wheel:p-1-py3-none-any.whl", "entry:e.py"])
    assert [(f["url"], f["sha256"], f["max_bytes"]) for f in manifest["fetches"]] == [
        (f.url, f.sha256, f.max_bytes) for f in contract.egress.fetches
    ]
    rt.validate_manifest(manifest)
    with pytest.raises(ValueError):
        mf.build_manifest(contract, ["entry:e.py"])
    with pytest.raises(ValueError):
        mf.build_manifest(contract, ["wheel:a.whl", "bogus:e.py"])
    path = tmp_path / "c.json"
    path.write_text(contract.model_dump_json())
    out = tmp_path / "m.json"
    assert mf.main([str(path), "--item", "wheel:p-1-py3-none-any.whl", "--item", "entry:e.py",
                    "--out", str(out)]) == 0  # fmt: skip
    assert sha(out.read_bytes()) == sha(mf.canonical(manifest))


# ---------------------------------------------------------------------- fetch


def test_fetch_all_verifies_and_records(tmp_path):
    paths, fetched = rt.fetch_all(manifest_for(BLOBS), str(tmp_path), deadline(), opener_for(BLOBS))
    assert Path(paths["entry"][0]).read_bytes() == ENTRY_SRC
    assert Path(paths["wheel"][0]).parent.name == "wheels"
    assert fetched == [
        {"url": u, "sha256": sha(d), "bytes": len(d)} for (u, _, _), d in BLOBS.items()
    ]


def test_sha_mismatch_refused_and_nothing_executed(tmp_path):
    tampered = dict(BLOBS)
    key = ("https://d.example.com/data.bin", "file", "data.bin")
    tampered[key] = b"evil"
    manifest = manifest_for(BLOBS)  # pins the genuine hashes
    request, _ = make_request(manifest)
    log, ran = [], []
    with pytest.raises(rt.Failure) as err:
        rt.execute(
            request, deadline(), str(tmp_path), custody_factory=lambda b, d: FakeCustody(log),
            opener=opener_for(tampered), installer=lambda *a: ran.append("install"),
            entry_runner=lambda *a: ran.append("entry"),
        )  # fmt: skip
    assert err.value.code == "sha256"
    assert ran == [] and log == []
    assert not (tmp_path / "files" / "data.bin").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_size_cap_enforced_while_streaming(tmp_path):
    reads = []

    class Endless(Resp):
        def read(self, n=-1):
            reads.append(n)
            return b"x" * n

    item = {"url": "https://d.example.com/big", "sha256": "0" * 64, "max_bytes": 100,
            "kind": "file", "dest": "big"}  # fmt: skip
    with pytest.raises(rt.Failure) as err:
        rt.fetch_one(item, str(tmp_path / "files" / "big"), deadline(), lambda u, t: Endless(b""))
    assert err.value.code == "size"
    assert sum(reads) <= 101 and len(reads) < 5
    assert not list(tmp_path.rglob("big*"))


def test_declared_content_length_over_cap_refused(tmp_path):
    resp = Resp(b"x")
    resp.headers = {"Content-Length": "101"}
    item = {"url": "https://d.example.com/b", "sha256": sha(b"x"), "max_bytes": 100,
            "kind": "file", "dest": "b"}  # fmt: skip
    with pytest.raises(rt.Failure) as err:
        rt.fetch_one(item, str(tmp_path / "b"), deadline(), lambda u, t: resp)
    assert err.value.code == "size"


def test_non_https_and_redirect_free_opener():
    with pytest.raises(rt.Failure):
        rt.open_url("http://example.com/x", 1)
    assert rt._NoRedirect().redirect_request() is None


# ----------------------------------------------------------------------- pip


def test_wheel_install_references_only_verified_files(tmp_path):
    paths, _ = rt.fetch_all(manifest_for(BLOBS), str(tmp_path), deadline(), opener_for(BLOBS))
    seen = {}

    def runner(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        return subprocess.CompletedProcess(command, 0)

    rt.install_wheels(paths["wheel"], str(tmp_path), deadline(), runner)
    command = seen["command"]
    assert command[1:6] == ["-I", "-m", "pip", "install", "--isolated"]
    for flag in ("--no-index", "--no-deps", "--find-links", "--target"):
        assert flag in command
    assert command[-1:] == paths["wheel"] and command[-1].endswith(".whl")
    assert command[command.index("--find-links") + 1] == str(tmp_path / "wheels")
    assert not any("http" in c for c in command)
    env = seen["kwargs"]["env"]
    assert env["PIP_NO_INDEX"] == "1" and env["PIP_CONFIG_FILE"] == os.devnull
    assert not any(k.startswith(("PYTHON", "XDG_")) for k in env)
    # no wheels -> pip is not invoked at all
    rt.install_wheels([], str(tmp_path), deadline(), lambda *a, **k: pytest.fail("pip"))
    with pytest.raises(rt.Failure):
        rt.install_wheels(paths["wheel"], str(tmp_path), deadline(),
                          lambda c, **k: subprocess.CompletedProcess(c, 1))  # fmt: skip


# ---------------------------------------------------------------------- entry


def test_entry_wall_clock_enforced(tmp_path):
    entry = tmp_path / "slow.py"
    entry.write_text("import time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(rt.Failure) as err:
        rt.run_entry(str(entry), str(tmp_path), str(tmp_path), str(tmp_path), deadline(1))
    assert err.value.code == "entry_timeout"
    assert time.monotonic() - started < 15


def test_entry_receives_work_and_out_dirs(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    entry = tmp_path / "e.py"
    entry.write_text(
        "import sys, json, os\n"
        "p = os.path.join(sys.argv[2], 'output.json')\n"
        "json.dump({'work': sys.argv[1], 'out': sys.argv[2]}, open(p, 'w'))\n"
    )
    rt.run_entry(str(entry), str(tmp_path), str(tmp_path), str(out), deadline())
    assert json.loads((out / "output.json").read_text()) == {
        "work": str(tmp_path), "out": str(out)
    }  # fmt: skip
    entry.write_text("raise SystemExit(3)\n")
    with pytest.raises(rt.Failure):
        rt.run_entry(str(entry), str(tmp_path), str(tmp_path), str(out), deadline())


# ------------------------------------------------------------------ execution


def test_begin_once_precedes_entry_and_output_retained_with_fetched(tmp_path):
    request, _ = make_request()
    log = []

    def entry_runner(entry, work, site, out_dir, dl):
        log.append(("entry", Path(entry).read_bytes()))
        (Path(out_dir) / "output.json").write_text('{"answer":42}')

    custody = FakeCustody(log)
    outputs = rt.execute(
        request, deadline(), str(tmp_path), custody_factory=lambda b, d: custody,
        opener=opener_for(BLOBS), installer=lambda *a: log.append("install") or "site",
        entry_runner=entry_runner,
    )  # fmt: skip
    assert [e if isinstance(e, str) else e[0] for e in log] == [
        "install",
        "begin",
        "plan",
        "entry",
        "chunk",
    ]
    output = custody.file("output.json")
    assert outputs == [{"name": "output.json", "sha256": sha(output), "bytes": len(output)}]
    body = json.loads(output)
    assert body["answer"] == 42
    assert body["fetched"] == [
        {"url": u, "sha256": sha(d), "bytes": len(d)} for (u, _, _), d in BLOBS.items()
    ]


def test_begin_once_denied_never_runs_entry(tmp_path):
    request, _ = make_request()

    class Denied(FakeCustody):
        def begin_once(self):
            return False

    with pytest.raises(rt.Failure):
        rt.execute(request, deadline(), str(tmp_path), custody_factory=lambda b, d: Denied([]),
                   opener=opener_for(BLOBS), installer=lambda *a: "s",
                   entry_runner=lambda *a: pytest.fail("entry ran"))  # fmt: skip


@pytest.mark.parametrize(
    "text", ["[1]", "not json", '{"fetched":[]}', '{"pad":"' + "x" * 4096 + '"}']
)
def test_bad_output_refused_not_retained(tmp_path, text):
    request, _ = make_request()
    log = []

    def entry_runner(entry, work, site, out_dir, dl):
        (Path(out_dir) / "output.json").write_text(text)

    with pytest.raises(rt.Failure):
        rt.execute(
            request, deadline(), str(tmp_path), custody_factory=lambda b, d: FakeCustody(log),
            opener=opener_for(BLOBS), installer=lambda *a: "s", entry_runner=entry_runner,
        )  # fmt: skip
    assert log == ["begin", "plan"]


def test_custody_posts_binding_and_protocol(monkeypatch):
    request, _ = make_request()
    binding = rt.binding_of(request)
    sent = []

    class Conn:
        def __init__(self, host, port, timeout=None, context=None):
            self.host = host

        def request(self, method, path, body=None, headers=None):
            sent.append((self.host, method, path, body, headers))
            self.path = path

        def getresponse(self):
            path = sent[-1][2]
            if path == "/v1/token":
                payload = base64.urlsafe_b64encode(
                    json.dumps({"exp": int(time.time()) + 600}).encode()
                ).decode().rstrip("=")  # fmt: skip
                body = {"jwt": f"aaa.{payload}.ccc"}
            elif path.endswith("begin-once"):
                body = {"schema": "tessera.begin-once.v1", "granted": True,
                        "operation": OP, "epoch": "7"}  # fmt: skip
            else:
                body = {"schema": "tessera.retained.v1", "sha256": sha(b'{"a":1}'),
                        "operation": OP, "epoch": "7", "retained": True}  # fmt: skip
            return type(
                "R", (), {"status": 200, "read": lambda s, n=-1: json.dumps(body).encode()}
            )()

        def close(self):
            pass

    monkeypatch.setattr(rt.http.client, "HTTPConnection", Conn)
    monkeypatch.setattr(rt.http.client, "HTTPSConnection", Conn)
    custody = rt.HTTPSCustody(binding, deadline())
    assert custody.begin_once() is True
    assert custody.retain(b'{"a":1}', sha(b'{"a":1}')) is True
    assert [s[2] for s in sent] == ["/v1/token", "/v1/workload/begin-once", "/v1/workload/output"]
    assert sent[0][0] == "169.254.169.254" and sent[1][0] == "custody.example.com"
    assert json.loads(sent[1][3]) == {"binding": binding}
    assert sent[1][4]["Authorization"].startswith("Bearer aaa.")


def test_main_prints_detail_free_failure(capsys):
    assert rt.main(["bogus"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "argv"}


def test_dockerfile_recipe_shape():
    text = (ROOT / "workloads/gpu_runtime_v1/Dockerfile").read_text()
    assert "ARG GPU_RUNTIME_BASE\n" in text and "GPU_RUNTIME_BASE=" not in text
    assert "FROM --platform=linux/amd64 ${GPU_RUNTIME_BASE}" in text
    assert "sha256:[a-f0-9]{64}" in text and "torch" not in text.lower()
    assert "ARG UBUNTU_SNAPSHOT\n" in text and "snapshot.ubuntu.com" in text
    assert "ca-certificates" in text and "UBUNTU_SNAPSHOT=" not in text
    assert "USER 65532:65532" in text
    assert 'ENTRYPOINT ["/tessera/bin/run"]' in text
    assert (ROOT / "workloads/gpu_runtime_v1/run").stat().st_mode & 0o111
    assert sys.version_info >= (3, 10)


def test_file_ref_refused_unless_test_only_flag(tmp_path):
    ref = f"file:{tmp_path}/m.json"
    argv = ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", "4096", "1", "c" * 64, ref]
    with pytest.raises(rt.Failure):
        rt.parse_argv(argv)
    assert rt.parse_argv(argv, allow_file=True)["manifest_ref"] == [ref]


def test_refuses_claim_with_too_little_time_left(tmp_path):
    request, _ = make_request()
    log = []
    with pytest.raises(rt.Failure) as err:
        rt.execute(
            request, deadline(rt.MIN_CLAIM_SECONDS - 5), str(tmp_path),
            custody_factory=lambda b, d: log.append("custody") or FakeCustody(log),
            opener=opener_for(BLOBS), installer=lambda *a: "s",
            entry_runner=lambda *a: pytest.fail("entry ran"),
        )  # fmt: skip
    assert err.value.code == "deadline" and log == []


def test_prepare_work_wipes_previous_state(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "old").write_text("x")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "output.json").write_text("{}")
    (tmp_path / "stray").write_text("x")
    out = rt.prepare_work(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"] and os.listdir(out) == []


def test_entry_env_drops_interpreter_state(monkeypatch):
    monkeypatch.setenv("PYTHONSTARTUP", "/evil")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/evil")
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    env = rt._entry_env("/w", "/w/site")
    assert "PYTHONSTARTUP" not in env and "XDG_CONFIG_HOME" not in env
    assert env["OMP_NUM_THREADS"] == "4" and env["PYTHONPATH"] == "/w/site"


# ------------------------------------------------- contract <-> manifest binding


def runtime_contract(**over):
    from tessera_control.salad_workload_contract import WorkloadContractV1

    fetches = [
        dict(url="https://w.example.com/p.whl", sha256="1" * 64, max_bytes=10),
        dict(url="https://d.example.com/e.py", sha256="2" * 64, max_bytes=20),
    ]
    base = dict(
        contract="tessera.salad.workload.v1",
        runtime="gpu_runtime_v1",
        argv_template=[],
        env={},
        workload_seconds=120,
        outputs=[dict(name="output.json", max_bytes=4096)],
        egress=dict(kind="provider_broad", fetches=fetches),
        custody_host="custody.example.com",
    )
    base.update(over)
    manifest = {
        "schema": rt.MANIFEST_SCHEMA,
        "fetches": [
            {**f, "kind": k, "dest": d}
            for f, k, d in zip(
                fetches, ("wheel", "entry"), ("p-1-py3-none-any.whl", "e.py"), strict=True
            )  # fmt: skip
        ],
    }
    manifest = over.pop("manifest", manifest) if "manifest" in over else manifest
    raw = mf.canonical(manifest)
    if not base["argv_template"]:
        base["argv_template"] = [
            "v1", "{operation}", "{epoch}", "{custody_host}", WATCHDOG,
            str(base["workload_seconds"]), "4096", "1", sha(raw), *mf.refs(raw),
        ]  # fmt: skip
    return WorkloadContractV1, base, manifest


def test_contract_binding_accepts_matching_manifest():
    cls, base, _ = runtime_contract()
    contract = cls.model_validate(base)
    request = rt.parse_argv(contract.derived_argv(OP, 7)[1:])
    assert rt.load_manifest(request)["fetches"][0]["url"] == "https://w.example.com/p.whl"


def _bad(edit):
    from pydantic import ValidationError

    cls, base, manifest = runtime_contract()
    edit(base, manifest)
    raw = mf.canonical(manifest)
    t = base["argv_template"]
    if "keep_digest" not in base:
        t[8:] = [sha(raw), *mf.refs(raw)]
    base.pop("keep_digest", None)
    with pytest.raises(ValidationError):
        cls.model_validate(base)


@pytest.mark.parametrize(
    "edit",
    [
        lambda b, m: m["fetches"].append(
            {
                "url": "https://evil.example.net/x",
                "sha256": "3" * 64,
                "max_bytes": 1,
                "kind": "file",
                "dest": "x",
            }  # fmt: skip
        ),
        lambda b, m: m["fetches"][0].update(sha256="9" * 64),
        lambda b, m: m["fetches"][0].update(max_bytes=11),
        lambda b, m: m["fetches"][0].update(url="https://w.example.com/other.whl"),
        lambda b, m: m["fetches"].pop(),
        lambda b, m: b["argv_template"].__setitem__(8, "0" * 64) or b.update(keep_digest=1),
        lambda b, m: b["argv_template"].__setitem__(5, "121"),
        lambda b, m: b["argv_template"].__setitem__(6, "4097"),
        lambda b, m: b["argv_template"].__setitem__(7, "2"),
        lambda b, m: b["argv_template"].__setitem__(4, "nothex"),
        lambda b, m: b["argv_template"].__setitem__(1, "literal"),
        lambda b, m: b.update(
            argv_template=[*b["argv_template"][:9], "file:/m.json"], keep_digest=1
        ),
        lambda b, m: b.update(argv_template=[*b["argv_template"][:9], "b64:!!"], keep_digest=1),
        lambda b, m: b.update(workload_seconds=59) or b["argv_template"].__setitem__(5, "59"),
    ],
)
def test_contract_binding_mismatch_refused(edit):
    _bad(edit)


def test_contract_without_runtime_field_unchanged():
    cls, base, _ = runtime_contract()
    base.pop("runtime")
    base["argv_template"] = ["anything", "{operation}"]
    assert cls.model_validate(base).runtime is None


# ------------------------------------------- ADR 0036 phase 3: declared multi-file outputs

C = rt.OUTPUT_CHUNK_BYTES
MULTI = [
    {"name": "output.json", "max_bytes": 4096},
    {"name": "weights.bin", "max_bytes": 3 * C},
    {"name": "log.txt", "max_bytes": 100},
]
WEIGHTS = bytes(range(256)) * (2 * C // 256) + b"tail"


def multi_request(outputs=MULTI):
    raw = mf.canonical(manifest_for(BLOBS))
    total = str(sum(o["max_bytes"] for o in outputs))
    argv = ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", total, str(len(outputs)),
            sha(raw), *mf.refs(raw)]  # fmt: skip
    return rt.parse_argv(argv)


def run_multi(tmp_path, write, custody=None, outputs=MULTI, request=None):
    log = []
    custody = custody or FakeCustody(log, outputs=outputs)
    custody.log = log

    def entry_runner(entry, work, site, out_dir, dl):
        log.append("entry")
        write(Path(out_dir))

    result = rt.execute(
        request or multi_request(outputs), deadline(), str(tmp_path / "work"),
        custody_factory=lambda b, d: custody, opener=opener_for(BLOBS),
        installer=lambda *a: "site", entry_runner=entry_runner,
    )  # fmt: skip
    return result, custody, log


def write_all(out):
    (out / "output.json").write_text('{"answer":42}')
    (out / "weights.bin").write_bytes(WEIGHTS)
    (out / "log.txt").write_bytes(b"")
    (out / "undeclared.bin").write_bytes(b"never uploaded")


def test_every_declared_output_is_staged_and_uploaded_in_chunks(tmp_path):
    result, custody, log = run_multi(tmp_path, write_all)
    assert log[:3] == ["begin", "plan", "entry"]
    chunks = [e[1:] for e in log if isinstance(e, tuple)]
    assert chunks == [
        ("output.json", 0), ("weights.bin", 0), ("weights.bin", 1), ("weights.bin", 2),
        ("log.txt", 0),
    ]  # fmt: skip
    assert custody.file("weights.bin") == WEIGHTS and custody.file("log.txt") == b""
    assert "undeclared.bin" not in custody.parts
    output = custody.file("output.json")
    assert json.loads(output)["fetched"] and json.loads(output)["answer"] == 42
    assert result == [
        {"name": "output.json", "sha256": sha(output), "bytes": len(output)},
        {"name": "weights.bin", "sha256": sha(WEIGHTS), "bytes": len(WEIGHTS)},
        {"name": "log.txt", "sha256": sha(b""), "bytes": 0},
    ]


@pytest.mark.parametrize("problem", ["missing", "oversize", "symlink", "directory", "bad-json"])
def test_any_bad_declared_output_fails_before_the_first_chunk(tmp_path, problem):
    def write(out):
        write_all(out)
        if problem == "missing":
            (out / "log.txt").unlink()
        elif problem == "oversize":
            (out / "log.txt").write_bytes(b"x" * 101)
        elif problem == "symlink":
            (out / "log.txt").unlink()
            (out / "log.txt").symlink_to(out / "undeclared.bin")
        elif problem == "directory":
            (out / "log.txt").unlink()
            (out / "log.txt").mkdir()
        else:
            (out / "output.json").write_text("[1]")

    custody = FakeCustody([], outputs=MULTI)
    with pytest.raises(rt.Failure) as err:
        run_multi(tmp_path, write, custody=custody)
    assert err.value.code == "output"
    assert custody.log == ["begin", "plan", "entry"] and custody.parts == {}


@pytest.mark.parametrize(
    "plan_edit",
    [
        lambda p: p.update(chunk_bytes=C // 2),
        lambda p: p.update(outputs=p["outputs"][:2]),
        lambda p: p["outputs"][1].update(max_bytes=3 * C - 1),
        lambda p: p["outputs"][1].update(name="../weights.bin"),
        lambda p: p["outputs"][2].update(name="output.json"),
    ],
)
def test_plan_that_disagrees_with_the_reviewed_argv_is_refused_before_the_entry(
    tmp_path, plan_edit
):
    class Edited(FakeCustody):
        def output_plan(self):
            plan = json.loads(json.dumps(super().output_plan()))
            plan_edit(plan)
            return plan

    custody = Edited([], outputs=MULTI)
    with pytest.raises(rt.Failure) as err:
        run_multi(tmp_path, lambda out: pytest.fail("entry ran"), custody=custody)
    assert err.value.code == "output_plan"


def test_chunk_retries_are_bounded_per_chunk_and_overall(tmp_path):
    flaky = FakeCustody([], outputs=MULTI, fail={("weights.bin", 1): 2})
    run_multi(tmp_path / "a", write_all, custody=flaky)
    assert flaky.file("weights.bin") == WEIGHTS
    broken = FakeCustody([], outputs=MULTI, fail={("weights.bin", 1): 3})
    with pytest.raises(rt.Failure):
        run_multi(tmp_path / "b", write_all, custody=broken)
    assert ("chunk", "log.txt", 0) not in broken.log
    spread = {("output.json", 0): 2, ("weights.bin", 0): 2, ("weights.bin", 1): 2,
              ("weights.bin", 2): 2, ("log.txt", 0): 1}  # fmt: skip
    exhausted = FakeCustody([], outputs=MULTI, fail=spread)
    with pytest.raises(rt.Failure):
        run_multi(tmp_path / "c", write_all, custody=exhausted)
    assert len([e for e in exhausted.log if isinstance(e, tuple)]) == 5 + rt.UPLOAD_RETRY_BUDGET


def test_upload_requires_the_guardian_to_report_all_outputs_complete(tmp_path):
    class NeverComplete(FakeCustody):
        def put_chunk(self, *args):
            file_complete, _ = super().put_chunk(*args)
            return file_complete, False

    with pytest.raises(rt.Failure) as err:
        run_multi(tmp_path, write_all, custody=NeverComplete([], outputs=MULTI))
    assert err.value.code == "custody"


def test_argv_accepts_up_to_sixteen_outputs_and_sixteen_mib():
    request = multi_request([{"name": f"f{i}.bin", "max_bytes": C} for i in range(16)])
    assert request["max_output_files"] == 16 and request["max_output_bytes"] == 16 * C


def test_custody_plan_and_chunk_protocol(monkeypatch):
    request = multi_request()
    binding = rt.binding_of(request)
    sent = []
    replies = []

    class Conn:
        def __init__(self, host, port, timeout=None, context=None):
            self.host = host

        def request(self, method, path, body=None, headers=None):
            sent.append((self.host, path, body))

        def getresponse(self):
            path = sent[-1][1]
            if path == "/v1/token":
                payload = base64.urlsafe_b64encode(
                    json.dumps({"exp": int(time.time()) + 600}).encode()
                ).decode().rstrip("=")  # fmt: skip
                body = {"jwt": f"aaa.{payload}.ccc"}
            else:
                body = replies.pop(0)
            return type(
                "R", (), {"status": 200, "read": lambda s, n=-1: json.dumps(body).encode()}
            )()

        def close(self):
            pass

    monkeypatch.setattr(rt.http.client, "HTTPConnection", Conn)
    monkeypatch.setattr(rt.http.client, "HTTPSConnection", Conn)
    custody = rt.HTTPSCustody(binding, deadline())
    plan = {"schema": "tessera.output-plan.v1", "operation": OP, "epoch": "7",
            "chunk_bytes": C, "outputs": MULTI}  # fmt: skip
    replies.append(plan)
    assert custody.output_plan() == plan
    ok = {"schema": "tessera.output-chunk.v1", "operation": OP, "epoch": "7",
          "name": "log.txt", "seq": 0, "stored": True, "file_complete": True,
          "outputs_complete": False}  # fmt: skip
    replies.append(ok)
    assert custody.put_chunk("log.txt", 3, sha(b"abc"), 0, b"abc") == (True, False)
    body = json.loads(sent[-1][2])
    assert sent[-1][1] == "/v1/workload/output-chunk" and body == {
        "binding": binding, "name": "log.txt", "sha256": sha(b"abc"), "bytes": 3, "seq": 0,
        "chunk_sha256": sha(b"abc"), "content_base64": base64.b64encode(b"abc").decode(),
    }  # fmt: skip
    for bad in ({**ok, "seq": 1}, {**ok, "stored": False}, {**ok, "extra": 1}, {**plan}):
        replies.append(bad)
        with pytest.raises(rt.Failure):
            custody.put_chunk("log.txt", 3, sha(b"abc"), 0, b"abc")
    replies.append({**plan, "epoch": "8"})
    with pytest.raises(rt.Failure):
        custody.output_plan()
