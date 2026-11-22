"""Workload admission under the provider trust setting (ADR 0040 section 4)."""

from __future__ import annotations

from contextlib import closing

import pytest
from provider_trust_fixture import CODE, DERIVED
from test_artifact_custody import _released_manifest
from test_code_approval import PUBLIC, _chunked, _labelled
from test_code_approval import _selection_for as _base_selection_for

from tessera_control.salad_workload_admission import check_workload_admission
from tessera_control.snapshot_chunks import ChunkedSnapshotStore
from tessera_control.store import _connect

CODE_FILES = {"main.py": b"x = 1\n"}


def _selection_for(digest, data_policy="provider_trust_policy_v1"):
    """The route's data_policy literal must match the admitted basis (ADR 0040 section 4)."""
    value = _base_selection_for(digest)
    value["salad"]["execution"]["data_policy"] = data_policy
    return value


@pytest.fixture
def stagings(tmp_path):
    """The same bytes staged three times in one project: private, code-only, public."""
    state, owner, worker, manifest, *_ = _released_manifest(tmp_path)
    project_id, actor, store = manifest["project_id"], _labelled(owner), ChunkedSnapshotStore(state)
    _, code_only = _chunked(
        store, actor, project_id, CODE_FILES, "code", code={"paths": ["main.py"]}
    )
    _, private = _chunked(store, actor, project_id, CODE_FILES, "private")
    _, public = _chunked(store, actor, project_id, CODE_FILES, "public", data_class=PUBLIC)
    assert private["digest"] == code_only["digest"] == public["digest"]
    with closing(_connect(state / "tessera.sqlite3")) as db:
        yield (
            db,
            project_id,
            worker["id"],
            {"private": private, "code": code_only, "public": public},
        )


def test_admission_prefers_public_then_code_only_then_private(stagings, trust_policy):
    db, project_id, worker, snaps = stagings
    selection = _selection_for(snaps["public"]["digest"], "public_released_only")
    # Default: only the public staging is admissible, and its evidence says so.
    evidence = check_workload_admission(db, project_id, selection, worker)
    assert evidence["admission_basis"] == "public_only" and evidence["provider_id"] == "salad"
    assert evidence["worker_principal_id"] == worker and evidence["input_code"] == "none"
    assert evidence["trust_policy_digest"]
    # With every class allowed the public staging still wins; nothing is relabelled.
    trust_policy({"salad": ([*DERIVED, "private_code_approved"], [worker])})
    assert (
        check_workload_admission(db, project_id, selection, worker)["input_data_class"] == "public"
    )


def test_a_code_only_staging_is_admitted_before_a_private_one(tmp_path, trust_policy):
    state, owner, worker, manifest, *_ = _released_manifest(tmp_path)
    project_id, actor, store = manifest["project_id"], _labelled(owner), ChunkedSnapshotStore(state)
    _, code_only = _chunked(
        store, actor, project_id, CODE_FILES, "code", code={"paths": ["main.py"]}
    )
    _, private = _chunked(store, actor, project_id, CODE_FILES, "private")
    selection = _selection_for(private["digest"])
    with closing(_connect(state / "tessera.sqlite3")) as db:
        # Default and a derived-only setting both refuse: neither staging is admissible.
        with pytest.raises(ValueError, match="public"):
            check_workload_admission(db, project_id, selection, worker["id"])
        trust_policy({"salad": (CODE, [worker["id"]])})
        evidence = check_workload_admission(db, project_id, selection, worker["id"])
        assert evidence["input_code"] == "private_code_approved"
        assert evidence["input_data_class"] == "none_all_approved_code"
        assert evidence["admission_basis"] == "provider_trust_policy"
        # With private_derived the unapproved private staging is admissible too, but the
        # code-only staging (earlier in the preference order) is the one admitted.
        trust_policy({"salad": ([*CODE, "private_derived"], [worker["id"]])})
        assert (
            check_workload_admission(db, project_id, selection, worker["id"])["input_code"]
            == "private_code_approved"
        )
    assert private["id"] != code_only["id"]


def test_a_public_literal_is_refused_when_the_admitted_basis_is_not_public_only(
    tmp_path, trust_policy
):
    state, owner, worker, manifest, *_ = _released_manifest(tmp_path)
    project_id, actor, store = manifest["project_id"], _labelled(owner), ChunkedSnapshotStore(state)
    _, code_only = _chunked(
        store, actor, project_id, CODE_FILES, "code", code={"paths": ["main.py"]}
    )
    trust_policy({"salad": (CODE, [worker["id"]])})
    with closing(_connect(state / "tessera.sqlite3")) as db:
        lying = _selection_for(code_only["digest"], "public_released_only")
        with pytest.raises(ValueError, match="provider_trust_policy_v1"):
            check_workload_admission(db, project_id, lying, worker["id"])
        honest = _selection_for(code_only["digest"])
        assert (
            check_workload_admission(db, project_id, honest, worker["id"])["admission_basis"]
            == "provider_trust_policy"
        )
