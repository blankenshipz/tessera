"""Deterministic boundaries for the opt-in remote OAuth Kubernetes runner."""

# ruff: noqa: E501
# fmt: off

from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runner_module():
    path = Path(__file__).parents[1] / "scripts" / "check_remote_kubernetes_oauth_reconnect_e2e.py"
    spec = importlib.util.spec_from_file_location("remote_kubernetes_oauth_reconnect_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(tmp_path: Path) -> Namespace:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("not-read-by-unit-test")
    return Namespace(
        preflight=True,
        run=False,
        kubeconfig=kubeconfig,
        context="remote-e2e",
        namespace="tessera-e2e",
        runner_deployment="oauth-runner",
        service_account="tessera-live",
        image="registry.example/tessera-runner@sha256:" + "a" * 64,
        oauth_owner_id="owner-1",
        oauth_runtime_ref="openai-chatgpt-oauth://owner-1/reference-42",
        model="gpt-5.6",
        operation_id="b" * 32,
        journal=tmp_path / "live-profile.sqlite3",
        timeout=180,
        disconnect_seconds=15,
        confirm_manifest_digest=None,
        confirm_provider_call=None,
    )


def _target(module) -> object:
    return module.Target(
        Path("/tmp/kubeconfig"),
        "remote-e2e",
        "tessera-e2e",
        "https://cluster.example",
        "a" * 64,
        "cluster-uid",
        "namespace-uid",
        "c" * 64,
    )


def test_manifest_is_pinned_one_call_and_cannot_enable_api_key_fallback_or_top_up(
    tmp_path: Path, runner_module
) -> None:
    args = _args(tmp_path)
    manifest = runner_module._manifest(args, _target(runner_module))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    environment = {entry["name"]: entry["value"] for entry in container["env"]}

    assert container["image"] == args.image
    assert "OPENAI_API_KEY" not in environment
    assert environment["TESSERA_ALLOW_API_KEYS"] == "false"
    assert environment["TESSERA_ALLOW_PROVIDER_FALLBACK"] == "false"
    assert environment["TESSERA_ALLOW_TOP_UP"] == "false"
    assert environment["TESSERA_MAX_MODEL_CALLS"] == "1"
    assert container["command"][container["command"].index("--max-model-calls") + 1] == "1"
    assert manifest["metadata"]["annotations"]["tessera.dev/spec-sha256"] == runner_module._manifest_digest(manifest)


def test_owner_mismatch_in_runtime_reference_fails_closed(tmp_path: Path, runner_module) -> None:
    args = _args(tmp_path)
    args.oauth_runtime_ref = "openai-chatgpt-oauth://different-owner/reference-42"

    assert "openai-chatgpt-oauth" in runner_module._validate_args(args)


def test_journal_requires_matching_preflight_before_run(tmp_path: Path, runner_module) -> None:
    args = _args(tmp_path)
    target = _target(runner_module)
    digest = "d" * 64
    with runner_module.LiveProfileJournal(args.journal) as journal:
        with pytest.raises(runner_module.LiveOAuthE2EError, match="preflight record"):
            journal.require_preflight(args, target, digest)
        journal.preflight(args, target, digest)
        journal.require_preflight(args, target, digest)
        journal.preflight(args, target, digest)
        journal.usage(args.operation_id, {"total_tokens": 7})
        journal.reconciliation(args.operation_id, "job-uid", "succeeded", False, {"same_job_uid": True})

    import sqlite3

    connection = sqlite3.connect(args.journal)
    try:
        assert connection.execute("SELECT state FROM live_profile_runs").fetchone() == ("preflight_passed",)
        assert json.loads(connection.execute("SELECT usage_json FROM live_profile_usage").fetchone()[0]) == {"total_tokens": 7}
        assert connection.execute(
            "SELECT reconciliation_required FROM live_profile_reconciliation"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_duplicate_create_must_report_already_exists(tmp_path: Path, runner_module, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    manifest = runner_module._manifest(args, _target(runner_module))

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(runner_module, "_kubectl", lambda *_args, **_kwargs: Completed())
    with pytest.raises(runner_module.LiveOAuthE2EError, match="duplicate execution"):
        runner_module._assert_duplicate_rejected(args, manifest)


def test_cli_is_default_disabled_without_touching_kubernetes(
    runner_module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner"])

    assert runner_module.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "fixture_verdict": "skipped",
        "message": "no live mode was selected",
        "state": "not_started",
    }
