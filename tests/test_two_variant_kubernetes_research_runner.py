"""Isolated contract checks only; they never substitute for a live dispatch."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_two_variant_kubernetes_research.py"
spec = importlib.util.spec_from_file_location("two_variant_research", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def _manifest() -> dict:
    return {
        "origin": "http://127.0.0.1:8765",
        "project_id": "project-one",
        "snapshot_id": "snapshot-one",
        "snapshot_digest": "a" * 64,
        "worker_principal_id": "worker-one",
        "pinned_image": "docker.io/library/cpu@sha256:" + "b" * 64,
        "baseline_argv": ["/hello", "--cpu-sum-squares", "10", "/outputs/result.txt"],
        "intervention_argv": ["/hello", "--cpu-sum-squares", "20", "/outputs/result.txt"],
    }


def test_requires_distinct_bounded_commands_and_explicit_binding(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _manifest()
    path.write_text(json.dumps(manifest))
    assert runner._manifest(path) == manifest
    manifest["intervention_argv"] = manifest["baseline_argv"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(runner.AdmissionError, match="distinct"):
        runner._manifest(path)
    manifest["origin"] = "http://0.0.0.0:8765"
    path.write_text(json.dumps(manifest))
    with pytest.raises(runner.AdmissionError, match="loopback"):
        runner._manifest(path)
    manifest = _manifest()
    manifest["baseline_argv"][2] = "1000000"
    path.write_text(json.dumps(manifest))
    with pytest.raises(runner.AdmissionError, match="reviewed"):
        runner._manifest(path)


def test_admission_resume_does_not_repeat_successful_mutations(tmp_path: Path, monkeypatch) -> None:
    manifest = _manifest()
    receipt = tmp_path / "receipt.json"
    calls: list[tuple[str, str, dict | None, str | None]] = []
    limits = {"currency": "USD", "monetary_limit_cents": 0, "max_concurrency": 1}
    grant_status = "active"
    measurement = False

    def api(origin, token, method, route, body=None, key=None):
        calls.append((method, route, body, key))
        if method == "GET":
            if route.endswith("/execution-limits"):
                return limits
            if route.startswith("/execution-grants/"):
                return {"status": grant_status}
            if route.startswith("/experiments/"):
                return {
                    "admitted_attempts": [
                        {
                            "id": baseline_attempt_id,
                            "outcome": "succeeded",
                            "revision": 5,
                            "operation_id": "a" * 32,
                        }
                    ],
                    "measurements": (
                        [
                            {
                                "id": "measurement-one",
                                "attempt_id": baseline_attempt_id,
                                "name": "sum_squares",
                                "value": 385,
                                "artifact_id": "artifact-one",
                            }
                        ]
                        if measurement
                        else []
                    ),
                    "artifact_links": [
                        {"attempt_id": baseline_attempt_id, "artifact_id": "artifact-one"}
                    ],
                    "artifacts": [
                        {
                            "id": "artifact-one",
                            "operation_id": "a" * 32,
                            "relative_path": "result.txt",
                        }
                    ],
                }
            return {"digest": manifest["snapshot_digest"], "project_id": manifest["project_id"]}
        if route.endswith("/tasks"):
            return {
                "id": f"task-{len(calls)}",
                "title": body["title"],
                "purpose": body["purpose"],
                "revision": 1,
            }
        if route.endswith("/plan"):
            return {"revision": 2, "current_plan_digest": "c" * 64}
        if route.endswith("/approve"):
            return {
                "id": route.split("/")[2],
                "revision": 3,
                "current_plan_digest": "c" * 64,
                "execution_granted": False,
            }
        if route.endswith("/experiments"):
            return {
                "id": "experiment-one",
                "current_design_revision": 1,
                "design": {
                    "variants": [
                        {"id": f"variant-{v['kind']}", "kind": v["kind"], "revision": 1}
                        for v in body["variants"]
                    ]
                },
            }
        if route.endswith("/execution-grants"):
            return {"id": f"grant-{body['task_id']}"}
        if route.endswith("/derived-measurements"):
            return {"id": "measurement-one", "value": 385, "artifact_id": "artifact-one"}
        return {"id": route.split("/")[2]}

    monkeypatch.setattr(runner, "_api", api)
    result = runner.admit(manifest, receipt, "not-a-real-token")
    assert result["state"] == "admitted_not_dispatched"
    assert len(result["attempt_ids"]) == 1
    baseline_attempt_id = result["attempt_ids"]["baseline"]
    assert receipt.stat().st_mode & 0o077 == 0
    first = [call for call in calls if call[0] != "GET"]
    runner.admit(manifest, receipt, "not-a-real-token")
    assert [call for call in calls if call[0] != "GET"] == first
    with pytest.raises(runner.AdmissionError, match="reconciled"):
        runner.admit(manifest, receipt, "not-a-real-token", phase="intervention")
    assert [call for call in calls if call[0] != "GET"] == first
    grant_status = "reconciled"
    with pytest.raises(runner.AdmissionError, match="measurement"):
        runner.admit(manifest, receipt, "not-a-real-token", phase="intervention")
    measurement = True
    derived = runner.admit(manifest, receipt, "not-a-real-token", phase="derive-baseline")
    assert derived["measurement_id"] == "measurement-one"
    assert [call for call in calls if call[1].endswith("/derived-measurements")][0][2] == {
        "expected_revision": 5,
        "name": "sum_squares",
        "artifact_id": "artifact-one",
        "json_pointer": "/sum_squares",
    }
    second = runner.admit(manifest, receipt, "not-a-real-token", phase="intervention")
    assert len(second["attempt_ids"]) == 2
    grant_calls = [call for call in calls if call[1].endswith("/execution-grants")]
    assert len(grant_calls) == 2
    assert grant_calls[0][2]["expires_at"] != grant_calls[1][2]["expires_at"]
    runner.admit(manifest, receipt, "not-a-real-token", phase="intervention")
    assert len([call for call in calls if call[1].endswith("/execution-grants")]) == 2
    with pytest.raises(runner.AdmissionError, match="does not match"):
        runner.admit(
            {
                **manifest,
                "intervention_argv": ["/hello", "--cpu-sum-squares", "30", "/outputs/result.txt"],
            },
            receipt,
            "not-a-real-token",
        )


def test_unconfigured_limits_installed_once_and_conflict_blocks(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = _manifest()
    calls = []

    def api(origin, token, method, route, body=None, key=None):
        calls.append((method, route, body))
        if route.endswith("/execution-limits"):
            if method == "GET":
                return {"not_configured": True}
            return {"currency": "USD", "monetary_limit_cents": 0, "max_concurrency": 1}
        if method == "GET":
            return {"digest": manifest["snapshot_digest"], "project_id": manifest["project_id"]}
        raise runner.AdmissionError("stop after limits for test")

    monkeypatch.setattr(runner, "_api", api)
    receipt = tmp_path / "receipt.json"
    for _ in range(2):
        with pytest.raises(runner.AdmissionError, match="stop after limits"):
            runner.admit(manifest, receipt, "test-token")
    limits_writes = [call for call in calls if call[0] == "PUT"]
    assert len(limits_writes) == 1
    assert limits_writes[0][2] == {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 0,
        "max_concurrency": 1,
    }

    def conflict(origin, token, method, route, body=None, key=None):
        if route.endswith("/execution-limits"):
            return {"currency": "USD", "monetary_limit_cents": 0, "max_concurrency": 2}
        return {"digest": manifest["snapshot_digest"], "project_id": manifest["project_id"]}

    monkeypatch.setattr(runner, "_api", conflict)
    with pytest.raises(runner.AdmissionError, match="one concurrent grant"):
        runner.admit(manifest, tmp_path / "other.json", "test-token")
