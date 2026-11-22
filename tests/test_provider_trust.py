"""Provider data-class trust setting (ADR 0040 section 4); synthetic state only."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import pytest
from provider_trust_fixture import ALL_CLASSES, CODE, DERIVED, build_policy
from test_artifact_custody import _released_manifest
from test_code_approval import APPROVED, FILES, PUBLIC, _chunked, _labelled

from tessera_control import provider_trust
from tessera_control.data_class import DataClassRefused
from tessera_control.provider_trust import (
    TrustPolicyRefused,
    check_narrowing,
    current_policy,
    install_policy,
    load_policy_file,
    policy_from_document,
    provider_admits,
    provider_admits_class,
    record_policy,
    record_reload_refused,
)
from tessera_control.snapshot_chunks import ChunkedSnapshotStore
from tessera_control.store import _connect

WORKER = "principal_local_worker"
OTHER = "principal_other_worker"


def _document(providers):
    return {"schema": "tessera.provider-trust.v1", "providers": providers}


def _write(path, value, mode=0o600):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(mode)
    return path


def _entry(classes, principals):
    return {"data_classes": list(classes), "worker_principal_ids": list(principals)}


# ------------------------------------------------------------------------------ semantics


def test_builtin_default_binds_no_principal_and_admits_public_only_everywhere():
    policy = current_policy()
    assert policy.source == "builtin" and policy.document["providers"] == {}
    for provider in ("local-docker", "self-hosted", "salad", "kubernetes", None):
        for principal in (WORKER, None):
            assert policy.allowed(provider, principal) == {"public"}
            assert not provider_admits_class(provider, principal, "private_derived")
            assert not provider_admits_class(provider, principal, "private_code_approved")
            assert provider_admits_class(provider, principal, "public")
    assert policy.provider_for_worker(WORKER) is None


def test_trust_is_keyed_on_the_provider_instance(trust_policy):
    trust_policy({"self-hosted": (ALL_CLASSES, [WORKER]), "salad": (DERIVED, [OTHER])})
    policy = current_policy()
    assert policy.allowed("self-hosted", WORKER) == set(ALL_CLASSES)
    # Listed under self-hosted, so a local-docker stage gets public only.
    assert policy.allowed("local-docker", WORKER) == {"public"}
    assert policy.allowed("salad", WORKER) == {"public"}  # bound to another provider
    assert policy.allowed("self-hosted", OTHER) == {"public"}
    assert policy.allowed("self-hosted", "principal_unlisted") == {"public"}
    assert policy.allowed("self-hosted", None) == {"public"}
    assert policy.allowed(None, WORKER) == {"public"}
    assert policy.allowed("unknown-provider", WORKER) == {"public"}
    assert policy.provider_for_worker(WORKER) == "self-hosted"
    assert policy.provider_for_worker("principal_unlisted") is None


def test_narrowing_and_the_approval_time_setting_only_narrow(trust_policy):
    trust_policy({"salad": (ALL_CLASSES, [WORKER])})
    assert provider_admits_class("salad", WORKER, "private_derived")
    assert not provider_admits_class("salad", WORKER, "private_derived", frozenset({"public"}))
    check_narrowing("salad", None)
    check_narrowing("salad", ["public", "private_derived"])
    with pytest.raises(ValueError, match="exceed"):
        check_narrowing("local-docker", ["private_derived"])
    with pytest.raises(ValueError, match="unknown"):
        check_narrowing("salad", ["everything"])
    # With a principal the instance's classes are the bound.
    check_narrowing("salad", ["private_derived"], WORKER)
    with pytest.raises(ValueError, match="exceed"):
        check_narrowing("salad", ["private_derived"], OTHER)


# ---------------------------------------------------------------------------------- loader


def test_loader_accepts_a_well_formed_file_and_canonicalizes(tmp_path):
    path = _write(
        tmp_path / "trust.json",
        _document(
            {
                "salad": _entry(["private_derived", "public", "private_code_approved"], ["b", "a"]),
                "self-hosted": _entry([], []),
            }
        ),
    )
    policy = load_policy_file(path)
    assert policy.source == "file"
    assert policy.document["providers"]["salad"] == {
        "data_classes": ["public", "private_code_approved", "private_derived"],
        "worker_principal_ids": ["a", "b"],
    }
    # Same content in another order and spelling: the same digest.
    again = _write(
        tmp_path / "again.json",
        _document(
            {
                "self-hosted": _entry([], []),
                "salad": _entry(ALL_CLASSES, ["a", "b"]),
            }
        ),
    )
    assert load_policy_file(again).digest == policy.digest


def test_a_principal_listed_under_two_providers_refuses_the_file(tmp_path):
    path = _write(
        tmp_path / "trust.json",
        _document(
            {"salad": _entry(ALL_CLASSES, [WORKER]), "self-hosted": _entry(ALL_CLASSES, [WORKER])}
        ),
    )
    with pytest.raises(TrustPolicyRefused, match="binds one provider"):
        load_policy_file(path)


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        {"schema": "tessera.provider-trust.v2", "providers": {}},
        {"schema": "tessera.provider-trust.v1"},
        {"schema": "tessera.provider-trust.v1", "providers": {}, "extra": 1},
        _document({"salad": _entry(["secret"], [])}),
        _document({"salad": _entry(["public", "public"], [])}),
        _document({"salad": {"data_classes": ["public"]}}),
        _document({"salad": {**_entry([], []), "extra": 1}}),
        _document({"Bad Id": _entry([], [])}),
        _document({"salad": _entry([], [3])}),
        _document({"salad": _entry([], [WORKER, WORKER])}),
        _document([]),
    ],
)
def test_loader_refuses_malformed_files(tmp_path, value):
    with pytest.raises(TrustPolicyRefused):
        load_policy_file(_write(tmp_path / "trust.json", value))


def test_loader_refuses_symlink_mode_owner_and_missing_files(tmp_path, monkeypatch):
    real = _write(tmp_path / "real.json", _document({}))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(TrustPolicyRefused, match="symlink"):
        load_policy_file(link)
    for mode in (0o620, 0o602, 0o666):
        real.chmod(mode)
        with pytest.raises(TrustPolicyRefused, match="writable"):
            load_policy_file(real)
    real.chmod(0o644)  # group/world-readable is fine; only writable is refused
    load_policy_file(real)
    other_user = os.geteuid() + 1
    monkeypatch.setattr(provider_trust.os, "geteuid", lambda: other_user)
    with pytest.raises(TrustPolicyRefused, match="owned by"):
        load_policy_file(real)
    monkeypatch.undo()
    with pytest.raises(TrustPolicyRefused, match="cannot be read"):
        load_policy_file(tmp_path / "missing.json")
    with pytest.raises(TrustPolicyRefused, match="regular file"):
        load_policy_file(tmp_path)


# ------------------------------------------------------------- recording, digest and events


def _events(db, event_type=None):
    sql = "SELECT event_type, decision, payload_json FROM operator_audit_events"
    if event_type:
        return db.execute(sql + " WHERE event_type=?", (event_type,)).fetchall()
    return db.execute(sql).fetchall()


def test_a_digest_change_appends_exactly_one_event_and_an_identical_load_none(tmp_path):
    state, owner, *_ = _released_manifest(tmp_path)
    first = build_policy({"salad": (DERIVED, [WORKER])})
    second = build_policy({"salad": (ALL_CLASSES, [WORKER]), "self-hosted": (CODE, [OTHER])})
    with closing(_connect(state / "tessera.sqlite3")) as db:
        db.execute("BEGIN IMMEDIATE")
        assert record_policy(db, first, "deployment_start", "2026-01-01T00:00:00Z") is True
        assert record_policy(db, first, owner["id"], "2026-01-01T00:00:01Z") is False  # identical
        assert record_policy(db, second, owner["id"], "2026-01-01T00:00:02Z") is True
        assert record_policy(db, second, owner["id"], "2026-01-01T00:00:03Z") is False
        db.execute("COMMIT")
        changed = _events(db, "provider_trust.policy_changed")
        assert len(changed) == 2
        start, reload = (json.loads(row[2]) for row in changed)
        assert start["actor"] == "deployment_start" and start["old_digest"] is None
        assert start["new_digest"] == first.digest
        assert reload["old_digest"] == first.digest and reload["new_digest"] == second.digest
        assert reload["actor"] == owner["id"]
        diff = reload["providers_changed"]
        assert diff["salad"]["data_classes"] == {"added": ["private_code_approved"], "removed": []}
        assert diff["self-hosted"]["provider"] == "added"
        # Policies are immutable and keyed by digest.
        stored = db.execute("SELECT digest, source FROM provider_trust_policies").fetchall()
        assert {row[0] for row in stored} == {first.digest, second.digest}
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE provider_trust_policies SET source='builtin'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM provider_trust_policies")


def test_reload_refused_is_audited_and_keeps_the_installed_policy(tmp_path):
    state, owner, *_ = _released_manifest(tmp_path)
    good = build_policy({"salad": (DERIVED, [WORKER])})
    install_policy(good)
    with closing(_connect(state / "tessera.sqlite3")) as db:
        db.execute("BEGIN IMMEDIATE")
        record_reload_refused(db, owner["id"], "2026-01-01T00:00:00Z", "trust file is malformed")
        db.execute("COMMIT")
        (row,) = _events(db, "provider_trust.reload_refused")
    assert row[1] == "denied" and json.loads(row[2])["kept_digest"] == good.digest
    assert current_policy() is good


# --------------------------------------------------------------------------- provider_admits


@pytest.fixture
def snapshots(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id, actor = manifest["project_id"], _labelled(owner)
    store = ChunkedSnapshotStore(state)
    made = {}
    _, made["public"] = _chunked(
        store, actor, project_id, {"o.txt": b"open"}, "pub", data_class=PUBLIC
    )
    _, made["private"] = _chunked(store, actor, project_id, {"p.txt": b"closed"}, "priv")
    _, made["code_public"] = _chunked(
        store, actor, project_id, FILES, "cp", data_class=PUBLIC, code=APPROVED
    )
    _, made["code_only"] = _chunked(
        store, actor, project_id, {"main.py": b"x = 1\n"}, "co", code={"paths": ["main.py"]}
    )
    with closing(_connect(state / "tessera.sqlite3")) as db:
        yield db, made, state


def test_the_default_denies_everything_private_to_every_provider_including_local_docker(snapshots):
    db, made, _ = snapshots
    for provider in ("local-docker", "self-hosted", "salad", "kubernetes"):
        admitted = provider_admits(db, provider, WORKER, made["public"]["id"])
        assert (admitted.basis, admitted.input_data_class) == ("public_only", "public")
        assert admitted.input_code == "none" and not admitted.code_paths
        assert admitted.policy_digest == current_policy().digest
        for key in ("private", "code_public", "code_only"):
            with pytest.raises(DataClassRefused, match=provider):
                provider_admits(db, provider, WORKER, made[key]["id"])


def test_an_unbound_or_differently_bound_principal_gets_public_only(snapshots, trust_policy):
    db, made, _ = snapshots
    trust_policy({"self-hosted": (ALL_CLASSES, [WORKER])})
    # Listed under self-hosted: a local-docker stage still gets public only.
    with pytest.raises(DataClassRefused, match=f"'local-docker' with worker principal '{WORKER}'"):
        provider_admits(db, "local-docker", WORKER, made["private"]["id"])
    with pytest.raises(DataClassRefused):
        provider_admits(db, "self-hosted", OTHER, made["private"]["id"])
    with pytest.raises(DataClassRefused):
        provider_admits(db, "self-hosted", None, made["private"]["id"])
    admitted = provider_admits(db, "self-hosted", WORKER, made["private"]["id"])
    assert (admitted.basis, admitted.input_data_class) == (
        "provider_trust_policy",
        "private_derived",
    )


def test_a_file_policy_admits_private_derived_for_salad(snapshots, trust_policy):
    db, made, _ = snapshots
    trust_policy({"salad": (DERIVED, [WORKER])})
    admitted = provider_admits(db, "salad", WORKER, made["private"]["id"])
    assert (
        admitted.input_data_class == "private_derived" and admitted.basis == "provider_trust_policy"
    )
    # Approved code still needs its own class.
    with pytest.raises(DataClassRefused, match="private_code_approved"):
        provider_admits(db, "salad", WORKER, made["code_only"]["id"])


def test_a_public_recorded_snapshot_with_a_code_approval_is_refused_without_the_code_class(
    snapshots, trust_policy
):
    """Item 2 regression: the snapshot's data is recorded public, but the approved files
    are private code and must not be counted as public data."""
    db, made, _ = snapshots
    assert made["code_public"]["data_class"]["data_class"] == "public"
    trust_policy({"salad": (["public", "private_derived"], [WORKER])})
    with pytest.raises(DataClassRefused, match="private_code_approved"):
        provider_admits(db, "salad", WORKER, made["code_public"]["id"])
    trust_policy({"salad": (CODE, [WORKER])})
    admitted = provider_admits(db, "salad", WORKER, made["code_public"]["id"])
    assert admitted.code_paths == {"main.py", "conf/run.toml"}
    assert admitted.input_code == "private_code_approved" and admitted.input_data_class == "public"
    assert admitted.basis == "provider_trust_policy"  # approval-bearing: never public_only
    only = provider_admits(db, "salad", WORKER, made["code_only"]["id"])
    assert only.input_data_class == "none_all_approved_code"


def test_spec_narrowing_refuses_what_the_setting_would_allow(snapshots, trust_policy):
    db, made, _ = snapshots
    trust_policy({"salad": (ALL_CLASSES, [WORKER])})
    provider_admits(db, "salad", WORKER, made["private"]["id"])
    with pytest.raises(DataClassRefused):
        provider_admits(db, "salad", WORKER, made["private"]["id"], narrowed=frozenset({"public"}))
    with pytest.raises(DataClassRefused, match="private_code_approved"):
        provider_admits(
            db, "salad", WORKER, made["code_only"]["id"], narrowed=frozenset({"private_derived"})
        )


def test_the_approval_time_setting_blocks_a_later_widening(snapshots, trust_policy):
    db, made, state = snapshots
    narrow = build_policy({"salad": (["public"], [WORKER])})
    wide = build_policy({"salad": (ALL_CLASSES, [WORKER])})
    db.execute("BEGIN IMMEDIATE")
    record_policy(db, narrow, "deployment_start", "2026-01-01T00:00:00Z")
    record_policy(db, wide, "deployment_start", "2026-01-01T00:00:01Z")
    db.execute("COMMIT")
    install_policy(wide)
    private = made["private"]["id"]
    provider_admits(db, "salad", WORKER, private)  # now allowed
    with pytest.raises(DataClassRefused):  # the approval saw only the narrow setting
        provider_admits(db, "salad", WORKER, private, approval_digest=narrow.digest)
    provider_admits(db, "salad", WORKER, private, approval_digest=wide.digest)
    # A later narrowing fails closed for an approval made under the wide setting.
    db.execute("BEGIN IMMEDIATE")
    record_policy(db, narrow, "deployment_start", "2026-01-01T00:00:02Z")
    db.execute("COMMIT")
    install_policy(narrow)
    with pytest.raises(DataClassRefused):
        provider_admits(db, "salad", WORKER, private, approval_digest=wide.digest)
    # An unknown approval-time digest grants nothing beyond public.
    install_policy(wide)
    with pytest.raises(DataClassRefused):
        provider_admits(db, "salad", WORKER, private, approval_digest="f" * 64)
    provider_admits(db, "salad", WORKER, made["public"]["id"], approval_digest="f" * 64)


def test_an_invalid_approval_or_unreadable_manifest_refuses_under_any_setting(
    snapshots, trust_policy
):
    db, made, state = snapshots
    trust_policy({"salad": (ALL_CLASSES, [WORKER])})
    snap = made["code_public"]["id"]
    provider_admits(db, "salad", WORKER, snap)
    with pytest.raises(DataClassRefused, match="not recorded"):
        provider_admits(db, "salad", WORKER, "snapshot_missing")
    db.execute("UPDATE source_snapshots SET manifest_json='{}' WHERE id=?", (snap,))
    with pytest.raises(DataClassRefused):
        provider_admits(db, "salad", WORKER, snap)


def test_the_policy_document_roundtrips_through_policy_from_document():
    policy = policy_from_document(_document({"salad": _entry(ALL_CLASSES, [WORKER])}), "file")
    assert policy_from_document(policy.document, "file").digest == policy.digest
    assert provider_trust.show(policy)["policy"] == policy.document
