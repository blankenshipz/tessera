"""Runtime image provenance on the attempt receipt: digest, base images, compressed size.

Fakes only: the control app runs in-process; the manifest read is a fake provider hook.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from campaign_runner_harness import IMAGE, World, dump
from pydantic import ValidationError

from tessera_control import image_provenance
from tessera_control.campaign_models import stage_provider
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.campaign_provider_worker import register_from_config
from tessera_control.execution_models import IssueExecutionGrant
from tessera_control.image_provenance import (
    ADAPTER_CANNOT_READ_MANIFEST,
    FIELDS,
    IMAGE_PROVENANCE_EVENT,
    SIZE_REASONS,
    SIZE_UNAVAILABLE,
)

BASE = "registry.example/base/cuda@sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _restore_local_docker():
    yield
    register_from_config(None)


def _run(tmp_path, reader=None):
    world = World(tmp_path)
    register_from_config({"local_docker": {"worker_principal_id": world.env.worker["id"]}})
    if reader is not None:
        stage_provider("local-docker").image_pull_size_bytes = reader
    env = world.env
    stage = {
        "key": "s1",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
    }
    spec = {
        "version": 2,
        "name": "provenance",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [stage],
    }
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    row = world.row(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"]
    )
    receipt = json.loads(row["receipt_json"])
    return world, receipt["stages"][0]["attempts"][0]


def test_digest_and_size_are_recorded_when_the_adapter_reports_them(tmp_path):
    world, attempt = _run(tmp_path, lambda stage: 9_500_000_000)
    name, digest = IMAGE.split("@")
    assert attempt["image_provenance"] == {
        "image_ref": name,
        "image_digest": digest,
        "base_images": [],
        "compressed_size_bytes": 9_500_000_000,
        "size_reason": None,
    }
    (event,) = world.events_of(IMAGE_PROVENANCE_EVENT)
    assert event["payload"]["attempt"] == 1


def test_a_provider_without_a_manifest_reader_records_a_closed_reason(tmp_path):
    _, attempt = _run(tmp_path)
    block = attempt["image_provenance"]
    assert set(block) == set(FIELDS)
    assert block["compressed_size_bytes"] is None
    assert block["size_reason"] == ADAPTER_CANNOT_READ_MANIFEST
    assert block["image_digest"] == IMAGE.split("@")[1]


def test_a_failing_or_nonsense_read_is_size_unavailable_and_leaks_nothing(tmp_path):
    def boom(stage):
        raise RuntimeError("https://user:secret@registry.example denied")

    world, attempt = _run(tmp_path, boom)
    assert attempt["image_provenance"]["size_reason"] == SIZE_UNAVAILABLE
    assert "secret" not in json.dumps(attempt) and "secret" not in json.dumps(
        world.events_of(IMAGE_PROVENANCE_EVENT)
    )


@pytest.mark.parametrize("value", [None, 0, -5, True, "12", 1.5, 1 << 50])
def test_unreadable_size_values_are_null_with_the_closed_reason(value):
    provider = SimpleNamespace(image_pull_size_bytes=lambda stage: value)
    stage = SimpleNamespace(image=IMAGE)
    block = image_provenance.image_provenance(stage, provider)
    assert block["compressed_size_bytes"] is None and block["size_reason"] == SIZE_UNAVAILABLE
    assert block["size_reason"] in SIZE_REASONS


def test_base_images_come_from_the_build_plan_only():
    stage = SimpleNamespace(image=IMAGE, build={"base_images": [BASE, "not a ref"]})
    block = image_provenance.image_provenance(stage, SimpleNamespace())
    assert block["base_images"] == [{"ref": BASE.split("@")[0], "digest": BASE.split("@")[1]}]
    assert block["size_reason"] == ADAPTER_CANNOT_READ_MANIFEST


def test_the_closed_view_drops_unknown_fields_and_credentialed_refs():
    block = image_provenance.closed(
        {
            "image_ref": "https://u:p@host/x",
            "image_digest": "sha256:" + "a" * 64,
            "token": "t",
            "compressed_size_bytes": 7,
            "size_reason": "free text",
            "base_images": [{"ref": "r/x", "digest": "sha256:" + "c" * 64, "extra": 1}],
        }
    )
    assert tuple(block) == FIELDS
    assert block["image_ref"] is None and block["size_reason"] is None
    assert block["base_images"] == [{"ref": "r/x", "digest": "sha256:" + "c" * 64}]


def test_the_salad_provider_reads_size_only_through_its_configured_reader():
    stage = SimpleNamespace(image=IMAGE)
    assert SaladRouteProvider().image_pull_size_bytes(stage) is None
    seen = []
    provider = SaladRouteProvider(manifest_size_reader=lambda ref: seen.append(ref) or 42)
    assert provider.image_pull_size_bytes(stage) == 42 and seen == [IMAGE]


def test_a_tag_only_image_is_still_refused_where_pinning_is_required():
    with pytest.raises(ValidationError):
        IssueExecutionGrant.model_validate(
            {
                "task_id": "t",
                "expected_task_revision": 1,
                "expected_plan_digest": "d",
                "worker_principal_id": "w",
                "snapshot_id": "s",
                "pinned_image": "registry.example/app:latest",
                "expires_at": "2030-01-01T00:00:00Z",
                "max_exposure_cents": 1,
            }
        )


GOOD_DIGEST = "sha256:" + "d" * 64


@pytest.mark.parametrize(
    "ref",
    [
        "user:pw@registry.example.invalid/img",
        "user@registry.example.invalid/img",
        "user:pw@registry.example.invalid/img@" + GOOD_DIGEST,
        "registry.example.invalid/img@sha256:" + "d" * 63,
        "registry.example.invalid/img@sha256:" + "D" * 64,
        "registry.example.invalid/img@",
        "@" + GOOD_DIGEST + "x",
    ],
)
def test_a_reference_with_at_but_no_valid_digest_records_nothing(ref):
    assert image_provenance.split_ref(ref) == (None, None)
    block = image_provenance.image_provenance(SimpleNamespace(image=ref), SimpleNamespace())
    assert block["image_ref"] is None and block["image_digest"] is None
    assert "pw" not in json.dumps(block) and "user" not in json.dumps(block)


@pytest.mark.parametrize("ref", ["user:pw/img", "user:pw/img:tag", "host/ns:x/img"])
def test_userinfo_like_names_without_at_are_refused(ref):
    assert image_provenance.split_ref(ref) == (None, None)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (
            "registry.example.invalid:5000/img@" + GOOD_DIGEST,
            ("registry.example.invalid:5000/img", GOOD_DIGEST),
        ),
        ("registry.example.invalid:5000/img:tag", ("registry.example.invalid:5000/img:tag", None)),
        ("registry.example.invalid/ns/img:1.2", ("registry.example.invalid/ns/img:1.2", None)),
        ("img:tag@" + GOOD_DIGEST, ("img:tag", GOOD_DIGEST)),
    ],
)
def test_well_formed_references_are_still_accepted(ref, expected):
    assert image_provenance.split_ref(ref) == expected


def test_the_closed_view_never_records_the_string_none():
    block = image_provenance.closed({"image_ref": None, "image_digest": GOOD_DIGEST})
    assert block["image_ref"] is None and block["image_digest"] == GOOD_DIGEST
    block = image_provenance.closed({"image_digest": GOOD_DIGEST})
    assert block["image_ref"] is None and block["image_digest"] == GOOD_DIGEST
    bases = image_provenance.closed({"base_images": [{"ref": None, "digest": GOOD_DIGEST}]})
    assert bases["base_images"] == []


def test_the_closed_view_refuses_credentialed_names_and_bad_digests_separately():
    block = image_provenance.closed(
        {"image_ref": "user:pw@registry.example.invalid/img", "image_digest": "sha256:bad"}
    )
    assert block["image_ref"] is None and block["image_digest"] is None
    block = image_provenance.closed(
        {"image_ref": "registry.example.invalid/img", "image_digest": "sha256:bad"}
    )
    assert block["image_ref"] == "registry.example.invalid/img" and block["image_digest"] is None


def test_a_size_read_that_hangs_is_abandoned_at_the_time_bound(monkeypatch):
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(image_provenance, "SIZE_READ_TIMEOUT_SECONDS", 0.05)
    provider = SimpleNamespace(image_pull_size_bytes=lambda stage: release.wait(30) and 42)
    started = time.monotonic()
    try:
        block = image_provenance.image_provenance(SimpleNamespace(image=IMAGE), provider)
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert block["compressed_size_bytes"] is None and block["size_reason"] == SIZE_UNAVAILABLE


def test_a_size_read_within_the_bound_is_recorded(monkeypatch):
    monkeypatch.setattr(image_provenance, "SIZE_READ_TIMEOUT_SECONDS", 5.0)
    provider = SimpleNamespace(image_pull_size_bytes=lambda stage: 1234)
    block = image_provenance.image_provenance(SimpleNamespace(image=IMAGE), provider)
    assert block["compressed_size_bytes"] == 1234 and block["size_reason"] is None
