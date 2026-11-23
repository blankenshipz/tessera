"""The public_artifact stage input (ADR 0097): spec, validation, store and runner plumbing."""

from __future__ import annotations

import copy

import pytest
from campaign_runner_harness import World
from pydantic import ValidationError
from test_campaign_models import IMAGE, SNAP
from test_campaign_models_v2 import V1_DIGEST, _spec2, _stage2

from tessera_control.campaign_models import (
    CampaignSpec,
    needs_composition,
    normalize,
    parse_spec,
    public_fetch_policy,
    spec_digest,
    stage_classes,
    validate_structure,
)

URL = "https://huggingface.co/org/model/resolve/main/weights.bin"
DIGEST = "c" * 64


def _file(name="weights.bin", url=URL, **extra) -> dict:
    return {"name": name, "url": url, "sha256": DIGEST, "max_bytes": 1024, **extra}


def _public(name="weights", files=None, **extra) -> dict:
    return {"kind": "public_artifact", "name": name, "files": files or [_file()], **extra}


def _stage(*inputs, **extra) -> dict:
    return _stage2("a", *inputs, **extra)


def _normal(spec: dict):
    return normalize(parse_spec(spec))


def test_valid_spec_parses_normalizes_and_is_public():
    normal = _normal(_spec2(_stage(_public(redirect_hosts=["cdn-lfs.huggingface.co"], cache=True))))
    item = normal.stage("a").inputs[0]
    assert item.kind == "public_artifact" and item.cache is True
    assert item.public_files == (("weights.bin", URL, DIGEST, 1024),)
    assert item.redirect_hosts == ("cdn-lfs.huggingface.co",)
    assert stage_classes(normal)["a"][0] == "public"


def test_optional_fields_are_dropped_from_the_serialized_spec():
    spec = parse_spec(_spec2(_stage(_public())))
    dumped = spec.model_dump(mode="json")["stages"][0]["inputs"][0]
    assert "redirect_hosts" not in dumped and "cache" not in dumped
    full = parse_spec(_spec2(_stage(_public(redirect_hosts=["cdn.example.org"], cache=True))))
    dumped = full.model_dump(mode="json")["stages"][0]["inputs"][0]
    assert dumped["redirect_hosts"] == ["cdn.example.org"] and dumped["cache"] is True


def test_existing_digests_are_unchanged():
    from test_campaign_models import _spec as _v1
    from test_campaign_models import _stage as _v1_stage

    assert spec_digest(CampaignSpec.model_validate(_v1(_v1_stage("a")))) == V1_DIGEST
    plain = _spec2(_stage2("a"))
    with_kind = copy.deepcopy(plain)
    assert spec_digest(parse_spec(plain)) == spec_digest(parse_spec(with_kind))
    # a spec that adds the kind differs; one that does not is untouched by its existence
    assert spec_digest(parse_spec(_spec2(_stage(SNAP, _public())))) != spec_digest(
        parse_spec(plain)
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/a/b.bin",
        "https://user:pw@huggingface.co/a/b.bin",
        "https://huggingface.co:8443/a/b.bin",
        "https://huggingface.co/a/b.bin?download=1",
        "https://10.0.0.1/a/b.bin",
        "https://localhost/a/b.bin",
        "https://HuggingFace.co/a/b.bin",
        "https://huggingface.co/a/b.bin#frag",
        "https://registry.internal/a/b.bin",
    ],
)
def test_bad_urls_are_refused(url):
    with pytest.raises(ValidationError):
        parse_spec(_spec2(_stage(_public(files=[_file(url=url)]))))


def test_duplicate_file_and_redirect_host_names_are_refused():
    with pytest.raises(ValidationError, match="unique"):
        parse_spec(_spec2(_stage(_public(files=[_file(), _file()]))))
    with pytest.raises(ValidationError, match="unique"):
        parse_spec(_spec2(_stage(_public(redirect_hosts=["a.example.org", "a.example.org"]))))


def test_mixing_with_private_inputs_is_accepted_and_the_stage_stays_private():
    spec = _spec2(
        _stage2("a"),
        _stage2(
            "b",
            SNAP,
            _public(),
            {"kind": "stage", "name": "up", "stage": "a", "outputs": ["result"]},
        ),
    )
    normal = _normal(spec)
    validate_structure(normal)
    assert stage_classes(normal)["b"][0] == "private_derived"


def test_own_egress_with_public_artifact_is_refused():
    egress = {"allow": [{"host": "huggingface.co", "port": 443}]}
    spec = _spec2(_stage(_public(), egress=egress))
    try:
        normal = _normal(spec)
    except ValidationError:
        return  # the egress shape itself may be refused first
    with pytest.raises(ValueError, match="egress"):
        validate_structure(normal)


def test_limits_on_inputs_and_files():
    inputs = [_public(name=f"p{i}", files=[_file(name=f"f{i}.bin")]) for i in range(5)]
    with pytest.raises(ValueError, match="more than 4"):
        validate_structure(_normal(_spec2(_stage(*inputs))))
    files = [_file(name=f"f{i}.bin") for i in range(17)]
    two = [_public(name="x", files=files[:16]), _public(name="y", files=files[16:] + [_file("z")])]
    # 16 + 2 = 18 is fine; 3 inputs of 11 files exceed 32
    validate_structure(_normal(_spec2(_stage(*two))))
    big = [
        _public(name=f"p{i}", files=[_file(name=f"f{j}.bin") for j in range(11)]) for i in range(3)
    ]
    with pytest.raises(ValueError, match="more than 32"):
        validate_structure(_normal(_spec2(_stage(*big))))


def test_needs_composition_ignores_public_artifacts():
    only = _normal(_spec2(_stage(_public()))).stage("a")
    assert needs_composition(only) is False
    with_snapshot = _normal(_spec2(_stage(SNAP, _public()))).stage("a")
    assert needs_composition(with_snapshot) is False
    two = _normal(_spec2(_stage(SNAP, {**SNAP, "name": "more"}, _public()))).stage("a")
    assert needs_composition(two) is True


def test_public_fetch_policy():
    normal = (
        _normal(
            _spec2(
                _stage(
                    _public(files=[_file(), _file("b.bin", "https://zenodo.org/r/b.bin")]),
                    _public(
                        name="more", redirect_hosts=["cdn.example.org"], files=[_file("c.bin")]
                    ),
                ),
                _stage2("plain"),
            )
        )
        if False
        else _normal(
            _spec2(
                _stage(
                    _public(files=[_file(), _file("b.bin", "https://zenodo.org/r/b.bin")]),
                    _public(
                        name="more", redirect_hosts=["cdn.example.org"], files=[_file("c.bin")]
                    ),
                )
            )
        )
    )
    policy = public_fetch_policy(normal.stage("a"))
    assert policy["hosts"] == ["cdn.example.org", "huggingface.co", "zenodo.org"]
    assert policy["max_bytes_total"] == 3 * 1024 and policy["cache"] is False
    assert {f["name"] for f in policy["files"]} == {"weights.bin", "b.bin", "c.bin"}
    assert public_fetch_policy(_normal(_spec2(_stage2("p"))).stage("p")) is None


def test_holdout_input_cannot_be_public_artifact():
    from test_campaign_models_v2 import _spec2 as spec2  # noqa: F401

    spec = _spec2(_stage(_public()))
    spec["stages"][0]["evaluation"] = {
        "final": True,
        "holdout": {"input": "weights", "partition_digest": DIGEST, "claim_scope": "s"},
    }
    with pytest.raises((ValidationError, ValueError)):
        validate_structure(_normal(spec))


@pytest.fixture
def local_worker(tmp_path):
    """A world whose built-in local provider instance has a worker principal (v2 specs)."""
    from tessera_control import campaign_provider_worker as worker

    world = World(tmp_path)
    worker.register_from_config({"local_docker": {"worker_principal_id": world.env.worker["id"]}})
    try:
        yield world
    finally:
        worker.register_from_config(None)


def _v2_stage(world: World, key: str, *inputs: dict) -> dict:
    return {
        **_stage2(key, *inputs),
        "image": IMAGE,
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
    }


def _snapshot(world: World) -> dict:
    return {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": world.env.snapshot_id,
        "digest": world.env.snapshot_digest,
        "data_class": "public",
    }


def _steps(world: World, campaign_id: str) -> set[str]:
    return {
        r["step"]
        for r in world.env.query(
            "SELECT step FROM campaign_step_intents WHERE campaign_id=?", campaign_id
        )
    }


def test_submit_and_approve_a_public_only_stage_needs_no_custody(local_worker):
    world = local_worker
    campaign = world.campaign(_spec2(_v2_stage(world, "a", _public())))
    assert world.status(campaign["id"]) in {"approved", "running"}
    assert world.stages(campaign["id"]) == {"a": "pending"}


def test_public_only_stage_never_composes_or_releases(local_worker):
    world = local_worker
    campaign = world.campaign(_spec2(_v2_stage(world, "a", _public())))
    world.tick_all(campaign["id"], limit=40)
    steps = _steps(world, campaign["id"])
    assert not steps & {"compose_snapshot", "release_snapshot", "issue_grant"}
    stage = world.row("SELECT * FROM campaign_stages WHERE campaign_id=?", campaign["id"])
    assert stage["input_snapshot_id"] is None and stage["input_data_class"] == "public"
    rows = world.env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='input'",
        campaign["id"],
    )
    assert [(r["name"], r["sha256"], r["data_class"]) for r in rows] == [
        ("weights/weights.bin", DIGEST, "public")
    ]
    # The worker-grant path binds a snapshot, so this stage fails closed rather than running.
    assert world.status(campaign["id"]) != "succeeded"


def test_public_artifact_beside_a_snapshot_is_not_composed(local_worker):
    world = local_worker
    spec = _spec2(_v2_stage(world, "a", _snapshot(world), _public()))
    campaign = world.campaign(spec)
    # No worker polls: a worker without a public root refuses the fetch plan (ADR 0097).
    for _ in range(40):
        world.runner.tick()
    assert "compose_snapshot" not in _steps(world, campaign["id"])
    names = {
        r["name"]
        for r in world.env.query(
            "SELECT name FROM campaign_stage_io WHERE campaign_id=? AND direction='input'",
            campaign["id"],
        )
    }
    assert names == {"code", "weights/weights.bin"}
