"""Built-in code review child tests: fake transport, placeholder key, no network."""

import base64
import hashlib
import io
import json
import math
import os
import sys
from urllib.parse import urlsplit

import pytest

from tessera_agent import code_review_child as child
from tessera_agent.anthropic_messages import API_URL, COUNT_URL
from tessera_agent.code_review_child import (
    CODE_REVIEW_PROMPT,
    CODE_REVIEW_PROMPT_SHA256,
    MAX_REVIEW_INPUT_BYTES,
    run_spec,
)
from tessera_control.review_jobs_models import ModelCallReceipt

PLACEHOLDER_KEY = "sk-ant-PLACEHOLDER-not-a-real-key-0123456789"
MODEL = "claude-sonnet-4-5"


def digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


RUBRIC, HEAD, BASE = digest("rubric"), digest("head"), digest("base")


def profile():
    return {
        "account_mode": "anthropic-api-key-ref",
        "model": {"providerID": "anthropic", "id": MODEL},
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "limits": {
            "max_output_tokens_per_call": 1000,
            "max_input_tokens_per_call": 200000,
            "max_cost_cents_per_call": 10,
            "daily_token_cap": 1000000,
            "daily_cost_cap_cents": 500,
        },
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
    }


def snapshot(snapshot_id, snapshot_digest, files, data_class="public"):
    return {
        "snapshot_id": snapshot_id,
        "digest": snapshot_digest,
        "data_class": data_class,
        "files": [
            {
                "path": path,
                "content_b64": base64.b64encode(
                    content if isinstance(content, bytes) else content.encode()
                ).decode(),
            }
            for path, content in files.items()
        ],
    }


def make_spec(tmp_path, *, mode="snapshot", **overrides):
    inputs = [
        snapshot("snap-rubric", RUBRIC, {"RUBRIC.md": "Check error handling.\n"}),
        snapshot(
            "snap-head",
            HEAD,
            {"a.py": "x = 2\n", "new.py": "y = 1\n"},
            data_class="private_code_approved",
        ),
    ]
    if mode == "diff":
        inputs.append(
            snapshot(
                "snap-base",
                BASE,
                {"a.py": "x = 1\n", "blob.bin": b"\xff\xfe\x00"},
                data_class="private_derived",
            )
        )
    spec = {
        "job_id": "job-1",
        "kind": "code_review",
        "attempt": 1,
        "tier": "sonnet",
        "budget_cents": 100,
        "egress": "model_runtime",
        "destination_id": "anthropic",
        "prompt_digest": CODE_REVIEW_PROMPT_SHA256,
        "credential_name": "anthropic-api-key",
        "code_review": {
            "mode": mode,
            "head_digest": HEAD,
            "base_digest": BASE if mode == "diff" else None,
            "rubric_digest": RUBRIC,
        },
        "inputs": inputs,
        "profile": profile(),
        "receipt_path": str(tmp_path / "out" / "receipt.json"),
    }
    (tmp_path / "out").mkdir(mode=0o700, exist_ok=True)
    spec.update(overrides)
    return spec


@pytest.fixture
def creds(tmp_path):
    directory = tmp_path / "creds"
    directory.mkdir(mode=0o700)
    path = directory / "anthropic-api-key"
    path.write_text(PLACEHOLDER_KEY + "\n")
    path.chmod(0o600)
    return directory


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, headers, body, *, timeout=None):
        self.calls.append((url, dict(headers), body))
        if not self.responses:
            raise AssertionError("unexpected model call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def count(n=100):
    return (200, {}, {"input_tokens": n})


def message(text, **usage):
    found = {"input_tokens": 100, "output_tokens": 50}
    found.update(usage)
    return (
        200,
        {"request-id": "req_upstream"},
        {
            "id": "msg_1",
            "model": MODEL,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": found,
        },
    )


FINDINGS = {
    "verdict": "CHANGES-REQUIRED",
    "findings": [
        {
            "severity": "major",
            "category": "correctness",
            "claim": "x changes without a test",
            "evidence": [{"source": "subject", "locator": "a.py:1"}],
            "confidence": 0.8,
        },
        {
            "severity": "info",
            "category": "style",
            "claim": "new module lacks a docstring",
            "evidence": [{"source": "subject", "locator": "new.py:1"}],
            "confidence": 0.5,
        },
    ],
}


def run(spec, creds, transport, clock=lambda: 1700000000.0):
    return run_spec(spec, transport=transport, clock=clock, credentials_dir=str(creds))


def assert_only_anthropic(transport):
    for url, _headers, _body in transport.calls:
        assert urlsplit(url).hostname == "api.anthropic.com"
        assert url in {COUNT_URL, API_URL}


def assert_clean(result):
    text = json.dumps(result)
    assert PLACEHOLDER_KEY not in text
    assert "req_upstream" not in text


def test_prompt_constant_is_pinned_and_generic():
    assert hashlib.sha256(CODE_REVIEW_PROMPT.encode()).hexdigest() == CODE_REVIEW_PROMPT_SHA256
    assert MAX_REVIEW_INPUT_BYTES == 600_000
    assert '"verdict"' in CODE_REVIEW_PROMPT and '"findings"' in CODE_REVIEW_PROMPT


def test_prompt_digest_mismatch_makes_no_call(tmp_path, creds):
    transport = FakeTransport()
    result = run(make_spec(tmp_path, prompt_digest=digest("other")), creds, transport)
    assert result == {"outcome": "failed", "reason": "prompt_not_pinned", "receipt": None}
    assert transport.calls == []


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"destination_id": "openai"}, "destination_not_supported"),
        ({"destination_id": None}, "destination_not_supported"),
        ({"egress": "none"}, "egress_not_permitted"),
        ({"egress": None}, "egress_not_permitted"),
    ],
)
def test_destination_and_egress_refusals_make_no_call(tmp_path, creds, override, reason):
    transport = FakeTransport()
    result = run(make_spec(tmp_path, **override), creds, transport)
    assert result == {"outcome": "failed", "reason": reason, "receipt": None}
    assert transport.calls == []


def test_input_too_large_makes_no_call(tmp_path, creds):
    spec = make_spec(tmp_path)
    big = "a" * (MAX_REVIEW_INPUT_BYTES + 1)
    spec["inputs"][1] = snapshot("snap-head", HEAD, {"big.py": big}, "private_code_approved")
    transport = FakeTransport()
    result = run(spec, creds, transport)
    assert result == {"outcome": "failed", "reason": "input_too_large", "receipt": None}
    assert transport.calls == []


def test_invalid_spec_makes_no_call(tmp_path, creds):
    spec = make_spec(tmp_path)
    spec["inputs"][1]["files"][0]["content_b64"] = "not base64!"
    transport = FakeTransport()
    assert run(spec, creds, transport)["reason"] == "spec_invalid"
    assert transport.calls == []


def test_budget_preflight_over_budget_never_posts_messages(tmp_path, creds):
    # Worst case: 100 input tokens at 300 + 1000 output tokens at 1500 cents/Mtok = 1.53 cents.
    transport = FakeTransport(count(100))
    result = run(make_spec(tmp_path, budget_cents=1), creds, transport)
    assert result == {"outcome": "failed", "reason": "budget_exceeded", "receipt": None}
    assert [call[0] for call in transport.calls] == [COUNT_URL]
    assert not os.path.exists(tmp_path / "out" / "receipt.json")


def check_receipt(result_receipt, spec):
    receipt = ModelCallReceipt.model_validate(result_receipt)
    assert receipt.model_configured == f"anthropic/{MODEL}"
    assert receipt.model_reported == f"anthropic/{MODEL}"
    assert receipt.model_identity_basis == "provider_response_model"
    assert receipt.cost_basis == "computed_from_pinned_prices"
    assert receipt.input_digests == {i["snapshot_id"]: i["digest"] for i in spec["inputs"]}
    assert receipt.cost_cents == math.ceil(receipt.cost)
    assert receipt.attempts == spec["attempt"]
    assert receipt.tier == spec["tier"]
    assert receipt.limits == spec["profile"]["limits"]
    assert receipt.prompt_sha256 == CODE_REVIEW_PROMPT_SHA256
    return receipt


def check_receipt_file(path, receipt):
    assert os.stat(path).st_mode & 0o777 == 0o600
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == receipt


def test_snapshot_review_completes_with_numbered_findings_and_receipt(tmp_path, creds):
    completion = json.dumps(FINDINGS)
    transport = FakeTransport(count(100), message(completion))
    spec = make_spec(tmp_path)
    result = run(spec, creds, transport)
    assert result["outcome"] == "completed"
    output = result["output"]
    assert output["verdict"] == "CHANGES-REQUIRED"
    assert [f["id"] for f in output["findings"]] == ["1", "2"]
    assert output["findings"][0]["claim"] == "x changes without a test"
    receipt = check_receipt(output["receipt"], spec)
    assert receipt.input_class == "private_code_approved"
    # 100 input at 300 + 50 output at 1500 cents/Mtok = 0.105 cents -> 1 cent.
    assert receipt.cost == pytest.approx(0.105) and receipt.cost_cents == 1
    assert receipt.tokens == {
        "input": 100,
        "output": 50,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 150,
    }
    assert receipt.output_sha256 == hashlib.sha256(completion.encode()).hexdigest()
    assert receipt.observed_at == 1700000000.0
    check_receipt_file(spec["receipt_path"], output["receipt"])
    assert_only_anthropic(transport)
    assert [call[0] for call in transport.calls] == [COUNT_URL, API_URL]
    sent = transport.calls[1][2]["messages"][0]["content"]
    assert sent.startswith(CODE_REVIEW_PROMPT)
    assert sent.index("Check error handling.") < sent.index("x = 2")
    assert "y = 1" in sent
    assert_clean(result)


def test_diff_review_sends_unified_diff_and_most_private_class(tmp_path, creds):
    completion = "```json\n" + json.dumps(FINDINGS) + "\n```"
    transport = FakeTransport(count(100), message(completion))
    spec = make_spec(tmp_path, mode="diff")
    result = run(spec, creds, transport)
    assert result["outcome"] == "completed"
    assert [f["id"] for f in result["output"]["findings"]] == ["1", "2"]
    receipt = check_receipt(result["output"]["receipt"], spec)
    assert receipt.input_class == "private_derived"
    assert set(receipt.input_digests.values()) == {RUBRIC, HEAD, BASE}
    sent = transport.calls[1][2]["messages"][0]["content"]
    assert "-x = 1" in sent and "+x = 2" in sent and "+y = 1" in sent
    assert "blob.bin (binary, skipped)" in sent
    assert_only_anthropic(transport)
    assert_clean(result)


def test_receipt_is_written_before_parsing_invalid_output(tmp_path, creds):
    transport = FakeTransport(count(100), message("I think it looks fine."))
    spec = make_spec(tmp_path)
    result = run(spec, creds, transport)
    assert result["outcome"] == "failed"
    assert result["reason"] == "failed_output_invalid"
    check_receipt(result["receipt"], spec)
    check_receipt_file(spec["receipt_path"], result["receipt"])
    assert "looks fine" not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        {"verdict": "SHIP-IT", "findings": []},
        {"findings": []},
        {"verdict": "MERGE", "findings": [{"severity": "huge"}]},
        {"verdict": "MERGE", "findings": "none"},
        [],
    ],
)
def test_invalid_output_shapes_are_refused_with_receipt(tmp_path, creds, body):
    transport = FakeTransport(count(100), message(json.dumps(body)))
    result = run(make_spec(tmp_path), creds, transport)
    assert result["reason"] == "failed_output_invalid"
    assert result["receipt"] is not None


def test_transport_500_after_count_fails_without_receipt(tmp_path, creds):
    transport = FakeTransport(
        count(100), (500, {}, {"error": {"type": "api_error", "message": "upstream detail"}})
    )
    spec = make_spec(tmp_path)
    result = run(spec, creds, transport)
    assert result == {"outcome": "failed", "reason": "model_failed", "receipt": None}
    assert not os.path.exists(spec["receipt_path"])
    assert "upstream detail" not in json.dumps(result)
    assert_only_anthropic(transport)


def test_missing_credential_makes_no_call(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    transport = FakeTransport()
    result = run(make_spec(tmp_path), empty, transport)
    assert result == {"outcome": "failed", "reason": "model_failed", "receipt": None}
    assert transport.calls == []


def test_crash_after_billed_call_still_returns_receipt(tmp_path, creds, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("parser crashed")

    monkeypatch.setattr(child, "parse_review_output", boom)
    transport = FakeTransport(count(100), message(json.dumps(FINDINGS)))
    spec = make_spec(tmp_path)
    result = run(spec, creds, transport)
    assert result["outcome"] == "failed" and result["reason"] == "model_failed"
    check_receipt(result["receipt"], spec)
    check_receipt_file(spec["receipt_path"], result["receipt"])


def feed_stdin(monkeypatch, data: bytes):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def test_main_prints_one_object_without_key(tmp_path, creds, monkeypatch, capsys):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    feed_stdin(monkeypatch, json.dumps(make_spec(tmp_path)).encode())
    transport = FakeTransport(count(100), message(json.dumps(FINDINGS)))
    assert child.main(transport=transport) == 0
    out, err = capsys.readouterr()
    body = json.loads(out)
    assert body["outcome"] == "completed"
    assert PLACEHOLDER_KEY not in out and PLACEHOLDER_KEY not in err
    assert_only_anthropic(transport)


@pytest.mark.parametrize("data", [b"", b"not json", b"[1, 2]", b"\xff\xfe"])
def test_main_never_raises_on_bad_input(monkeypatch, capsys, data):
    feed_stdin(monkeypatch, data)
    assert child.main(transport=FakeTransport()) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["outcome"] == "failed" and body["receipt"] is None


def test_main_reports_unexpected_errors_as_model_failed(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(child, "_run", boom)
    feed_stdin(monkeypatch, b"{}")
    assert child.main(transport=FakeTransport()) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"outcome": "failed", "reason": "model_failed", "receipt": None}
    assert "secret detail" not in out
