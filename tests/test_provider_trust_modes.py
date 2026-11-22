"""Owner-set trust mode ``all_registered_providers`` (ADR 0047); synthetic state only.

Agents and specs route data among configured, registered providers; a principal listed
nowhere or under an unregistered provider id stays public-only, specs still narrow, and the
egress refusals for non-public inputs hold under every mode.
"""

from __future__ import annotations

import json

import pytest
from test_provider_trust import OTHER, WORKER, _document, _entry, _write, snapshots  # noqa: F401

from tessera_control import provider_trust
from tessera_control.data_class import DataClassRefused
from tessera_control.provider_trust import (
    MODE_ALL_REGISTERED,
    TrustPolicyRefused,
    check_narrowing,
    install_policy,
    load_policy_file,
    policy_from_document,
    provider_admits,
    provider_admits_class,
)


def _mode_policy(providers, mode=MODE_ALL_REGISTERED):
    return policy_from_document({**_document(providers), "mode": mode}, "file")


def test_listed_principals_of_registered_providers_receive_every_class(snapshots):  # noqa: F811
    db, made, _ = snapshots
    install_policy(
        _mode_policy({"salad": _entry([], [WORKER]), "local-docker": _entry([], [OTHER])})
    )
    for provider, principal in (("salad", WORKER), ("local-docker", OTHER)):
        for key in ("private", "code_only", "code_public"):
            admitted = provider_admits(db, provider, principal, made[key]["id"])
            assert admitted.basis == "provider_trust_policy"


def test_unlisted_or_unregistered_stays_public_only(snapshots):  # noqa: F811
    db, made, _ = snapshots
    install_policy(
        _mode_policy(
            {"salad": _entry([], [WORKER]), "not-a-registered-plugin": _entry([], [OTHER])}
        )
    )
    # Listed under salad only: a local-docker stage with that principal gets public only.
    with pytest.raises(DataClassRefused):
        provider_admits(db, "local-docker", WORKER, made["private"]["id"])
    # A principal listed nowhere.
    with pytest.raises(DataClassRefused):
        provider_admits(db, "salad", "principal_unlisted", made["private"]["id"])
    # Listed, but under a provider id no module registered: fail closed.
    with pytest.raises(DataClassRefused):
        provider_admits(db, "not-a-registered-plugin", OTHER, made["private"]["id"])
    assert provider_admits(db, "not-a-registered-plugin", OTHER, made["public"]["id"]).basis == (
        "public_only"
    )


def test_specs_and_the_approval_time_setting_still_narrow(snapshots):  # noqa: F811
    db, made, _ = snapshots
    listed = policy_from_document(_document({"salad": _entry(["public"], [WORKER])}), "file")
    provider_trust.record_policy(db, listed, "test", "2026-01-01T00:00:00Z")
    db.commit()
    widened = _mode_policy({"salad": _entry([], [WORKER])})
    install_policy(widened)
    provider_trust.record_policy(db, widened, "test", "2026-01-01T00:00:01Z")
    db.commit()
    with pytest.raises(DataClassRefused):
        provider_admits(db, "salad", WORKER, made["private"]["id"], narrowed=frozenset({"public"}))
    # An approval taken under the earlier, narrower setting never gains the widening.
    assert not provider_admits_class(
        "salad", WORKER, "private_derived", approval_digest=listed.digest, connection=db
    )
    assert provider_admits_class("salad", WORKER, "private_derived", connection=db)
    with pytest.raises(ValueError, match="exceed"):
        check_narrowing("not-a-registered-plugin", ["private_derived"])


def test_default_mode_is_omitted_so_existing_digests_do_not_change(tmp_path):
    plain = _document({"salad": _entry(["public"], [WORKER])})
    explicit = {**plain, "mode": "listed_classes"}
    assert (
        policy_from_document(plain, "file").digest == policy_from_document(explicit, "file").digest
    )
    assert "mode" not in policy_from_document(explicit, "file").document
    widened = _mode_policy({"salad": _entry([], [WORKER])})
    assert widened.document["mode"] == MODE_ALL_REGISTERED
    assert widened.digest != policy_from_document(plain, "file").digest
    loaded = load_policy_file(_write(tmp_path / "trust.json", widened.document))
    assert loaded.digest == widened.digest
    for bad in ({**plain, "mode": "everything"}, {**plain, "mode": None}, {**plain, "extra": 1}):
        with pytest.raises(TrustPolicyRefused):
            policy_from_document(bad, "file")


def test_a_mode_change_is_audited(snapshots):  # noqa: F811
    db, _made, _ = snapshots
    before = policy_from_document(_document({"salad": _entry(["public"], [WORKER])}), "file")
    after = _mode_policy({"salad": _entry(["public"], [WORKER])})
    provider_trust.record_policy(db, before, "test", "2026-01-01T00:00:00Z")
    provider_trust.record_policy(db, after, "test", "2026-01-01T00:00:01Z")
    db.commit()
    payloads = [
        json.loads(row[0])
        for row in db.execute(
            "SELECT payload_json FROM operator_audit_events WHERE event_type=?",
            (provider_trust.POLICY_CHANGED,),
        )
    ]
    expected = {"from": "listed_classes", "to": MODE_ALL_REGISTERED}
    assert any(p.get("providers_changed", {}).get("_mode") == expected for p in payloads), payloads


def test_egress_refusals_for_non_public_inputs_hold_in_every_mode():
    """The never-to-the-public-internet invariant is in spec validation, not in trust."""
    import test_campaign_models as local_models
    import test_campaign_salad_models as salad_models

    install_policy(
        _mode_policy({"salad": _entry([], [WORKER]), "local-docker": _entry([], [OTHER])})
    )
    local_models.test_egress_requires_local_backend_and_public_inputs()
    salad_models.test_a_salad_stage_with_non_public_input_cannot_declare_hosts()
