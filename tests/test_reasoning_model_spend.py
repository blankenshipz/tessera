# ruff: noqa: F811
"""Model-spend ledger settlement from the reasoning supervisor (reserve, settle, release).

Synthetic only: a fake control stands in for the control plane's ledger and a fake transport for
the allowlisted Messages API. No network, no keys.
"""

import hashlib
import json
import math
from urllib.parse import parse_qs, urlsplit

import pytest
from test_anthropic_runtime_integration import (
    COMPLETION,
    MODEL,
    PROMPT,
    Transport,
    creds,  # noqa: F401 (fixture)
    make_profile,
    ok,
    setup,
)
from test_model_call_receipts import build as build_openai

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.model_spend_client import SpendLedger
from tessera_agent.remote_reasoning_runtime import JsonEndpoint, RuntimeBlocked, validate_profile
from tessera_agent.remote_reasoning_store import ReasoningStore

QUALIFIED = "anthropic/" + MODEL


class FakeControl:
    """The control plane's ledger as the supervisor sees it (contract: model-spend routes)."""

    def __init__(self, events=None):
        self.calls, self.by_key, self.events = [], {}, events if events is not None else []
        self.deny = None  # a denial code for reserve
        self.down = False  # transport failure for everything
        self.malformed = False
        self.settle_down = False
        self.cap_exceeded = False
        self.call_runs: dict[str, str] = {}  # call_id -> the run that reserved it
        self.statuses: dict[str, dict] = {}  # call_id -> GET /model-spend/reservations/{id}

    def call(self, method, path, body=None, key=None):
        self.calls.append((method, path, body, key))
        if self.down:
            raise RuntimeBlocked("endpoint_unavailable_or_invalid")
        if method == "GET" and path.startswith("/model-spend/reservations/"):
            parts = urlsplit(path)
            call_id = parts.path.rsplit("/", 1)[-1]
            # As the route reads it (a FastAPI query parameter): other parameters are ignored, a
            # repeated run_id takes the last value, and a blank or missing one is refused (an agent
            # that sends no run_id gets a 422; both read as an exception here).
            parsed = parse_qs(parts.query, keep_blank_values=True)
            asked = parsed.get("run_id", [])
            asked_run = asked[-1] if asked else None
            # The control plane answers only for the caller's own run (404 otherwise).
            if not asked_run or self.call_runs.get(call_id, asked_run) != asked_run:
                raise RuntimeBlocked("endpoint_unavailable_or_invalid")
            return self.statuses.get(
                call_id,
                {"call_id": call_id, "status": "reserved", "resolution": None,
                 "settled_micro_cents": None, "day": "2026-01-01"},
            )  # fmt: skip
        if key is not None and key in self.by_key:
            return self.by_key[key]  # an idempotent replay returns the stored answer
        if path == "/model-spend/reservations":
            self.events.append("reserve")
            if self.malformed:
                return {"unexpected": True}
            if self.deny:
                answer = {"allowed": False, "code": self.deny}
            else:
                answer = {
                    "allowed": True,
                    "call_id": f"call{len(self.by_key) + 1}",
                    "status": "reserved",
                    "reserved_micro_cents": math.ceil(body["worst_case_cents"] * 1e6),
                }
                self.call_runs[answer["call_id"]] = body["run_id"]
        elif path.endswith("/settlement"):
            self.events.append("settle")
            if self.settle_down:
                raise RuntimeBlocked("endpoint_unavailable_or_invalid")
            answer = {
                "call_id": path.split("/")[-2],
                "status": "settled",
                "settled_micro_cents": math.ceil(body["actual_cents"] * 1e6),
                "over_reservation": False,
                "cap_exceeded": self.cap_exceeded,
            }
        elif path.endswith("/release"):
            self.events.append("release")
            answer = {"call_id": path.split("/")[-2], "status": "released"}
        else:
            raise AssertionError(path)
        if key is not None:
            self.by_key[key] = answer
        return answer

    def paths(self):
        return [c[1].rsplit("/", 1)[-1] if c[1] != "/model-spend/reservations" else "reserve"
                for c in self.calls]  # fmt: skip


class OrderedTransport(Transport):
    def __init__(self, events, *replies, **kwargs):
        super().__init__(*replies, **kwargs)
        self.events = events

    def post_json(self, url, headers, body, *, timeout=None):
        if "count_tokens" not in url:
            self.events.append("messages")
        return super().post_json(url, headers, body, timeout=timeout)


def rig(tmp_path, creds, *replies, profile=None, control=None, project="project1"):  # noqa: F811
    control = control or FakeControl()
    transport = OrderedTransport(control.events, *replies)
    profile = profile or make_profile(model_spend={"project_id": project} if project else {})
    if not project:
        profile.pop("model_spend", None)
    store, supervisor, clock, _ = setup(tmp_path, creds, transport, profile=profile)
    supervisor.spend = SpendLedger(control, None, "anthropic", QUALIFIED)
    return store, supervisor, control, transport, clock


# -- the happy path --


def test_the_cost_is_reserved_before_the_call_and_settled_after_with_the_receipt_digest(
    tmp_path, creds
):
    store, supervisor, control, transport, _ = rig(tmp_path, creds, ok())
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert control.events == ["reserve", "messages", "settle"]  # reserved before anything is sent
    reserve, settle = control.calls[0], control.calls[1]
    assert reserve[2]["project_id"] == "project1" and reserve[2]["run_id"] == "run1"
    assert reserve[2]["provider_id"] == "anthropic" and reserve[2]["model"] == QUALIFIED
    (call,) = store.model_calls("run1")
    assert reserve[2]["worst_case_cents"] >= call["cost_cents_estimate_max"]
    assert math.isclose(
        reserve[2]["worst_case_cents"], call["cost_cents_estimate_max"], abs_tol=1e-6
    )
    assert settle[2]["actual_cents"] >= call["cost"]
    assert math.isclose(settle[2]["actual_cents"], call["cost"], abs_tol=1e-6)
    ledger = call["spend_ledger"]
    assert ledger["status"] == "settled" and ledger["call_id"] == "call1"
    assert ledger["settled_micro_cents"] == math.ceil(call["cost"] * 1e6)
    # The digest binds the receipt as stored, without the settlement answer.
    # The final turn's output digest is annotated after settlement (ADR 0083), so the digest
    # bound the receipt with output_sha256 still null.
    stored = {
        k: v for k, v in call.items() if k not in {"spend_ledger", "seq", "observed_at", "status"}
    } | {"output_sha256": None}
    digest = hashlib.sha256(
        json.dumps(stored, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert settle[2]["receipt_sha256"] == digest
    assert [r["state"] for r in store.spend_rows("run1")] == ["settled"]
    # The report row keeps the amount the supervisor itself settled.
    assert store.spend_rows("run1")[0]["settled_micro_cents"] == ledger["settled_micro_cents"]
    assert store.spend_open("run1") is None
    body = json.dumps([c[2] for c in control.calls])
    for secret in (PROMPT, COMPLETION):
        assert secret not in body


def test_idempotency_keys_are_derived_from_run_sequence_and_operation(tmp_path, creds):
    _, supervisor, control, _, _ = rig(tmp_path, creds, ok())
    supervisor.tick("run1", PROMPT)
    key = SpendLedger._key
    assert [c[3] for c in control.calls] == [key("run1", 1, "reserve"), key("run1", 1, "settle")]
    assert len(key("r" * 128, 1, "reserve")) <= 128
    assert len({key("r" * 128, n, op) for n in (1, 2) for op in ("reserve", "settle")}) == 4


# -- refusals: nothing is sent, the model never changes --


def test_a_denied_reservation_sends_nothing_and_does_not_downshift(tmp_path, creds):
    control = FakeControl()
    control.deny = "model_spend_cap_exceeded"
    store, supervisor, control, transport, _ = rig(tmp_path, creds, ok(), control=control)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "stop_acknowledged" and row["reason"] == "model_call_cap_exceeded"
    assert transport.messages() == [] and store.model_calls("run1") == []
    assert control.events == ["reserve"]
    # A refusal holds nothing, but is recorded so the next attempt gets a new sequence and key.
    assert [r["state"] for r in store.spend_rows("run1")] == ["denied"]
    assert store.spend_open("run1") is None
    assert {json.loads(s[2]).get("model") for s in transport.sent} <= {
        MODEL
    }  # only the pinned model


def test_the_local_per_call_cap_refuses_before_the_ledger_is_asked(tmp_path, creds):
    profile = make_profile(model_spend={"project_id": "project1"})
    profile["limits"]["max_cost_cents_per_call"] = 1
    _, supervisor, control, transport, _ = rig(tmp_path, creds, ok(), profile=profile)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_call_cap_exceeded" and transport.messages() == []
    assert control.calls == []


@pytest.mark.parametrize("mode", ["down", "malformed"])
def test_an_unavailable_or_malformed_ledger_fails_closed(tmp_path, creds, mode):
    control = FakeControl()
    setattr(control, mode, True)
    _, supervisor, _, transport, _ = rig(tmp_path, creds, ok(), control=control)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_spend_unavailable" and transport.messages() == []


def test_a_ledger_that_says_not_configured_blocks_the_run(tmp_path, creds):
    control = FakeControl()
    control.deny = "model_spend_not_configured"
    _, supervisor, _, transport, _ = rig(tmp_path, creds, ok(), control=control)
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_spend_unconfigured"
    assert transport.messages() == []


def test_no_project_means_unconfigured_before_any_call(tmp_path, creds):
    _, supervisor, control, transport, _ = rig(tmp_path, creds, ok(), project=None)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_spend_unconfigured"
    assert control.calls == [] and transport.sent == []


def test_a_grant_and_profile_project_that_disagree_is_unconfigured(tmp_path, creds):
    store, supervisor, control, transport, _ = rig(tmp_path, creds, ok())
    supervisor.authority = lambda _gid: {**_grant(), "project_id": "other-project"}
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_spend_unconfigured" and transport.sent == []


def _grant():
    from test_anthropic_runtime_integration import grant

    return grant()


def test_the_project_may_come_from_the_grant(tmp_path, creds):
    _, supervisor, control, _, _ = rig(tmp_path, creds, ok(), project=None)
    supervisor.authority = lambda _gid: {**_grant(), "project_id": "project-from-grant"}
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert control.calls[0][2]["project_id"] == "project-from-grant"


# -- release and uncertainty --


def test_a_definite_rejection_releases_the_reservation(tmp_path, creds):
    rate = (429, {"retry-after": "30"}, {"error": {"type": "rate_limit_error"}})
    store, supervisor, control, transport, _ = rig(tmp_path, creds, rate)
    supervisor.tick("run1", PROMPT)
    assert control.events == ["reserve", "messages", "release"]
    assert [r["state"] for r in store.spend_rows("run1")] == ["released"]
    assert store.spend_open("run1") is None
    assert store.model_calls("run1") == []  # nothing was billed, so there is no settlement


def test_a_retry_after_a_release_reserves_again_under_a_new_sequence(tmp_path, creds):
    rate = (429, {"retry-after": "30"}, {"error": {"type": "rate_limit_error"}})
    store, supervisor, control, transport, clock = rig(tmp_path, creds, rate, ok())
    supervisor.tick("run1", PROMPT)
    clock.now += 31
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert control.events == ["reserve", "messages", "release", "reserve", "messages", "settle"]
    assert [c[3] for c in control.calls if c[1] == "/model-spend/reservations"] == [
        SpendLedger._key("run1", 1, "reserve"),
        SpendLedger._key("run1", 2, "reserve"),
    ]
    assert [r["state"] for r in store.spend_rows("run1")] == ["released", "settled"]


def test_an_uncertain_call_keeps_its_reservation_open(tmp_path, creds):
    store, supervisor, control, _, _ = rig(tmp_path, creds, ConnectionError("boom"))
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] in {"stop_acknowledged", "stop_unknown"}
    assert control.events == ["reserve", "messages"]  # no release, no settlement
    (open_row,) = [r for r in store.spend_rows("run1") if r["state"] == "reserved"]
    assert open_row["call_id"] == "call1"
    # Both reservations survive: the local worst-case receipt (ADR 0083) and the open ledger one.
    (receipt,) = store.model_calls("run1", every=True)
    assert receipt["status"] == "settled" and receipt["outcome_class"] == "uncertain"
    assert receipt["cost_basis"] == "reserved_worst_case_estimate"
    assert receipt["spend_ledger"] == {"call_id": "call1", "status": "reserved"}
    # It is never replayed: another tick does not reserve or send again.
    supervisor.tick("run1", PROMPT)
    assert control.events == ["reserve", "messages"]


def test_a_daily_cap_refused_inside_the_reservation_write_releases_the_ledger(tmp_path, creds):
    from tessera_agent.remote_reasoning_store import DailyCapRefused

    store, supervisor, control, transport, _ = rig(tmp_path, creds, ok())

    def refused(*args, **kwargs):
        raise DailyCapRefused

    store.reserve_model_call = refused  # another run took the day's room after the preflight
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "daily_cap_exceeded"
    assert transport.messages() == [] and control.events == ["reserve", "release"]
    assert [r["state"] for r in store.spend_rows("run1")] == ["released"]
    assert store.spend_open("run1") is None
    assert store.model_calls("run1", every=True) == []  # no local pending row either


def test_a_sent_call_whose_void_fails_leaves_the_ledger_reservation_open(tmp_path, creds):
    rate = (429, {"retry-after": "30"}, {"error": {"type": "rate_limit_error"}})
    store, supervisor, control, transport, _ = rig(tmp_path, creds, rate)
    store.db.execute(
        "CREATE TEMP TRIGGER fail_void BEFORE UPDATE ON model_calls "
        "WHEN NEW.status='void' BEGIN SELECT RAISE(ABORT, 'injected void failure'); END"
    )
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_call_unrecorded"
    assert len(transport.messages()) == 1
    assert store.effect("run1", "prompt") != "not_sent"
    assert "release" not in control.events  # possibly sent: a person resolves it
    assert store.spend_open("run1")["call_id"] == "call1"
    (receipt,) = store.model_calls("run1", every=True)
    assert receipt["outcome_class"] == "uncertain"
    assert receipt["spend_ledger"] == {"call_id": "call1", "status": "reserved"}
    supervisor.tick("run1", PROMPT)
    assert len(transport.messages()) == 1  # never resent


# -- settlement failures --


def test_a_settlement_over_the_cap_halts_and_keeps_the_receipt(tmp_path, creds):
    control = FakeControl()
    control.cap_exceeded = True
    store, supervisor, _, _, _ = rig(tmp_path, creds, ok(), control=control)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "daily_cap_exceeded"
    (call,) = store.model_calls("run1")
    assert (
        call["spend_ledger"]["cap_exceeded"] is True and call["spend_ledger"]["status"] == "settled"
    )


def test_an_unreachable_ledger_at_settlement_halts_but_keeps_the_receipt_and_reservation(
    tmp_path, creds
):
    control = FakeControl()
    control.settle_down = True
    store, supervisor, _, _, _ = rig(tmp_path, creds, ok(), control=control)
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_spend_unavailable"
    (call,) = store.model_calls("run1")
    assert call["spend_ledger"] == {"call_id": "call1", "status": "unsettled"}
    assert call["cost"] > 0  # the receipt still records what was spent
    assert store.spend_open("run1")["call_id"] == "call1"  # still counted against the caps


def test_a_billed_cost_with_no_reservation_fails_closed(tmp_path, creds):
    store, supervisor, control, _, _ = rig(tmp_path, creds, ok())
    # Simulate a call that bypassed the preflight reservation.
    supervisor.budget_check = lambda *a: True
    supervisor.harness.budget_check = lambda *a: True
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "model_spend_unavailable"
    (call,) = store.model_calls("run1")
    assert call["spend_ledger"] == {"status": "unreserved"}


# -- unchanged paths and the pieces --


def test_a_destination_that_is_not_billed_never_touches_the_ledger(tmp_path):
    store, supervisor, harness, _ = build_openai(tmp_path)
    control = FakeControl()
    supervisor.spend = SpendLedger(control, "project1", "openai", "openai/gpt-5-codex")
    assert supervisor.tick("run1", "SENTINEL-PROMPT-TEXT-do-not-journal")["state"] == "completed"
    assert control.calls == []
    assert "spend_ledger" not in store.model_calls("run1")[0]


def test_without_a_ledger_port_the_local_caps_behave_as_before(tmp_path, creds):
    store, supervisor, transport, _ = None, None, None, None
    transport = Transport(ok())
    store, supervisor, _, _ = setup(tmp_path, creds, transport, profile=make_profile())
    assert supervisor.spend is None
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    assert "spend_ledger" not in store.model_calls("run1")[0]


def test_ledger_client_rounds_up_and_rejects_malformed_answers():
    class C:
        def __init__(self, answer):
            self.answer, self.sent = answer, []

        def call(self, method, path, body=None, key=None):
            self.sent.append((path, body, key))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    good = {"allowed": True, "call_id": "c1", "status": "reserved", "reserved_micro_cents": 5}
    control = C(good)
    ledger = SpendLedger(control, "p", "anthropic", QUALIFIED)
    assert ledger.reserve("r", 1, 0.0000011).allowed
    assert control.sent[0][1]["worst_case_cents"] == 0.000002  # never understated
    for bad in (
        None,
        [],
        {"allowed": "yes"},
        {**good, "call_id": "../x"},
        {**good, "status": "settled"},
        {**good, "reserved_micro_cents": -1},
        RuntimeError("down"),
    ):
        result = SpendLedger(C(bad), "p", "anthropic", QUALIFIED).reserve("r", 1, 1.0)
        assert not result.allowed and result.code == "model_spend_unavailable"
    for cents in (-1, math.nan, math.inf, "1", True):
        assert not SpendLedger(C(good), "p", "anthropic", QUALIFIED).reserve("r", 1, cents).allowed
    assert not SpendLedger(C(good), None, "anthropic", QUALIFIED).reserve("r", 1, 1).allowed
    denied = SpendLedger(C({"allowed": False, "code": "model_spend_cap_exceeded"}), "p", "a", "m")
    assert denied.reserve("r", 1, 1).code == "model_spend_cap_exceeded"
    unknown = SpendLedger(C({"allowed": False, "code": "whatever"}), "p", "a", "m")
    assert unknown.reserve("r", 1, 1).code == "model_spend_unavailable"
    assert not ledger.settle("../x", "r", 1, 1.0, "d").ok
    assert not SpendLedger(C({"call_id": "c1"}), "p", "a", "m").settle("c1", "r", 1, 1.0, "d").ok
    assert not SpendLedger(C({"call_id": "c1", "status": "settled"}), "p", "a", "m").release(
        "c1", "r", 1
    )


def test_store_validates_the_spend_ledger_receipt_field(tmp_path):
    store = ReasoningStore(tmp_path / "s")
    store.bind(
        "r",
        {
            "grant_id": "g",
            "revision": 1,
            "epoch": 1,
            "subject_credential_id": "a",
            "destination": "d",
            "model": "m",
            "prompt_sha256": "0" * 64,
            "profile_sha256": "0" * 64,
        },
        1.0,
    )
    for bad in ({"call_id": ["x"]}, {"unknown": 1}, "text"):
        with pytest.raises(ValueError):
            store.model_call("r", {"spend_ledger": bad}, 2.0)
    assert store.model_call("r", {"spend_ledger": {"call_id": "c", "status": "settled"}}, 2.0) == 1
    assert store.spend_next_seq("r") == 1
    store.spend_reserved("r", 1, "c", 1.5)
    assert store.spend_next_seq("r") == 2 and store.spend_open("r")["call_id"] == "c"
    store.spend_resolve("r", 1, "settled")
    store.spend_resolve("r", 1, "released")  # a resolved reservation never changes again
    assert store.spend_rows("r")[0]["state"] == "settled"
    with pytest.raises(ValueError):
        store.spend_resolve("r", 1, "reserved")


def test_the_control_endpoint_sends_the_idempotency_key(monkeypatch):
    seen = {}

    class Response:
        def read(self, n):
            return b"{}"

        def close(self):
            pass

    def opener(request, timeout):
        seen["headers"] = dict(request.header_items())
        return Response()

    monkeypatch.setattr(rr, "_default_open", opener)
    endpoint = JsonEndpoint("http://127.0.0.1:9", "token-value", "lab")
    endpoint.call("POST", "/x", {}, key="model-spend:run1:1:reserve")
    assert seen["headers"]["Idempotency-key"] == "model-spend:run1:1:reserve"
    endpoint.call("POST", "/x", {})
    assert "Idempotency-key" not in seen["headers"]
    with pytest.raises(RuntimeBlocked):
        endpoint.call("POST", "/x", {}, key="has space")


@pytest.mark.parametrize(
    "value",
    [
        {"project_id": ""},
        {"project_id": "../x"},
        {"project_id": 1},
        {},
        {"project_id": "p", "x": 1},
        "p",
    ],
)
def test_a_malformed_model_spend_setting_is_refused(value):
    with pytest.raises(ValueError):
        validate_profile(make_profile(model_spend=value))
    validate_profile(make_profile(model_spend={"project_id": "project1"}))


# -- a refusal is never replayed: the next attempt reserves under a new sequence and key --


def test_a_refusal_is_recorded_and_the_next_attempt_uses_a_new_key(tmp_path, creds):
    control = FakeControl()
    store, supervisor, control, _, _ = rig(tmp_path, creds, ok(), control=control)
    control.deny = "model_spend_cap_exceeded"
    assert supervisor._reserve("run1", 1.0) is False
    control.deny = None  # the cause is fixed (a limit raised, the day rolled over)
    assert supervisor._reserve("run1", 1.0) is True
    keys = [c[3] for c in control.calls]
    assert keys == [SpendLedger._key("run1", 1, "reserve"), SpendLedger._key("run1", 2, "reserve")]
    assert [r["state"] for r in store.spend_rows("run1")] == ["denied", "reserved"]


def test_an_unreachable_ledger_does_not_advance_the_sequence(tmp_path, creds):
    store, supervisor, control, _, _ = rig(tmp_path, creds, ok())
    control.down = True
    with pytest.raises(RuntimeBlocked):
        supervisor._reserve("run1", 1.0)
    control.down = False
    assert supervisor._reserve("run1", 1.0) is True
    assert {c[3] for c in control.calls} == {SpendLedger._key("run1", 1, "reserve")}
    assert [r["state"] for r in store.spend_rows("run1")] == ["reserved"]


# -- the report shows the control plane's resolution of an open reservation --


def _uncertain(tmp_path, creds):
    store, supervisor, control, _, _ = rig(tmp_path, creds, ConnectionError("boom"))
    supervisor.tick("run1", PROMPT)
    (open_row,) = [r for r in store.spend_rows("run1") if r["state"] == "reserved"]
    return store, supervisor, control, open_row


def _resolved(call_id, status="released", resolution="person_released", micro=None):
    return {"call_id": call_id, "status": status, "resolution": resolution,
            "settled_micro_cents": micro, "day": "2026-01-01"}  # fmt: skip


def test_the_report_shows_a_persons_resolution_and_leaves_the_run_alone(tmp_path, creds):
    store, supervisor, control, open_row = _uncertain(tmp_path, creds)
    before = dict(store.get("run1"))
    control.statuses[open_row["call_id"]] = _resolved(open_row["call_id"])
    rr.refresh_spend_rows(store, supervisor.spend, "run1")
    (row,) = [r for r in store.spend_rows("run1") if r["call_id"] == open_row["call_id"]]
    assert row["state"] == "released" and row["resolution"] == "person_released"
    assert row["settled_micro_cents"] is None  # a release records no amount
    assert dict(store.get("run1")) == before  # state, reason, tokens, cost: untouched
    # A spent resolution is shown too, and a second refresh asks nothing more about it.
    store2, supervisor2, control2, open2 = _uncertain(tmp_path / "again", creds)
    control2.statuses[open2["call_id"]] = _resolved(
        open2["call_id"], "settled", "person_settled", 1_500_000
    )
    rr.refresh_spend_rows(store2, supervisor2.spend, "run1")
    rows = store2.spend_rows("run1")
    assert [
        (r["state"], r["resolution"], r["settled_micro_cents"]) for r in rows if r["call_id"]
    ] == [
        ("settled", "person_settled", 1_500_000)  # the amount a person recorded is in the report
    ]
    asked = len([c for c in control2.calls if c[0] == "GET"])
    rr.refresh_spend_rows(store2, supervisor2.spend, "run1")
    assert len([c for c in control2.calls if c[0] == "GET"]) == asked


def test_a_reservation_still_open_on_the_control_plane_stays_open_in_the_report(tmp_path, creds):
    store, supervisor, control, open_row = _uncertain(tmp_path, creds)
    rr.refresh_spend_rows(store, supervisor.spend, "run1")  # the control plane says reserved
    (row,) = [r for r in store.spend_rows("run1") if r["call_id"] == open_row["call_id"]]
    assert row["state"] == "reserved" and row["resolution"] is None


def test_an_unreachable_or_malformed_control_plane_leaves_the_rows_as_they_were(tmp_path, creds):
    store, supervisor, control, open_row = _uncertain(tmp_path, creds)
    before = store.spend_rows("run1")
    control.down = True
    rr.refresh_spend_rows(store, supervisor.spend, "run1")
    assert store.spend_rows("run1") == before
    control.down = False
    control.statuses[open_row["call_id"]] = {"call_id": "other", "status": "released"}
    rr.refresh_spend_rows(store, supervisor.spend, "run1")
    control.statuses[open_row["call_id"]] = _resolved(open_row["call_id"], resolution="a_hacker")
    rr.refresh_spend_rows(store, supervisor.spend, "run1")
    assert store.spend_rows("run1") == before


def test_the_ledger_status_read_is_allowlisted_and_never_raises():
    class C:
        def __init__(self, answer):
            self.answer = answer

        def call(self, method, path, body=None, key=None):
            assert (method, path) == ("GET", "/model-spend/reservations/c1?run_id=run1")
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    def read(answer):
        return SpendLedger(C(answer), "p", "anthropic", QUALIFIED).status("c1", "run1")

    good = read(_resolved("c1", "settled", "person_settled", 7))
    assert (good.status, good.resolution, good.settled_micro_cents) == (
        "settled",
        "person_settled",
        7,
    )
    assert read({"call_id": "c1", "status": "reserved", "resolution": None}).status == "reserved"
    for bad in (
        OSError(),
        None,
        [],
        {"call_id": "c2", "status": "released"},
        {"call_id": "c1", "status": "weird"},
        {"call_id": "c1", "status": "released", "resolution": "nobody"},
        {"call_id": "c1", "status": "settled", "settled_micro_cents": -1},
        {"call_id": "c1", "status": "settled", "settled_micro_cents": 1.5},
    ):
        assert read(bad) is None
    assert SpendLedger(C({}), "p", "anthropic", QUALIFIED).status("../x", "run1") is None
    assert SpendLedger(C({}), "p", "anthropic", QUALIFIED).status("c1", "") is None


def test_a_422_from_the_run_scoped_route_leaves_the_rows_as_they_were(tmp_path, creds):
    """An older supervisor sends no run_id and the route answers 422; the status read turns any
    such failure into "no answer" and the report keeps its rows (mixed versions only lose the
    resolved view). Also: with no run id the client does not even ask."""
    store, supervisor, control, open_row = _uncertain(tmp_path, creds)
    control.statuses[open_row["call_id"]] = _resolved(open_row["call_id"])
    before = store.spend_rows("run1")
    asked = []

    class Answers422:
        def call(self, method, path, body=None, key=None):
            asked.append(path)
            raise RuntimeError("HTTP 422")

    ledger = SpendLedger(Answers422(), "p", "anthropic", QUALIFIED)
    assert ledger.status(open_row["call_id"], "") is None and asked == []
    assert ledger.status(open_row["call_id"], "run1") is None and len(asked) == 1
    rr.refresh_spend_rows(store, ledger, "run1")
    assert store.spend_rows("run1") == before


def test_the_status_read_names_the_run_and_quotes_it():
    seen = []

    class C:
        def call(self, method, path, body=None, key=None):
            seen.append(path)
            raise OSError("stop")

    SpendLedger(C(), "p", "anthropic", QUALIFIED).status("c1", "run 1/&x")
    assert seen == ["/model-spend/reservations/c1?run_id=run%201%2F%26x"]


def test_another_runs_reservation_is_never_reported(tmp_path, creds):
    """The control plane refuses a call id that belongs to another run; the report then keeps the
    local row exactly as it was (it never shows or adopts the other run's resolution)."""
    store, supervisor, control, open_row = _uncertain(tmp_path, creds)
    before = store.spend_rows("run1")
    control.call_runs[open_row["call_id"]] = "some-other-run"
    control.statuses[open_row["call_id"]] = _resolved(open_row["call_id"])
    rr.refresh_spend_rows(store, supervisor.spend, "run1")
    assert store.spend_rows("run1") == before
    assert [c[1] for c in control.calls if c[0] == "GET"] == [
        f"/model-spend/reservations/{open_row['call_id']}?run_id=run1"
    ]


def test_an_unbilled_destination_or_missing_credentials_never_reach_the_control_plane(
    tmp_path, monkeypatch
):
    store = ReasoningStore(tmp_path / "state")
    profile = {
        "control_url": "http://127.0.0.1:1",
        "agent_token_env": "NO_SUCH_TOKEN_VARIABLE",
        "destination": "d",
        "run_id": "run1",
        "model": {"providerID": "anthropic", "id": "m"},
    }

    class Control:
        def call(self, *args, **kwargs):
            raise AssertionError("the control plane must not be asked")

    unbilled = type("Found", (), {"billed": False})()
    rr._refresh_report_spend(store, profile, unbilled, Control())
    billed = type("Found", (), {"billed": True})()
    monkeypatch.delenv("NO_SUCH_TOKEN_VARIABLE", raising=False)
    rr._refresh_report_spend(store, profile, billed)  # no credential: silently nothing


def test_an_older_local_table_is_upgraded_in_place_and_its_rows_survive(tmp_path):
    import sqlite3

    directory = tmp_path / "state"
    store = ReasoningStore(directory)
    store.bind("run1", {"grant_id": "g", "revision": 1, "epoch": 1}, 1.0)
    store.spend_reserved("run1", 1, "call1", 2.5)
    store.close()
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    db.execute("ALTER TABLE model_spend_reservations DROP COLUMN resolution")  # the old shape
    db.execute("ALTER TABLE model_spend_reservations DROP COLUMN settled_micro_cents")
    db.commit()
    db.close()
    store = ReasoningStore(directory)
    assert store.spend_rows("run1") == [
        {
            "seq": 1,
            "call_id": "call1",
            "state": "reserved",
            "reserved_cents": 2.5,
            "resolution": None,
            "settled_micro_cents": None,
        }  # fmt: skip
    ]
    store.spend_resolve("run1", 1, "settled", "person_settled", 2_500_000)
    assert store.spend_rows("run1")[0]["resolution"] == "person_settled"
    assert store.spend_rows("run1")[0]["settled_micro_cents"] == 2_500_000
