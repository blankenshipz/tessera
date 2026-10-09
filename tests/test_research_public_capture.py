"""Synthetic eligible producer; actual private capture journal and supervisor seam."""

from types import SimpleNamespace

import pytest
from test_public_capture_outbox import Client, adapter, binding, observation  # noqa: F401
from test_remote_reasoning_runtime import runtime  # noqa: F401

from tessera_agent.remote_reasoning_runtime import RuntimeBlocked
from tessera_agent.research_public_capture import PublicStatusCapture
from tessera_control.public_capture_outbox import PublicCaptureOutbox


def test_capture_callsite_preserves_unknown_coverage_and_ack(tmp_path, request):
    pins = request.getfixturevalue("binding")
    event = observation(pins, capture_state="unknown")
    outbox = PublicCaptureOutbox(tmp_path / "capture", adapter(pins))
    capture = PublicStatusCapture(
        SimpleNamespace(read_public_metadata=lambda row: [event]), outbox, Client()
    )
    capture.after_status(
        {"id": pins["run_id"], "session_id": pins["session_id"]},
        {"tokens": 1, "cost": 0, "outcome": None},
    )
    watermarks = outbox.watermarks()
    assert watermarks["acknowledged_events"] == 1
    assert watermarks["unknown_capture_events"] == 1
    outbox.close()


def test_actual_supervisor_fences_capture_before_terminal_completion(request):
    store, supervisor, harness, grant, _ = request.getfixturevalue("runtime")
    calls = []

    def after_status(row, observation):
        calls.append((row["session_id"], observation["outcome"]))
        grant["status"] = "revoked"

    supervisor.public_capture = SimpleNamespace(after_status=after_status)
    harness.observation["outcome"] = "succeeded"
    result = supervisor.tick("run1", "bounded goal")
    assert result["state"] == "stop_acknowledged" and result["reason"] == "grant_revoked"
    assert len(calls) == 1 and harness.calls == ["create", "prompt", "interrupt"]


def test_actual_supervisor_unknown_capture_does_not_mark_completed(request):
    store, supervisor, harness, grant, _ = request.getfixturevalue("runtime")

    def after_status(row, observation):
        raise ValueError("synthetic private source error")

    supervisor.public_capture = SimpleNamespace(after_status=after_status)
    harness.observation["outcome"] = "succeeded"
    result = supervisor.tick("run1", "bounded goal")
    assert result["state"] == "stop_acknowledged" and "private" not in result["reason"]


def test_source_cannot_capture_another_runtime_session(tmp_path, request):
    pins = request.getfixturevalue("binding")
    outbox = PublicCaptureOutbox(tmp_path / "capture", adapter(pins))
    capture = PublicStatusCapture(
        SimpleNamespace(read_public_metadata=lambda row: [observation(pins)]), outbox, Client()
    )
    with pytest.raises(RuntimeBlocked, match="public_capture_source_or_delivery_unknown"):
        capture.after_status({"id": pins["run_id"], "session_id": "foreign"}, {})
    assert outbox.watermarks()["captured_events"] == 0
    outbox.close()
