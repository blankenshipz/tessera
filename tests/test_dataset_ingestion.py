from __future__ import annotations

from copy import deepcopy
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from tessera_control.dataset_ingestion import DatasetIngestionStore
from tessera_control.store import (
    Conflict,
    StoreError,
    canonical_digest,
    initialize_state,
)

_COUNTER = count()


def _command(
    method: Any, principal: dict[str, Any], *args: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    status, response = method(
        principal,
        *args,
        f"dataset-{method.__name__}-{next(_COUNTER)}",
        canonical_digest(payload),
        payload,
    )
    assert status in {200, 201}
    return response


def _setup(tmp_path: Path) -> tuple[DatasetIngestionStore, dict[str, Any], str]:
    state = tmp_path / "state"
    initialize_state(state)
    store = DatasetIngestionStore(state)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    owner = store.authenticate(token)
    project_body = {"name": "MixFM"}
    _, project = store.create_project(
        owner, "project", canonical_digest(project_body), project_body
    )
    study = _command(
        store.create_study,
        owner,
        project["id"],
        payload={"purpose": "Record Outcome 0/1 input provenance.", "protocol": "Review inputs."},
    )
    source = _command(
        store.create_source,
        owner,
        study["id"],
        payload={
            "uri": "https://example.invalid/mixfm-dataset",
            "quoted_passage": "Local source declaration for a dataset snapshot.",
            "content_digest": "c" * 64,
            "locator": "manifest",
        },
    )
    source = _command(
        store.assess_source,
        owner,
        source["id"],
        payload={
            "expected_revision": source["revision"],
            "verdict": "supported",
            "reason": "Human assessed the cited source declaration.",
        },
    )
    return store, owner, study["id"], source


def _snapshot_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "Outcome 1 declared local snapshot",
        "snapshot_digest": "a" * 64,
        "schema": {
            "profile": "mixfm-outcome-1-v1",
            "columns": [
                {
                    "name": "ingredient_id",
                    "logical_type": "categorical",
                    "nullable": False,
                    "missingness": "none",
                },
                {
                    "name": "concentration",
                    "logical_type": "number",
                    "nullable": True,
                    "unit": "mM",
                    "missingness": "declared",
                },
            ],
        },
        "record_count": 12,
        "sources": [
            {
                "source_id": source["id"],
                "expected_source_revision": source["revision"],
                "expected_content_digest": source["content_digest"],
                "license_expression": "CC-BY-4.0",
                "license_status": "declared",
                "provenance_kind": "source_assertion",
                "provenance_status": "declared",
            }
        ],
        "validation_summary": {
            "status": "not_run",
            "summary": "No dataset bytes were supplied or checked by Tessera.",
            "checked_rows": 0,
        },
        "split_strategy": {
            "kind": "grouped",
            "declaration": "Hold out ingredient families before fitting.",
            "leakage_policy": "Do not share grouped rows across train and evaluation.",
            "leakage_status": "declared",
        },
    }


def test_malformed_dataset_schema_is_rejected_before_recording(tmp_path: Path) -> None:
    store, owner, study_id, source = _setup(tmp_path)
    body = _snapshot_payload(source)
    body["schema"]["columns"].append(
        {
            "name": "ingredient_id",
            "logical_type": "string",
            "nullable": False,
            "missingness": "none",
        }
    )
    with pytest.raises(StoreError, match="Invalid research request"):
        _command(store.register_dataset_snapshot, owner, study_id, payload=body)
    assert store.list_dataset_snapshots(owner, study_id) == []


def test_dataset_source_digest_or_revision_mismatch_is_rejected(tmp_path: Path) -> None:
    store, owner, study_id, source = _setup(tmp_path)
    body = _snapshot_payload(source)
    body["sources"][0]["expected_source_revision"] = source["revision"] - 1
    with pytest.raises(Conflict, match="digest or revision"):
        _command(store.register_dataset_snapshot, owner, study_id, payload=body)
    body = _snapshot_payload(source)
    body["sources"][0]["expected_content_digest"] = "d" * 64
    with pytest.raises(Conflict, match="digest or revision"):
        _command(store.register_dataset_snapshot, owner, study_id, payload=body)


@pytest.mark.parametrize("unsupported_field", ["license_status", "provenance_status"])
def test_unsupported_license_or_provenance_cannot_be_approved(
    tmp_path: Path, unsupported_field: str
) -> None:
    store, owner, study_id, source = _setup(tmp_path)
    body = _snapshot_payload(source)
    body["sources"][0][unsupported_field] = "unsupported"
    snapshot = _command(store.register_dataset_snapshot, owner, study_id, payload=body)
    proposal = _command(
        store.create_ingestion_proposal,
        owner,
        study_id,
        payload={
            "snapshot_id": snapshot["id"],
            "expected_snapshot_revision": snapshot["revision"],
            "intended_use": "Evaluate the declared MixFM Outcome 1 split.",
        },
    )
    with pytest.raises(Conflict, match="license or provenance"):
        _command(
            store.review_ingestion_proposal,
            owner,
            proposal["id"],
            payload={
                "expected_revision": proposal["revision"],
                "decision": "approve",
                "rationale": "The review must fail closed for unsupported declarations.",
            },
        )
    proposal_status = store.export_study(owner, study_id)["dataset_ingestion_proposals"][0][
        "status"
    ]
    assert proposal_status == "proposed"


def test_duplicate_snapshot_digest_is_rejected_without_second_record(tmp_path: Path) -> None:
    store, owner, study_id, source = _setup(tmp_path)
    body = _snapshot_payload(source)
    _command(store.register_dataset_snapshot, owner, study_id, payload=body)
    with pytest.raises(Conflict, match="already registered"):
        _command(store.register_dataset_snapshot, owner, study_id, payload=deepcopy(body))
    assert len(store.list_dataset_snapshots(owner, study_id)) == 1


def test_complete_dataset_accounting_exports_snapshot_and_reviewable_proposal(
    tmp_path: Path,
) -> None:
    store, owner, study_id, source = _setup(tmp_path)
    snapshot = _command(
        store.register_dataset_snapshot, owner, study_id, payload=_snapshot_payload(source)
    )
    assert snapshot["schema_digest"] == canonical_digest(snapshot["schema"])
    assert snapshot["content_validation"] == "not_performed"
    proposal = _command(
        store.create_ingestion_proposal,
        owner,
        study_id,
        payload={
            "snapshot_id": snapshot["id"],
            "expected_snapshot_revision": snapshot["revision"],
            "intended_use": "Review-only Outcome 1 local dataset ingestion.",
        },
    )
    reviewed = _command(
        store.review_ingestion_proposal,
        owner,
        proposal["id"],
        payload={
            "expected_revision": proposal["revision"],
            "decision": "approve",
            "rationale": "Human approved declared metadata; no external validation occurred.",
        },
    )
    exported = store.export_study(owner, study_id)
    assert exported["completeness"] == "complete_in_scope_study_ledger"
    assert exported["dataset_snapshots"] == [snapshot]
    assert exported["dataset_ingestion_proposals"] == [reviewed]
    assert any(
        event["event_type"] == "research.dataset_ingestion_reviewed" for event in exported["events"]
    )
