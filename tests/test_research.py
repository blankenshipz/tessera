from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from tessera_control.research import ResearchStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    canonical_digest,
    initialize_state,
)

_COMMAND_COUNTER = count()


def _setup(tmp_path: Path) -> tuple[Path, ResearchStore, dict[str, Any]]:
    state = tmp_path / "state"
    initialize_state(state)
    store = ResearchStore(state)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    return state, store, store.authenticate(token)


def _command(
    method: Any, principal: dict[str, Any], *args: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    status, response = method(
        principal,
        *args,
        f"key-{method.__name__}-{next(_COMMAND_COUNTER)}-{uuidish(args)}",
        canonical_digest(payload),
        payload,
    )
    assert status in {200, 201}
    return response


def uuidish(value: tuple[Any, ...]) -> str:
    return "-".join(str(item) for item in value).replace("_", "")[-24:]


def _project(
    store: ResearchStore, principal: dict[str, Any], name: str = "Research"
) -> dict[str, Any]:
    body = {"name": name}
    status, result = store.create_project(
        principal, f"project-{name}", canonical_digest(body), body
    )
    assert status == 201
    return result


def _study(
    store: ResearchStore, principal: dict[str, Any], project_id: str, mode: str = "exploratory"
) -> dict[str, Any]:
    body = {
        "purpose": "Measure the declared synthetic outcome.",
        "protocol": "Record all variants.",
        "mode": mode,
    }
    return _command(store.create_study, principal, project_id, payload=body)


def _variant(
    store: ResearchStore, principal: dict[str, Any], study_id: str, name: str
) -> dict[str, Any]:
    body = {
        "name": name,
        "method_digest": "a" * 64,
        "input_digest": "b" * 64,
        "metadata": {"synthetic": True},
    }
    return _command(store.register_variant, principal, study_id, payload=body)


def _source(
    store: ResearchStore,
    principal: dict[str, Any],
    study_id: str,
    quote: str = "Synthetic evidence.",
) -> dict[str, Any]:
    body = {
        "uri": "https://example.invalid/source",
        "quoted_passage": quote,
        "content_digest": "c" * 64,
    }
    return _command(store.create_source, principal, study_id, payload=body)


def _assess(
    store: ResearchStore,
    principal: dict[str, Any],
    source: dict[str, Any],
    verdict: str = "supported",
) -> dict[str, Any]:
    body = {
        "expected_revision": source["revision"],
        "verdict": verdict,
        "reason": "Synthetic human review.",
    }
    return _command(store.assess_source, principal, source["id"], payload=body)


def test_export_contains_all_80_registered_variants_and_unverified_imports(tmp_path: Path) -> None:
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    study = _study(store, owner, project["id"])
    variants = [_variant(store, owner, study["id"], f"variant-{index:02}") for index in range(80)]
    for index, variant in enumerate(variants):
        outcome = {"favorable": index < 3}
        body = {
            "expected_revision": 1,
            "status": "completed",
            "outcome": outcome,
            "provenance": "imported_unverified",
        }
        updated = _command(store.record_variant_status, owner, variant["id"], payload=body)
        assert updated["outcome_provenance"] == "imported_unverified"
        assert updated["execution_receipt"] is None
    exported = store.export_study(owner, study["id"], {"max_bytes": 500_000})
    assert len(exported["variants"]) == 80
    assert sum(item["outcome"]["favorable"] for item in exported["variants"]) == 3
    assert all(item["execution_receipt"] is None for item in exported["variants"])
    with pytest.raises(Conflict, match="byte cap"):
        store.export_study(owner, study["id"], {"max_bytes": 256})


def test_unsupported_citation_blocks_acceptance_and_independence_is_unavailable(
    tmp_path: Path,
) -> None:
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    study = _study(store, owner, project["id"])
    variant = _variant(store, owner, study["id"], "rerun")
    assert variant["independence_status"] == "unavailable"
    source = _assess(store, owner, _source(store, owner, study["id"]), "unsupported")
    finding = _command(
        store.create_finding,
        owner,
        study["id"],
        payload={
            "statement": "This is a candidate.",
            "source_ids": [source["id"]],
            "variant_ids": [variant["id"]],
        },
    )
    with pytest.raises(Conflict, match="supported"):
        _command(store.accept_finding, owner, finding["id"], payload={"expected_revision": 1})
    with pytest.raises(StoreError, match="Invalid research request"):
        _command(
            store.register_variant,
            owner,
            study["id"],
            payload={
                "name": "independent",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "replication_label": "independent",
            },
        )


def test_source_cross_project_and_agent_human_gates(tmp_path: Path) -> None:
    _, store, owner = _setup(tmp_path)
    first, second = _project(store, owner, "First"), _project(store, owner, "Second")
    first_study, second_study = (
        _study(store, owner, first["id"]),
        _study(store, owner, second["id"]),
    )
    source = _source(store, owner, first_study["id"])
    variant = _variant(store, owner, second_study["id"], "candidate")
    with pytest.raises(NotFound):
        _command(
            store.create_finding,
            owner,
            second_study["id"],
            payload={
                "statement": "Wrong project source.",
                "source_ids": [source["id"]],
                "variant_ids": [variant["id"]],
            },
        )
    agent_token = "a" * 43
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    credential_body = {
        "kind": "agent",
        "audience": "control",
        "project_id": first["id"],
        "expires_at": expires_at,
    }
    store.issue_credential(
        owner,
        "agent-credential",
        canonical_digest(credential_body),
        credential_body,
        agent_token,
    )
    agent = store.authenticate(agent_token)
    with pytest.raises(Forbidden, match="Human review"):
        _assess(store, agent, source)


def test_correction_keeps_original_and_flags_dependent_finding_atomically(tmp_path: Path) -> None:
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    study = _study(store, owner, project["id"])
    variant = _variant(store, owner, study["id"], "variant")
    source = _assess(store, owner, _source(store, owner, study["id"], "Original quoted passage."))
    finding = _command(
        store.create_finding,
        owner,
        study["id"],
        payload={
            "statement": "Supported claim.",
            "source_ids": [source["id"]],
            "variant_ids": [variant["id"]],
        },
    )
    accepted = _command(
        store.accept_finding, owner, finding["id"], payload={"expected_revision": 1}
    )
    correction = _command(
        store.correct_source,
        owner,
        source["id"],
        payload={
            "expected_revision": source["revision"],
            "uri": "https://example.invalid/corrected",
            "quoted_passage": "Corrected quoted passage.",
            "content_digest": "d" * 64,
            "reason": "Transcription correction.",
        },
    )
    exported = store.export_study(owner, study["id"])
    original = next(item for item in exported["sources"] if item["id"] == source["id"])
    assert original["quoted_passage"] == "Original quoted passage."
    assert original["superseded_at"] is not None
    assert correction["replacement"]["supersedes_source_id"] == source["id"]
    dependent = next(item for item in exported["findings"] if item["id"] == accepted["id"])
    assert dependent["status"] == "needs_review"


def test_source_downgrade_flags_accepted_finding_idempotently_and_stale_write_rolls_back(
    tmp_path: Path,
) -> None:
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    study = _study(store, owner, project["id"])
    variant = _variant(store, owner, study["id"], "variant")
    source = _assess(store, owner, _source(store, owner, study["id"]))
    finding = _command(
        store.create_finding,
        owner,
        study["id"],
        payload={
            "statement": "Initially supported claim.",
            "source_ids": [source["id"]],
            "variant_ids": [variant["id"]],
        },
    )
    _command(store.accept_finding, owner, finding["id"], payload={"expected_revision": 1})
    downgrade = {
        "expected_revision": source["revision"],
        "verdict": "unsupported",
        "reason": "Human review found the passage does not support the claim.",
    }
    digest = canonical_digest(downgrade)
    first = store.assess_source(owner, source["id"], "downgrade", digest, downgrade)
    assert first == store.assess_source(owner, source["id"], "downgrade", digest, downgrade)
    exported = store.export_study(owner, study["id"])
    flagged = next(item for item in exported["findings"] if item["id"] == finding["id"])
    assert flagged["status"] == "needs_review"
    assert flagged["revision"] == 3
    assessed_event = exported["events"][-1]
    assert assessed_event["payload"]["dependent_findings_flagged"] == 1

    resupport = {
        "expected_revision": 3,
        "verdict": "supported",
        "reason": "A subsequent human assessment restored source support.",
    }
    _command(store.assess_source, owner, source["id"], payload=resupport)
    rechecked = store.export_study(owner, study["id"])
    source_events = [
        event for event in rechecked["events"] if event["event_type"] == "research.source_assessed"
    ]
    assert [event["payload"]["reason"] for event in source_events] == [
        "Synthetic human review.",
        downgrade["reason"],
        resupport["reason"],
    ]
    rechecked_finding = next(item for item in rechecked["findings"] if item["id"] == finding["id"])
    assert rechecked_finding["status"] == "needs_review"

    with pytest.raises(Conflict, match="revision"):
        store.assess_source(owner, source["id"], "stale", digest, downgrade)
    unchanged = store.export_study(owner, study["id"])
    still_flagged = next(item for item in unchanged["findings"] if item["id"] == finding["id"])
    assert still_flagged["status"] == "needs_review"
    assert still_flagged["revision"] == 3


def test_idempotency_revision_conflict_and_atomic_rollback(tmp_path: Path) -> None:
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    study = _study(store, owner, project["id"])
    variant = _variant(store, owner, study["id"], "one")
    body = {"expected_revision": 1, "status": "failed", "reason": "Synthetic failure."}
    digest = canonical_digest(body)
    first = store.record_variant_status(owner, variant["id"], "same", digest, body)
    assert first == store.record_variant_status(owner, variant["id"], "same", digest, body)
    with pytest.raises(Conflict):
        store.record_variant_status(owner, variant["id"], "new", digest, body)
    source = _source(store, owner, study["id"])
    before = store.export_study(owner, study["id"])
    with pytest.raises(NotFound):
        _command(
            store.create_finding,
            owner,
            study["id"],
            payload={
                "statement": "Atomic failure.",
                "source_ids": [source["id"], "source_missing"],
                "variant_ids": [variant["id"]],
            },
        )
    after = store.export_study(owner, study["id"])
    assert len(after["findings"]) == len(before["findings"]) == 0


def test_export_fails_closed_at_the_per_study_record_bound(tmp_path: Path) -> None:
    state, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    study = _study(store, owner, project["id"])
    now = "2026-01-01T00:00:00Z"
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.executemany(
            """INSERT INTO research_variants
            (id,study_id,name,method_digest,input_digest,metadata_json,replication_label,status,
             reason,outcome_json,outcome_provenance,revision,created_at,updated_at,created_by_principal_id)
            VALUES (?,?,?,?,?,?,?,'registered',NULL,NULL,NULL,1,?,?,?)""",
            [
                (
                    f"variant_bound_{index}",
                    study["id"],
                    f"synthetic-{index}",
                    "a" * 64,
                    "b" * 64,
                    "{}",
                    "unavailable",
                    now,
                    now,
                    owner["id"],
                )
                for index in range(1001)
            ],
        )
    with pytest.raises(Conflict, match="record limit"):
        store.export_study(owner, study["id"], {"max_bytes": 10_000_000})
