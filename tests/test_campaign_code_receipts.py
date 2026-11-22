"""A stage's declared code input on the receipt: code digest, image digest, transfer (ADR 0095).

Fakes only: the control app runs in-process and a fake worker adapter stands in for Docker.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from campaign_runner_harness import IMAGE, World, dump

from tessera_control import campaign_receipts, campaign_runner
from tessera_control.campaign_provider_worker import register_from_config
from tessera_control.campaign_receipts import CODE_GRANTED_EVENT, build_receipt, code_summary

CODE_EVENT = "campaign.code_granted"


@pytest.fixture(autouse=True)
def _restore_local_docker():
    yield
    register_from_config(None)  # the builtin local-docker without a worker, as other tests expect


def _grants_to(world):
    """v2 specs name providers, not a spec-level worker: local-docker grants to the test worker."""
    register_from_config({"local_docker": {"worker_principal_id": world.env.worker["id"]}})


def _stage(env, key="s1", **extra) -> dict:
    return {
        "key": key,
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
        **extra,
    }


def _spec(env, *stages) -> dict:
    return {
        "version": 2,
        "name": "code input",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": list(stages),
    }


def _receipt(world, campaign_id) -> dict:
    row = world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    return json.loads(row["receipt_json"])


def _files(world) -> list[tuple[str, str]]:
    row = world.row("SELECT manifest_json FROM source_snapshots WHERE id=?", world.env.snapshot_id)
    entries = json.loads(row["manifest_json"])["entries"]
    return sorted((e["relative_path"], e["sha256"]) for e in entries)


def _digest(pairs) -> str:
    raw = json.dumps([list(p) for p in pairs], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def test_a_declared_code_input_shows_the_code_digest_image_digest_and_transfer(tmp_path):
    world = World(tmp_path)
    _grants_to(world)
    env = world.env
    campaign = world.campaign(_spec(env, _stage(env, code_input="code")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    receipt = _receipt(world, campaign["id"])
    (stage,) = receipt["stages"]
    code = stage["code"]
    expected = _digest(_files(world))
    image_digest = IMAGE.split("@", 1)[1]
    assert code["input"] == "code" and code["snapshot_id"] == env.snapshot_id
    assert code["snapshot_digest"] == env.snapshot_digest
    assert code["code_digest"] == expected  # independently computed over the snapshot's files
    pairs = _files(world)
    assert code["files"] == len(pairs) and isinstance(code["files"], int)
    assert "files_truncated" not in code and code["approved"] is False
    # Private code paths and per-file hashes never appear anywhere in the receipt.
    receipt_text = json.dumps(receipt)
    for path, sha in pairs:
        assert path not in receipt_text and sha not in receipt_text
    assert code["image_digest"] == image_digest
    (attempt,) = stage["attempts"]
    transfer = attempt["code_transfer"]
    assert transfer["granted"] is True and transfer["provider_id"] == "local-docker"
    assert "delivered" not in transfer
    # No code approval stood when the grant was issued; the receipt says so from the event.
    assert transfer["approved"] is False and transfer["approval_digest"] is None
    assert transfer["code_digest"] == expected and transfer["image_digest"] == image_digest
    assert transfer["worker_principal_id"] == attempt["worker_principal_id"]
    assert transfer["trust_policy_digest"] == attempt["trust_policy_digest"]
    assert transfer["admission_basis"] == attempt["admission_basis"]
    (event,) = world.events_of(CODE_EVENT)
    payload = event["payload"]
    assert payload["stage_key"] == "s1" and payload["attempt"] == 1
    assert payload["code_digest"] == expected and payload["image_digest"] == image_digest
    assert payload["worker_principal_id"] == transfer["worker_principal_id"]
    assert payload["provider_id"] == "local-docker" and payload["input"] == "code"
    assert payload["attempt_id"] == world.attempts(campaign["id"])[0]["id"]
    assert payload["approved"] is False and payload["approval_digest"] is None
    # The record names digests and ids only: no file bytes, paths or argv.
    text = json.dumps(payload)
    assert "print(1)" not in text and "files" not in payload


def test_a_stage_without_a_code_input_has_the_same_receipt_shape_as_before(tmp_path):
    world = World(tmp_path)
    _grants_to(world)
    env = world.env
    campaign = world.campaign(_spec(env, _stage(env)))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (stage,) = _receipt(world, campaign["id"])["stages"]
    assert "code" not in stage and "code_transfer" not in stage["attempts"][0]
    assert world.events_of(CODE_EVENT) == []


def _rebuilt_receipt(world, campaign_id) -> dict:
    """The receipt built again now, from the recorded rows as they stand."""
    connection = _connection(world)
    try:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, campaign["status"])
    finally:
        connection.close()
    return receipt


def test_a_grant_without_its_code_event_is_not_reported_as_granted(tmp_path, monkeypatch):
    """The old crash window: the grant was written but its code event never was."""
    world = World(tmp_path)
    _grants_to(world)
    env = world.env
    monkeypatch.setattr(
        campaign_runner.CampaignRunner, "_record_code_grant_in", lambda *a, **k: None
    )
    campaign = world.campaign(_spec(env, _stage(env, code_input="code")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert world.events_of(CODE_EVENT) == []
    (stage,) = _receipt(world, campaign["id"])["stages"]
    (attempt,) = stage["attempts"]
    assert attempt["grant_id"] is not None  # the grant exists ...
    transfer = attempt["code_transfer"]
    # ... but nothing recorded the code grant, so the receipt does not claim one, and the
    # approval at grant time is unknown rather than read from today's state.
    assert transfer["granted"] is False
    assert transfer["approved"] is None and transfer["approval_digest"] is None


def test_a_failed_code_event_write_rolls_back_the_grant(tmp_path, monkeypatch):
    world = World(tmp_path)
    _grants_to(world)
    env = world.env
    store = world.runner.store
    original = store.record_runner_event
    failures = []

    def failing(connection, campaign, event_type, payload, **kwargs):
        if event_type == CODE_GRANTED_EVENT:
            failures.append(payload["attempt"])
            raise RuntimeError("injected code event failure")
        return original(connection, campaign, event_type, payload, **kwargs)

    monkeypatch.setattr(store, "record_runner_event", failing)
    campaign = world.campaign(_spec(env, _stage(env, code_input="code")))
    world.tick_until(lambda: len(failures) >= 2, worker=False, limit=40)  # the runner logs it
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["grant_id"] is None and attempt["status"] != "granted"
    assert world.events_of(CODE_EVENT) == []
    # Once the write succeeds the grant and its event land together, once.
    monkeypatch.setattr(store, "record_runner_event", original)
    world.restart_runner()
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (event,) = world.events_of(CODE_EVENT)
    (attempt,) = world.attempts(campaign["id"])
    assert event["payload"]["attempt_id"] == attempt["id"] and attempt["grant_id"] is not None
    (stage,) = _receipt(world, campaign["id"])["stages"]
    assert stage["attempts"][0]["code_transfer"]["granted"] is True


def test_an_approval_recorded_after_the_grant_does_not_change_the_receipt(tmp_path, monkeypatch):
    world = World(tmp_path)
    _grants_to(world)
    env = world.env
    campaign = world.campaign(_spec(env, _stage(env, code_input="code")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    # A valid approval now stands for the snapshot, recorded after the grant was issued.
    monkeypatch.setattr(
        campaign_receipts,
        "snapshot_code_approval",
        lambda c, s: {"valid": True, "files": [], "actor_principal_id": "later"},
    )
    (stage,) = _rebuilt_receipt(world, campaign["id"])["stages"]
    transfer = stage["attempts"][0]["code_transfer"]
    assert transfer["granted"] is True
    assert transfer["approved"] is False and transfer["approval_digest"] is None


def test_an_approval_invalidated_after_the_grant_does_not_change_the_receipt(tmp_path, monkeypatch):
    world = World(tmp_path)
    _grants_to(world)
    env = world.env
    at_grant = {
        "code": "private_code_approved",
        "files": [],
        "actor_principal_id": "principal_human",
        "decision_class": "human_session",
        "recorded_at": "2026-01-01T00:00:00Z",
        "valid": True,
    }
    monkeypatch.setattr(campaign_runner, "snapshot_code_approval", lambda c, s: dict(at_grant))
    campaign = world.campaign(_spec(env, _stage(env, code_input="code")))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    expected = campaign_runner.canonical_digest({k: v for k, v in at_grant.items() if k != "valid"})
    (event,) = world.events_of(CODE_EVENT)
    assert event["payload"]["approved"] is True
    assert event["payload"]["approval_digest"] == expected
    # The approval no longer holds when the receipt is built.
    monkeypatch.setattr(
        campaign_receipts, "snapshot_code_approval", lambda c, s: {"valid": False, "files": []}
    )
    for receipt in (_receipt(world, campaign["id"]), _rebuilt_receipt(world, campaign["id"])):
        transfer = receipt["stages"][0]["attempts"][0]["code_transfer"]
        assert transfer["approved"] is True and transfer["approval_digest"] == expected
        assert "principal_human" not in json.dumps(transfer)


def _fake_stage(snapshot_id, digest, image=IMAGE, code_input="code"):
    source = SimpleNamespace(name="code", kind="snapshot", snapshot_id=snapshot_id, digest=digest)
    return SimpleNamespace(code_input=code_input, inputs=[source], image=image)


def _connection(world):
    from tessera_control.store import _connect

    return _connect(world.env.state / "tessera.sqlite3")


def test_the_approved_flag_follows_a_valid_code_approval_only(tmp_path, monkeypatch):
    world = World(tmp_path)
    stage = _fake_stage(world.env.snapshot_id, world.env.snapshot_digest)
    connection = _connection(world)
    try:
        assert code_summary(connection, stage)["approved"] is False
        for found, expected in (
            ({"valid": True, "files": []}, True),
            ({"valid": False, "files": []}, False),
            (None, False),
        ):
            monkeypatch.setattr(
                campaign_receipts, "snapshot_code_approval", lambda c, s, found=found: found
            )
            assert code_summary(connection, stage)["approved"] is expected
    finally:
        connection.close()


def test_the_code_digest_changes_when_a_files_bytes_differ(tmp_path):
    world = World(tmp_path)
    env = world.env
    row = world.row("SELECT * FROM source_snapshots WHERE id=?", env.snapshot_id)
    manifest = json.loads(row["manifest_json"])
    manifest["entries"][0]["sha256"] = "0" * 64  # one file's bytes differ
    env.execute(
        "INSERT INTO source_snapshots (id, project_id, digest, manifest_json, created_at, "
        "actor_principal_id) VALUES ('snapshot_other', ?, ?, ?, ?, ?)",
        row["project_id"],
        "f" * 64,
        json.dumps(manifest),
        row["created_at"],
        row["actor_principal_id"],
    )
    connection = _connection(world)
    try:
        first = code_summary(connection, _fake_stage(env.snapshot_id, env.snapshot_digest))
        second = code_summary(connection, _fake_stage("snapshot_other", "f" * 64))
    finally:
        connection.close()
    assert first["code_digest"] != second["code_digest"]
    assert first["code_digest"] == _digest(_files(world))


def test_an_unreadable_manifest_gives_no_digest_not_a_guess(tmp_path):
    world = World(tmp_path)
    env = world.env
    env.execute("UPDATE source_snapshots SET manifest_json='{}' WHERE id=?", env.snapshot_id)
    connection = _connection(world)
    try:
        summary = code_summary(connection, _fake_stage(env.snapshot_id, env.snapshot_digest))
    finally:
        connection.close()
    assert summary["code_digest"] is None and summary["files"] == 0
    assert summary["approved"] is False


@pytest.mark.parametrize("image", ["no-digest-image"])
def test_a_stage_without_a_pinned_image_digest_reports_none(tmp_path, image):
    world = World(tmp_path)
    env = world.env
    connection = _connection(world)
    try:
        stage = _fake_stage(env.snapshot_id, env.snapshot_digest, image=image)
        assert code_summary(connection, stage)["image_digest"] is None
        assert code_summary(connection, _fake_stage(env.snapshot_id, "x", code_input=None)) is None
    finally:
        connection.close()


def test_the_code_summary_counts_files_and_never_names_them(tmp_path):
    world = World(tmp_path)
    env = world.env
    sentinel = "secret_sentinel_dir/private_model_xyzzy.py"
    manifest = {
        "entries": [
            {"relative_path": sentinel, "sha256": "ab" * 32, "size": 3},
            {"relative_path": "other.py", "sha256": "cd" * 32, "size": 4},
        ]
    }
    env.execute(
        "UPDATE source_snapshots SET manifest_json=? WHERE id=?",
        json.dumps(manifest),
        env.snapshot_id,
    )
    connection = _connection(world)
    try:
        summary = code_summary(connection, _fake_stage(env.snapshot_id, env.snapshot_digest))
    finally:
        connection.close()
    assert summary["files"] == 2 and "files_truncated" not in summary
    text = json.dumps(summary)
    assert "xyzzy" not in text and "other.py" not in text and "ab" * 32 not in text
    expected = _digest(sorted([(sentinel, "ab" * 32), ("other.py", "cd" * 32)]))
    assert summary["code_digest"] == expected
