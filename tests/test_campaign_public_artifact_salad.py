"""Salad binds a spec's ``public_artifact`` pins to the workload contract (ADR 0097). Fakes only."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    contract_data,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage,
    salad_stage_v2,
)

from tessera_control import campaign_provider_salad as module
from tessera_control import campaign_providers as providers
from tessera_control.campaign_models import CampaignSpecV2, NormalInput, normalize
from tessera_control.campaign_provider_salad import (
    PUBLIC_ARTIFACT_DELIVERY,
    PUBLIC_ARTIFACT_MISMATCH,
    register_provider_instance,
)
from tessera_control.campaign_receipts import public_fetch_entry
from tessera_control.store import Conflict

URL = "https://models.example.com/w.bin"  # the fixture contract's one fetch row
SHA = "c" * 64
SIZE = 1000
HOST = "models.example.com"
CDN = "cdn.example.net"


@pytest.fixture
def world(tmp_path):
    built = SaladWorld(tmp_path)
    register_provider_instance(built.profiles)
    return built


@pytest.fixture
def provider(world):
    return providers.get_provider("salad")


def pin(url=URL, sha=SHA, size=SIZE, name="w"):
    return (name, url, sha, size)


def public_input(*files, hosts=()):
    return NormalInput(
        "public_artifact",
        "weights",
        public_files=tuple(files or (pin(),)),
        redirect_hosts=tuple(hosts),
    )


def snapshot_input(world, data_class):
    return NormalInput(
        "snapshot",
        "code",
        snapshot_id=world.env.snapshot_id,
        digest=world.env.snapshot_digest,
        data_class=data_class,
    )


def base(world):
    """A real normalized Salad stage whose fixture contract fetches exactly ``pin()``."""
    spec = CampaignSpecV2.model_validate(salad_spec_v2(world.env, [salad_stage_v2(world.env)]))
    return normalize(spec).stages[0]


def stage(world, *inputs):
    return replace(base(world), inputs=tuple(inputs))


def stub_config(fetches=None, hosts=(), runtime="salad-workload-v1", input=None):
    fetches = (pin(),) if fetches is None else fetches
    rows = [SimpleNamespace(url=u, sha256=h, max_bytes=n) for _, u, h, n in fetches]
    egress = SimpleNamespace(fetches=rows, redirect_hosts=list(hosts))
    return SimpleNamespace(workload=SimpleNamespace(egress=egress, input=input, runtime=runtime))


def db(world):
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def project(connection):
    return connection.execute("SELECT id FROM projects").fetchone()["id"]


def test_matching_contract_is_accepted(world, provider):
    item = stage(world, public_input())
    provider.validate_stage(item)
    with db(world) as connection:
        provider.check_stage(connection, project(connection), item)


OTHER = "https://models.example.com/other.bin"
EXTRA = pin(OTHER, "e" * 64, 5, "z")


@pytest.mark.parametrize(
    "files, hosts",
    [
        ((pin(sha="d" * 64),), ()),  # changed sha
        ((pin(size=999),), ()),  # changed max_bytes
        ((pin(url=OTHER),), ()),  # changed url
        ((pin(), EXTRA), ()),  # the contract lacks a row the spec pins
        ((pin(),), (CDN,)),  # the spec names a redirect host the contract does not
    ],
)
def test_any_difference_is_refused_with_the_closed_reason(world, provider, files, hosts):
    item = stage(world, public_input(*files, hosts=hosts))
    with pytest.raises(ValueError, match=PUBLIC_ARTIFACT_MISMATCH):
        provider.validate_stage(item)
    with db(world) as connection, pytest.raises(Conflict, match=PUBLIC_ARTIFACT_MISMATCH):
        provider.check_stage(connection, project(connection), item)


def test_contract_side_differences_are_refused():
    item = SimpleNamespace(inputs=[public_input(hosts=[CDN])])
    refusal = module.public_artifact_refusal
    assert refusal(item, stub_config(hosts=[CDN])) is None
    assert refusal(item, stub_config(hosts=[])) == PUBLIC_ARTIFACT_MISMATCH  # missing host
    assert refusal(item, stub_config(hosts=["cdn2.example.net"])) == PUBLIC_ARTIFACT_MISMATCH
    assert refusal(item, stub_config(hosts=[CDN, "cdn2.example.net"])) == PUBLIC_ARTIFACT_MISMATCH
    plain = SimpleNamespace(inputs=[public_input()])
    assert refusal(plain, stub_config(hosts=[CDN])) == PUBLIC_ARTIFACT_MISMATCH  # extra host
    assert refusal(plain, stub_config(fetches=(pin(), EXTRA))) == PUBLIC_ARTIFACT_MISMATCH
    assert refusal(plain, stub_config(fetches=())) == PUBLIC_ARTIFACT_MISMATCH


def test_a_declared_snapshot_input_that_is_never_delivered_is_refused():
    item = SimpleNamespace(inputs=[public_input()])
    assert (
        module.public_artifact_refusal(item, stub_config(input=object()))
        == PUBLIC_ARTIFACT_DELIVERY
    )


def test_a_stage_without_public_inputs_is_unchanged(world, provider):
    item = stage(world, snapshot_input(world, "public"))
    assert module.public_pins(item) is None
    provider.validate_stage(item)  # the fixture contract's fetch is fine for a public stage
    private = stage(world, snapshot_input(world, "private_derived"))
    with pytest.raises(ValueError, match="non-public input cannot declare"):
        provider.validate_stage(private)


def test_private_stage_with_exact_pins_is_accepted(world, provider):
    item = stage(world, snapshot_input(world, "private_derived"), public_input())
    assert module.stage_has_nonpublic_input(item)
    provider.validate_stage(item)
    # The same rule on a private stage that names redirect hosts: exact match only.
    hosted = SimpleNamespace(
        inputs=[snapshot_input(world, "private_derived"), public_input(hosts=[CDN])]
    )
    assert module.public_artifact_refusal(hosted, stub_config(hosts=[CDN])) is None
    assert module.public_artifact_refusal(hosted, stub_config(hosts=[])) == PUBLIC_ARTIFACT_MISMATCH


def test_private_stage_with_one_extra_or_different_row_is_refused(world, provider):
    private = snapshot_input(world, "private_derived")
    for files in ((pin(), EXTRA), (pin(sha="d" * 64),)):
        with pytest.raises(ValueError, match=PUBLIC_ARTIFACT_MISMATCH):
            provider.validate_stage(stage(world, private, public_input(*files)))


def test_private_gpu_runtime_stage_needs_an_inline_manifest(monkeypatch):
    item = SimpleNamespace(inputs=[snapshot_input_stub(), public_input()])
    config = stub_config(runtime=module.GPU_RUNTIME_V1)
    monkeypatch.setattr(module, "gpu_runtime_inline_manifest", lambda _w: None)
    assert module.public_artifact_refusal(item, config) == PUBLIC_ARTIFACT_MISMATCH
    monkeypatch.setattr(module, "gpu_runtime_inline_manifest", lambda _w: {"fetches": []})
    assert module.public_artifact_refusal(item, config) is None


def snapshot_input_stub():
    return NormalInput("snapshot", "code", data_class="private_derived")


# -- receipt ---------------------------------------------------------------------------------


def receipt_stage(world):
    return stage(world, public_input(hosts=[CDN]))


def test_receipt_declared_only_without_retained_rows(world):
    entry = public_fetch_entry("salad", receipt_stage(world), None)
    assert entry == {
        "provider": "salad",
        "placement": "provider_side",
        "hosts": [CDN, HOST],
        "files": [
            {
                "name": "w",
                "source_host": HOST,
                "sha256": SHA,
                "bytes": None,
                "seconds": None,
                "evidence": "declared_only",
            }
        ],
        "via_control": False,
    }


def test_receipt_uses_provider_reported_rows_only_when_url_and_pin_match(world):
    item = receipt_stage(world)
    reported = [{"url": URL, "sha256": SHA, "bytes": 900, "seconds": 1.5}]
    (file,) = public_fetch_entry("salad", item, reported)["files"]
    assert (file["bytes"], file["seconds"], file["evidence"]) == (900, 1.5, "provider_reported")
    other = [{"url": URL, "sha256": "d" * 64, "bytes": 900, "seconds": 1.5}]
    (file,) = public_fetch_entry("salad", item, other)["files"]
    assert (file["bytes"], file["evidence"]) == (None, "declared_only")
    assert URL not in repr(public_fetch_entry("salad", item, reported))


def test_receipt_has_no_entry_without_public_inputs(world):
    assert public_fetch_entry("salad", stage(world, snapshot_input(world, "public"))) is None
