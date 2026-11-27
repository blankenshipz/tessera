"""Salad admission bans declared workload fetch/redirect hosts for a stage with non-public
input of any kind, not only snapshots (ADR 0040 section 4); pinned public downloads by digest
stay allowed (ADR 0097). Fakes only."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)

from tessera_control import campaign_provider_salad as module
from tessera_control import campaign_providers as providers
from tessera_control.campaign_models import CampaignSpecV2, NormalInput, normalize
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.store import Conflict

URL = "https://models.example.com/w.bin"  # the fixture contract's one fetch row
SHA = "c" * 64
SIZE = 1000
REFUSAL = "input is not public"


@pytest.fixture
def world(tmp_path):
    built = SaladWorld(tmp_path)
    register_provider_instance(built.profiles)
    return built


@pytest.fixture
def provider(world):
    return providers.get_provider("salad")


def base(world):
    spec = CampaignSpecV2.model_validate(salad_spec_v2(world.env, [salad_stage_v2(world.env)]))
    return normalize(spec).stages[0]


def public_snapshot(world):
    return NormalInput(
        "snapshot",
        "data",
        snapshot_id=world.env.snapshot_id,
        digest=world.env.snapshot_digest,
        data_class="public",
    )


def public_pin():
    return NormalInput("public_artifact", "weights", public_files=(("w", URL, SHA, SIZE),))


def artifact():
    return NormalInput(
        "artifact", "prior", campaign_id="c1", stage="up", output="out", sha256="a" * 64
    )


def folder():
    return NormalInput(
        "artifact_folder",
        "prior_all",
        campaign_id="c1",
        stage="up",
        files=(("a", "a.json", "a" * 64), ("b", "b.json", "b" * 64)),
    )


def stage_output():
    return NormalInput("stage", "chained", stage="up", paths=("out.json",))


def unknown_kind():
    return SimpleNamespace(kind="receipt_output", name="r", stage="up", output="out")


def unrecorded_snapshot():
    return NormalInput("snapshot", "ghost", snapshot_id="no-such-snapshot", data_class="public")


def connection(world):
    db = sqlite3.connect(world.env.state / "tessera.sqlite3")
    db.row_factory = sqlite3.Row
    return db


def check(world, provider, stage):
    db = connection(world)
    try:
        project = db.execute("SELECT id FROM projects").fetchone()["id"]
        provider.check_stage(db, project, stage)
    finally:
        db.close()


def test_a_public_snapshot_stage_may_declare_the_contract_hosts(world, provider):
    stage = replace(base(world), inputs=(public_snapshot(world),))
    assert module._hosts_declared(module._salad_config(stage))
    check(world, provider, stage)


@pytest.mark.parametrize(
    "extra",
    [artifact, folder, stage_output, unknown_kind, unrecorded_snapshot],
    ids=["artifact", "artifact_folder", "stage", "unknown_kind", "unrecorded_snapshot"],
)
def test_any_non_public_input_kind_refuses_declared_hosts_at_admission(world, provider, extra):
    stage = replace(base(world), inputs=(public_snapshot(world), extra()))
    with connection(world) as db:
        assert module.admitted_nonpublic_input(db, stage)
    with pytest.raises(Conflict, match=REFUSAL):
        check(world, provider, stage)


def test_a_missing_or_absent_snapshot_id_fails_closed(world):
    with connection(world) as db:
        for snapshot_id in (None, ""):
            item = SimpleNamespace(kind="snapshot", snapshot_id=snapshot_id)
            assert module.admitted_nonpublic_input(db, SimpleNamespace(inputs=[item]))
        assert module.admitted_nonpublic_input(
            db, SimpleNamespace(inputs=[SimpleNamespace(name="no-kind")])
        )


def test_send_classes_naming_a_non_public_class_is_non_public(world):
    with connection(world) as db:
        public = SimpleNamespace(kind="snapshot", snapshot_id=world.env.snapshot_id)
        assert not module.admitted_nonpublic_input(db, SimpleNamespace(inputs=[public]))
        assert module.admitted_nonpublic_input(
            db, SimpleNamespace(inputs=[public], send_classes=["public", "private_derived"])
        )


def test_pinned_public_downloads_are_never_non_public(world):
    with connection(world) as db:
        assert not module.admitted_nonpublic_input(db, SimpleNamespace(inputs=[public_pin()]))


def test_exact_public_pins_with_a_private_artifact_input_stay_allowed(world, provider):
    """The pinned rows are checked by digest against the contract instead (ADR 0097)."""
    stage = replace(base(world), inputs=(public_snapshot(world), artifact(), public_pin()))
    with connection(world) as db:
        assert module.admitted_nonpublic_input(db, stage)
    check(world, provider, stage)


def test_a_non_public_input_without_declared_hosts_is_not_refused_for_hosts(world, provider):
    stage = replace(base(world), inputs=(public_snapshot(world), artifact()))
    config = module._salad_config(stage)
    egress = config.workload.egress.model_copy(update={"fetches": [], "redirect_hosts": []})
    workload = config.workload.model_copy(update={"egress": egress})
    salad = config.model_copy(update={"workload": workload})
    stage = replace(stage, provider_config=salad.model_dump(mode="json", by_alias=True))
    assert not module._hosts_declared(module._salad_config(stage))
    check(world, provider, stage)
