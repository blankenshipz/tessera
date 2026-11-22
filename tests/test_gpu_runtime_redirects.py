"""Reviewed HTTP redirects for gpu_runtime_v1 fetches (synthetic, no network).

A pinned fetch URL may redirect to a CDN whose signed URL cannot be pinned. The
contract declares the exact redirect hosts (``egress.redirect_hosts``), they join
the capability network, an argv ``redirect-hosts:`` token binds the manifest's
list, and the runtime follows at most three HTTPS redirects to exactly those
hosts while the per-fetch cap and sha256 still bind the final bytes.
"""

import copy
import json
import sqlite3
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from pydantic import ValidationError
from test_gpu_runtime_v1 import OP, WATCHDOG, FakeCustody, Resp, deadline, mf, rt, sha
from test_salad_input_path import PUBLIC_INPUT, staged_dir
from test_salad_live_setup import inputs, private_file, setup_script  # noqa: F401
from test_salad_workload_contract import contract, execution

from tessera_control import salad_workload_contract as swc
from tessera_control.remote_execution_models import SaladExecution
from tessera_control.salad_workload_admission import check_workload_admission
from tessera_control.salad_workload_contract import (
    WorkloadContractV1,
    capability_network_matches,
    fetches_digest,
    redirect_hosts_digest,
)
from tessera_control.salad_workload_input import check_staged_input, staged_input_manifest
from tessera_control.store import canonical_digest

ORIGIN = "https://hub.example.com/org/repo/resolve/0123abcd/weights.bin"
CDN = "cdn.example.net"
HOSTS = [CDN, "mirror.example.org"]
DATA = b"artifact-bytes"
ENTRY = b"print('x')\n"
FETCHES = [
    dict(url=ORIGIN, sha256=sha(DATA), max_bytes=100),
    dict(url="https://hub.example.com/entry.py", sha256=sha(ENTRY), max_bytes=100),
]


def token(hosts=HOSTS):
    return "redirect-hosts:" + redirect_hosts_digest(hosts)


def runtime_manifest(hosts=HOSTS):
    rows = [{**f, "kind": k, "dest": d} for f, k, d in zip(
        FETCHES, ("file", "entry"), ("weights.bin", "entry.py"), strict=True)]  # fmt: skip
    manifest = {"schema": rt.MANIFEST_SCHEMA, "fetches": rows}
    if hosts:
        manifest["redirect_hosts"] = list(hosts)
    return manifest


def build(hosts=HOSTS, *, manifest=None, tok="auto", input_digest=None, in_input=False,
          custody_host="localhost", max_bytes=1 << 20):  # fmt: skip
    """A gpu_runtime_v1 contract with reviewed redirect hosts; returns (value, manifest raw)."""
    raw = mf.canonical(runtime_manifest(hosts) if manifest is None else manifest)
    tail = [f"input:{input_digest}:{max_bytes}"] if input_digest else []
    tok = (token(hosts) if hosts else None) if tok == "auto" else tok
    tail += [tok] if tok else []
    if in_input:
        rows = [(f["url"], f["sha256"], f["max_bytes"]) for f in FETCHES]
        tail.append(f"input-manifest:{fetches_digest(rows)}:runtime/manifest.json")
    else:
        tail += mf.refs(raw)
    egress = dict(kind="provider_broad", fetches=FETCHES)
    if hosts:
        egress["redirect_hosts"] = list(hosts)
    value = dict(
        contract="tessera.salad.workload.v1", runtime="gpu_runtime_v1",
        argv_template=["v1", "{operation}", "{epoch}", "{custody_host}", WATCHDOG, "120",
                       "4096", "1", sha(raw), *tail],
        env={}, workload_seconds=120, outputs=[dict(name="output.json", max_bytes=4096)],
        egress=egress, custody_host=custody_host,
    )  # fmt: skip
    if input_digest:
        value["input"] = dict(kind="staged_snapshot", snapshot_digest=input_digest,
                              max_bytes=max_bytes)  # fmt: skip
    return value, raw


# ------------------------------------------------------------------ contract


# Digests of the fixture contract and Salad execution computed with the code at
# d652415, before redirect_hosts existed. An empty list must not change them.
CONTRACT_DIGEST = "aa9992b4afae2d5e4b79f21a74c66b83e3d66746b3bcb02b34e2e0e65f0ccecf"
EXECUTION_DIGEST = "632730c7d7d5b17f9cb7319ca773c4900e04ceabacfa8d75d0c2b1571b42275e"


def test_empty_redirect_hosts_keep_existing_contract_and_proposal_digests():
    for work in (contract(), contract(egress={**contract()["egress"], "redirect_hosts": []})):
        model = WorkloadContractV1.model_validate(work)
        dumped = model.model_dump(mode="json")
        assert "redirect_hosts" not in dumped["egress"]
        assert canonical_digest(dumped) == CONTRACT_DIGEST
        assert json.loads(model.model_dump_json()) == dumped
        dumped_exec = SaladExecution.model_validate(execution(work)).model_dump(mode="json")
        assert canonical_digest(dumped_exec) == EXECUTION_DIGEST


def test_declared_redirect_hosts_are_serialized_and_round_trip():
    value, _ = build()
    model = WorkloadContractV1.model_validate(value)
    dumped = model.model_dump(mode="json")
    assert dumped["egress"]["redirect_hosts"] == HOSTS
    assert WorkloadContractV1.model_validate(dumped) == model


def test_redirect_hosts_join_the_capability_network_declaration():
    work, _ = build()
    model = WorkloadContractV1.model_validate(work)
    expected = [
        dict(host=CDN, port=443),
        dict(host="hub.example.com", port=443),
        dict(host="mirror.example.org", port=443),
    ]
    assert model.capability_network() == expected
    profile = execution(work)
    assert capability_network_matches(profile, dict(network=expected))
    # Fetch hosts alone no longer satisfy the declaration: reviewers approve the CDN too.
    assert not capability_network_matches(profile, dict(network=[expected[1]]))
    # A redirect host already a fetch host on 443 is one destination, not two.
    same, _ = build(hosts=["hub.example.com"])
    assert WorkloadContractV1.model_validate(same).capability_network() == [expected[1]]


def test_redirect_hosts_require_the_gpu_runtime_v1_runtime():
    """Only gpu_runtime_v1 binds and enforces the set; elsewhere it would be unenforced."""
    with pytest.raises(ValidationError, match="gpu_runtime_v1"):
        WorkloadContractV1.model_validate(
            contract(egress={**contract()["egress"], "redirect_hosts": HOSTS})
        )
    value, _ = build()
    with pytest.raises(ValidationError, match="gpu_runtime_v1"):
        WorkloadContractV1.model_validate(dict(value, runtime=None))
    # Without redirect hosts the runtime field stays optional, as before.
    WorkloadContractV1.model_validate(
        contract(egress={**contract()["egress"], "redirect_hosts": []})
    )


@pytest.mark.parametrize(
    "hosts",
    [
        ["*.example.net"],
        ["cdn.example.net:443"],
        ["https://cdn.example.net"],
        ["CDN.example.net"],
        ["10.0.0.1"],
        ["localhost"],
        ["cdn.example.net."],
        ["-cdn.example.net"],
        ["cdn..example.net"],
        ["cdn.example.net/path"],
        ["b.example.net", "a.example.net"],  # unsorted
        ["a.example.net", "a.example.net"],  # duplicate
        [f"h{i:02d}.example.net" for i in range(17)],  # over 16
        ["a" * 64 + ".example.net"],  # label over 63
        ["0x7f.0x1"],  # hex IP-like labels
        ["0x7f.example.net"],
        ["cdn.0x1f.example.net"],
        ["127.0.0.1.example.net"],  # all-digit labels anywhere
        ["cdn.123.example.net"],
        ["0177.0.0.1"],  # octal IP-like
        [""],
    ],
)
def test_redirect_hosts_must_be_exact_sorted_dns_names(hosts):
    with pytest.raises(ValidationError, match="redirect"):
        swc.Egress.model_validate(dict(kind="provider_broad", fetches=[], redirect_hosts=hosts))


def test_redirect_hosts_count_toward_the_destination_bound():
    sixteen = [f"h{i:02d}.example.net" for i in range(16)]
    fetches = [dict(url=f"https://f{i:02d}.example.com/x", sha256="c" * 64, max_bytes=1)
               for i in range(17)]  # fmt: skip
    with pytest.raises(ValidationError, match="32"):
        swc.Egress.model_validate(
            dict(kind="provider_broad", fetches=fetches, redirect_hosts=sixteen)
        )
    swc.Egress.model_validate(
        dict(kind="provider_broad", fetches=fetches[:16], redirect_hosts=sixteen)
    )


def test_gpu_runtime_contract_binds_redirect_hosts_through_argv_and_manifest():
    for in_input in (False, True):
        value, _ = build(input_digest="d" * 64 if in_input else None, in_input=in_input)
        model = WorkloadContractV1.model_validate(value)
        request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
        assert request["redirect_hosts_sha256"] == redirect_hosts_digest(HOSTS)
        assert request["manifest_in_input"] is not None or rt.load_manifest(request)[
            "redirect_hosts"] == HOSTS  # fmt: skip


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(tok=None),  # hosts declared, no token
        dict(tok=token(["other.example.net"])),  # token for another list
        dict(tok="redirect-hosts:" + "A" * 64),
        dict(hosts=[], tok=token()),  # token without declared hosts
        dict(manifest=runtime_manifest(["other.example.net"])),  # manifest list differs
        dict(manifest=runtime_manifest(None)),  # manifest omits the list
    ],
)
def test_contract_redirect_binding_mismatches_are_refused(kwargs):
    value, _ = build(**kwargs)
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(value)


def test_token_must_follow_the_input_token():
    value, _ = build(input_digest="d" * 64)
    t = value["argv_template"]
    t[9], t[10] = t[10], t[9]
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(value)


def test_strict_rules_refuse_a_manifest_naming_hosts_the_contract_does_not():
    """Lenient at parse (stored proposals), strict for new routes (setup, admission)."""
    value, _ = build(hosts=[], manifest=runtime_manifest(HOSTS))
    model = WorkloadContractV1.model_validate(value)  # parses: no hosts declared
    with pytest.raises(ValueError, match="redirect_hosts"):
        swc.check_gpu_runtime_inline_rules(model)
    with pytest.raises(ValueError, match="redirect_hosts"):
        check_workload_admission(None, "project", _selection(value))
    stray = {**runtime_manifest(None), "extra": 1}
    with pytest.raises(ValueError, match="keys"):
        swc.check_gpu_runtime_manifest_rules(stray, False)
    good, _ = build()
    check_workload_admission(None, "project", _selection(good))
    # And the runtime refuses the same manifest without a token, before any fetch.
    request = rt.parse_argv(model.derived_argv(OP, 7)[1:])
    with pytest.raises(rt.Failure) as err:
        rt.load_manifest(request)
    assert err.value.code == "manifest"


def _selection(work):
    from test_salad_http import selection

    value = selection().model_dump(mode="json")
    value["salad"]["execution"] = execution(work)
    return value


def test_runtime_and_contract_share_the_redirect_rules():
    assert rt.MAX_REDIRECT_HOSTS == swc.MAX_REDIRECT_HOSTS
    assert rt.redirect_hosts_digest(HOSTS) == redirect_hosts_digest(HOSTS)
    hosts = [CDN, "a.b", "x-1.example.com", "*.a.com", "a", "1.2.3.4", "A.com", "a.com:1",
             "0x7f.0x1", "0x7f.example.net", "a.0xff.com", "a.123.com", "0177.0.0.1",
             "x0.example.com", "a1.b2.com", "ox7f.example.net"]  # fmt: skip
    for host in hosts:
        assert rt._redirect_host_ok(host) == (swc.redirect_host_refusal(host) is None), host
    assert not rt._redirect_host_ok("0x7f.0x1") and not rt._redirect_host_ok("a.123.com")
    assert rt._redirect_host_ok("x0.example.com") and rt._redirect_host_ok("ox7f.example.net")


def test_manifest_helper_writes_redirect_hosts_and_prints_the_token(tmp_path, capsys):
    value, raw = build()
    # The helper reads a contract before its argv is final, so drop the runtime layout.
    draft = dict(value, runtime=None, argv_template=["placeholder"])
    path = tmp_path / "c.json"
    path.write_text(json.dumps(draft))
    argv = [str(path), "--item", "file:weights.bin", "--item", "entry:entry.py", "--redirect-ref"]
    assert mf.main(argv) == 0
    lines = capsys.readouterr().out.split()
    assert lines == [sha(raw), token()]
    no_hosts = dict(draft, egress=dict(kind="provider_broad", fetches=FETCHES))
    path.write_text(json.dumps(no_hosts))
    with pytest.raises(SystemExit):
        mf.main(argv)


# ------------------------------------------------------------------- runtime


class Hops:
    """Single-hop fake transport: url -> (status, Location or body)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        status, value = self.routes[url]
        if status == 200:
            return Resp(value)
        resp = Resp(b"", status=status)
        resp.headers = {} if value is None else {"Location": value}
        return resp


ITEM = dict(url=ORIGIN, sha256=sha(DATA), max_bytes=100, kind="file", dest="weights.bin")
SIGNED = f"https://{CDN}/bridge/abc?X-Signature=s1&Expires=9"


def fetch(tmp_path, routes, hosts=frozenset(HOSTS), item=ITEM):
    hops = Hops(routes)
    try:
        return rt.fetch_one(item, str(tmp_path / "files" / item["dest"]), deadline(), hops,
                            hosts), hops  # fmt: skip
    except rt.Failure as error:
        assert not list(tmp_path.rglob("*.part"))
        error.hops = hops
        raise


def refused(tmp_path, routes, hosts=frozenset(HOSTS), code="redirect"):
    with pytest.raises(rt.Failure) as err:
        fetch(tmp_path, routes, hosts)
    assert err.value.code == code
    assert not (tmp_path / "files" / "weights.bin").exists()
    return err.value.hops


@pytest.mark.parametrize("status", rt.REDIRECT_STATUSES)
def test_redirect_to_a_reviewed_host_is_followed_and_verified(tmp_path, status):
    record, hops = fetch(tmp_path, {ORIGIN: (status, SIGNED), SIGNED: (200, DATA)})
    assert [u for u, _ in hops.calls] == [ORIGIN, SIGNED]
    assert (tmp_path / "files" / "weights.bin").read_bytes() == DATA
    # The record names the pinned URL, never the expiring signed one.
    assert record == {"url": ORIGIN, "sha256": sha(DATA), "bytes": len(DATA)}


def test_three_redirects_are_followed_and_a_fourth_is_refused(tmp_path):
    a, b, c, d = (f"https://{CDN}/{n}" for n in "abcd")
    record, hops = fetch(tmp_path, {ORIGIN: (302, a), a: (307, b), b: (301, c), c: (200, DATA)})
    assert len(hops.calls) == 4 and record["bytes"] == len(DATA)
    hops = refused(tmp_path / "again", {ORIGIN: (302, a), a: (302, b), b: (302, c), c: (302, d)})
    assert len(hops.calls) == 4  # never requests d


def test_relative_redirect_on_a_reviewed_host_is_resolved_against_it(tmp_path):
    first = f"https://{CDN}/a/b"
    target = f"https://{CDN}/a/c?sig=1"
    _, hops = fetch(tmp_path, {ORIGIN: (302, first), first: (302, "c?sig=1"),
                               target: (200, DATA)})  # fmt: skip
    assert hops.calls[-1][0] == target


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.net/x",  # undeclared host
        "http://cdn.example.net/x",  # downgrade to http
        "https://cdn.example.net:8443/x",  # different port
        "https://user@cdn.example.net/x",  # credentials
        "https://CDN.example.net/x",  # not exactly the declared name
        "https://sub.cdn.example.net/x",  # no implied subdomains
        "https://cdn.example.net./x",
        "//evil.example.net/x",  # scheme-relative to another host
        "/other/path",  # relative: resolves to the origin, which is not a redirect host
        "ftp://cdn.example.net/x",
        "https://cdn.example.net/x y",  # whitespace
        "https://cdn.example.net/é",  # non-ASCII
        "",
        None,  # missing Location
    ],
)
def test_redirect_refusals(tmp_path, location):
    hops = refused(tmp_path, {ORIGIN: (302, location)})
    assert len(hops.calls) == 1


@pytest.mark.parametrize("values", [[SIGNED, SIGNED], [SIGNED, "https://mirror.example.org/x"]])
def test_duplicate_or_multiple_location_headers_are_refused(tmp_path, values):
    from email.message import Message

    headers = Message()
    for value in values:
        headers["Location"] = value
    resp = Resp(b"", status=302)
    resp.headers = headers
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return resp if url == ORIGIN else Resp(DATA)

    assert rt._location(resp) is None
    with pytest.raises(rt.Failure) as err:
        rt.fetch_one(ITEM, str(tmp_path / "w"), deadline(), opener, frozenset(HOSTS))
    assert err.value.code == "redirect" and calls == [ORIGIN]


def test_relative_redirect_from_a_cdn_to_another_host_is_refused(tmp_path):
    first = f"https://{CDN}/a"
    hops = refused(tmp_path, {ORIGIN: (302, first), first: (302, "//evil.example.net/b")})
    assert [u for u, _ in hops.calls] == [ORIGIN, first]


def test_no_reviewed_hosts_means_no_redirect_is_followed(tmp_path):
    hops = refused(tmp_path, {ORIGIN: (302, SIGNED), SIGNED: (200, DATA)}, hosts=frozenset())
    assert len(hops.calls) == 1


def test_sha_and_size_still_bind_the_bytes_after_a_redirect(tmp_path):
    refused(tmp_path, {ORIGIN: (302, SIGNED), SIGNED: (200, b"tampered")}, code="sha256")
    refused(tmp_path, {ORIGIN: (302, SIGNED), SIGNED: (200, b"x" * 101)}, code="size")


def test_a_slow_hop_that_returns_past_the_deadline_is_refused(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rt.time, "monotonic", lambda: now[0])
    calls, closed = [], []

    def slow(url, timeout):
        calls.append((url, timeout))
        now[0] += 41  # the hop returns after the 40 s budget has run out
        resp = Resp(b"", status=302)
        resp.headers = {"Location": SIGNED}
        resp.close = lambda: closed.append(url)
        return resp

    with pytest.raises(rt.Failure) as err:
        rt.fetch_one(ITEM, str(tmp_path / "w"), 1040.0, slow, frozenset(HOSTS))
    assert err.value.code == "deadline"
    # One hop only, its socket timeout a quarter of the budget (4 hops may follow).
    assert calls == [(ORIGIN, 10.0)] and closed == [ORIGIN]
    assert not list(tmp_path.rglob("*.part"))


def test_hop_timeouts_split_the_remaining_budget_by_hops_left(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rt.time, "monotonic", lambda: now[0])
    a, b, c = (f"https://{CDN}/{n}" for n in "abc")
    hops = Hops({ORIGIN: (302, a), a: (302, b), b: (302, c), c: (200, DATA)})
    rt.open_following(ORIGIN, 80.0, hops, frozenset(HOSTS))
    assert [t for _, t in hops.calls] == [20.0, 80.0 / 3, 30, 30]
    # Without reviewed redirect hosts a single hop keeps the whole (capped) budget.
    plain = Hops({ORIGIN: (200, DATA)})
    rt.open_following(ORIGIN, 20.0, plain, frozenset())
    assert plain.calls == [(ORIGIN, 20.0)]


@pytest.mark.parametrize("stall_on", ["data", "eof"])
def test_the_streamed_body_after_a_redirect_is_bounded_by_the_run_deadline(
    tmp_path, monkeypatch, stall_on
):
    now = [1000.0]
    monkeypatch.setattr(rt.time, "monotonic", lambda: now[0])

    class Slow(Resp):
        def read(self, n=-1):
            chunk = super().read(n)
            if bool(chunk) == (stall_on == "data"):
                now[0] += 100  # this read returns after the run deadline
            return chunk

    def opener(url, timeout):
        if url == ORIGIN:
            resp = Resp(b"", status=302)
            resp.headers = {"Location": SIGNED}
            return resp
        return Slow(DATA)

    with pytest.raises(rt.Failure) as err:
        rt.fetch_one(ITEM, str(tmp_path / "w"), 1040.0, opener, frozenset(HOSTS))
    assert err.value.code == "deadline"
    assert not (tmp_path / "w").exists() and not list(tmp_path.rglob("*.part"))


def test_hops_share_the_wall_clock_budget(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rt.time, "monotonic", lambda: now[0])
    a = f"https://{CDN}/a"

    def slow(url, timeout):
        assert timeout <= 30
        now[0] += 25  # each hop eats most of the 40 s budget
        resp = Resp(b"", status=302)
        resp.headers = {"Location": a}
        return resp

    with pytest.raises(rt.Failure) as err:
        rt.fetch_one(ITEM, str(tmp_path / "w"), 1040.0, slow, frozenset(HOSTS))
    assert err.value.code == "deadline"


def test_real_open_url_returns_redirects_without_following_them(monkeypatch):
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, request.header_items()))
            raise urllib.error.HTTPError(request.full_url, 302, "Found",
                                         {"Location": SIGNED}, None)  # fmt: skip

    monkeypatch.setattr(rt.urllib.request, "build_opener", lambda *handlers: Opener())
    response = rt.open_url(ORIGIN, 5)
    assert response.status == 302 and rt._location(response) == SIGNED
    assert seen == [(ORIGIN, [])]  # a fresh request with no Authorization or other header

    class NotFound(Opener):
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "x", {}, None)

    monkeypatch.setattr(rt.urllib.request, "build_opener", lambda *handlers: NotFound())
    with pytest.raises(urllib.error.HTTPError):
        rt.open_url(ORIGIN, 5)


def _request(value):
    return rt.parse_argv(WorkloadContractV1.model_validate(value).derived_argv(OP, 7)[1:])


def test_execute_follows_reviewed_redirects_end_to_end(tmp_path):
    value, _ = build()
    log = []
    hops = Hops({ORIGIN: (302, SIGNED), SIGNED: (200, DATA),
                 FETCHES[1]["url"]: (200, ENTRY)})  # fmt: skip

    def entry_runner(entry, work, site, out_dir, dl):
        log.append("entry")
        (Path(out_dir) / "output.json").write_text("{}")

    result = rt.execute(
        _request(value), deadline(), str(tmp_path / "work"),
        custody_factory=lambda b, d: FakeCustody(log), opener=hops,
        installer=lambda *a: "site", entry_runner=entry_runner,
    )  # fmt: skip
    assert result[0]["name"] == "output.json" and "entry" in log
    assert [u for u, _ in hops.calls] == [ORIGIN, SIGNED, FETCHES[1]["url"]]


def test_runtime_refuses_a_manifest_whose_hosts_differ_from_the_token_before_the_claim(
    tmp_path,
):
    value, _ = build()
    other = mf.canonical(runtime_manifest(["other.example.net"]))
    request = _request(value)
    request.update(manifest_sha256=sha(other), manifest_ref=mf.refs(other))
    log = []
    with pytest.raises(rt.Failure) as err:
        rt.execute(request, deadline(), str(tmp_path / "work"),
                   custody_factory=lambda b, d: FakeCustody(log), opener=Hops({}),
                   installer=lambda *a: "site", entry_runner=lambda *a: None)  # fmt: skip
    assert err.value.code == "manifest_redirects" and log == []


@pytest.mark.parametrize(
    "hosts",
    [[], ["b.example.net", "a.example.net"], ["*.example.net"], ["1.2.3.4"], "cdn.example.net",
     [f"h{i:02d}.example.net" for i in range(17)], ["0x7f.0x1"], ["cdn.0x1f.example.net"],
     ["cdn.123.example.net"]],
)  # fmt: skip
def test_runtime_manifest_validation_of_redirect_hosts(hosts):
    manifest = runtime_manifest(None)
    manifest["redirect_hosts"] = hosts
    with pytest.raises(rt.Failure):
        rt.validate_manifest(manifest)


def test_argv_redirect_token_shape():
    value, _ = build()
    argv = WorkloadContractV1.model_validate(value).derived_argv(OP, 7)[1:]
    bad = copy.copy(argv)
    bad[9] = "redirect-hosts:" + "0" * 63
    with pytest.raises(rt.Failure):
        rt.parse_argv(bad)
    assert rt.parse_argv(argv)["redirect_hosts_sha256"] == redirect_hosts_digest(HOSTS)


# ------------------------------------------------- setup and admission (carried manifest)


def _setup_value(inputs, raw):  # noqa: F811
    directory = staged_dir(inputs["root"], raw)
    _, digest = staged_input_manifest(directory)
    value, _ = build(input_digest=digest, in_input=True,
                     custody_host="custody.example-tailnet.ts.net")  # fmt: skip
    return directory, value


def _run_setup(inputs, directory, value):  # noqa: F811
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    argv = inputs["argv"] + ["--workload-contract", str(path), "--input-dir", str(directory),
                             *PUBLIC_INPUT]  # fmt: skip
    return setup_script.main(argv)


def test_setup_declares_redirect_hosts_and_admission_binds_the_carried_manifest(
    inputs,  # noqa: F811
    capsys,
):
    raw = mf.canonical(runtime_manifest())
    directory, value = _setup_value(inputs, raw)
    assert _run_setup(inputs, directory, value) == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    config = json.loads(Path(summary["config_file"]).read_text())
    selection = config["policy"]["target"]["selection"]
    assert selection["salad"]["execution"]["workload"]["egress"]["redirect_hosts"] == HOSTS
    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        (project_id,) = db.execute("SELECT id FROM projects").fetchone()
        check_workload_admission(db, project_id, selection)
        # A selection whose (self-consistent) hosts differ from the staged manifest's.
        other = copy.deepcopy(selection)
        work = other["salad"]["execution"]["workload"]
        work["egress"]["redirect_hosts"] = ["other.example.net"]
        work["argv_template"][10] = token(["other.example.net"])
        WorkloadContractV1.model_validate(work)  # parses: the carried bytes are unseen
        with pytest.raises(ValueError, match="redirect_hosts"):
            check_workload_admission(db, project_id, other)


def test_setup_text_lists_redirect_targets_apart_from_fetch_sources(inputs, capsys):  # noqa: F811
    raw = mf.canonical(runtime_manifest())
    directory, value = _setup_value(inputs, raw)
    assert _run_setup(inputs, directory, value) == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    basis = next(
        r["basis"] for r in summary["review_required"] if r["field"] == "capability.declaration"
    )
    assert "redirect targets on port 443 (cdn.example.net, mirror.example.org;" in basis
    assert "not pinned fetch sources" in basis
    model = WorkloadContractV1.model_validate(value)
    passage = setup_script.workload_source_passage(model)
    fetches, _, redirects = passage.partition(" Reviewed redirect targets")
    assert "hub.example.com" in fetches and CDN not in fetches
    assert redirects.endswith(": cdn.example.net, mirror.example.org.")
    assert "signed URLs" in redirects and "not pinned fetch sources" in redirects
    # Contracts without redirect hosts keep their wording.
    plain = WorkloadContractV1.model_validate(contract())
    assert "redirect" not in setup_script.workload_source_passage(plain)
    assert "redirect" not in setup_script.capability_network_basis(plain)


def test_setup_refuses_a_carried_manifest_with_other_redirect_hosts(
    inputs,  # noqa: F811
    capsys,
):
    evil = mf.canonical(runtime_manifest(["evil.example.net"]))
    directory, value = _setup_value(inputs, evil)
    value["argv_template"][8] = sha(evil)
    model = WorkloadContractV1.model_validate(value)
    with pytest.raises(ValueError, match="redirect_hosts"):
        check_staged_input(model, directory)
    assert _run_setup(inputs, directory, value) == 1
    assert "redirect_hosts" in capsys.readouterr().err
    assert not inputs["output"].exists()
