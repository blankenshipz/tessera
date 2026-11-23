"""Typed environment digest and headline metric on design revisions (README step 1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_goal_to_decision import _command, _design
from test_result_checkpoints import _reconciled_manifest

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.experiments_models import CreateExperiment
from tessera_control.store import NotFound, Store, _token_hash, utc_now

ENV_BASE = "e" * 64
ENV_NEW = "f" * 64


def _typed(revision=None, grant=None, *, metric="wall_time_seconds", base=ENV_BASE, new=ENV_NEW):
    body = _design(revision, grant)
    body["metric"] = metric
    body["variants"][0]["environment_digest"] = base
    body["variants"][1]["environment_digest"] = new
    return body


def _env(design):
    return {v["kind"]: v["environment_digest"] for v in design["variants"]}


def test_environment_and_metric_are_recorded_shown_and_exported(tmp_path: Path) -> None:
    state, owner, _worker, grant, *_ = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    created = _command(
        records.create_experiment, owner, grant["project_id"], payload=_typed(grant=grant)
    )
    assert created["design"]["metric"] == "wall_time_seconds"
    assert _env(created["design"]) == {"baseline": ENV_BASE, "intervention": ENV_NEW}
    listed = records.list_experiments(owner, grant["project_id"])
    assert [item["id"] for item in listed] == [created["id"]]
    exported = records.export_experiment(owner, created["id"])
    assert exported["designs"][0]["metric"] == "wall_time_seconds"
    assert {v["environment_digest"] for v in exported["variants"]} == {ENV_BASE, ENV_NEW}


def test_omitted_environment_and_metric_stay_null(tmp_path: Path) -> None:
    state, owner, _worker, grant, *_ = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    created = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    assert created["design"]["metric"] is None
    assert _env(created["design"]) == {"baseline": None, "intervention": None}
    exported = records.export_experiment(owner, created["id"])
    assert exported["designs"][0]["metric"] is None
    assert all(v["environment_digest"] is None for v in exported["variants"])


def test_invalid_digest_and_blank_metric_are_rejected() -> None:
    for bad in ("A" * 64, "e" * 63, "g" * 64):
        body = _design()
        body["variants"][0]["environment_digest"] = bad
        with pytest.raises(ValidationError):
            CreateExperiment.model_validate(body)
    for blank in ("", "   ", "m" * 201):
        with pytest.raises(ValidationError):
            CreateExperiment.model_validate(_design() | {"metric": blank})
    with pytest.raises(ValidationError):  # extra=forbid stays strict
        CreateExperiment.model_validate(_design() | {"metrics": "x"})


def test_http_rejects_invalid_values_with_422(tmp_path: Path) -> None:
    state, owner, _worker, grant, *_ = _reconciled_manifest(tmp_path)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    client = TestClient(create_app(state))
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "k-env-1"}
    url = f"/projects/{grant['project_id']}/experiments"
    bad_metric = client.post(url, json=_typed(grant=grant, metric="  "), headers=headers)
    assert bad_metric.status_code == 422
    bad_digest = client.post(
        url,
        json=_typed(grant=grant, base="not-a-digest"),
        headers=headers | {"Idempotency-Key": "k2"},
    )
    assert bad_digest.status_code == 422
    assert owner  # the owner principal exists; nothing was created
    assert ExperimentStore(state).list_experiments(owner, grant["project_id"]) == []


def test_a_revision_changing_environment_keeps_the_old_value_and_records_the_change(
    tmp_path: Path,
) -> None:
    state, owner, _worker, grant, *_ = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    created = _command(
        records.create_experiment, owner, grant["project_id"], payload=_typed(grant=grant)
    )
    revised = _command(
        records.revise_design,
        owner,
        created["id"],
        payload=_typed(1, grant, metric="peak_memory_mb", new="1" * 64)
        | {"change_reason": "Rebuilt the image with a pinned lockfile."},
    )
    assert revised["design"]["metric"] == "peak_memory_mb"
    assert _env(revised["design"]) == {"baseline": ENV_BASE, "intervention": "1" * 64}
    exported = records.export_experiment(owner, created["id"])
    first, second = exported["designs"]
    assert (first["revision"], first["metric"]) == (1, "wall_time_seconds")
    assert _env(first) == {"baseline": ENV_BASE, "intervention": ENV_NEW}
    assert (second["revision"], second["metric"]) == (2, "peak_memory_mb")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        payload = db.execute(
            "SELECT payload_json FROM events WHERE event_type='experiment.design_revised'"
        ).fetchone()[0]
    assert "pinned lockfile" in payload


def test_other_tenant_cannot_read_environment(tmp_path: Path) -> None:
    state, owner, _worker, grant, *_ = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    created = _command(
        records.create_experiment, owner, grant["project_id"], payload=_typed(grant=grant)
    )
    token = "y" * 43
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("INSERT INTO organizations VALUES ('org_env_other','Other',?)", (utc_now(),))
        db.execute(
            "INSERT INTO principals VALUES ('env_other','org_env_other','human',?,NULL,?)",
            (_token_hash(token), utc_now()),
        )
        db.execute(
            """INSERT INTO credentials VALUES
            ('env_other_credential','env_other','local_owner','control',NULL,NULL,NULL,NULL,?,'other')""",
            (utc_now(),),
        )
    other = Store(state).authenticate(token)
    with pytest.raises(NotFound):
        records.get_experiment(other, created["id"])
    with pytest.raises(NotFound):
        records.export_experiment(other, created["id"])


def test_a_legacy_design_without_the_fields_still_works(tmp_path: Path) -> None:
    state, owner, _worker, grant, *_ = _reconciled_manifest(tmp_path)
    records = ExperimentStore(state)
    created = _command(
        records.create_experiment, owner, grant["project_id"], payload=_design(grant=grant)
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE research_experiment_designs SET metric=NULL")
        db.execute("UPDATE research_experiment_variants SET environment_digest=NULL")
    legacy = records.export_experiment(owner, created["id"])
    assert legacy["designs"][0]["metric"] is None
    revised = _command(records.revise_design, owner, created["id"], payload=_typed(1, grant))
    assert revised["design"]["metric"] == "wall_time_seconds"
