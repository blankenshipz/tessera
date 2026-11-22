import dataclasses
import sqlite3

import pytest

from tessera_agent.auto_research_jobs import (
    DEFAULT_JOB_CLASSES,
    AutoJobLedger,
    AutoJobPlanner,
    AutoJobPolicy,
    PlanTrigger,
    ReviewSubmitRefused,
    idempotency_key,
)

D = "a" * 64
PUB = (("snap1", "b" * 64, "public"),)
PRIV = (("snap1", "b" * 64, "public"), ("snap2", "c" * 64, "private_derived"))


class Stub:
    def __init__(self, fail=(), refuse=None):
        self.calls = []
        self.fail = set(fail)
        self.refuse = refuse  # callable(payload) -> reason | None

    def submit(self, project_id, key, payload):
        self.calls.append({"project_id": project_id, "key": key, "payload": payload})
        if payload["kind"] in self.fail:
            raise RuntimeError("down")
        if self.refuse and (reason := self.refuse(payload)):
            raise ReviewSubmitRefused(reason)
        return {"id": f"job{len(self.calls)}"}


def policy(**kw):
    base = dict(
        project_id="p",
        classes=DEFAULT_JOB_CLASSES,
        tier="sonnet",
        per_job_cap_cents=100,
        max_jobs_total=100,
        max_total_budget_cents=10_000,
        destination_id="dest1",
        prompt_digests={k: D for k in DEFAULT_JOB_CLASSES},
    )
    base.update(kw)
    return AutoJobPolicy(**base)


def trig(tid="t1", **kw):
    base = dict(
        trigger_id=tid,
        event="admitted",
        subject_type="plan",
        subject_id="plan1",
        subject_digest="d" * 8,
        inputs=PUB,
    )
    base.update(kw)
    return PlanTrigger(**base)


def make(pol=None, stub=None):
    stub = stub or Stub()
    ledger = AutoJobLedger(":memory:")
    return AutoJobPlanner(pol or policy(), ledger, stub), ledger, stub


def test_policy_validation():
    with pytest.raises(ValueError):
        policy(classes=("bogus",))
    with pytest.raises(ValueError):
        policy(per_job_cap_cents=0)
    with pytest.raises(ValueError):
        policy(classes=())
    with pytest.raises(ValueError):
        trig(event="other")
    with pytest.raises(ValueError):
        policy(tier="standard")
    with pytest.raises(ValueError):
        policy(prompt_digests={"plan_review": D})
    with pytest.raises(ValueError):
        policy(prompt_digests={k: "xyz" for k in DEFAULT_JOB_CLASSES})
    with pytest.raises(ValueError):
        trig(inputs=())
    with pytest.raises(ValueError):
        trig(inputs=(("s", D, "secret"),))
    with pytest.raises(ValueError):
        trig(inputs=(("s", "bad", "public"),))
    with pytest.raises(ValueError):
        trig(inputs=(("s", D, "public"),) * 17)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy().tier = "x"


def test_creates_all_and_replay_is_idempotent():
    pl, ledger, stub = make()
    r1 = pl.on_trigger(trig())
    assert [r["status"] for r in r1] == ["created"] * 4
    r2 = pl.on_trigger(trig())
    assert [r["status"] for r in r2] == ["existing"] * 4
    assert [r["job_id"] for r in r2] == [r["job_id"] for r in r1]
    assert len(stub.calls) == 4
    assert ledger.totals("p") == (4, 400)


def test_new_trigger_creates_new_jobs():
    pl, ledger, stub = make()
    pl.on_trigger(trig("t1"))
    pl.on_trigger(trig("t2", event="advanced"))
    assert len(stub.calls) == 8
    assert len(ledger.list_jobs("p")) == 8


def test_payload_shape_key_and_tier_passthrough():
    pl, ledger, stub = make(policy(tier="opus", per_job_cap_cents=7))
    pl.on_trigger(trig())
    assert {c["payload"]["tier"] for c in stub.calls} == {"opus"}
    assert {c["payload"]["budget_cents"] for c in stub.calls} == {7}
    c0 = stub.calls[0]
    assert set(c0["payload"]) == {
        "kind",
        "title",
        "inputs",
        "prompt_digest",
        "destination_id",
        "tier",
        "budget_cents",
        "egress",
    }
    assert c0["payload"]["title"] == "plan_review for plan plan1"
    assert c0["payload"]["destination_id"] == "dest1"
    assert c0["payload"]["prompt_digest"] == D
    assert c0["key"] == idempotency_key("p", "t1", "plan_review")
    assert len({c["key"] for c in stub.calls}) == 4
    assert {j["created_by"] for j in ledger.list_jobs("p")} == {"agent_loop"}


def test_idempotency_key_deterministic():
    import hashlib

    expect = hashlib.sha256(b"agent_loop|p|t1|plan_review").hexdigest()
    assert idempotency_key("p", "t1", "plan_review") == expect


def test_long_title_truncated():
    pl, _, stub = make()
    pl.on_trigger(trig(subject_id="x" * 500))
    assert all(len(c["payload"]["title"]) <= 200 for c in stub.calls)


def test_cap_exhaustion_records_skips_once_no_more_submits():
    pl, ledger, stub = make(policy(max_jobs_total=2))
    r = pl.on_trigger(trig())
    assert [x["status"] for x in r] == ["created", "created", "skipped", "skipped"]
    assert r[2]["reason"] == "cap_exhausted"
    n = len(stub.calls)
    r2 = pl.on_trigger(trig())
    assert [x["status"] for x in r2] == ["existing", "existing", "skipped", "skipped"]
    pl.on_trigger(trig("t2"))
    assert len(stub.calls) == n
    skips = ledger.list_skips("p")
    assert len(skips) == 2 + 4
    assert {s["reason"] for s in skips} == {"cap_exhausted"}


def test_budget_math():
    pl, ledger, stub = make(policy(per_job_cap_cents=100, max_total_budget_cents=250))
    r = pl.on_trigger(trig())
    assert [x["status"] for x in r] == ["created", "created", "skipped", "skipped"]
    assert ledger.totals("p") == (2, 200)
    # exact fit is allowed
    pl2, l2, _ = make(policy(per_job_cap_cents=100, max_total_budget_cents=300))
    pl2.on_trigger(trig())
    assert l2.totals("p") == (3, 300)


def test_class_subset_respected():
    pl, _, stub = make(policy(classes=("plan_review",)))
    pl.on_trigger(trig())
    assert [c["payload"]["kind"] for c in stub.calls] == ["plan_review"]


def test_disabled_records_skip_creates_nothing():
    pl, ledger, stub = make(policy(enabled=False))
    r = pl.on_trigger(trig())
    assert {x["reason"] for x in r} == {"disabled"}
    pl.on_trigger(trig())
    assert stub.calls == []
    assert len(ledger.list_skips("p")) == 4
    assert ledger.totals("p") == (0, 0)


def test_sealed_skipped():
    pl, ledger, stub = make()
    r = pl.on_trigger(trig(sealed=True))
    assert {x["reason"] for x in r} == {"input_sealed"}
    assert stub.calls == []


def test_private_input_refused_by_destination_policy_is_final_skip():
    stub = Stub(
        refuse=lambda p: (
            "data_class_not_allowed_for_destination" if p["egress"] == "model_runtime" else None
        )
    )
    pl, ledger, _ = make(stub=stub)
    r = pl.on_trigger(trig(inputs=PRIV))
    assert {x["reason"] for x in r} == {"data_class_not_allowed_for_destination"}
    # The body never carries a data class (control derives it); a private input shows as egress.
    assert stub.calls[0]["payload"]["egress"] == "model_runtime"
    assert all(set(i) == {"snapshot_id", "digest"} for i in stub.calls[0]["payload"]["inputs"])
    n = len(stub.calls)
    pl.on_trigger(trig(inputs=PRIV))  # permanent: no resubmit
    assert len(stub.calls) == n
    assert ledger.totals("p") == (0, 0)


def test_private_input_admitted_never_widens_egress():
    pl, _, stub = make()
    r = pl.on_trigger(trig(inputs=PRIV))
    assert {x["status"] for x in r} == {"created"}
    assert {c["payload"]["egress"] for c in stub.calls} == {"model_runtime"}
    assert not hasattr(policy(), "allow_private_destination")
    pl2, _, stub2 = make()
    pl2.on_trigger(trig(inputs=PUB))
    assert {c["payload"]["egress"] for c in stub2.calls} == {"none"}


def test_other_refusal_reasons_recorded_final():
    for reason in ("destination_not_enrolled", "tier_exceeds_policy", "budget_exceeds_policy"):
        pl, ledger, stub = make(stub=Stub(refuse=lambda p, r=reason: r))
        r = pl.on_trigger(trig())
        assert {x["reason"] for x in r} == {reason}
        assert ledger.get_skip("p", "t1", "plan_review")["reason"] == reason


def test_submit_failure_not_recorded_as_created_and_retry_allowed():
    stub = Stub(fail={"technique_scout"})
    pl, ledger, _ = make(stub=stub)
    r = pl.on_trigger(trig())
    bad = [x for x in r if x["job_class"] == "technique_scout"][0]
    assert bad == {"job_class": "technique_scout", "status": "skipped", "reason": "submit_failed"}
    assert not ledger.has_job("p", "t1", "technique_scout")
    assert ledger.totals("p") == (3, 300)
    stub.fail.clear()
    r2 = pl.on_trigger(trig())
    by = {x["job_class"]: x for x in r2}
    assert by["technique_scout"]["status"] == "created"
    assert by["plan_review"]["status"] == "existing"
    assert ledger.has_job("p", "t1", "technique_scout")
    assert ledger.list_skips("p") == []


def test_retry_final_skip_replaces_submit_failed_cap_exhausted():
    stub = Stub(fail={"plan_review"})
    pl, ledger, _ = make(policy(max_jobs_total=1, classes=("plan_review", "dataset_scout")), stub)
    r = pl.on_trigger(trig())
    assert [x["reason"] for x in r if x["status"] == "skipped"] == ["submit_failed"]
    assert ledger.has_job("p", "t1", "dataset_scout")
    stub.fail.clear()
    r2 = pl.on_trigger(trig())
    by = {x["job_class"]: x for x in r2}
    assert by["plan_review"] == {
        "job_class": "plan_review",
        "status": "skipped",
        "reason": "cap_exhausted",
    }
    assert ledger.get_skip("p", "t1", "plan_review")["reason"] == "cap_exhausted"
    assert ledger.has_job("p", "t1", "dataset_scout")
    assert ledger.totals("p") == (1, 100)


def test_retry_after_failure_with_policy_disabled_records_disabled():
    stub = Stub(fail={"plan_review"})
    pl, ledger, _ = make(stub=stub)
    pl.on_trigger(trig())
    pl.policy = policy(enabled=False)
    r = pl.on_trigger(trig())
    assert {x["job_class"]: x for x in r}["plan_review"]["reason"] == "disabled"
    assert ledger.get_skip("p", "t1", "plan_review")["reason"] == "disabled"


def test_final_skip_never_overwritten_by_apply():
    ledger = AutoJobLedger(":memory:")
    ledger.record_skip("p", "t1", "plan_review", "cap_exhausted")
    ledger.record_skip("p", "t1", "plan_review", "disabled")
    assert ledger.get_skip("p", "t1", "plan_review")["reason"] == "cap_exhausted"


def test_two_connections_cannot_both_exceed_cap(tmp_path):
    path = tmp_path / "ledger.db"
    l1, l2 = AutoJobLedger(path), AutoJobLedger(path)
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    s1, s2 = Stub(), Stub()
    p1, p2 = AutoJobPlanner(pol, l1, s1), AutoJobPlanner(pol, l2, s2)
    a = p1.on_trigger(trig("t1"))
    b = p2.on_trigger(trig("t2"))
    assert a[0]["status"] == "created"
    assert b[0] == {"job_class": "plan_review", "status": "skipped", "reason": "cap_exhausted"}
    assert s2.calls == []
    assert l2.totals("p") == (1, 100)


def test_writer_lock_blocks_second_connection(tmp_path):
    path = tmp_path / "ledger.db"
    l1, l2 = AutoJobLedger(path), AutoJobLedger(path)
    l2.db.execute("PRAGMA busy_timeout=50")
    with l1.transaction():
        with pytest.raises(sqlite3.OperationalError):
            with l2.transaction():
                pass


def test_failed_submit_does_not_consume_budget():
    stub = Stub(fail={"plan_review"})
    pl, ledger, _ = make(policy(max_jobs_total=3), stub)
    r = pl.on_trigger(trig())
    assert [x["status"] for x in r] == ["skipped", "created", "created", "created"]


def test_ledger_transaction_atomic():
    ledger = AutoJobLedger(":memory:")
    jobs = [
        {
            "job_class": "plan_review",
            "job_id": "j1",
            "created_by": "agent_loop",
            "cost_cap_cents": 5,
        }
    ]
    skips = [{"job_class": "pre_gate_review", "reason": None}]  # violates NOT NULL
    with pytest.raises(sqlite3.IntegrityError):
        ledger.apply("p", "t1", jobs, skips)
    assert ledger.list_jobs("p") == []
    assert ledger.list_skips("p") == []
    assert ledger.totals("p") == (0, 0)


def test_planner_has_no_decision_surface():
    names = {n for n in dir(AutoJobPlanner) if not n.startswith("_")}
    assert "on_trigger" in names
    assert not {"accept", "approve", "decide", "write_gate"} & names


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_slow_submit_does_not_block_other_writer(tmp_path):
    import threading

    path = tmp_path / "ledger.db"
    AutoJobLedger(path).close()
    started, release = threading.Event(), threading.Event()

    class Slow:
        def submit(self, project_id, key, payload):
            started.set()
            assert release.wait(10)
            return {"id": "slow1"}

    out = {}

    def worker():
        ledger = AutoJobLedger(path)
        pl = AutoJobPlanner(policy(classes=("plan_review",)), ledger, Slow())
        out["r"] = pl.on_trigger(trig("t1"))
        ledger.close()

    th = threading.Thread(target=worker)
    th.start()
    try:
        assert started.wait(10)
        l2 = AutoJobLedger(path)
        l2.db.execute("PRAGMA busy_timeout=200")  # would raise if the lock were held
        s2 = Stub()
        r = AutoJobPlanner(policy(classes=("plan_review",)), l2, s2).on_trigger(trig("t2"))
        assert r[0]["status"] == "created"
        assert len(l2.list_reservations("p")) == 1
    finally:
        release.set()
        th.join(10)
    assert out["r"][0] == {"job_class": "plan_review", "status": "created", "job_id": "slow1"}
    assert l2.totals("p") == (2, 200)
    assert l2.list_reservations("p") == []


def test_cap_counts_concurrent_reservations(tmp_path):
    path = tmp_path / "ledger.db"
    l1, l2 = AutoJobLedger(path), AutoJobLedger(path)
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    inner = {}

    class Reentrant:
        def submit(self, project_id, key, payload):
            inner["r"] = AutoJobPlanner(pol, l2, Stub()).on_trigger(trig("t2"))
            return {"id": "j1"}

    r = AutoJobPlanner(pol, l1, Reentrant()).on_trigger(trig("t1"))
    assert r[0]["status"] == "created"
    assert inner["r"][0] == {
        "job_class": "plan_review",
        "status": "skipped",
        "reason": "cap_exhausted",
    }
    assert l1.totals("p") == (1, 100)


def test_budget_cap_counts_reservations():
    ledger = AutoJobLedger(":memory:")
    args = dict(cost_cap_cents=100, max_jobs_total=10, max_total_budget_cents=150, ttl=900, now=1.0)
    assert ledger.reserve("p", "t1", "plan_review", **args)[0] == "reserved"
    assert ledger.reserve("p", "t2", "plan_review", **args) == ("skipped", "cap_exhausted")


def test_stale_reservation_expires_and_resubmits_same_key():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    # a crashed process left a reservation
    ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, clock())
    stub = Stub()
    pl = AutoJobPlanner(pol, ledger, stub, clock=clock)
    assert pl.on_trigger(trig("t2"))[0]["reason"] == "cap_exhausted"  # live: counts
    ledger.db.execute("DELETE FROM skips")
    clock.t += 901
    r = pl.on_trigger(trig("t1"))
    assert r[0]["status"] == "created"
    assert stub.calls[0]["key"] == idempotency_key("p", "t1", "plan_review")
    assert ledger.list_reservations("p") == []


def test_in_flight_skip_is_retryable_not_recorded():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    ledger.reserve("p", "t1", "plan_review", 100, 10, 10_000, 900, clock())
    stub = Stub()
    pl = AutoJobPlanner(policy(classes=("plan_review",)), ledger, stub, clock=clock)
    r = pl.on_trigger(trig("t1"))
    assert r == [{"job_class": "plan_review", "status": "skipped", "reason": "in_flight"}]
    assert stub.calls == [] and ledger.list_skips("p") == []


def test_ledger_busy_retries_then_skips(tmp_path):
    path = tmp_path / "ledger.db"
    l1, l2 = AutoJobLedger(path), AutoJobLedger(path)
    l2.db.execute("PRAGMA busy_timeout=10")
    sleeps = []
    stub = Stub()
    pl = AutoJobPlanner(policy(classes=("plan_review",)), l2, stub, sleep=sleeps.append)
    with l1.transaction():
        r = pl.on_trigger(trig())
    assert r == [{"job_class": "plan_review", "status": "skipped", "reason": "ledger_busy"}]
    assert len(sleeps) == 3 and stub.calls == []
    assert l2.list_skips("p") == []
    assert pl.on_trigger(trig())[0]["status"] == "created"  # retryable


def test_crash_after_submit_before_commit_replays_same_key():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    keys = []

    class Crash:
        def submit(self, project_id, key, payload):
            keys.append(key)
            raise KeyboardInterrupt  # not caught: simulates a crash before commit

    pol = policy(classes=("plan_review",))
    with pytest.raises(KeyboardInterrupt):
        AutoJobPlanner(pol, ledger, Crash(), clock=clock).on_trigger(trig())
    assert len(ledger.list_reservations("p")) == 1
    stub = Stub()
    pl = AutoJobPlanner(pol, ledger, stub, clock=clock)
    assert pl.on_trigger(trig())[0]["reason"] == "in_flight"
    clock.t += 901
    assert pl.on_trigger(trig())[0]["status"] == "created"
    assert [stub.calls[0]["key"]] == keys


def test_refusal_and_failure_release_reservation():
    pl, ledger, _ = make(stub=Stub(refuse=lambda p: "tier_exceeds_policy"))
    pl.on_trigger(trig())
    assert ledger.list_reservations("p") == []
    pl, ledger, _ = make(stub=Stub(fail={"plan_review"}))
    pl.on_trigger(trig())
    assert ledger.list_reservations("p") == []


def test_ledger_file_created_0600_and_broad_file_refused(tmp_path):
    import os
    import stat

    path = tmp_path / "ledger.db"
    AutoJobLedger(path).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    AutoJobLedger(path).close()  # reopen ok
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        AutoJobLedger(path)


def test_submit_outliving_ttl_never_exceeds_cap():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    inner = {}

    class Slow:
        def submit(self, project_id, key, payload):
            clock.t += 5000  # outlives the ttl
            inner["r"] = AutoJobPlanner(pol, ledger, Stub(), clock=clock).on_trigger(trig("t2"))
            return {"id": "j1"}

    r = AutoJobPlanner(pol, ledger, Slow(), clock=clock, submit_timeout_seconds=60).on_trigger(
        trig("t1")
    )
    assert inner["r"][0]["reason"] == "cap_exhausted"
    assert r[0]["status"] == "created"
    assert ledger.totals("p") == (1, 100)
    assert ledger.list_reservations("p") == []


def test_stale_reservation_counts_in_caps():
    ledger = AutoJobLedger(":memory:")
    ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, 0.0)
    out = ledger.reserve("p", "t2", "plan_review", 100, 1, 10_000, 900, 99999.0)
    assert out == ("skipped", "cap_exhausted")
    assert len(ledger.list_reservations("p")) == 1


def test_replay_takes_over_stale_and_operator_release():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, clock())
    clock.t += 2000
    stub = Stub()
    pl = AutoJobPlanner(
        policy(max_jobs_total=1, classes=("plan_review",)), ledger, stub, clock=clock
    )
    assert pl.on_trigger(trig("t1"))[0]["status"] == "created"
    assert ledger.totals("p") == (1, 100) and ledger.list_reservations("p") == []
    # operator release
    ledger.reserve("p", "t9", "plan_review", 100, 5, 10_000, 900, clock())
    assert ledger.release_reservation("p", "t9", "plan_review") is True
    assert ledger.release_reservation("p", "t9", "plan_review") is False
    assert ledger.list_reservations("p") == []


def test_created_unrecorded_when_complete_busy_then_replay_records(tmp_path):
    clock = Clock()
    path = tmp_path / "ledger.db"
    l1, l2 = AutoJobLedger(path), AutoJobLedger(path)
    l2.db.execute("PRAGMA busy_timeout=10")
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    keys = []

    class LockInside:
        def submit(self, project_id, key, payload):
            keys.append(key)
            l1.db.execute("BEGIN IMMEDIATE")
            return {"id": "jobX"}

    pl = AutoJobPlanner(pol, l2, LockInside(), clock=clock, sleep=lambda s: None)
    r = pl.on_trigger(trig("t1"))
    assert r == [
        {
            "job_class": "plan_review",
            "status": "created_unrecorded",
            "job_id": "jobX",
            "reason": "ledger_busy",
        }
    ]
    l1.db.execute("ROLLBACK")
    assert l1.totals("p") == (0, 0) and len(l1.list_reservations("p")) == 1
    assert pl.on_trigger(trig("t2"))[0]["reason"] == "cap_exhausted"  # slot still counted
    clock.t += 901

    class Same:
        def submit(self, project_id, key, payload):
            assert key == keys[0]
            return {"id": "jobX"}

    pl2 = AutoJobPlanner(pol, l2, Same(), clock=clock)
    assert pl2.on_trigger(trig("t1"))[0] == {
        "job_class": "plan_review",
        "status": "created",
        "job_id": "jobX",
    }
    assert l2.totals("p") == (1, 100) and l2.list_reservations("p") == []


def test_prune_keeps_jobs_and_reservations():
    ledger = AutoJobLedger(":memory:", clock=lambda: 10.0)
    ledger.record_job("p", "t1", "plan_review", "j1", "agent_loop", 100)
    ledger.record_skip("p", "t2", "plan_review", "cap_exhausted")
    ledger.record_skip("p", "t3", "plan_review", "submit_failed")
    ledger.reserve("p", "t4", "plan_review", 100, 10, 10_000, 900, 1.0)
    assert ledger.prune("p", 11.0) == 2
    assert ledger.list_skips("p") == []
    assert len(ledger.list_jobs("p")) == 1 and len(ledger.list_reservations("p")) == 1


def test_create_race_file_exists_error_falls_through(tmp_path, monkeypatch):
    import os

    path = tmp_path / "ledger.db"
    real = os.open

    def racing(p, flags, mode=0o777):
        if str(p) == str(path):
            real(p, flags, mode)  # the "other process" wins
        return real(p, flags, mode)

    monkeypatch.setattr(os, "open", racing)
    AutoJobLedger(path).close()


def _takeover_pair(ledger, clock):
    """A reserves t1, its submit outlives the ttl, replay B takes over. -> (tokenA, tokenB)."""
    a = ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, clock())
    assert a[0] == "reserved"
    clock.t += 1000
    b = ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, clock())
    assert b[0] == "reserved" and b[1] != a[1]
    return a[1], b[1]


def test_stale_owner_submit_failed_does_not_free_current_reservation():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    tok_a, tok_b = _takeover_pair(ledger, clock)
    out = ledger.complete("p", "t1", "plan_review", 100, skip_reason="submit_failed", owner=tok_a)
    assert out == "stale"
    assert [r["owner"] for r in ledger.list_reservations("p")] == [tok_b]
    assert ledger.list_skips("p") == []
    pl = AutoJobPlanner(
        policy(max_jobs_total=1, classes=("plan_review",)), ledger, Stub(), clock=clock
    )
    assert pl.on_trigger(trig("t3"))[0]["reason"] == "cap_exhausted"
    assert ledger.complete("p", "t1", "plan_review", 100, job_id="j1", owner=tok_b) == "recorded"
    assert ledger.totals("p") == (1, 100) and ledger.list_reservations("p") == []


def test_planner_stale_owner_failure_reports_stale_owner():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    inner = {}

    class SlowFail:
        def submit(self, project_id, key, payload):
            clock.t += 1000

            inner["tok"] = ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, clock())
            raise RuntimeError("down")

    r = AutoJobPlanner(pol, ledger, SlowFail(), clock=clock).on_trigger(trig("t1"))
    assert r == [{"job_class": "plan_review", "status": "skipped", "reason": "stale_owner"}]
    assert ledger.list_skips("p") == []
    assert [x["owner"] for x in ledger.list_reservations("p")] == [inner["tok"][1]]
    assert (
        AutoJobPlanner(pol, ledger, Stub(), clock=clock).on_trigger(trig("t3"))[0]["reason"]
        == "cap_exhausted"
    )


def test_stale_owner_late_success_then_current_owner_no_double_count():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    tok_a, tok_b = _takeover_pair(ledger, clock)
    assert ledger.complete("p", "t1", "plan_review", 100, job_id="j1", owner=tok_a) == "recorded"
    assert ledger.totals("p") == (1, 100) and ledger.list_reservations("p") == []
    assert ledger.reserved_totals("p") == (0, 0)
    assert ledger.complete("p", "t1", "plan_review", 100, job_id="j1", owner=tok_b) == "existing"
    assert ledger.totals("p") == (1, 100)
    assert len(ledger.list_jobs("p")) == 1
    # current owner failing after the stale success must not record a skip either
    assert ledger.complete("p", "t1", "plan_review", 100, skip_reason="x", owner=tok_b) == (
        "existing"
    )
    assert ledger.list_skips("p") == []


def test_stale_owner_late_refusal_records_nothing_and_keeps_slot():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    tok_a, tok_b = _takeover_pair(ledger, clock)
    assert ledger.complete("p", "t1", "plan_review", 100, skip_reason="policy", owner=tok_a) == (
        "stale"
    )
    assert ledger.list_skips("p") == []
    assert ledger.reserved_totals("p") == (1, 100)
    assert ledger.complete("p", "t1", "plan_review", 100, job_id="j9", owner=tok_b) == "recorded"
    assert ledger.get_job("p", "t1", "plan_review")["job_id"] == "j9"


def test_legacy_ledger_without_owner_column_migrates(tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE reservations (
        project_id TEXT NOT NULL, trigger_id TEXT NOT NULL, job_class TEXT NOT NULL,
        cost_cap_cents INTEGER NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(project_id, trigger_id, job_class));
      INSERT INTO reservations VALUES('p','t1','plan_review',100,0.0);
    """)
    db.commit()
    db.close()
    path.chmod(0o600)
    ledger = AutoJobLedger(path)
    AutoJobLedger(path).close()  # second open: migration is idempotent
    assert ledger.list_reservations("p")[0]["owner"] is None
    assert ledger.reserved_totals("p") == (1, 100)
    # legacy unowned row matches any token
    assert ledger.complete("p", "t1", "plan_review", 100, job_id="j1", owner="whatever") == (
        "recorded"
    )
    assert ledger.totals("p") == (1, 100) and ledger.list_reservations("p") == []
    assert ledger.reserve("p", "t2", "plan_review", 100, 5, 10_000, 900, 1.0)[0] == "reserved"


def test_late_success_after_release_is_recorded_once_and_flagged_over_cap():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    pol = policy(max_jobs_total=1, classes=("plan_review",))
    inner = {}

    class SlowOk:
        def submit(self, project_id, key, payload):
            # operator releases the dead-looking reservation; another job takes the slot
            assert ledger.release_reservation("p", "t1", "plan_review")
            other = AutoJobPlanner(pol, ledger, Stub(), clock=clock).on_trigger(trig("t2"))
            inner["other"] = other[0]
            return {"id": "late"}

    r = AutoJobPlanner(pol, ledger, SlowOk(), clock=clock).on_trigger(trig("t1"))
    assert inner["other"]["status"] == "created"
    assert r == [
        {"job_class": "plan_review", "status": "created", "job_id": "late", "over_cap": True}
    ]
    jobs = {j["trigger_id"]: j for j in ledger.list_jobs("p")}
    assert len(ledger.list_jobs("p")) == 2 and ledger.totals("p") == (2, 200)
    assert jobs["t1"]["over_cap"] == 1 and jobs["t2"]["over_cap"] == 0
    assert ledger.list_reservations("p") == []
    # replay is a no-op: recorded exactly once
    again = AutoJobPlanner(pol, ledger, Stub(), clock=clock).on_trigger(trig("t1"))
    assert again[0]["status"] == "existing" and len(ledger.list_jobs("p")) == 2


def test_complete_over_cap_by_budget_and_default_not_flagged():
    ledger = AutoJobLedger(":memory:")
    ledger.record_job("p", "t0", "plan_review", "j0", "agent_loop", 100)
    out = ledger.complete("p", "t1", "plan_review", 100, job_id="j1", max_total_budget_cents=150)
    assert out == "recorded_over_cap"
    assert ledger.get_job("p", "t1", "plan_review")["over_cap"] == 1
    assert ledger.complete("p", "t2", "plan_review", 100, job_id="j2") == "recorded"
    assert ledger.get_job("p", "t2", "plan_review")["over_cap"] == 0


def test_stale_completions_receipts_and_prune():
    clock = Clock()
    ledger = AutoJobLedger(":memory:", clock=lambda: 5.0)
    tok_a, tok_b = _takeover_pair(ledger, clock)
    assert ledger.complete("p", "t1", "plan_review", 100, skip_reason="policy", owner=tok_a) == (
        "stale"
    )
    assert ledger.complete("p", "t1", "plan_review", 100, skip_reason="submit_failed") == "stale"
    rows = ledger.list_stale_completions("p")
    assert [(r["kind"], r["reason"], r["job_class"], r["trigger_id"]) for r in rows] == [
        ("refused", "policy", "plan_review", "t1"),
        ("submit_failed", "submit_failed", "plan_review", "t1"),
    ]
    assert rows[0]["created_at"] == 5.0 and ledger.list_stale_completions("other") == []
    assert ledger.reserved_totals("p") == (1, 100) and ledger.list_skips("p") == []
    assert ledger.prune("p", 5.0) == 0
    assert ledger.prune("p", 6.0) == 2
    assert ledger.list_stale_completions("p") == []


def test_planner_stale_owner_failure_writes_receipt():
    clock = Clock()
    ledger = AutoJobLedger(":memory:")
    pol = policy(max_jobs_total=1, classes=("plan_review",))

    class SlowFail:
        def submit(self, project_id, key, payload):
            clock.t += 1000
            ledger.reserve("p", "t1", "plan_review", 100, 1, 10_000, 900, clock())
            raise RuntimeError("down")

    AutoJobPlanner(pol, ledger, SlowFail(), clock=clock).on_trigger(trig("t1"))
    assert [r["kind"] for r in ledger.list_stale_completions("p")] == ["submit_failed"]


def test_legacy_ledger_migrates_over_cap_and_stale_completions(tmp_path):
    path = tmp_path / "legacy2.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE jobs (
        project_id TEXT NOT NULL, trigger_id TEXT NOT NULL, job_class TEXT NOT NULL,
        job_id TEXT NOT NULL, created_by TEXT NOT NULL, state TEXT NOT NULL,
        cost_cap_cents INTEGER NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(project_id, trigger_id, job_class));
      INSERT INTO jobs VALUES('p','t0','plan_review','j0','agent_loop','submitted',100,0.0);
    """)
    db.commit()
    db.close()
    path.chmod(0o600)
    ledger = AutoJobLedger(path)
    AutoJobLedger(path).close()  # idempotent
    assert ledger.list_jobs("p")[0]["over_cap"] == 0
    assert ledger.list_stale_completions("p") == []
    out = ledger.complete("p", "t1", "plan_review", 100, job_id="j1", max_jobs_total=1)
    assert out == "recorded_over_cap"
    assert ledger.get_job("p", "t1", "plan_review")["over_cap"] == 1
