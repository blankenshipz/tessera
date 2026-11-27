"""The Salad executor keeps bounded memory across a long-running service (fakes only).

``run_once`` returns the pass's notes (the service loop journals them); the executor keeps only
that pass's notes, and per-attempt finish state of attempts control no longer lists is dropped.
State that guards a forward step (a cached lease, a fence, a once-only no-receipt report) is kept.
"""

from __future__ import annotations

from test_campaign_salad_executor import FakeControl, Harness, item


def _queued():
    # A queued route whose grant is not pending yet: one ``not_pending`` note every pass.
    control = FakeControl(pending=())
    control.items = [item("awaiting_lease", attempt_id=f"att_{n}") for n in range(5)]
    return control


def test_many_passes_keep_only_the_last_pass_notes(tmp_path):
    h = Harness(tmp_path, _queued())
    expected = [{"action": "not_pending", "attempt_id": f"att_{n}", "detail": ""} for n in range(5)]
    returned = []
    for _ in range(500):
        actions = h.executor.run_once()
        returned.append(actions)
        assert actions == expected
        assert len(h.executor.log) == len(expected)
    # Each pass's return value is its own list, never changed by a later pass.
    assert all(actions == expected for actions in returned)
    assert len({id(actions) for actions in returned}) == len(returned)


def test_an_unavailable_work_list_returns_that_pass_only(tmp_path):
    h = Harness(tmp_path, _queued())
    h.executor.run_once()

    def down():
        raise RuntimeError("control unavailable")

    h.control.work = down
    actions = h.executor.run_once()
    assert actions == [{"action": "work_unavailable", "attempt_id": None, "detail": "RuntimeError"}]
    assert h.executor.log == actions


def test_finish_state_of_unlisted_attempts_is_pruned_and_guards_are_kept(tmp_path):
    h = Harness(tmp_path, _queued())
    executor = h.executor
    for attempt in ("att_0", "gone", "deferred"):
        executor._final_retryable[attempt] = True
        executor._termination_waited.add(attempt)
        executor.no_receipt.add(attempt)
    executor._finish_deferred["deferred"] = (0.0, 1)
    executor._leases["gone"] = ("lease_9", 1)
    executor.fenced["gone"] = "Conflict"
    executor._no_receipt_resumed.add("gone")

    for _ in range(3):
        executor.run_once()

    # Listed (att_0) and deferred-finish attempts keep their state; the ended one loses it.
    assert set(executor._final_retryable) == {"att_0", "deferred"}
    assert executor._termination_waited == {"att_0", "deferred"}
    assert executor.no_receipt == {"att_0", "deferred"}
    # Forward-step guards stay (a fence, a cached lease, a reported no-receipt marker).
    assert executor._leases["gone"] == ("lease_9", 1)
    assert executor.fenced["gone"] == "Conflict"
    assert "gone" in executor._no_receipt_resumed


def test_pruned_state_does_not_grow_over_many_ended_attempts(tmp_path):
    h = Harness(tmp_path, _queued())
    executor = h.executor
    for n in range(200):
        attempt = f"ended_{n}"
        executor._final_retryable[attempt] = False
        executor._termination_waited.add(attempt)
        executor.no_receipt.add(attempt)
        executor.run_once()
    assert executor._final_retryable == {}
    assert executor._termination_waited == set()
    assert executor.no_receipt == set()
