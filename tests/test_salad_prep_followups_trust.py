"""DataClassRefused names the class that would lift a refusal (ADR 0077 review follow-up).

Needs the provider trust fixtures, which import ``cryptography`` (skipped where it is missing)."""
# ruff: noqa: F811

from __future__ import annotations

import pytest
from test_provider_trust import WORKER, snapshots  # noqa: F401

from tessera_control import code_approval
from tessera_control.data_class import DataClassRefused
from tessera_control.provider_trust import provider_admits


def refused(db, snapshot_id, **kw):
    with pytest.raises(DataClassRefused) as error:
        provider_admits(db, "salad", WORKER, snapshot_id, **kw)
    return error.value


def test_admit_class_names_the_class_that_would_lift_the_refusal(snapshots, trust_policy):
    db, made, _ = snapshots
    assert refused(db, made["private"]["id"]).admit_class == "private_derived"
    trust_policy({"salad": (["public", "private_derived"], [WORKER])})
    assert refused(db, made["code_public"]["id"]).admit_class == "private_code_approved"


def test_nothing_trust_could_fix_has_no_admit_class(snapshots, trust_policy, monkeypatch):
    db, made, _ = snapshots
    trust_policy({"salad": (["public", "private_derived", "private_code_approved"], [WORKER])})
    snap = made["code_public"]["id"]
    monkeypatch.setattr(code_approval, "snapshot_code_approval", lambda c, s: {"valid": False})
    assert refused(db, snap).admit_class is None
    monkeypatch.undo()
    plain = made["public"]["id"]  # no code approval: the manifest is what fails
    db.execute("UPDATE source_snapshots SET manifest_json='{}' WHERE id=?", (plain,))
    error = refused(db, plain)
    assert "unreadable" in str(error) and error.admit_class is None
