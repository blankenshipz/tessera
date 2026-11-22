"""Cleanup confirmation: exact-ID O(1) absence query and its own bound (offline)."""

import json

import pytest

from tessera_worker.local_docker_engine import (
    CLEANUP_CALL_SECONDS,
    DEFAULT_CALL_SECONDS,
    DockerReplyUnknown,
    LocalDockerEngine,
)
from tessera_worker.local_docker_namespace import STRONG_CLASS, WEAK_CLASS

CID = "c" * 64
OTHER = "d" * 64
IMAGE_ID = "sha256:" + "a" * 64


class Plan:
    name = "tessera-fixture-x"
    labels = {"tessera.fixture": "1"}
    output_volume_name = "tessera-output-" + "b" * 64


def value(running=False):
    return dict(
        Id=CID,
        Name="/" + Plan.name,
        Image=IMAGE_ID,
        Config=dict(Labels=Plan.labels),
        State=dict(Running=running, Status="exited"),
    )


def engine(listing, calls, cls=STRONG_CLASS, rm=b""):
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["container", "inspect"]:
            return json.dumps([value()]).encode()
        if args[0] == "rm":
            if isinstance(rm, Exception):
                raise rm
            return rm
        if args[:2] == ["container", "ls"]:
            if isinstance(listing, Exception):
                raise listing
            return listing
        raise AssertionError(args)

    return LocalDockerEngine(run, attestation_class=cls)


@pytest.mark.parametrize("cls", [STRONG_CLASS, WEAK_CLASS])
def test_absence_is_empty_exact_filtered_reply(cls):
    calls = []
    engine(b"", calls, cls).cleanup(Plan, IMAGE_ID, CID)
    args, kwargs = calls[-1]
    assert args == [
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        f"id={CID}",
        "--format",
        "{{.ID}}",
    ]
    assert kwargs["timeout"] == CLEANUP_CALL_SECONDS > DEFAULT_CALL_SECONDS == 2.0


@pytest.mark.parametrize("cls", [STRONG_CLASS, WEAK_CLASS])
@pytest.mark.parametrize(
    "listing",
    [
        DockerReplyUnknown(),
        TimeoutError(),
        (CID + "\n").encode(),
        (OTHER + "\n").encode(),
        b"not-an-id\n",
        (OTHER + "\nnot-an-id\n").encode(),
        b"\xff\xfe",
    ],
)
def test_absence_fails_closed(cls, listing):
    with pytest.raises((DockerReplyUnknown, Exception)) as raised:
        engine(listing, [], cls).cleanup(Plan, IMAGE_ID, CID)
    assert isinstance(raised.value, (DockerReplyUnknown, TimeoutError))


def test_rm_failure_is_unknown_and_no_absence_query():
    calls = []
    with pytest.raises(DockerReplyUnknown):
        engine(b"", calls, rm=DockerReplyUnknown()).cleanup(Plan, IMAGE_ID, CID)
    assert not any(a[:2] == ["container", "ls"] for a, _ in calls)


def test_malformed_cid_never_builds_a_filter():
    calls = []
    with pytest.raises(DockerReplyUnknown):
        engine(b"", calls).confirm_absent("c" * 63 + ",name=x")
    assert calls == []


def test_cleanup_calls_use_cleanup_bound_and_others_keep_default():
    calls = []
    e = engine(b"", calls)
    e.cleanup(Plan, IMAGE_ID, CID)
    assert calls and all(k["timeout"] == CLEANUP_CALL_SECONDS for _, k in calls)
    calls.clear()
    e.inspect(Plan, IMAGE_ID, CID)
    assert calls[0][1] == {}  # runner default (2.0) applies
    assert DEFAULT_CALL_SECONDS == 2.0


def _stop_engine(calls, *, running=True, paused=False):
    """Fake docker whose every call records (args, timeout); `timeout` omitted means the
    runner's own 2 s default. A hang is modelled by the recorded bound, not by sleeping."""
    state = {"running": running}

    def run(args, timeout=DEFAULT_CALL_SECONDS, **kwargs):
        calls.append((list(args), timeout))
        if args[:2] == ["container", "inspect"]:
            body = value(state["running"])
            body["State"].update(Paused=paused, Status="running" if state["running"] else "exited")
            return json.dumps([body]).encode()
        if args[0] == "kill":
            state["running"] = False
            return b""
        if args[0] == "unpause":
            return b""
        raise AssertionError(args)

    return LocalDockerEngine(run, attestation_class=STRONG_CLASS)


def test_stop_bounds_pre_kill_calls_short_and_only_readback_long():
    calls = []
    _stop_engine(calls, running=True, paused=True).stop(Plan, IMAGE_ID, CID)
    verbs = [args[0] if args[0] != "container" else "inspect" for args, _ in calls]
    assert verbs == ["inspect", "unpause", "kill", "inspect"]
    bounds = [timeout for _, timeout in calls]
    # inspect, unpause, kill happen while the workload may still run: short bound each.
    assert bounds[:3] == [DEFAULT_CALL_SECONDS] * 3
    # worst case before the workload is confirmed dead is three short bounds, not 3 x 10 s
    assert sum(bounds[:3]) == 6.0 < CLEANUP_CALL_SECONDS
    # only the post-kill readback gets the longer bound
    assert bounds[3] == CLEANUP_CALL_SECONDS


def test_stop_of_an_exited_container_uses_long_bound_only_for_readback():
    calls = []
    _stop_engine(calls, running=False).stop(Plan, IMAGE_ID, CID)
    assert [a[0] for a, _ in calls] == ["container", "container"]
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS, CLEANUP_CALL_SECONDS]


def test_stop_hanging_kill_surfaces_after_one_short_bound_and_no_readback():
    calls = []

    def run(args, timeout=DEFAULT_CALL_SECONDS, **kwargs):
        calls.append((list(args), timeout))
        if args[:2] == ["container", "inspect"]:
            return json.dumps([value(True)]).encode()
        raise TimeoutError()  # dockerd hangs: the runner raises at its deadline

    with pytest.raises(TimeoutError):
        LocalDockerEngine(run, attestation_class=STRONG_CLASS).stop(Plan, IMAGE_ID, CID)
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS, DEFAULT_CALL_SECONDS]
    assert calls[-1][0][0] == "kill"


def _racing_engine(calls, *, fail, running_after, inspect_fails_after=False, paused=False):
    """Fake docker whose unpause/kill fail (the container stopped on its own first)."""
    state = {"inspects": 0}

    def run(args, timeout=DEFAULT_CALL_SECONDS, **kwargs):
        calls.append((list(args), timeout))
        if args[:2] == ["container", "inspect"]:
            state["inspects"] += 1
            if state["inspects"] > 1 and inspect_fails_after:
                raise DockerReplyUnknown()
            first = state["inspects"] == 1
            running = True if first else running_after
            body = value(running)
            body["State"].update(Paused=paused and first, Status="running" if running else "exited")
            return json.dumps([body]).encode()
        if args[0] in fail:
            raise DockerReplyUnknown()
        raise AssertionError(args)

    return LocalDockerEngine(run, attestation_class=STRONG_CLASS)


def test_kill_that_loses_the_race_with_the_workload_exit_is_benign_once_reinspected():
    calls = []
    observed = _racing_engine(calls, fail={"kill"}, running_after=False).stop(Plan, IMAGE_ID, CID)
    assert observed["State"]["Running"] is False
    # inspect, kill (fails), short re-inspection, then the long-bound readback.
    assert [a[0] if a[0] != "container" else "inspect" for a, _ in calls] == [
        "inspect",
        "kill",
        "inspect",
        "inspect",
    ]
    assert [t for _, t in calls] == [
        DEFAULT_CALL_SECONDS,
        DEFAULT_CALL_SECONDS,
        DEFAULT_CALL_SECONDS,
        CLEANUP_CALL_SECONDS,
    ]


def _verbs(calls):
    return [a[0] if a[0] != "container" else "inspect" for a, _ in calls]


def test_unpause_that_loses_the_race_is_benign_once_reinspected():
    calls = []
    observed = _racing_engine(
        calls, fail={"unpause", "kill"}, running_after=False, paused=True
    ).stop(Plan, IMAGE_ID, CID)
    assert observed["State"]["Running"] is False
    # The kill decision uses the first (stale, running) inspection, so kill is still tried;
    # it loses the same race and is benign under the same re-inspection rule.
    assert _verbs(calls) == ["inspect", "unpause", "inspect", "kill", "inspect", "inspect"]
    assert calls[1][0] == ["unpause", CID]
    assert calls[3][0] == ["kill", "--signal", "KILL", CID]
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS] * 5 + [CLEANUP_CALL_SECONDS]


def test_failed_unpause_while_the_container_is_still_running_stays_unknown():
    calls = []
    with pytest.raises(DockerReplyUnknown):
        _racing_engine(calls, fail={"unpause"}, running_after=True, paused=True).stop(
            Plan, IMAGE_ID, CID
        )
    # Re-inspection shows it running: no kill and no long-bound readback follow.
    assert _verbs(calls) == ["inspect", "unpause", "inspect"]
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS] * 3


def test_failed_unpause_for_a_container_that_can_no_longer_be_inspected_stays_unknown():
    calls = []
    with pytest.raises(DockerReplyUnknown):
        _racing_engine(
            calls, fail={"unpause"}, running_after=False, inspect_fails_after=True, paused=True
        ).stop(Plan, IMAGE_ID, CID)
    assert _verbs(calls) == ["inspect", "unpause", "inspect"]
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS] * 3


def test_failed_kill_while_the_container_is_still_running_stays_unknown():
    calls = []
    with pytest.raises(DockerReplyUnknown):
        _racing_engine(calls, fail={"kill"}, running_after=True).stop(Plan, IMAGE_ID, CID)
    # No readback is attempted after an unconfirmed kill.
    assert _verbs(calls) == ["inspect", "kill", "inspect"]
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS] * 3


def test_failed_kill_for_a_container_that_can_no_longer_be_inspected_stays_unknown():
    calls = []
    with pytest.raises(DockerReplyUnknown):
        _racing_engine(calls, fail={"kill"}, running_after=False, inspect_fails_after=True).stop(
            Plan, IMAGE_ID, CID
        )
    assert _verbs(calls) == ["inspect", "kill", "inspect"]
    assert [t for _, t in calls] == [DEFAULT_CALL_SECONDS] * 3
