"""Provider-side fetch of pinned public files fails closed (ADR 0097, ADR 0116).

The Salad stage's in-container runtime (``gpu_runtime_v1``) downloads each pinned file itself and
verifies its sha256 and ``max_bytes`` before anything uses it. These tests drive the whole runtime
(``execute`` and ``main``) offline with fake transports and check that a missing file, an oversize
body, a redirect to an unlisted host, a tampered body or a poisoned cache entry stops the run before
the single custody claim: no wheel install, no entry, no output, no ``.part`` file, and a poisoned
cache entry is never served.
"""

import hashlib
import importlib.util
import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = _load("gpu_runtime_fail_closed", ROOT / "workloads/gpu_runtime_v1/runtime.py")
mf = _load("workload_manifest_fail_closed", ROOT / "scripts/workload_manifest.py")

OP, WATCHDOG = "a" * 32, "b" * 64
WEIGHTS_URL = "https://models.example.com/weights.bin"
OTHER_URL = "https://models.example.com/tokenizer.json"
ENTRY_URL = "https://code.example.com/entry.py"
UNLISTED = "https://mirror.example.net/weights.bin"
WEIGHTS = b"pinned-public-weights" * 4
OTHER = b'{"vocab": 1}'
ENTRY = b"print('x')\n"
CAP = 200


def sha(data):
    return hashlib.sha256(data).hexdigest()


def row(url, data, dest, kind="file", cache=None):
    item = {"url": url, "sha256": sha(data), "max_bytes": CAP, "kind": kind, "dest": dest}
    if cache is not None:
        item["cache"] = cache
    return item


def manifest(cache=None, other=False):
    rows = [row(WEIGHTS_URL, WEIGHTS, "weights.bin", cache=cache)]
    if other:
        rows.append(row(OTHER_URL, OTHER, "tokenizer.json"))
    rows.append(row(ENTRY_URL, ENTRY, "entry.py", kind="entry"))
    return {"schema": rt.MANIFEST_SCHEMA, "fetches": rows}


def argv(value):
    raw = mf.canonical(value)
    return ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", "4096", "1", sha(raw),
            *mf.refs(raw)]  # fmt: skip


class Resp:
    def __init__(self, data=b"", status=200, headers=None):
        self.data, self.status, self.headers = io.BytesIO(data), status, headers or {}

    def read(self, n=-1):
        return self.data.read(n)

    def close(self):
        pass


class Transport:
    """One request per call: url -> bytes, a Resp, or an exception to raise."""

    def __init__(self, **over):
        self.routes = {WEIGHTS_URL: WEIGHTS, OTHER_URL: OTHER, ENTRY_URL: ENTRY, **over}
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, Resp) else Resp(value)


class Spy:
    """Records every step after the fetch; any of them happening means the run did not stop."""

    def __init__(self):
        self.log = []

    def custody(self, binding, deadline):
        self.log.append("claim")
        raise AssertionError("the custody claim must never be reached")

    def install(self, *args):
        self.log.append("install")
        return "site"

    def entry(self, *args, **kwargs):
        self.log.append("entry")


def run(tmp_path, transport, value=None, first_ok=False):
    spy = Spy()
    work = tmp_path / "work"
    with pytest.raises(rt.Failure) as err:
        rt.execute(
            rt.parse_argv(argv(value or manifest())), time.monotonic() + 120, str(work),
            custody_factory=spy.custody, opener=transport, installer=spy.install,
            entry_runner=spy.entry,
        )  # fmt: skip
    assert spy.log == []  # nothing installed, run, claimed or uploaded
    weights = work / "files" / "weights.bin"
    # Only verified pinned bytes may exist (an earlier file that verified); never anything else.
    assert weights.read_bytes() == WEIGHTS if first_ok else not weights.exists()
    assert not list(tmp_path.rglob("*.part"))
    return err.value.code


def http_error(code):
    return urllib.error.HTTPError(WEIGHTS_URL, code, "x", {}, io.BytesIO(b""))


# ------------------------------------------------------------------- missing file


@pytest.mark.parametrize(
    "answer, code",
    [
        (http_error(404), "fetch"),  # what the real open_url raises for a missing file
        (http_error(410), "fetch"),
        (Resp(b"not found", status=404), "fetch"),  # a non-redirect, non-200 status
        (urllib.error.URLError("unreachable"), "fetch"),
        (ConnectionResetError(), "fetch"),
        (Resp(b""), "sha256"),  # an empty 200 body
        (Resp(WEIGHTS[:-1]), "sha256"),  # a truncated body
    ],
)
def test_a_missing_or_truncated_file_fails_before_the_claim(tmp_path, answer, code):
    transport = Transport(**{WEIGHTS_URL: answer})
    assert run(tmp_path, transport) == code
    assert ENTRY_URL not in transport.calls  # fail fast: nothing after the failed file


# ----------------------------------------------------------------------- oversize


@pytest.mark.parametrize(
    "answer",
    [
        Resp(b"x" * (CAP + 1)),  # streamed beyond max_bytes
        Resp(WEIGHTS, headers={"Content-Length": str(CAP + 1)}),  # declared beyond max_bytes
        Resp(WEIGHTS + b"x" * CAP),  # the right prefix followed by extra bytes
    ],
)
def test_an_oversize_file_fails_before_the_claim(tmp_path, answer):
    assert run(tmp_path, Transport(**{WEIGHTS_URL: answer})) == "size"


# -------------------------------------------------------------------- redirection


@pytest.mark.parametrize("status", rt.REDIRECT_STATUSES)
def test_a_redirect_to_an_unlisted_host_fails_and_the_host_is_never_contacted(tmp_path, status):
    transport = Transport(
        **{WEIGHTS_URL: Resp(status=status, headers={"Location": UNLISTED}), UNLISTED: WEIGHTS}
    )
    assert run(tmp_path, transport) == "redirect"
    assert transport.calls == [WEIGHTS_URL]


# ------------------------------------------------------------- partial completion


def test_a_later_file_failing_leaves_the_verified_earlier_file_unused(tmp_path):
    transport = Transport(**{OTHER_URL: b"tampered"})
    assert run(tmp_path, transport, manifest(other=True), first_ok=True) == "sha256"
    assert transport.calls == [WEIGHTS_URL, OTHER_URL]
    assert not (tmp_path / "work" / "files" / "tokenizer.json").exists()


# -------------------------------------------------------------------------- cache


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    path = tmp_path / "persist"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    monkeypatch.setenv(rt.CACHE_DIR_ENV, str(path))
    monkeypatch.setenv(rt.CACHE_MAX_BYTES_ENV, "100000")
    return path


def poison(cache_dir, data):
    entry = cache_dir / sha(WEIGHTS)
    entry.write_bytes(data)
    entry.chmod(0o600)
    return entry


@pytest.mark.parametrize("planted", [b"y" * len(WEIGHTS), b"evil", WEIGHTS + b"tail"])
def test_a_poisoned_entry_is_never_served_and_the_run_uses_only_verified_bytes(
    tmp_path, cache_dir, planted
):
    poison(cache_dir, planted)
    seen = []

    def entry(path, work, site, out_dir, deadline, **kwargs):
        seen.append(Path(work, "files", "weights.bin").read_bytes())
        Path(out_dir, "output.json").write_text("{}")

    class Custody:
        def __init__(self, binding, deadline):
            self.done = set()

        def begin_once(self):
            return True

        def output_plan(self):
            return {
                "schema": "tessera.output-plan.v1", "operation": OP, "epoch": "7",
                "chunk_bytes": rt.OUTPUT_CHUNK_BYTES,
                "outputs": [{"name": "output.json", "max_bytes": 4096}],
            }  # fmt: skip

        def put_chunk(self, name, size, digest, seq, data):
            return True, True

    transport = Transport()
    rt.execute(
        rt.parse_argv(argv(manifest(cache=True))), time.monotonic() + 120, str(tmp_path / "w"),
        custody_factory=Custody, opener=transport, installer=lambda *a: "site",
        entry_runner=entry,
    )  # fmt: skip
    assert seen == [WEIGHTS]  # the pinned bytes, fetched again, never the planted ones
    assert WEIGHTS_URL in transport.calls


def test_a_poisoned_entry_and_a_tampered_source_fail_and_nothing_is_cached(tmp_path, cache_dir):
    entry = poison(cache_dir, b"y" * len(WEIGHTS))
    code = run(tmp_path, Transport(**{WEIGHTS_URL: b"z" * len(WEIGHTS)}), manifest(cache=True))
    assert code == "sha256"
    assert not entry.exists()  # the confirmed mismatch is discarded
    assert [p.name for p in cache_dir.iterdir()] == []  # the tampered bytes are not stored


# --------------------------------------------------------------------------- main


def test_main_reports_a_detail_free_failure_and_never_claims(tmp_path, monkeypatch, capsys):
    claims = []
    monkeypatch.setattr(rt, "WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(rt.HTTPSCustody, "__init__", lambda *a: claims.append(a))
    monkeypatch.setattr(rt, "open_url", Transport(**{WEIGHTS_URL: http_error(404)}))
    assert rt.main(argv(manifest())) == 1
    status = json.loads(capsys.readouterr().out)
    assert status == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "fetch"}
    assert claims == [] and not list(tmp_path.rglob("*.part"))
    assert not (tmp_path / "work" / "files" / "weights.bin").exists()
