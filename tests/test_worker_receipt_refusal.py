"""Permanent control refusal of a run receipt is terminal; transient errors are bounded."""

from __future__ import annotations

import gc
import io
import json
import sqlite3
import sys
import warnings
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from test_development_worker_service import (
    _exit_after_start,
    _InspectingAdapter,
    _ReceiptChannel,
    _service,
    _unleased_delivery,
)

from tessera_control.api import create_app
from tessera_control.store import _connect
from tessera_worker.control_channel import (
    AUTHORITY_GONE_DETAILS,
    ControlChannelError,
    HttpControlChannel,
    classify_control_failure,
)
from tessera_worker.exit_codes import EXIT_CREDENTIAL_REJECTED, EXIT_OK, EXIT_TERMINAL
from tessera_worker.service import RUN_RECEIPT_ATTEMPTS, ReceiptRefusalJournal


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ControlChannelError("control rejected request", status), "permanent")
        for status in (400, 404, 409, 410, 422, 451, 499)
    ]
    + [(ControlChannelError("control rejected request", status), "auth") for status in (401, 403)]
    + [
        (ControlChannelError("control rejected request", status), "transient")
        for status in (408, 425, 429, 500, 502, 503, 504, 599, 302, 200)
    ]
    + [
        (ControlChannelError("control transport unavailable"), "transient"),
        (ConnectionError("reset"), "transient"),
        (TimeoutError("slow"), "transient"),
        (RuntimeError("x"), "transient"),
    ],
)
def test_classification_table(error, expected):
    assert classify_control_failure(error) == expected


class _Flaky(_ReceiptChannel):
    """Fails the first ``failures`` receipt posts, then lets control record it."""

    def __init__(self, state, failures, error):
        super().__init__(state)
        self.failures, self.error = failures, error

    def post(self, path, body, key):
        if path.endswith("/run-receipt") and len(self.receipts) < self.failures:
            self.receipts.append((path, body, key))
            raise self.error
        return super().post(path, body, key)


def _run(tmp_path, channel_factory, *, sleeps=None):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = _InspectingAdapter()
    channel = channel_factory(state)
    exiting = _exit_after_start(adapter)

    def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
        exiting(seconds)

    def build(ch):
        return _service(
            tmp_path, state, grant, channel=ch, adapter=adapter, max_ticks=5, sleep=sleep
        )

    return state, grant, adapter, channel, build


def test_permanent_refusal_is_terminal_durable_and_never_replayed(tmp_path):
    body = "secret-body " + str(tmp_path)
    state, grant, adapter, channel, build = _run(
        tmp_path, lambda s: _ReceiptChannel(s, fail=ControlChannelError(body, 409))
    )
    result = build(channel).run()
    assert result["state"] == "unknown" and result["quarantined"] is False
    assert result["reason"] == "run_receipt_refused" and result["terminal"] is True
    assert "HTTP 409" in result["detail"] and len(result["detail"]) <= 200
    assert "secret-body" not in result["detail"] and str(tmp_path) not in result["detail"]
    assert "receipt" not in result and "run_receipt" not in result
    assert len(channel.receipts) == 1  # permanent: no retry
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))
    # Restart: same terminal evidence, zero further receipt posts, no second start.
    second_channel = _ReceiptChannel(state)
    again = build(second_channel).run()
    assert again == result
    assert second_channel.receipts == []
    assert adapter.calls.count("start") == 1
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_transient_error_retries_with_one_key_then_records_exactly_once(tmp_path):
    sleeps = []
    _, _, adapter, channel, build = _run(
        tmp_path,
        lambda s: _Flaky(s, 2, ControlChannelError("control rejected request", 503)),
        sleeps=sleeps,
    )
    result = build(channel).run()
    assert result["state"] == "exited" and result["run_receipt"]["status"] == "recorded"
    assert len(channel.receipts) == 3
    assert len({key for _, _, key in channel.receipts}) == 1
    assert adapter.calls.count("start") == 1


def test_transient_exhaustion_is_bounded_unknown_and_a_restart_may_retry(tmp_path):
    state, grant, adapter, channel, build = _run(
        tmp_path, lambda s: _ReceiptChannel(s, fail=ControlChannelError("down", 503))
    )
    result = build(channel).run()
    assert result["reason"] == "run_receipt_failed" and "terminal" not in result
    assert "retries exhausted" in result["detail"] and len(result["detail"]) <= 200
    assert len(channel.receipts) == RUN_RECEIPT_ATTEMPTS
    second_channel = _ReceiptChannel(state)
    again = build(second_channel).run()
    assert again["state"] == "exited" and again["run_receipt"]["status"] == "recorded"
    assert len(second_channel.receipts) == 1
    assert adapter.calls.count("start") == 1


def test_cli_exits_nonzero_with_bounded_stderr_on_terminal_refusal(monkeypatch, capsys):
    import sys

    from tessera_worker import cli

    class Service:
        def __init__(self, config):
            pass

        def run(self):
            return {
                "state": "unknown",
                "quarantined": False,
                "reason": "run_receipt_refused",
                "detail": "control refused the run receipt (HTTP 409)",
                "terminal": True,
                "operation_id": "a" * 32,
            }

    monkeypatch.setattr(cli, "DevelopmentWorkerService", Service)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera-worker", "development-run", "--control-url", "http://127.0.0.1:1",
            "--grant-id", "g", "--credential-file", "/c", "--workspace-root", "/w",
            "--development-output-root", "/o", "--allowed-image", "i",
        ],
    )  # fmt: skip
    assert cli.main() == EXIT_TERMINAL
    captured = capsys.readouterr()
    assert "run_receipt_refused" in captured.out
    assert captured.err.strip() == (
        f"development worker terminal: operation {'a' * 32} reason run_receipt_refused"
    )


@pytest.mark.parametrize("status", [401, 403])
def test_auth_refusal_is_not_recorded_not_retried_and_restart_reposts(tmp_path, status):
    state, grant, adapter, channel, build = _run(
        tmp_path, lambda s: _ReceiptChannel(s, fail=ControlChannelError("bad token", status))
    )
    result = build(channel).run()
    assert result["state"] == "unknown" and result["reason"] == "run_receipt_failed"
    assert "terminal" not in result and len(result["detail"]) <= 200
    assert f"HTTP {status}" in result["detail"] and "credential" in result["detail"]
    assert "bad token" not in result["detail"]
    assert len(channel.receipts) == 1  # one attempt, no retry loop
    journal = ReceiptRefusalJournal(tmp_path / "worker")
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM receipt_refusals").fetchone() == (0,)
    fixed = _ReceiptChannel(state)
    again = build(fixed).run()
    assert again["state"] == "exited" and again["run_receipt"]["status"] == "recorded"
    assert len(fixed.receipts) == 1 and adapter.calls.count("start") == 1


def test_cli_exit_is_zero_for_non_terminal_unknown(monkeypatch, capsys):
    import sys

    from tessera_worker import cli

    class Service:
        def __init__(self, config):
            pass

        def run(self):
            return {"state": "unknown", "reason": "run_receipt_failed", "detail": "x"}

    monkeypatch.setattr(cli, "DevelopmentWorkerService", Service)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera-worker", "development-run", "--control-url", "http://127.0.0.1:1",
            "--grant-id", "g", "--credential-file", "/c", "--workspace-root", "/w",
            "--development-output-root", "/o", "--allowed-image", "i",
        ],
    )  # fmt: skip
    assert cli.main() == EXIT_OK
    assert capsys.readouterr().err == ""


def test_record_failure_is_terminal_unrecorded_and_restart_reposts(tmp_path, monkeypatch):
    state, grant, adapter, channel, build = _run(
        tmp_path, lambda s: _ReceiptChannel(s, fail=ControlChannelError("no", 409))
    )

    def boom(self, operation_id, http_status, detail):
        raise sqlite3.OperationalError("disk I/O error " + str(tmp_path))

    with monkeypatch.context() as patch:
        patch.setattr(ReceiptRefusalJournal, "record", boom)
        result = build(channel).run()
    assert result["reason"] == "run_receipt_refusal_unrecorded" and result["terminal"] is True
    assert result["state"] == "unknown" and result["quarantined"] is False
    assert result["detail"] == (
        "control refused the run receipt (HTTP 409); the refusal could not be recorded "
        "durably, so a restart will repost"
    )
    assert str(tmp_path) not in result["detail"] and result["operation_id"]
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))
    fixed = _ReceiptChannel(state)
    again = build(fixed).run()
    assert again["state"] == "exited" and len(fixed.receipts) == 1


def test_unreadable_journal_at_start_fails_closed(tmp_path, monkeypatch):
    state, grant, adapter, channel, build = _run(tmp_path, lambda s: _ReceiptChannel(s))

    def boom(self, operation_id):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(ReceiptRefusalJournal, "detail", boom)
    result = build(channel).run()
    assert result["reason"] == "run_receipt_refusal_unrecorded" and result["terminal"] is True
    assert "could not be read" in result["detail"] and len(result["detail"]) <= 200
    assert channel.receipts == []  # nothing reposted
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))


def test_corrupt_journal_file_does_not_crash_construction_and_fails_closed(tmp_path):
    (tmp_path / "receipt-refusals.sqlite3").write_bytes(b"not a sqlite database" * 20)
    journal = ReceiptRefusalJournal(tmp_path)
    with pytest.raises(sqlite3.Error):
        journal.detail("a" * 32)


def test_journal_connections_are_closed_even_on_error(tmp_path):
    journal = ReceiptRefusalJournal(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        journal.record("a" * 32, 409, "d")
        assert journal.detail("a" * 32) == "d"
        assert journal.detail("b" * 32) is None
        with pytest.raises(RuntimeError), journal._connect():
            raise RuntimeError("boom")
        gc.collect()
    # No lingering handle: the file is movable and an exclusive lock is available.
    with sqlite3.connect(journal.path, timeout=0) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
    journal.path.rename(tmp_path / "moved.sqlite3")


# --- credential rejection (401), authority-gone 403, and exit codes ---------------

_GRANT_GONE = "Worker is not assigned to this execution grant."


class _AppResponse:
    def __init__(self, response):
        self._body = response.content
        self.headers = {"Content-Type": response.headers.get("content-type", "")}
        self._status = response.status_code

    def getcode(self):
        return self._status

    def read(self, limit=-1):
        return self._body

    def close(self):
        pass


def _app_opener(client, rewrite=None):
    """Bridge HttpControlChannel to the in-process control app, like urllib would."""

    def open_request(request, timeout):
        path = "/" + request.full_url.split("/", 3)[3]
        headers = {name: value for name, value in request.header_items()}
        headers = rewrite(path, headers) if rewrite else headers
        response = client.post(path, content=request.data, headers=headers)
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url, response.status_code, "x", {}, io.BytesIO(response.content)
            )
        return _AppResponse(response)

    return open_request


class _HttpControl:
    """Real HttpControlChannel -> real control app; records paths and receipt posts."""

    def __init__(self, state, *, bad_token_on=(), tamper=None):
        self.client = TestClient(create_app(state))
        self.paths: list[str] = []
        self.receipts: list[str] = []
        self.tamper = tamper

        def rewrite(path, headers):
            self.paths.append(path)
            if path.endswith("/run-receipt"):
                self.receipts.append(path)
                if self.tamper is not None:
                    self.tamper()
            if any(path.startswith(prefix) for prefix in bad_token_on):
                headers = {**headers, "Authorization": "Bearer not-a-worker-token"}
            else:
                headers = {**headers, "Authorization": "Bearer worker-token"}
            return headers

        self.channel = HttpControlChannel(
            "http://127.0.0.1:8765", "worker-token", opener=_app_opener(self.client, rewrite)
        )

    def post(self, path, body, key):
        return self.channel.post(path, body, key)


def _no_durable_record(tmp_path):
    journal = ReceiptRefusalJournal(tmp_path / "worker")
    with sqlite3.connect(journal.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM receipt_refusals").fetchone() == (0,)


def test_real_control_401_on_receipt_marks_credential_rejected_and_restart_reposts(tmp_path):
    state, grant, adapter, _, build = _run(tmp_path, lambda s: None)
    control = _HttpControl(state, bad_token_on=("/worker/external-operations",))
    result = build(control).run()
    assert result["state"] == "unknown" and result["reason"] == "run_receipt_failed"
    assert result["credential_rejected"] is True and result["control_status"] == 401
    assert "terminal" not in result and len(result["detail"]) <= 200
    assert "Unauthorized" not in result["detail"] and str(tmp_path) not in result["detail"]
    assert len(control.receipts) == 1  # no retry loop
    assert _no_durable_record(tmp_path)
    fixed = _HttpControl(state)
    again = build(fixed).run()
    assert again["state"] == "exited" and again["run_receipt"]["status"] == "recorded"
    assert "credential_rejected" not in again and adapter.calls.count("start") == 1


def test_real_control_401_before_lease_is_marked_and_never_starts_provider(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)
    control = _HttpControl(state, bad_token_on=("/worker/",))
    result = build(control).run()
    assert result == {
        "state": "unknown",
        "quarantined": False,
        "reason": "credential_rejected",
        "credential_rejected": True,
        "control_status": 401,
    }
    assert control.paths == ["/worker/heartbeat"]  # fail closed: one request, no loop
    assert adapter.calls.count("start") == 0


def test_real_control_401_on_observation_cancels_first_then_marks(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)
    control = _HttpControl(state, bad_token_on=("/worker/observations",))
    result = build(control).run()
    # Not a recorded verdict: the observation waits in the outbox for a renewed credential.
    assert result["state"] == "report_pending" and result["quarantined"] is False
    assert result["credential_rejected"] is True and result["control_status"] == 401
    assert adapter.calls.count("start") == 1
    assert adapter.state == "exited"  # bounded cancellation of the container still happened


def test_real_control_401_on_package_refresh_denies_cancels_and_marks(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)

    class Control(_HttpControl):
        def post(self, path, body, key):
            if path.endswith("/package") and adapter.calls.count("start"):
                self.channel = HttpControlChannel(
                    "http://127.0.0.1:8765",
                    "worker-token",
                    opener=_app_opener(
                        self.client,
                        lambda p, h: {**h, "Authorization": "Bearer not-a-worker-token"},
                    ),
                )
            return super().post(path, body, key)

    result = build(Control(state)).run()
    assert result["state"] in {"unknown", "report_pending"}
    assert result["credential_rejected"] is True
    assert result["control_status"] == 401 and adapter.state == "exited"


def test_real_control_unregistered_worker_403_is_credential_class_not_permanent(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM worker_registry")
    result = build(_HttpControl(state)).run()
    assert result["reason"] == "credential_rejected" and result["control_status"] == 403
    assert result["credential_rejected"] is True and "terminal" not in result
    assert adapter.calls.count("start") == 0


def test_allowlisted_403_detail_is_exactly_what_real_control_returns(tmp_path):
    state, _, _, worker, grant = _unleased_delivery(tmp_path)
    client = TestClient(create_app(state))
    headers = {"Authorization": "Bearer worker-token"}
    leased = client.post(
        "/worker/leases",
        json={"grant_id": grant["id"]},
        headers=headers | {"Idempotency-Key": "l" * 8},
    )
    assert leased.status_code == 201, leased.text
    lease = leased.json()
    with _connect(state / "tessera.sqlite3") as connection:
        other = connection.execute(
            "SELECT id FROM principals WHERE id != ? LIMIT 1", (grant["worker_principal_id"],)
        ).fetchone()[0]
        connection.execute(
            "UPDATE execution_grants SET worker_principal_id=? WHERE id=?", (other, grant["id"])
        )
    # The worker still holds its lease, so the grant moving away is reported as authority gone.
    response = client.post(
        "/worker/leases/renew",
        json={"lease_id": lease["id"], "expected_epoch": lease["epoch"]},
        headers=headers | {"Idempotency-Key": "k" * 8},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == _GRANT_GONE and _GRANT_GONE in AUTHORITY_GONE_DETAILS
    # Naming another worker's grant without a lease is answered exactly as a missing grant
    # (404, also a permanent refusal for the worker), never revealing that the grant exists.
    foreign = client.post(
        "/worker/leases",
        json={"grant_id": grant["id"]},
        headers=headers | {"Idempotency-Key": "f" * 8},
    )
    missing = client.post(
        "/worker/leases",
        json={"grant_id": "grant_missing"},
        headers=headers | {"Idempotency-Key": "m" * 8},
    )
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()


def _forbidden(detail, *, raw=None):
    body = raw if raw is not None else json.dumps({"detail": detail}).encode()
    return HTTPError("http://127.0.0.1:8765/x", 403, "Forbidden", {}, io.BytesIO(body))


def _classified(error_http):
    def opener(request, timeout):
        raise error_http

    channel = HttpControlChannel("http://127.0.0.1:8765", "t", opener=opener)
    with pytest.raises(ControlChannelError) as raised:
        channel.post("/worker/x", {}, "key")
    return raised.value


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (lambda: _forbidden(_GRANT_GONE), "permanent"),
        (lambda: _forbidden("Worker is retired."), "permanent"),
        (lambda: _forbidden("Worker is not registered."), "auth"),
        (lambda: _forbidden("Worker is not registered for this project."), "auth"),
        (lambda: _forbidden("Browser origins are not accepted."), "auth"),
        (lambda: _forbidden(_GRANT_GONE + " "), "auth"),  # exact match only
        (lambda: _forbidden("x " + _GRANT_GONE), "auth"),  # never substring
        (lambda: _forbidden(_GRANT_GONE.lower()), "auth"),
        (lambda: _forbidden(None, raw=b"not json " + _GRANT_GONE.encode()), "auth"),
        (lambda: _forbidden(None, raw=json.dumps([_GRANT_GONE]).encode()), "auth"),
        (lambda: _forbidden(None, raw=json.dumps({"detail": [_GRANT_GONE]}).encode()), "auth"),
        (lambda: _forbidden(None, raw=b"{" * 5000), "auth"),
        (lambda: _forbidden(None, raw=b""), "auth"),
    ],
)
def test_403_classification_uses_exact_detail_allowlist(make, expected):
    error = _classified(make())
    assert classify_control_failure(error) == expected
    assert _GRANT_GONE not in str(error)


def test_401_with_authority_gone_text_is_still_auth():
    error = HTTPError("u", 401, "x", {}, io.BytesIO(json.dumps({"detail": _GRANT_GONE}).encode()))
    assert classify_control_failure(_classified(error)) == "auth"


def test_real_control_revoked_grant_on_receipt_is_permanent_durable_and_not_replayed(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)

    def reconcile_grant():  # the grant authority is gone before the receipt arrives
        with _connect(state / "tessera.sqlite3") as connection:
            connection.execute("UPDATE execution_grants SET status='reconciled'")

    control = _HttpControl(state, tamper=reconcile_grant)
    result = build(control).run()
    assert result["reason"] == "run_receipt_refused" and result["terminal"] is True
    assert "HTTP 409" in result["detail"] and "credential_rejected" not in result
    assert len(control.receipts) == 1
    again = build(_HttpControl(state)).run()
    assert again == result and adapter.calls.count("start") == 1


def test_authority_gone_403_on_receipt_is_permanent_durable_and_not_replayed(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)

    class Gone(_HttpControl):
        def post(self, path, body, key):
            if path.endswith("/run-receipt"):
                self.receipts.append(path)
                raise _classified(_forbidden(_GRANT_GONE))
            return super().post(path, body, key)

    control = Gone(state)
    result = build(control).run()
    assert result["reason"] == "run_receipt_refused" and result["terminal"] is True
    assert "HTTP 403" in result["detail"] and _GRANT_GONE not in result["detail"]
    assert "credential_rejected" not in result and len(control.receipts) == 1
    second = _HttpControl(state)
    assert build(second).run() == result and second.receipts == []
    assert adapter.calls.count("start") == 1


def test_unknown_403_on_receipt_is_credential_class_with_no_durable_record(tmp_path):
    state, _, adapter, _, build = _run(tmp_path, lambda s: None)

    class Denied(_HttpControl):
        def post(self, path, body, key):
            if path.endswith("/run-receipt"):
                self.receipts.append(path)
                raise _classified(_forbidden("Worker is not registered."))
            return super().post(path, body, key)

    control = Denied(state)
    result = build(control).run()
    assert result["reason"] == "run_receipt_failed" and "terminal" not in result
    assert result["credential_rejected"] is True and result["control_status"] == 403
    assert "not registered" not in result["detail"] and len(control.receipts) == 1
    assert _no_durable_record(tmp_path)


def _argv(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera-worker", "development-run", "--control-url", "http://127.0.0.1:1",
            "--grant-id", "g", "--credential-file", "/c", "--workspace-root", "/w",
            "--development-output-root", "/o", "--allowed-image", "i",
        ],
    )  # fmt: skip


def _cli_with(monkeypatch, outcome):
    from tessera_worker import cli

    class Service:
        def __init__(self, config):
            pass

        def run(self):
            return outcome

    monkeypatch.setattr(cli, "DevelopmentWorkerService", Service)
    _argv(monkeypatch)
    return cli


def test_exit_code_constants_are_stable():
    assert (EXIT_OK, EXIT_TERMINAL, EXIT_CREDENTIAL_REJECTED) == (0, 2, 3)


def test_cli_exits_3_with_one_bounded_stderr_line_on_credential_rejection(monkeypatch, capsys):
    outcome = {
        "state": "unknown",
        "quarantined": False,
        "reason": "run_receipt_failed",
        "detail": "x",
        "credential_rejected": True,
        "control_status": 401,
    }
    cli = _cli_with(monkeypatch, outcome)
    assert cli.main() == EXIT_CREDENTIAL_REJECTED
    captured = capsys.readouterr()
    assert json.loads(captured.out)["credential_rejected"] is True
    assert captured.err == (
        "development worker: control rejected the worker credential (HTTP 401); "
        "renew or replace the credential, then restart\n"
    )


def test_cli_terminal_wins_over_credential_marker(monkeypatch, capsys):
    outcome = {
        "state": "unknown",
        "reason": "run_receipt_refused",
        "terminal": True,
        "operation_id": "b" * 32,
        "credential_rejected": True,
    }
    assert _cli_with(monkeypatch, outcome).main() == EXIT_TERMINAL
    assert "terminal" in capsys.readouterr().err


# --- restart with a still-broken journal must not start the provider again -------


def test_restart_with_still_broken_journal_cannot_start_provider_again(tmp_path):
    state, _, adapter, channel, build = _run(
        tmp_path, lambda s: _ReceiptChannel(s, fail=ControlChannelError("no", 409))
    )
    first = build(channel).run()
    assert first["reason"] == "run_receipt_refused" and adapter.calls.count("start") == 1
    journal_path = tmp_path / "worker" / "receipt-refusals.sqlite3"
    journal_path.write_bytes(b"not a sqlite database" * 50)  # journal is now unreadable
    second_channel = _ReceiptChannel(state)
    again = build(second_channel).run()
    assert again["reason"] == "run_receipt_refusal_unrecorded" and again["terminal"] is True
    assert "could not be read" in again["detail"]
    assert second_channel.receipts == []  # nothing reposted
    assert adapter.calls.count("start") == 1  # first run + restart: exactly one start
    assert list((tmp_path / "outputs").glob("development-only-cpu-outputs/workspaces/*"))
    # Still broken on a third try: same fail-closed answer, still one start.
    third_channel = _ReceiptChannel(state)
    assert build(third_channel).run()["reason"] == "run_receipt_refusal_unrecorded"
    assert third_channel.receipts == [] and adapter.calls.count("start") == 1
