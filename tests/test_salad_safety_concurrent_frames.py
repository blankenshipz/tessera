"""``max_open_billable_frames`` above 1 (ADR 0041 section 7.2, resolved question 4b).

Every per-frame rule holds for each frame, and the total cap holds in aggregate over all frames.
Fake provider only; no network."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

pytest.importorskip("cryptography")
import test_salad_safety_service as base
from cryptography.hazmat.primitives.asymmetric import ec
from test_salad_safety_service import GROUP, Provider, make_service, reservation

from tessera_control.salad_safety_service import (
    EXTRA_FRAME_PASS_SECONDS,
    MAX_OPEN_BILLABLE_FRAMES_LIMIT,
    UNSEEN_RUN_GAP_SECONDS,
    unseen_run_gap_seconds,
)


class GroupRateProvider(Provider):
    """The shared fake, with a published rate per reserved group name."""

    def __init__(self):
        super().__init__()
        self.rates = {}

    def factory(self, selection):
        inner = super().factory(selection)
        owner = self

        class Transport:
            def gpu_class(self, chosen, secret):
                saved = owner.rate
                owner.rate = owner.rates.get(chosen.salad.group, saved)
                try:
                    return inner.gpu_class(chosen, secret)
                finally:
                    owner.rate = saved

            def read_absence(self, *args):
                return inner.read_absence(*args)

            def send(self, *args):
                return inner.send(*args)

        return Transport()


def _body(operation, now, group=None, **kwargs):
    """A reserve for ``operation`` whose selection names its own group (``attempt-<op>``)."""
    group = group or "attempt-" + operation
    original = base.selection

    class Chosen:
        def model_dump(self, mode):
            value = original().model_dump(mode=mode)
            value["salad"]["group"] = group
            return value

    base.selection = Chosen
    try:
        return reservation(now=now, operation=operation, **kwargs)
    finally:
        base.selection = original


def _service(tmp_path, provider=None, **kwargs):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = provider or Provider()
    service = make_service(
        tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider, **kwargs
    )
    service.resolver.expires_at = 100 + 86400
    return service, clock, provider


def _reserve(service, clock, operation, **kwargs):
    body = _body(operation, clock[0], **kwargs)
    service.loop()
    service.admin(body)
    return body


def _refusals(service):
    rows = service.db.execute(
        "SELECT reservation, operation, open_frames, frame_limit FROM fence_refusals "
        "ORDER BY sequence"
    ).fetchall()
    return [tuple(row) for row in rows]


def _count(service):
    return service.db.execute("SELECT count(*) FROM reservations").fetchone()[0]


def _close_absent(service, body):
    with service.db:
        service.db.execute(
            "UPDATE reservations SET recovery=1, lost_create_resolution=? WHERE reservation=?",
            (json.dumps({"kind": "automatic", "resolved_at": 100.0}), body["reservation"]),
        )


def _bind(service, body):
    service.admin(
        dict(
            operation="bind", reservation=body["reservation"], provider_id=GROUP, allocation_id=None
        )
    )


def _status(service, body):
    return service.admin(dict(operation="status", reservation=body["reservation"]))


# (a) the default stays one frame.
def test_default_is_one_and_a_second_reserve_is_refused(tmp_path):
    service, clock, _ = _service(tmp_path)
    try:
        first = _reserve(service, clock, "a" * 32)
        assert service.max_open_billable_frames == 1
        assert service.unseen_run_gap_seconds == UNSEEN_RUN_GAP_SECONDS == 45
        second = _body("b" * 32, clock[0])
        service.loop()
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            service.admin(second)
        assert _refusals(service) == [(second["reservation"], "b" * 32, 1, 1)]
        assert _count(service) == 1
        status = _status(service, first)
        assert status["max_open_billable_frames"] == 1
        assert status["unseen_run_gap_seconds"] == 45
    finally:
        service.close()


# (b) two admitted, the third refused with a receipt naming only itself.
def test_two_frames_are_admitted_and_a_third_is_refused(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2)
    try:
        _reserve(service, clock, "a" * 32)  # ambiguous creates: no provider id, both count
        _reserve(service, clock, "b" * 32)
        assert _refusals(service) == []
        third = _body("c" * 32, clock[0])
        service.loop()
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            service.admin(third)
        assert _refusals(service) == [(third["reservation"], "c" * 32, 2, 2)]
        assert _count(service) == 2
        # An idempotent retry of an admitted reserve is never counted against itself.
        service.loop()
        service.admin(_body("b" * 32, 100.0))
        assert _count(service) == 2
    finally:
        service.close()


# (c) the total cap is enforced over both frames, in the insert's transaction.
def test_total_cap_is_aggregate_over_every_frame(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2, total_cap_cents=19)
    try:
        first = _reserve(service, clock, "a" * 32)
        assert Decimal(_status(service, first)["total_committed_cents"]) == 10
        with pytest.raises(ValueError, match="total_budget_exhausted"):
            _reserve(service, clock, "b" * 32)
        assert _refusals(service) == []  # a total-cap refusal is not a fence refusal
        assert _count(service) == 1
    finally:
        service.close()


def test_two_holds_that_fit_are_both_admitted_and_summed(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2, total_cap_cents=20)
    try:
        first = _reserve(service, clock, "a" * 32)
        _reserve(service, clock, "b" * 32)
        status = _status(service, first)
        assert Decimal(status["total_committed_cents"]) == 20
        assert Decimal(status["total_remaining_cents"]) == 0
    finally:
        service.close()


def test_cap_sum_and_open_count_are_read_inside_the_insert_transaction(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2, total_cap_cents=20)
    try:
        _reserve(service, clock, "a" * 32)
        seen = []
        real_committed, real_open = service._committed, service._open_frames

        def committed(**kwargs):
            if kwargs.get("account") is False:
                seen.append(("committed", service.db.in_transaction))
            return real_committed(**kwargs)

        def open_frames(operation, *, account=True):
            if not account:
                seen.append(("open", service.db.in_transaction))
            return real_open(operation, account=account)

        service._committed, service._open_frames = committed, open_frames
        second = _body("b" * 32, clock[0])
        service.loop()  # the loop's own aggregate check reads outside any reserve
        seen.clear()
        service.admin(second)
        assert ("committed", True) in seen and ("open", True) in seen
        assert all(inside for _, inside in seen)
    finally:
        service.close()


# (d) one frame's accrual never masks the other's hold; its cap breach stops only itself.
def _two_frames_one_running(tmp_path, total_cap_cents):
    provider = GroupRateProvider()
    service, clock, _ = _service(
        tmp_path, provider, max_open_billable_frames=2, total_cap_cents=total_cap_cents
    )
    a = _reserve(service, clock, "a" * 32, seconds=300, workload_seconds=60)
    b = _reserve(service, clock, "b" * 32, seconds=300, workload_seconds=60)
    _bind(service, a)
    provider.state = "running"
    for _ in range(3):
        clock[0] += 5
        service.loop()
        for body in (a, b):
            service.admin(dict(operation="heartbeat", reservation=body["reservation"]))
    return service, clock, provider, a, b


def test_one_frames_accrual_never_reduces_the_other_frames_hold(tmp_path):
    service, clock, _provider, a, b = _two_frames_one_running(tmp_path, 40)
    try:
        accrued = Decimal(service._row(a["reservation"])["accrued_cents"])
        assert accrued > 0  # A runs and accrues
        assert Decimal(service._row(b["reservation"])["accrued_cents"]) == 0
        # Each unsettled frame counts max(accrued, hold): 10 + 10 while A is under its hold.
        assert service._committed(account=False) == 20
        with service.db:  # A's accrual past its own hold is counted in full, B's hold stays
            service.db.execute(
                "UPDATE reservations SET accrued_cents='12' WHERE reservation=?",
                (a["reservation"],),
            )
        assert service._committed(account=False) == 22
        assert Decimal(_status(service, b)["total_committed_cents"]) >= 22
    finally:
        service.close()


def test_a_cap_breach_stops_only_that_frame(tmp_path):
    service, clock, provider, a, b = _two_frames_one_running(tmp_path, 40)
    try:
        provider.rates["attempt-" + "a" * 32] = "5000"  # A's own rate jumps past its cap
        clock[0] += 5
        service.loop()
        row_a, row_b = service._row(a["reservation"]), service._row(b["reservation"])
        assert row_a["budget_fenced"] == 1 and row_a["recovery"] == 1
        assert service.guardian.status("a" * 32)["status"] != "armed"
        assert row_b["budget_fenced"] == 0 and row_b["recovery"] == 0
        service.admin(dict(operation="heartbeat", reservation=b["reservation"]))  # B continues
        # The aggregate still holds: A's accrual (or hold) plus B's hold, within the cap.
        assert service._committed(account=False) <= 40
        assert not service.db.execute(
            "SELECT 1 FROM events WHERE kind='total_cap_breached'"
        ).fetchone()
    finally:
        service.close()


def test_an_aggregate_overshoot_fences_every_frame_that_may_bill(tmp_path):
    service, clock, _provider, a, b = _two_frames_one_running(tmp_path, 20)
    try:
        with service.db:  # A overshoots its hold before its stop lands: 15 + 10 > 20
            service.db.execute(
                "UPDATE reservations SET accrued_cents='15' WHERE reservation=?",
                (a["reservation"],),
            )
        clock[0] += 1
        service.loop()
        for body in (a, b):
            row = service._row(body["reservation"])
            assert row["budget_fenced"] == 1 and row["recovery"] == 1
        kinds = service.db.execute(
            "SELECT reservation FROM events WHERE kind='total_cap_breached'"
        ).fetchall()
        # The aggregate fences every frame not already fenced (A's own rate check may not be
        # due in this pass, so A may be fenced by either rule), and B, under its hold, too.
        assert b["reservation"] in {k for (k,) in kinds}
        assert {k for (k,) in kinds} <= {a["reservation"], b["reservation"]}
        with pytest.raises(ValueError, match="stop_only"):
            service.admin(dict(operation="heartbeat", reservation=b["reservation"]))
    finally:
        service.close()


def test_an_overshoot_within_the_aggregate_leaves_the_other_frame_running(tmp_path):
    service, clock, _provider, a, b = _two_frames_one_running(tmp_path, 25)
    try:
        with service.db:  # 12 + 10 <= 25: only the per-frame fence applies, and only to A
            service.db.execute(
                "UPDATE reservations SET accrued_cents='12' WHERE reservation=?",
                (a["reservation"],),
            )
        clock[0] += 5
        service.loop()
        assert service._row(a["reservation"])["budget_fenced"] == 1
        assert service._row(b["reservation"])["budget_fenced"] == 0
    finally:
        service.close()


# (e) a stop or settlement frees exactly one slot; an ambiguous create still counts.
def test_a_settled_frame_frees_exactly_one_slot(tmp_path):
    service, clock, provider = _service(tmp_path, max_open_billable_frames=2)
    try:
        a = _reserve(service, clock, "a" * 32)
        b = _reserve(service, clock, "b" * 32)
        provider.group_missing = True  # the name reads unknown: B's create stays ambiguous
        service.admin(dict(operation="cancel", reservation=b["reservation"]))
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            _reserve(service, clock, "c" * 32)  # a cancelled ambiguous create still counts
        _close_absent(service, a)
        _reserve(service, clock, "c" * 32)
        fourth = _body("d" * 32, clock[0])
        service.loop()
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            service.admin(fourth)
        assert [row[2:] for row in _refusals(service)] == [(2, 2), (2, 2)]
        assert _refusals(service)[-1][:2] == (fourth["reservation"], "d" * 32)
    finally:
        service.close()


def test_a_stopped_bound_frame_frees_its_slot(tmp_path):
    service, clock, provider = _service(tmp_path, max_open_billable_frames=2)
    try:
        a = _reserve(service, clock, "a" * 32)
        _reserve(service, clock, "b" * 32)
        _bind(service, a)
        provider.state = "running"
        clock[0] += 5
        service.loop()
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            _reserve(service, clock, "c" * 32)
        service.admin(dict(operation="cancel", reservation=a["reservation"]))
        provider.state = "stopped"
        clock[0] += 5
        service.loop()
        assert _status(service, a)["status"] == "stopped"
        _reserve(service, clock, "c" * 32)
        assert _count(service) == 3
    finally:
        service.close()


# Two open frames never share one provider group.
def test_two_open_frames_never_share_a_reserved_group_name(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2)
    try:
        _reserve(service, clock, "a" * 32, group="attempt-shared")
        with pytest.raises(ValueError, match="provider_group_in_use"):
            _reserve(service, clock, "b" * 32, group="attempt-shared")
        assert _refusals(service) == [] and _count(service) == 1
    finally:
        service.close()


def test_a_provider_group_held_by_an_open_frame_is_never_bound_to_another(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2)
    try:
        a = _reserve(service, clock, "a" * 32)
        b = _reserve(service, clock, "b" * 32)
        _bind(service, a)
        # The fake answers every name with the same group id: B must not adopt A's group.
        with pytest.raises(ValueError, match="provider_identity_in_use"):
            service.admin(
                dict(
                    operation="bind",
                    reservation=b["reservation"],
                    provider_id=GROUP,
                    allocation_id=None,
                )
            )
        assert service._row(b["reservation"])["provider_id"] is None
    finally:
        service.close()


# The unseen-run gap grows with the frames one pass may read, and every charge uses it.
def test_the_unseen_gap_grows_with_the_setting():
    assert [unseen_run_gap_seconds(n) for n in range(1, MAX_OPEN_BILLABLE_FRAMES_LIMIT + 1)] == [
        45,
        45 + EXTRA_FRAME_PASS_SECONDS,
        45 + 2 * EXTRA_FRAME_PASS_SECONDS,
        45 + 3 * EXTRA_FRAME_PASS_SECONDS,
    ]
    assert EXTRA_FRAME_PASS_SECONDS == 16


@pytest.mark.parametrize("frames,admitted", [(1, True), (2, False)])
def test_admission_charges_the_configured_gap(tmp_path, frames, admitted):
    provider = Provider()
    provider.rate = "5"  # 10 cents buy 72 s: 20 s + 45 s fits, 20 s + 61 s does not
    service, clock, _ = _service(tmp_path, provider, max_open_billable_frames=frames)
    try:
        if admitted:
            _reserve(service, clock, "a" * 32, seconds=20)
        else:
            with pytest.raises(ValueError, match="budget_insufficient"):
                _reserve(service, clock, "a" * 32, seconds=20)
    finally:
        service.close()


def test_backdated_accrual_uses_the_configured_gap(tmp_path):
    service, clock, provider = _service(tmp_path, max_open_billable_frames=2)
    try:
        provider.state = "running"  # no earlier poll saw it not running
        a = _reserve(service, clock, "a" * 32, seconds=300, workload_seconds=60)
        _bind(service, a)
        clock[0] += 1
        service.loop()
        assert service._row(a["reservation"])["first_running_at"] == clock[0] - 61
    finally:
        service.close()


# (f) startup refuses anything but an integer 1..4.
@pytest.mark.parametrize("value", [0, 5, -1, True, False, "2", 2.0, None])
def test_startup_refuses_values_outside_one_to_four(tmp_path, value):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError, match="invalid_max_open_billable_frames"):
        make_service(
            tmp_path,
            lambda: 100.0,
            ec.generate_private_key(ec.SECP256R1()),
            Provider(),
            max_open_billable_frames=value,
        )


# (g) status reports the setting and the gap it implies.
@pytest.mark.parametrize("frames", [2, 3, 4])
def test_status_reports_the_setting(tmp_path, frames):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=frames)
    try:
        status = _status(service, _reserve(service, clock, "a" * 32))
        assert status["max_open_billable_frames"] == frames
        assert status["unseen_run_gap_seconds"] == unseen_run_gap_seconds(frames)
    finally:
        service.close()


# (h) one frame that raises in the loop pass never aborts the pass for the others.
OTHER_GROUP = "c" * 32


class TwoGroupProvider(GroupRateProvider):
    """Each reserved group name reads back as its own provider group id."""

    ids = {"attempt-" + "a" * 32: GROUP, "attempt-" + "b" * 32: OTHER_GROUP}

    def factory(self, selection):
        inner = super().factory(selection)
        ids = self.ids

        class Transport:
            def gpu_class(self, *args):
                return inner.gpu_class(*args)

            def read_absence(self, *args):
                return inner.read_absence(*args)

            def send(self, action, chosen, provider_id, secret):
                result = inner.send(action, chosen, provider_id, secret)
                observation = getattr(result, "observation", None)
                if type(observation) is dict and "provider_id" in observation:
                    observation["provider_id"] = ids.get(chosen.salad.group, GROUP)
                return result

        return Transport()


def _two_bound_running(tmp_path):
    provider = TwoGroupProvider()
    service, clock, _ = _service(tmp_path, provider, max_open_billable_frames=2, total_cap_cents=40)
    a = _reserve(service, clock, "a" * 32, seconds=300, workload_seconds=60)
    b = _reserve(service, clock, "b" * 32, seconds=300, workload_seconds=60)
    for body, group in ((a, GROUP), (b, OTHER_GROUP)):
        service.admin(
            dict(
                operation="bind",
                reservation=body["reservation"],
                provider_id=group,
                allocation_id=None,
            )
        )
    provider.state = "running"
    for _ in range(3):
        clock[0] += 5
        service.loop()
        for body in (a, b):
            service.admin(dict(operation="heartbeat", reservation=body["reservation"]))
    return service, clock, provider, a, b


def _inject(service, where, operation, reservation, error):
    """Make ``where`` raise ``error`` for one frame; returns nothing (patches in place)."""
    if where == "arm":
        real = service.guardian.arm

        def arm(assignment, **kwargs):
            if assignment.binding.operation == operation:
                raise error
            return real(assignment, **kwargs)

        service.guardian.arm = arm
    elif where == "account":
        real_account = service._account
        left = [1]  # the loop's own accounting raises; the aggregate read stays readable

        def account(row, **kwargs):
            if row["reservation"] == reservation and left[0]:
                left[0] -= 1
                raise error
            return real_account(row, **kwargs)

        service._account = account
        return left
    else:  # cancel: reached on the recovery path
        service.admin(dict(operation="cancel", reservation=reservation))
        real_cancel = service.guardian.cancel

        def cancel(op):
            if op == operation:
                raise error
            return real_cancel(op)

        service.guardian.cancel = cancel


def _spy(service):
    seen = {"tick": [], "cap": 0}
    real_tick, real_cap = service.guardian.tick, service._enforce_total_cap

    def tick(op):
        seen["tick"].append(op)
        return real_tick(op)

    def cap():
        seen["cap"] += 1
        return real_cap()

    service.guardian.tick, service._enforce_total_cap = tick, cap
    return seen


def _texts(service):
    events = service.db.execute("SELECT kind, detail FROM events").fetchall()
    records = service.db.execute("SELECT kind, detail FROM frame_records").fetchall()
    return [str(tuple(r)) for r in events + records]


@pytest.mark.parametrize("where", ["arm", "account", "cancel"])
def test_one_raising_frame_is_fenced_and_the_pass_continues(tmp_path, where):
    service, clock, _provider, a, b = _two_bound_running(tmp_path)
    try:
        # The raising frame is the first one the pass visits, so the other comes after it.
        first = service.db.execute("SELECT reservation FROM reservations").fetchone()[0]
        bad, good = (a, b) if a["reservation"] == first else (b, a)
        bad_op, good_op = (service._row(x["reservation"])["operation"] for x in (bad, good))
        left = _inject(
            service,
            where,
            bad_op,
            bad["reservation"],
            RuntimeError("do-not-echo /secret/path"),
        )
        seen = _spy(service)
        clock[0] += 5
        service.loop()  # never raises out of the pass
        assert good_op in seen["tick"]  # polled in the same pass
        assert service._row(good["reservation"])["accounted_at"] == clock[0]  # and accounted
        assert seen["cap"] == 1  # the aggregate cap still ran
        row_bad, row_good = service._row(bad["reservation"]), service._row(good["reservation"])
        assert row_bad["budget_fenced"] == 1 and row_bad["recovery"] == 1
        assert row_good["budget_fenced"] == 0 and row_good["recovery"] == 0
        failures = service.db.execute(
            "SELECT reservation, detail FROM events WHERE kind='loop_frame_failed'"
        ).fetchall()
        assert [tuple(r) for r in failures] == [(bad["reservation"], "RuntimeError")]
        record = service.db.execute(
            "SELECT detail FROM frame_records WHERE reservation=? AND kind='loop_frame_failed'",
            (bad["reservation"],),
        ).fetchone()
        assert json.loads(record[0]) == {"error_type": "RuntimeError"}
        assert not any("do-not-echo" in text or "/secret" in text for text in _texts(service))
        status = _status(service, bad)
        assert status["last_loop_frame_failure"] == "RuntimeError"
        assert status["last_loop_frame_failure_at"] == clock[0]
        assert _status(service, good)["last_loop_frame_failure"] is None
        # Later passes: the raising frame stays fenced, never re-raises, and is not re-logged
        # within the dedup window; the other keeps its polls.
        if left:
            left[0] = 1
        for _ in range(2):
            clock[0] += 5
            service.loop()
        assert service._row(bad["reservation"])["budget_fenced"] == 1
        assert seen["cap"] == 3 and seen["tick"].count(good_op) >= 3
        count = service.db.execute(
            "SELECT count(*) FROM events WHERE kind='loop_frame_failed'"
        ).fetchone()[0]
        assert count == 1
        service.admin(dict(operation="heartbeat", reservation=good["reservation"]))  # it continues
    finally:
        service.close()


def test_every_frame_raising_still_runs_the_aggregate_cap(tmp_path):
    service, clock, _provider, a, b = _two_bound_running(tmp_path)
    try:

        def arm(assignment, **kwargs):
            raise OSError("do-not-echo")

        service.guardian.arm = arm
        seen = _spy(service)
        clock[0] += 5
        service.loop()
        assert seen["cap"] == 1
        for body in (a, b):
            row = service._row(body["reservation"])
            assert row["budget_fenced"] == 1 and row["recovery"] == 1
            assert _status(service, body)["last_loop_frame_failure"] == "OSError"
        clock[0] += 5
        service.loop()
        assert seen["cap"] == 2
    finally:
        service.close()


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_interrupts_are_never_swallowed_by_the_frame_isolation(tmp_path, error):
    service, clock, _provider, a, _b = _two_bound_running(tmp_path)
    try:
        _inject(service, "arm", "a" * 32, a["reservation"], error())
        clock[0] += 5
        with pytest.raises(error):
            service.loop()
    finally:
        service.close()


# (i) a lost-create-closed frame keeps its hold for the 24 h late-resource watch.
def _close(service, body, kind, at):
    with service.db:
        service.db.execute(
            "UPDATE reservations SET recovery=1, lost_create_resolution=? WHERE reservation=?",
            (json.dumps({"kind": kind, "resolved_at": at}), body["reservation"]),
        )


@pytest.mark.parametrize("kind", ["automatic", "operator"])
def test_a_lost_create_hold_stays_committed_while_it_may_reopen(tmp_path, kind):
    from tessera_control.salad_safety_service import LOST_CREATE_WATCH_SECONDS

    service, clock, _ = _service(tmp_path, max_open_billable_frames=2, total_cap_cents=20)
    try:
        a = _reserve(service, clock, "a" * 32)
        _reserve(service, clock, "b" * 32)
        _close(service, a, kind, clock[0])
        assert _status(service, a)["status"] == "reconciled"
        assert service._committed(account=False) == 20
        # The slot is free, the budget is not: a late group could still reopen A.
        with pytest.raises(ValueError, match="total_budget_exhausted"):
            _reserve(service, clock, "c" * 32)
        with service.db:  # the watch voids the resolution: the total does not move
            service.db.execute(
                "UPDATE reservations SET lost_create_resolution=NULL WHERE reservation=?",
                (a["reservation"],),
            )
        assert service._committed(account=False) == 20
        _close(service, a, kind, clock[0])
        clock[0] += LOST_CREATE_WATCH_SECONDS - 1
        assert service._committed(account=False) == 20
        clock[0] += 1  # the watch is over: only its frozen accrual (zero) counts
        assert service._committed(account=False) == 10
    finally:
        service.close()


def test_a_clean_create_rejection_frees_its_hold_at_once(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2, total_cap_cents=20)
    try:
        a = _reserve(service, clock, "a" * 32)
        _reserve(service, clock, "b" * 32)
        _close(service, a, "create_rejected", clock[0])
        assert service._committed(account=False) == 10
        _reserve(service, clock, "c" * 32)
        assert _count(service) == 3
    finally:
        service.close()


def test_an_unreadable_resolution_record_keeps_the_hold(tmp_path):
    service, clock, _ = _service(tmp_path, max_open_billable_frames=2, total_cap_cents=20)
    try:
        a = _reserve(service, clock, "a" * 32)
        with service.db:
            service.db.execute(
                "UPDATE reservations SET recovery=1, lost_create_resolution='{}' "
                "WHERE reservation=?",
                (a["reservation"],),
            )
        assert service._committed(account=False) == 10
    finally:
        service.close()
