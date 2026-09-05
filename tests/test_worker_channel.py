from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError

import pytest

from tessera_worker.control_channel import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ControlChannelError,
    HttpControlChannel,
)


@dataclass
class FakeResponse:
    body: bytes = b'{"ok":true}'
    status: int = 200
    content_type: str = "application/json"
    closed: bool = False

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": self.content_type}

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int) -> bytes:
        assert amount == MAX_RESPONSE_BYTES + 1
        return self.body

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, response: FakeResponse | Exception | None = None):
        self.response = response or FakeResponse()
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class BlockingResponse(FakeResponse):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed_event = threading.Event()

    def read(self, amount: int) -> bytes:
        self.entered.set()
        assert self.release.wait(1)
        return super().read(amount)

    def close(self) -> None:
        super().close()
        self.closed_event.set()


class CloseFailureResponse(FakeResponse):
    def close(self) -> None:
        raise RuntimeError("worker-token-must-not-leak")


def test_post_pins_url_authentication_key_and_json_body() -> None:
    opener = FakeOpener()
    channel = HttpControlChannel("https://control.example/", "worker-token", opener=opener)
    assert channel.post("/worker/leases/lease-1/package", {"expected_epoch": 1}, "package") == {
        "ok": True
    }
    request, timeout = opener.requests[0]
    assert request.full_url == "https://control.example/worker/leases/lease-1/package"
    assert timeout == 5.0
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer worker-token"
    assert request.get_header("Idempotency-key") == "package"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"expected_epoch": 1}


@pytest.mark.parametrize(
    "url",
    [
        "http://control.example",
        "http://localhost",
        "http://127.0.0.1.evil.example",
        "http://[::1].evil.example",
        "https://token@control.example",
        "https://control.example/base",
        "https://control.example/?query=1",
        "https://control.example/#fragment",
        " https://control.example",
        "https://control.example\n.evil.example",
        "https://control.example\\evil",
    ],
)
def test_base_url_rejects_non_loopback_http_and_ambiguous_origins(url: str) -> None:
    with pytest.raises(ValueError):
        HttpControlChannel(url, "worker-token")


@pytest.mark.parametrize(
    "path",
    [
        "worker/leases",
        "//worker/leases",
        "/worker?x=1",
        "/worker#part",
        "/worker/%2f",
        "/../worker",
        "/a/./b",
    ],
)
def test_paths_cannot_inject_a_new_destination(path: str) -> None:
    with pytest.raises(ValueError):
        HttpControlChannel("https://control.example", "worker-token").post(path, {}, "key")


def test_redirect_is_rejected_without_a_second_credentialed_request() -> None:
    redirect = HTTPError("https://control.example/redirect", 302, "moved", {}, None)
    opener = FakeOpener(redirect)
    channel = HttpControlChannel("https://control.example", "worker-token", opener=opener)
    with pytest.raises(ControlChannelError) as raised:
        channel.post("/worker/leases/lease/package", {}, "key")
    assert raised.value.reason == "control rejected request"
    assert raised.value.status == 302
    assert len(opener.requests) == 1
    assert opener.requests[0][0].get_header("Authorization") == "Bearer worker-token"


@pytest.mark.parametrize(
    "response,reason",
    [
        (FakeResponse(status=503), "control rejected request"),
        (FakeResponse(body=b"not-json"), "control response is not valid JSON"),
        (FakeResponse(body=b"[" * 2000 + b"]" * 2000), "control response is not valid JSON"),
        (FakeResponse(body=b"[]"), "control response must be a JSON object"),
        (FakeResponse(content_type="text/plain"), "control response has an invalid content type"),
        (FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1)), "control response exceeds its bound"),
    ],
)
def test_invalid_or_oversized_response_is_sanitized(response: FakeResponse, reason: str) -> None:
    channel = HttpControlChannel(
        "https://control.example", "worker-token", opener=FakeOpener(response)
    )
    with pytest.raises(ControlChannelError) as raised:
        channel.post("/worker/leases/lease/package", {}, "key")
    assert raised.value.reason == reason
    assert raised.value.status == response.status
    assert "worker-token" not in str(raised.value)
    assert response.closed


def test_http_error_is_sanitized_without_token_or_response_body() -> None:
    secret = "token-that-must-not-appear"
    error_body = FakeResponse()
    opener = FakeOpener(HTTPError("https://control.example", 401, secret, {}, error_body))
    channel = HttpControlChannel("https://control.example", secret, opener=opener)
    with pytest.raises(ControlChannelError) as raised:
        channel.post("/worker/leases/lease/package", {}, "key")
    assert raised.value.reason == "control rejected request"
    assert raised.value.status == 401
    assert secret not in str(raised.value)
    assert error_body.closed


def test_close_failure_does_not_replace_or_leak_a_sanitized_protocol_error() -> None:
    response = CloseFailureResponse(body=b"not-json")
    channel = HttpControlChannel(
        "https://control.example", "worker-token", opener=FakeOpener(response)
    )
    with pytest.raises(ControlChannelError) as raised:
        channel.post("/worker/leases/lease/package", {}, "key")
    assert raised.value.reason == "control response is not valid JSON"
    assert "worker-token" not in str(raised.value)


def test_instances_never_share_origin_or_worker_credentials() -> None:
    first, second = FakeOpener(), FakeOpener()
    one = HttpControlChannel("https://one.example", "token-one", opener=first)
    two = HttpControlChannel("https://two.example", "token-two", opener=second)
    one.post("/worker/leases/one/package", {}, "one")
    two.post("/worker/leases/two/package", {}, "two")
    assert first.requests[0][0].full_url.startswith("https://one.example/")
    assert first.requests[0][0].get_header("Authorization") == "Bearer token-one"
    assert second.requests[0][0].full_url.startswith("https://two.example/")
    assert second.requests[0][0].get_header("Authorization") == "Bearer token-two"


def test_bounded_inputs_and_timeout_reject_before_network() -> None:
    opener = FakeOpener()
    channel = HttpControlChannel("https://control.example", "worker-token", opener=opener)
    with pytest.raises(ValueError):
        channel.post("/worker/leases/lease/package", {"data": "x" * MAX_REQUEST_BYTES}, "key")
    with pytest.raises(ValueError):
        channel.post("/worker/leases/lease/package", {}, "key\r\nInjected: value")
    with pytest.raises(ValueError):
        HttpControlChannel("https://control.example", "worker-token", timeout_seconds=31)
    assert opener.requests == []


def test_total_deadline_fences_a_blocked_request_without_spawning_more_threads() -> None:
    response = BlockingResponse()
    opener = FakeOpener(response)
    channel = HttpControlChannel(
        "https://control.example", "worker-token", timeout_seconds=0.05, opener=opener
    )
    started = time.monotonic()
    with pytest.raises(ControlChannelError, match="deadline") as raised:
        channel.post("/worker/leases/lease/package", {}, "first")
    assert time.monotonic() - started < 0.5
    assert raised.value.reason == "control response deadline exceeded"
    assert response.entered.wait(0.5)
    with pytest.raises(ControlChannelError, match="in progress"):
        channel.post("/worker/leases/lease/package", {}, "second")
    assert len(opener.requests) == 1
    response.release.set()
    assert response.closed_event.wait(0.5)
    channel._inflight.result(timeout=0.5)
    assert channel.post("/worker/leases/lease/package", {}, "third") == {"ok": True}
    assert len(opener.requests) == 2
