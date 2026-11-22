"""Pure run-receipt builder tests and a round trip through the control model."""

from __future__ import annotations

import hashlib
import json

import pytest

from tessera_control.execution_run_receipts import ReportRunReceipt
from tessera_worker.run_receipt import (
    RunReceiptError,
    TerminalFacts,
    build_run_receipt,
    canonical_json,
    command_digest,
    idempotency_key,
    parse_docker_time,
    terminal_facts,
)

IMAGE = "registry.example/tool@sha256:" + "a" * 64
OPERATION = "b" * 32
PLAN = "c" * 64
MANIFEST = "d" * 64  # control's manifest digest (the top-level output_digest)
SEALED = "5" * 64  # the worker's local seal digest


def inspect_reply(**state) -> str:
    base = {
        "Status": "exited",
        "Running": False,
        "ExitCode": 3,
        "StartedAt": "2026-01-01T00:00:00.123456789Z",
        "FinishedAt": "2026-01-01T00:00:05Z",
    }
    base.update(state)
    return json.dumps(
        [
            {
                "Id": "cid",
                "Image": "sha256:" + "e" * 64,
                "Config": {
                    "Labels": {"tessera.operation": OPERATION, "tessera.plan": PLAN},
                    "Env": ["SECRET=value"],
                },
                "State": base,
                "HostConfig": {"Binds": ["/private/tmp/host-path:/out"]},
            }
        ]
    )


def facts(**state) -> TerminalFacts:
    return terminal_facts(
        inspect_reply(**state),
        operation_id=OPERATION,
        plan_digest=PLAN,
        container_id="cid",
        exit_code=state.get("ExitCode", 3),
    )


def body(**overrides):
    values = {
        "lease_id": "lease-1",
        "epoch": 2,
        "operation_id": OPERATION,
        "plan_digest": PLAN,
        "image": IMAGE,
        "argv": ("python", "-c", "print('é')"),
        "network_profile": "none",
        "facts": facts(),
        "control_manifest_digest": MANIFEST,
        "sealed_manifest_digest": SEALED,
    }
    values.update(overrides)
    return build_run_receipt(**values)


def test_body_is_verifiable_pathless_and_matches_the_control_model():
    result = body()
    model = ReportRunReceipt(**result)  # the control side's own validation
    canonical = canonical_json(result["receipt_payload"])
    assert hashlib.sha256(canonical.encode()).hexdigest() == result["receipt_digest"]
    assert len(canonical.encode()) <= 65536
    assert model.provider_id == "cid" and model.exit_code == 3
    assert (
        result["command_digest"]
        == hashlib.sha256(b'["python","-c","print(\'\\u00e9\')"]').hexdigest()
    )
    assert result["output_digest"] == MANIFEST
    assert result["receipt_payload"]["sealed_manifest_digest"] == SEALED
    assert "output_manifest_digest" not in result["receipt_payload"]
    assert result["started_at"] == "2026-01-01T00:00:00.123456+00:00"
    assert set(result) == {
        "lease_id",
        "expected_epoch",
        "provider_id",
        "command_digest",
        "exit_code",
        "started_at",
        "finished_at",
        "output_digest",
        "receipt_payload",
        "receipt_digest",
    }
    payload = result["receipt_payload"]
    assert payload["image_digest"] == "a" * 64 and payload["image_id"] == "e" * 64
    assert payload["termination_confirmed"] is True
    assert payload["adapter_version"] == "docker-local-cpu-v1"
    text = json.dumps(result)
    for forbidden in ("/private", "host-path", "SECRET", "print(", "registry.example"):
        assert forbidden not in text


def test_idempotency_key_is_deterministic_and_identity_bound():
    key = idempotency_key(OPERATION, "lease-1", 2)
    assert key == idempotency_key(OPERATION, "lease-1", 2)
    assert key != idempotency_key(OPERATION, "lease-1", 3)
    assert key.isascii() and len(key) < 200


@pytest.mark.parametrize(
    "state",
    [
        {"StartedAt": "0001-01-01T00:00:00Z"},
        {"FinishedAt": "not-a-time"},
        {"Running": True},
        {"Status": "running"},
        {"ExitCode": 9},  # disagrees with the confirmed exit_code (3)
    ],
)
def test_unconfirmed_or_disagreeing_inspect_yields_no_facts(state):
    with pytest.raises(RunReceiptError):
        terminal_facts(
            inspect_reply(**state),
            operation_id=OPERATION,
            plan_digest=PLAN,
            container_id="cid",
            exit_code=3,
        )


def test_wrong_container_or_labels_yield_no_facts():
    for kwargs in (
        {"container_id": "other"},
        {"operation_id": "f" * 32},
        {"plan_digest": "0" * 64},
    ):
        values = {
            "operation_id": OPERATION,
            "plan_digest": PLAN,
            "container_id": "cid",
            "exit_code": 3,
        } | kwargs
        with pytest.raises(RunReceiptError):
            terminal_facts(inspect_reply(), **values)


def test_builder_never_guesses_missing_facts():
    with pytest.raises(RunReceiptError):
        body(sealed_manifest_digest="")
    with pytest.raises(RunReceiptError):
        body(control_manifest_digest="")
    with pytest.raises(RunReceiptError):
        body(control_manifest_digest=None)
    with pytest.raises(RunReceiptError):
        body(image="registry.example/tool:latest")
    with pytest.raises(RunReceiptError):
        body(argv=())
    with pytest.raises(RunReceiptError):
        parse_docker_time(None)
    assert command_digest(["a"]) != command_digest(["a", ""])


def test_worker_and_control_argv_digests_share_vectors():
    from tessera_control.execution_run_receipts import argv_digest

    for argv in (["a"], ["python", "-c", "print('é')"], ["a", ""], ["x y", '"q"', "\\n"]):
        assert argv_digest(list(argv)) == command_digest(argv)
    assert (
        command_digest(["python", "-c", "print('é')"])
        == hashlib.sha256(b'["python","-c","print(\'\\u00e9\')"]').hexdigest()
    )
