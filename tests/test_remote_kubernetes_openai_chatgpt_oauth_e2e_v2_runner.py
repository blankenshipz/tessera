"""Deterministic safety coverage for Astra's opt-in v2 live-profile runner."""

# ruff: noqa: E501, E701, E702
# fmt: off

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
from argparse import Namespace
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runner_module():
    path = Path(__file__).parents[1] / "scripts" / "check_remote_kubernetes_openai_chatgpt_oauth_e2e_v2.py"
    spec = importlib.util.spec_from_file_location("remote_kubernetes_oauth_v2_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    return module


def _args(tmp_path: Path) -> Namespace:
    kubeconfig = tmp_path / "kubeconfig"; kubeconfig.write_text("fixture")
    return Namespace(preflight=True, run=False, cancel=False, kubeconfig=kubeconfig, context="remote-e2e", namespace="tessera-e2e", runner_deployment="oauth-runner", service_account="tessera-live", image="registry.example/tessera-runner@sha256:" + "a" * 64, oauth_owner_id="owner-1", oauth_runtime_ref="openai-chatgpt-oauth://owner-1/reference-42", model="gpt-5.6", operation_id="b" * 32, journal=tmp_path / "v2.sqlite3", timeout=600, disconnect_seconds=15, max_turn_tokens=2_000, max_total_tokens=20_000, max_turn_seconds=60, confirm_manifest_digest=None, confirm_provider_calls=None)


def _target(module):
    return module.Target(Path("/tmp/kubeconfig"), "remote-e2e", "tessera-e2e", "https://cluster.example", "a" * 64, "cluster-uid", "namespace-uid", "c" * 64)


def _attestation(args: Namespace, module, *, usage_known: bool = True, turn_tokens: int = 7) -> dict[str, object]:
    conversation, invocation = module.LiveProfileJournal._identity(args)
    turns = [{"turn_number": number, "model": args.model, "model_calls": 1, "tool_calls": ["read_experiment_status"], "elapsed_seconds": 1, "usage": {"total_tokens": turn_tokens}} for number in range(1, 11)]
    return {"operation_id": args.operation_id, "conversation_id": conversation, "invocation_id": invocation, "job_uid": "job-uid", "provider": module.PROVIDER, "model": args.model, "turn_count": 10, "model_calls": 10, "billing_mode": "chatgpt_oauth_subscription_only", "api_key_used": False, "fallback_used": False, "top_up_used": False, "automatic_retries_used": False, "shell_used": False, "mcp_used": False, "skills_used": False, "remote_workflow_protocol": module.PROTOCOL, "usage_known": usage_known, "turns": turns, "usage": {"total_tokens": 10 * turn_tokens, "elapsed_seconds": 10}}


def test_manifest_binds_ten_turn_limits_subscription_metadata_and_only_status_read(tmp_path: Path, runner_module) -> None:
    args = _args(tmp_path); manifest = runner_module._manifest(args, _target(runner_module))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    environment = {entry["name"]: entry for entry in container["env"]}
    command = container["command"]
    assert command[command.index("--turn-count") + 1] == "10"
    assert command[command.index("--max-model-calls-per-turn") + 1] == "1"
    assert command[command.index("--max-model-calls") + 1] == "10"
    assert command[command.index("--max-turn-tokens") + 1] == "2000"
    assert command[command.index("--max-total-tokens") + 1] == "20000"
    assert environment["TESSERA_ALLOWED_TOOLS"]["value"] == "read_experiment_status"
    assert environment["TESSERA_MAX_MODEL_CALLS_PER_TURN"]["value"] == "1"
    assert environment["TESSERA_MAX_MODEL_CALLS"]["value"] == "10"
    assert environment["TESSERA_BILLING_MODE"]["value"] == "chatgpt_oauth_subscription_only"
    assert environment["TESSERA_KUBERNETES_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert "TESSERA_KUBERNETES_JOB_UID" not in environment
    assert all(environment[key]["value"] == "true" for key in ("TESSERA_DISABLE_SHELL", "TESSERA_DISABLE_MCP", "TESSERA_DISABLE_SKILLS", "TESSERA_DISABLE_AUTOMATIC_RETRIES"))
    assert "OPENAI_API_KEY" not in environment
    assert manifest["metadata"]["annotations"]["tessera.dev/spec-sha256"] == runner_module._manifest_digest(manifest)
    with pytest.raises(runner_module.LiveOAuthV2Error, match="per-turn"):
        runner_module._validate_attestation(args, _attestation(args, runner_module, turn_tokens=2_001))


def test_job_owner_uid_and_pod_uid_are_distinct_and_both_attested(tmp_path: Path, runner_module, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path); name = "fixture-job"
    pod = {"items": [{"metadata": {"uid": "pod-uid", "ownerReferences": [{"kind": "Job", "name": name, "uid": "job-uid", "controller": True}]}}]}
    monkeypatch.setattr(runner_module, "_json_command", lambda *_args: pod)

    assert runner_module._owner_job_and_pod_uid(args, name, "job-uid") == ("job-uid", "pod-uid")
    conversation, invocation = runner_module.LiveProfileJournal._identity(args)
    spoofed = _attestation(args, runner_module)
    spoofed["job_uid"] = "pod-uid"
    spoofed["pod_uid"] = "pod-uid"
    def logs(_args, *parts, **_kwargs):
        class Completed: stdout = json.dumps(spoofed); returncode = 0; stderr = ""
        return Completed()
    monkeypatch.setattr(runner_module, "_kubectl", logs)
    with pytest.raises(runner_module.LiveOAuthV2Error, match="did not attest"):
        runner_module._attestation(args, name, conversation, invocation, "job-uid", "pod-uid")


def test_confirmation_is_one_use_and_atomic_under_concurrent_claims(tmp_path: Path, runner_module) -> None:
    args = _args(tmp_path); target = _target(runner_module); digest = "d" * 64
    with runner_module.LiveProfileJournal(args.journal) as journal: journal.preflight(args, target, digest)
    args.confirm_manifest_digest = digest; args.confirm_provider_calls = f"TEN_OAUTH_MODEL_CALLS:{digest}"
    barrier, results = threading.Barrier(2), []

    def claim() -> None:
        try:
            with runner_module.LiveProfileJournal(args.journal) as journal:
                barrier.wait(); results.append(("claimed", journal.reserve_and_claim_confirmation(args, digest)))
        except Exception as error: results.append(("rejected", str(error)))

    left, right = threading.Thread(target=claim), threading.Thread(target=claim)
    left.start(); right.start(); left.join(); right.join()
    assert [result[0] for result in results].count("claimed") == 1
    assert [result[0] for result in results].count("rejected") == 1
    with runner_module.LiveProfileJournal(args.journal) as journal:
        with pytest.raises(runner_module.LiveOAuthV2Error, match="already claimed"):
            journal.reserve_and_claim_confirmation(args, digest)


def test_lost_create_effect_records_reconciliation_without_resubmit(tmp_path: Path, runner_module, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path); target = _target(runner_module); manifest = runner_module._manifest(args, target); digest = runner_module._manifest_digest(manifest)
    args.confirm_manifest_digest = digest; args.confirm_provider_calls = f"TEN_OAUTH_MODEL_CALLS:{digest}"
    monkeypatch.setattr(runner_module, "_preflight", lambda _args: (target, manifest, digest))
    monkeypatch.setattr(runner_module, "_existing_job", lambda *_args: False)
    calls: list[tuple[str, ...]] = []
    def lost_create(_args, *parts, **_kwargs):
        calls.append(parts)
        raise ConnectionError("lost create response")
    monkeypatch.setattr(runner_module, "_kubectl", lost_create)
    with pytest.raises(ConnectionError, match="lost create"):
        runner_module._run_live(args)
    assert sum(parts[:1] == ("create",) for parts in calls) == 1
    with sqlite3.connect(args.journal) as connection:
        assert connection.execute("SELECT state,reconciliation_required,reservation_state FROM live_profile_v2_runs").fetchone() == ("reconciliation_required", 1, "unknown")


def test_restart_and_cancellation_fence_submit_and_do_not_terminate_provider(tmp_path: Path, runner_module) -> None:
    args = _args(tmp_path); target = _target(runner_module); digest = "d" * 64
    with runner_module.LiveProfileJournal(args.journal) as journal: journal.preflight(args, target, digest)
    args.confirm_manifest_digest = digest; args.confirm_provider_calls = f"TEN_OAUTH_MODEL_CALLS:{digest}"
    with runner_module.LiveProfileJournal(args.journal) as journal: journal.reserve_and_claim_confirmation(args, digest)
    with runner_module.LiveProfileJournal(args.journal) as journal:
        with pytest.raises(runner_module.LiveOAuthV2Error, match="no longer runnable"):
            journal.require_preflight(args, target, digest)
        journal.cancel(args.operation_id)
    with sqlite3.connect(args.journal) as connection:
        assert connection.execute("SELECT state FROM live_profile_v2_runs").fetchone() == ("cancel_requested",)
        assert connection.execute("SELECT classification,evidence_json FROM live_profile_v2_evidence").fetchone()[0] == "cancellation_requested"


def test_usage_unknown_and_evidence_classification_fail_closed(tmp_path: Path, runner_module) -> None:
    args = _args(tmp_path)
    with pytest.raises(runner_module.LiveOAuthV2Error, match="usage is unknown"):
        runner_module._validate_attestation(args, _attestation(args, runner_module, usage_known=False))
    target, digest = _target(runner_module), "d" * 64
    with runner_module.LiveProfileJournal(args.journal) as journal:
        journal.preflight(args, target, digest)
        journal.record_turns(args.operation_id, runner_module._validate_attestation(args, _attestation(args, runner_module)))
        journal.evidence(args.operation_id, "local_observation_paused", {"remote_progress_not_observed_during_pause": True})
        journal.evidence(args.operation_id, "remote_progress", {"same_job_uid": True})
    with sqlite3.connect(args.journal) as connection:
        assert connection.execute("SELECT count(*) FROM live_profile_v2_turns WHERE state='observed'").fetchone() == (10,)
        assert [row[0] for row in connection.execute("SELECT classification FROM live_profile_v2_evidence ORDER BY id")] == ["local_observation_paused", "remote_progress"]


def test_duplicate_probe_is_read_only_and_never_creates_a_job(tmp_path: Path, runner_module, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path); calls: list[tuple[str, ...]] = []
    class Completed: returncode = 1; stdout = ""; stderr = "NotFound"
    def observed(_args, *parts, **_kwargs): calls.append(parts); return Completed()
    monkeypatch.setattr(runner_module, "_kubectl", observed)
    assert runner_module._existing_job(args, "fixture") is False
    assert calls == [("get", "job", "fixture", "-n", args.namespace)]


def test_post_submit_report_exception_is_reconciliation_not_a_retry(tmp_path: Path, runner_module, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path); target = _target(runner_module); manifest = runner_module._manifest(args, target); digest = runner_module._manifest_digest(manifest)
    args.confirm_manifest_digest = digest; args.confirm_provider_calls = f"TEN_OAUTH_MODEL_CALLS:{digest}"
    monkeypatch.setattr(runner_module, "_preflight", lambda _args: (target, manifest, digest))
    monkeypatch.setattr(runner_module, "_existing_job", lambda *_args: False)
    monkeypatch.setattr(runner_module, "_wait_for_start", lambda *_args: {"metadata": {"uid": "job-uid"}})
    monkeypatch.setattr(runner_module, "_owner_job_and_pod_uid", lambda *_args: ("job-uid", "pod-uid"))
    monkeypatch.setattr(runner_module, "_assert_one_identity", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_assert_no_duplicate_execution", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_job", lambda *_args: {"metadata": {"uid": "job-uid"}})
    states = iter(("running", "succeeded")); monkeypatch.setattr(runner_module, "_job_state", lambda *_args: next(states))
    monkeypatch.setattr(runner_module, "_attestation", lambda *_args: (_ for _ in ()).throw(ConnectionError("lost report acknowledgement")))
    class Completed: returncode = 0; stdout = ""; stderr = ""
    calls: list[tuple[str, ...]] = []
    def commands(_args, *parts, **_kwargs): calls.append(parts); return Completed()
    monkeypatch.setattr(runner_module, "_kubectl", commands)
    with pytest.raises(ConnectionError, match="lost report"):
        runner_module._run_live(args)
    assert sum(parts[:1] == ("create",) for parts in calls) == 1
    with sqlite3.connect(args.journal) as connection:
        assert connection.execute("SELECT state FROM live_profile_v2_runs").fetchone() == ("reconciliation_required",)


def test_default_cli_is_disabled_without_kubernetes(runner_module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["runner"])
    assert runner_module.main() == 1
    assert json.loads(capsys.readouterr().out)["fixture_verdict"] == "skipped"
