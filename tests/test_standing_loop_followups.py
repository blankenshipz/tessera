import pytest
from test_standing_loop import make

from tessera_agent.standing_loop import CallNotStarted, KillSwitchEngaged
from tessera_agent.stuck_loop_action import _ITEM_ID
from tessera_agent.stuck_loop_control import _ACK, FINDING_ID

FID = "stuck_" + "a" * 32
HANDLER_REASONS = [
    "kill_switch",
    "model_not_allowed",
    "malformed_item",
    "ack_refused_409",
    "ack_not_allowed",
]


def _finished(events):
    return [e for e in events if e["event"] == "call_finished"]


@pytest.mark.parametrize("reason", HANDLER_REASONS)
def test_refusal_reason_is_journaled(tmp_path, reason):
    def start(item, **kw):
        raise CallNotStarted(reason)

    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    loop.tick()
    (done,) = _finished(events)
    assert done["outcome"] == "failed" and done["reason"] == reason
    assert done["error"] == "CallNotStarted"


@pytest.mark.parametrize(
    "text", ["boom secret detail", "x" * 65, "Upper", "host.example", "a/b", "", "a\nb", "ünï"]
)
def test_unsafe_reason_is_dropped(tmp_path, text):
    def start(item, **kw):
        raise CallNotStarted(text)

    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    loop.tick()
    (done,) = _finished(events)
    assert "reason" not in done
    assert text not in str(events) or text == ""


def test_max_length_reason_is_kept(tmp_path):
    def start(item, **kw):
        raise CallNotStarted("a" * 64)

    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    loop.tick()
    assert _finished(events)[0]["reason"] == "a" * 64


def test_uncertain_path_never_records_a_reason(tmp_path):
    def start(item, **kw):
        raise RuntimeError("safe_looking")

    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    loop.tick()
    assert all("reason" not in e for e in events if e["event"] != "skipped")
    assert "safe_looking" not in str(events)


def test_kill_switch_at_handler_never_counts_or_halts(tmp_path):
    def start(item, **kw):
        raise KillSwitchEngaged("kill_switch")

    loop, events, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    for _ in range(8):  # well past max_consecutive_failures=3
        assert loop.tick()["state"] == "idle"
    done = _finished(events)
    assert len(done) == 8
    assert all(e["outcome"] == "kill_switch" and e["reason"] == "kill_switch" for e in done)
    assert all("error" not in e for e in done)
    assert loop._failures == 0


def test_kill_switch_does_not_reset_existing_failures(tmp_path):
    mode = ["fail"]

    def start(item, **kw):
        raise (CallNotStarted("x") if mode[0] == "fail" else KillSwitchEngaged("kill_switch"))

    loop, _, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    loop.tick()
    mode[0] = "kill"
    loop.tick()
    assert loop._failures == 1


def test_genuine_call_not_started_still_halts_at_cap(tmp_path):
    def start(item, **kw):
        raise CallNotStarted("ack_refused_500")

    loop, _, _, _ = make(tmp_path, [{"id": "a", "kind": "k"}], start)
    assert [loop.tick()["state"] for _ in range(3)] == ["idle", "idle", "halted"]


@pytest.mark.parametrize(
    "fid,ok",
    [
        (FID, True),
        ("stuck_" + "0" * 32, True),
        ("stuck_" + "a" * 31, False),
        ("stuck_" + "a" * 33, False),
        ("stuck_" + "A" * 32, False),
        ("stuck_" + "g" * 32, False),
        ("a:b", False),
        ("x" * 40, False),
        ("../" + FID, False),
        (FID + ":x", False),
    ],
)
def test_finding_id_patterns_agree(fid, ok):
    assert bool(_ITEM_ID.fullmatch(f"{fid}:1")) is ok
    assert bool(_ACK.fullmatch(f"/projects/p/campaign-stuck/{fid}/acknowledge")) is ok
    assert bool(__import__("re").fullmatch(FINDING_ID, fid)) is ok
