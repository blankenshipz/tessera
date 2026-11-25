# ruff: noqa: F811
"""A stage whose only reach is pinned public downloads runs on Salad (ADR 0097, 0076, 0141).

The stage adapter carries the stage's ``public_artifact`` pins into the derived workload: the
contract's fetch rows and redirect hosts are exactly the pins, the inline manifest is the image
entry plus one ``file`` item per pin, and the runtime fetches and verifies them before the claim.
Free-form egress is still refused. Fakes only: no Salad, Docker or network call is made.

A digest mismatch at fetch fails closed in the runtime (``tests/test_gpu_runtime_v1.py::
test_sha_mismatch_refused_and_nothing_executed``) and on a local worker
(``tests/test_public_fetch.py::test_digest_mismatch_leaves_nothing``); not duplicated here.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_salad_stage_adapter import NEAR, cpu_world, expanded, near, spec_of  # noqa: F401

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_placement_preference as preference
from tessera_control import program_budget
from tessera_control.campaign_models import (
    HEAVY_REMOTE_ADDED,
    SaladStageSpec,
    normalize,
    parse_spec,
    stage_on_candidate,
)
from tessera_control.campaign_provider_salad import PUBLIC_ARTIFACT_CACHE, PUBLIC_ARTIFACT_MISMATCH
from tessera_control.campaign_providers import get_provider
from tessera_control.salad_workload_contract import (
    check_gpu_runtime_manifest_rules,
    redirect_hosts_digest,
)
from tessera_control.store import Conflict

GIB = 1024**3
HOST = "models.example.com"
CDN = "cdn.example.net"
URL = f"https://{HOST}/weights/w.bin"
SHA = "c" * 64
SIZE = 4096
ARGV = ["python", "/inputs/main.py", "--weights=/inputs-public/weights/w.bin", "/outputs"]
ROOT = Path(__file__).resolve().parent.parent / "workloads/gpu_runtime_v1"


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


def public(*, files=None, hosts=(), cache=False, name="weights"):
    value = {
        "kind": "public_artifact",
        "name": name,
        "files": files or [{"name": "w.bin", "url": URL, "sha256": SHA, "max_bytes": SIZE}],
    }
    if hosts:
        value["redirect_hosts"] = list(hosts)
    if cache:
        value["cache"] = True
    return value


def inputs(world, *extra):
    env = world.env
    snapshot = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    return [snapshot, *extra]


def _config(spec) -> SaladStageSpec:
    (item,) = spec.stages[0].alternate_providers
    return SaladStageSpec.model_validate(item.provider_config)


def _trust(world, near):
    """Both providers listed without a licence-label limit (ADR 0113): the stage's pinned public
    input carries ``unknown-lineage``, which an unlisted provider may never receive."""
    from provider_trust_fixture import install_trust

    install_trust(
        {
            NEAR: (["public"], [near.principal["id"]]),
            "salad": (["public"], [world.worker["id"]]),
        }
    )


def _manifest(config: SaladStageSpec) -> dict:
    refs = [t for t in config.workload.argv_template if t.startswith("b64:")]
    text = "".join(t[4:] for t in refs)
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the adapter carries the pins, and only the pins ----------------------------------------


def test_pinned_fetch_only_stage_becomes_a_salad_candidate_whose_hosts_are_the_pins(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = expanded(world, argv=ARGV, inputs=inputs(world, public(hosts=[CDN], cache=True)))
    config = _config(spec)
    work = config.workload
    assert [(f.url, f.sha256, f.max_bytes) for f in work.egress.fetches] == [(URL, SHA, SIZE)]
    assert work.egress.redirect_hosts == [CDN]
    # The only network reach the reviewed workload declares: the pinned host and its redirect.
    assert work.capability_network() == [{"host": CDN, "port": 443}, {"host": HOST, "port": 443}]
    manifest = _manifest(config)
    assert manifest["redirect_hosts"] == [CDN]
    assert manifest["fetches"] == [
        {"kind": "entry", "source": "image", "path": "stage_argv.py", "argv": ARGV},
        {"url": URL, "sha256": SHA, "max_bytes": SIZE, "kind": "file",
         "dest": "weights/w.bin", "cache": True},
    ]  # fmt: skip
    assert f"redirect-hosts:{redirect_hosts_digest([CDN])}" in work.argv_template
    # The submit-time checks of the Salad provider accept the derived candidate.
    provider = get_provider("salad")
    candidate = stage_on_candidate(normalize(spec).stages[0], "salad")
    provider.validate_stage(candidate)


def test_a_pinned_download_stage_with_a_salad_request_is_submitted(tmp_path, near):
    world = cpu_world(tmp_path, near)
    _trust(world, near)
    world.spec = spec_of(world, argv=ARGV, inputs=inputs(world, public()))
    submitted = world.env.submit(spec=world.spec)
    assert submitted["spec_digest"]
    stored = json.loads(world.env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])
    (item,) = stored["stages"][0]["alternate_providers"]
    assert item["provider_config"]["workload"]["egress"]["fetches"] == [
        {"url": URL, "sha256": SHA, "max_bytes": SIZE}
    ]


def test_a_public_only_stage_carries_no_input_declaration(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = expanded(world, argv=ARGV, inputs=[public()])
    work = _config(spec).workload
    assert work.input is None and [f.url for f in work.egress.fetches] == [URL]


def test_free_form_egress_is_still_refused_by_the_adapter(tmp_path, near):
    world = cpu_world(tmp_path, near)
    egress = {
        "profile": "allowlist-proxy-v1",
        "destinations": [{"host": HOST, "port": 443}],
        "max_egress_bytes": 1 << 20,
    }
    with pytest.raises(Conflict, match="egress cannot be carried to Salad"):
        expanded(world, egress=egress)


def test_pins_over_the_profile_disk_are_refused(tmp_path, near):
    world = cpu_world(tmp_path, near, disk_mb=1024)
    big = [{"name": "w.bin", "url": URL, "sha256": SHA, "max_bytes": 2 * GIB}]
    with pytest.raises(Conflict, match="disk_mb"):
        expanded(world, argv=ARGV, inputs=inputs(world, public(files=big)))


# -- the provider refuses anything but exactly the pins --------------------------------------


def _tampered(world, mutate):
    """The derived candidate with its contract and manifest changed consistently by ``mutate``."""
    spec = expanded(world, argv=ARGV, inputs=inputs(world, public()))
    stage = spec.stages[0]
    data = stage.alternate_providers[0].provider_config
    workload = data["workload"]
    manifest = _manifest(SaladStageSpec.model_validate(data))
    mutate(workload["egress"], manifest)
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    import hashlib

    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    template = [t for t in workload["argv_template"] if not t.startswith("b64:")]
    template[8] = hashlib.sha256(raw).hexdigest()
    template += [f"b64:{encoded[i : i + 250]}" for i in range(0, len(encoded), 250)]
    workload["argv_template"] = template
    candidate = stage_on_candidate(normalize(spec).stages[0], "salad")
    return candidate.__class__(**{**candidate.__dict__, "provider_config": data})


def _unpinned(egress, manifest):
    row = {"url": f"https://{HOST}/other.bin", "sha256": "d" * 64, "max_bytes": 10}
    egress["fetches"].append(row)
    manifest["fetches"].append({**row, "kind": "file", "dest": "weights/other.bin"})


def _other_host(egress, manifest):
    url = "https://attacker.example.org/weights/w.bin"
    egress["fetches"][0]["url"] = url
    manifest["fetches"][1]["url"] = url


@pytest.mark.parametrize("mutate", [_unpinned, _other_host], ids=["unpinned_url", "host"])
def test_a_fetch_row_that_is_not_a_pin_is_refused_with_the_closed_reason(tmp_path, near, mutate):
    world = cpu_world(tmp_path, near)
    candidate = _tampered(world, mutate)
    with pytest.raises(ValueError, match=PUBLIC_ARTIFACT_MISMATCH):
        get_provider("salad").validate_stage(candidate)


def test_a_manifest_item_that_differs_from_the_pins_is_refused(tmp_path, near):
    world = cpu_world(tmp_path, near)

    def moved(egress, manifest):
        manifest["fetches"][1]["dest"] = "elsewhere/w.bin"

    with pytest.raises(ValueError, match="adapted stage"):
        get_provider("salad").validate_stage(_tampered(world, moved))


def test_an_adapted_workload_without_pins_still_declares_no_fetch(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = expanded(world)  # no public_artifact input
    data = spec.stages[0].alternate_providers[0].provider_config
    data["workload"]["egress"]["fetches"] = [{"url": URL, "sha256": SHA, "max_bytes": SIZE}]
    candidate = stage_on_candidate(normalize(spec).stages[0], "salad")
    candidate = candidate.__class__(**{**candidate.__dict__, "provider_config": data})
    with pytest.raises(ValueError, match="manifest fetches must equal egress.fetches"):
        get_provider("salad").validate_stage(candidate)


def test_an_extra_redirect_host_on_a_pinned_stage_is_refused(tmp_path, near):
    world = cpu_world(tmp_path, near)

    def extra(egress, manifest):
        egress["redirect_hosts"] = ["extra.example.org"]
        manifest["redirect_hosts"] = ["extra.example.org"]

    # The contract model refuses first: a redirect host needs its digest token, and a token the
    # adapter did not derive from the pins is not carried by the tampered argv.
    with pytest.raises(ValueError, match="redirect-hosts token must be present exactly"):
        get_provider("salad").validate_stage(_tampered(world, extra))


def test_a_redirect_host_declared_on_an_unpinned_stage_is_refused(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = expanded(world)  # no public_artifact input
    data = spec.stages[0].alternate_providers[0].provider_config
    data["workload"]["egress"]["redirect_hosts"] = [CDN]
    candidate = stage_on_candidate(normalize(spec).stages[0], "salad")
    candidate = candidate.__class__(**{**candidate.__dict__, "provider_config": data})
    with pytest.raises(ValueError, match="redirect-hosts token must be present exactly"):
        get_provider("salad").validate_stage(candidate)


@pytest.mark.parametrize("flag", [True, False], ids=["manifest_adds", "manifest_drops"])
def test_a_cache_flag_that_differs_from_the_spec_pin_is_refused(tmp_path, near, flag):
    world = cpu_world(tmp_path, near)

    def flipped(egress, manifest):
        if flag:
            manifest["fetches"][1]["cache"] = True
        else:
            manifest["fetches"][1].pop("cache", None)

    extra = public(cache=not flag)
    spec = expanded(world, argv=ARGV, inputs=inputs(world, extra))
    data = spec.stages[0].alternate_providers[0].provider_config
    manifest = _manifest(SaladStageSpec.model_validate(data))
    flipped(data["workload"]["egress"], manifest)
    import hashlib

    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    template = [t for t in data["workload"]["argv_template"] if not t.startswith("b64:")]
    template[8] = hashlib.sha256(raw).hexdigest()
    template += [f"b64:{encoded[i : i + 250]}" for i in range(0, len(encoded), 250)]
    data["workload"]["argv_template"] = template
    candidate = stage_on_candidate(normalize(spec).stages[0], "salad")
    candidate = candidate.__class__(**{**candidate.__dict__, "provider_config": data})
    with pytest.raises(ValueError, match=PUBLIC_ARTIFACT_CACHE):
        get_provider("salad").validate_stage(candidate)


def test_pins_beside_a_private_snapshot_input_are_adapted_and_accepted(tmp_path, near):
    from campaign_fixture import record_snapshot_private

    world = cpu_world(tmp_path, near)
    record_snapshot_private(world.env.state, world.env.snapshot_id, "derived")
    spec = expanded(world, argv=ARGV, inputs=inputs(world, public()))
    config = _config(spec)
    assert config.workload.input is not None
    assert [f.url for f in config.workload.egress.fetches] == [URL]
    candidate = stage_on_candidate(normalize(spec).stages[0], "salad")
    get_provider("salad").validate_stage(candidate)


# -- ADR 0141: a heavy pinned-download stage gets the remote candidate -----------------------


def _configure():
    preference.configure(
        {"heavy_remote_candidate": {"provider": "salad", "max_exposure_cents": 10}},
        [NEAR, "salad"],
    )


def test_submit_adds_the_remote_candidate_to_a_heavy_pinned_download_stage(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    _trust(world, near)
    world.spec = spec_of(
        world,
        argv=ARGV,
        alternate_providers=None,
        memory_bytes=2 * GIB,
        inputs=inputs(world, public()),
    )
    world.env.submit(spec=world.spec)
    stage = json.loads(world.env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])[
        "stages"
    ][0]
    record = stage["heavy_remote_candidate"]
    assert record["added"] is True and record["reason"] == HEAVY_REMOTE_ADDED
    (first,) = stage["alternate_providers"]
    egress = first["provider_config"]["workload"]["egress"]
    assert egress["fetches"] == [{"url": URL, "sha256": SHA, "max_bytes": SIZE}]


def test_large_pinned_downloads_make_a_stage_heavy_and_not_egress_bound(tmp_path, near):
    world = cpu_world(tmp_path, near)
    big = [{"name": "w.bin", "url": URL, "sha256": SHA, "max_bytes": 2 * GIB}]
    spec = parse_spec(
        spec_of(world, alternate_providers=None, inputs=inputs(world, public(files=big)))
    )
    stage = normalize(spec).stages[0]
    assert preference.stage_class(stage)[0] != "egress_bound"
    heavy, signals = guard.heavy(stage)
    assert heavy and "large_public_inputs" in signals


def test_a_heavy_stage_with_free_form_egress_is_still_egress_bound(tmp_path, near):
    world = cpu_world(tmp_path, near)
    spec = parse_spec(spec_of(world, alternate_providers=None, memory_bytes=2 * GIB))
    stage = normalize(spec).stages[0]
    from dataclasses import replace

    from tessera_control.egress_models import EgressPolicy

    egress = EgressPolicy.model_validate(
        {
            "profile": "allowlist-proxy-v1",
            "destinations": [{"host": HOST, "port": 443}],
            "max_egress_bytes": 1 << 20,
        }
    )
    assert preference.stage_class(replace(stage, egress=egress))[0] == "egress_bound"


# -- the runtime and launcher ----------------------------------------------------------------

ITEM = {"kind": "entry", "source": "image", "path": "stage_argv.py", "argv": ["python", "x.py"]}
FILE = {"url": URL, "sha256": SHA, "max_bytes": SIZE, "kind": "file", "dest": "weights/w.bin"}
SCHEMA = "tessera.gpu-runtime.manifest.v1"


def test_the_contract_and_runtime_accept_an_image_entry_with_pinned_files_only():
    rt = _load("gpu_runtime_v1_heavy_download", ROOT / "runtime.py")
    manifest = {"schema": SCHEMA, "fetches": [ITEM, FILE]}
    assert check_gpu_runtime_manifest_rules(manifest, input_declared=False) == []
    assert rt.validate_manifest(manifest) == manifest
    assert rt.url_items(manifest) == [FILE]
    for extra in (
        {**FILE, "kind": "wheel", "dest": "w.whl"},
        {**FILE, "kind": "entry", "dest": "e.py"},
        {"kind": "file", "source": "input", "path": "data.bin"},
    ):
        bad = {"schema": SCHEMA, "fetches": [ITEM, extra]}
        with pytest.raises(ValueError):
            check_gpu_runtime_manifest_rules(bad, input_declared=True)
        with pytest.raises(rt.Failure):
            rt.validate_manifest(bad)


def test_the_launcher_maps_the_public_mount_to_the_fetched_files(tmp_path):
    launcher = _load("gpu_runtime_v1_stage_argv_public", ROOT / "stage_argv.py")
    out = launcher.rewrite(
        ["py", "/inputs-public/weights/w.bin", "--w=/inputs-public/weights", "/inputs/a"],
        "/w",
        "/o",
    )
    assert out == ["py", "/w/files/weights/w.bin", "--w=/w/files/weights", "/w/input/a"]
    work, out_dir = tmp_path / "work", tmp_path / "out"
    (work / "files" / "weights").mkdir(parents=True)
    out_dir.mkdir()
    (work / "files" / "weights" / "w.bin").write_text("weights")
    script = "import sys; open(sys.argv[2],'w').write(open(sys.argv[1]).read())"
    argv = [sys.executable, "-c", script, "/inputs-public/weights/w.bin", "/outputs/r.txt"]
    assert launcher.main([str(work), str(out_dir), "--", *argv]) == 0
    assert (out_dir / "r.txt").read_text() == "weights"
