# ruff: noqa: F811
"""An off-allowlist reported transfer fails the attempt (ADR 0134); fakes only, no network."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from test_campaign_public_artifact_salad import SHA, URL, receipt_stage, world  # noqa: F401

from tessera_control import campaign_receipts
from tessera_control.campaign_models import FAILURE_CLASSES
from tessera_control.campaign_providers import Observation
from tessera_control.campaign_receipts import public_fetch_entry
from tessera_control.campaign_runner import CampaignRunner

GOOD = {"url": URL, "sha256": SHA, "bytes": 900, "seconds": 1.5}
OFF = {"url": "https://elsewhere.example.org/w.bin", "sha256": SHA, "bytes": 5, "seconds": 0.1}
PASSED = Observation("succeeded", evidence_kind="run_receipt", evidence_digest="e" * 64)


def _observe(monkeypatch, world, rows, observed=PASSED):
    provider = SimpleNamespace(
        observe=lambda services, attempt: observed,
        retained_fetched=lambda connection, attempt: rows,
    )
    monkeypatch.setattr(campaign_receipts, "stage_provider", lambda provider_id: provider)

    @contextmanager
    def connection():
        yield None

    runner = SimpleNamespace(
        _provider=lambda spec_stage: provider,
        store=SimpleNamespace(runner_connection=connection),
    )
    stage = receipt_stage(world)
    attempt = {"provider_id": "salad"}
    return CampaignRunner._observation(runner, stage, attempt), stage


def test_allowed_transfer_passes(monkeypatch, world):
    observed, _ = _observe(monkeypatch, world, [GOOD])
    assert observed.outcome == "succeeded" and observed.failure_class is None


def test_no_reported_transfer_passes(monkeypatch, world):
    observed, _ = _observe(monkeypatch, world, None)
    assert observed.outcome == "succeeded"


def test_off_list_host_fails_the_attempt_permanently(monkeypatch, world):
    observed, _ = _observe(monkeypatch, world, [GOOD, OFF])
    assert (observed.outcome, observed.failure_class) == ("failed_permanent", "egress_violation")
    assert "elsewhere.example.org" in observed.detail and "/w.bin" not in observed.detail
    assert observed.failure_class in FAILURE_CLASSES


def test_unpinned_file_on_an_allowed_host_fails_the_attempt(monkeypatch, world):
    row = {**GOOD, "sha256": "d" * 64}
    observed, _ = _observe(monkeypatch, world, [row])
    assert observed.failure_class == "egress_violation"


def test_a_provider_failure_is_left_as_it_is(monkeypatch, world):
    failed = Observation("failed_transient", "salad_not_passed", "x")
    observed, _ = _observe(monkeypatch, world, [OFF], failed)
    assert observed is failed


def test_receipt_still_records_the_violation(monkeypatch, world):
    _, stage = _observe(monkeypatch, world, [GOOD, OFF])
    entry = public_fetch_entry("salad", stage, [GOOD, OFF])
    assert entry["egress_verdict"] == "violation"
    assert entry["refused_transfers"] == [
        {"host": "elsewhere.example.org", "reason": "not_allowlisted"}
    ]


def test_report_names_the_failure_class():
    from tessera_control.campaign_report import _FAILURES

    assert "egress_violation" in _FAILURES
