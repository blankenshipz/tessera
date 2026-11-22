"""The opt-in Salad CPU live check (ADR 0062): dry run, bounds, cleanup, billing comparison.

Everything here runs against a stateful in-process fake of Salad's container-group API behind the
vetted transport's connection factory: no network, no credential, no spend. The harness's real
``--run`` is never exercised against Salad by this suite (and ``main`` refuses it without an
exact confirmation), so default pytest can never spend.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from test_salad_campaign_profiles import IMAGE, contract_data, profile_data
from test_salad_http import GROUP, Response

SPEC = importlib.util.spec_from_file_location(
    "salad_cpu_live_check", Path(__file__).parents[1] / "scripts/salad_cpu_live_check.py"
)
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool  # a dataclass resolves its own module while the class is built
SPEC.loader.exec_module(tool)

RATE = dict(vcpu_hour_usd="0.04", memory_gb_hour_usd="0.01")  # 1 vCPU + 1 GB = $0.05/hour
TOKEN = b"isolated-live-check-token"


def private(path: Path, data: bytes, mode=0o600) -> Path:
    path.write_bytes(data)
    path.chmod(mode)
    return path


@pytest.fixture
def files(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    profile = profile_data(name="cpu-check", gpu_class=None, cpu_rate=RATE)
    document = dict(schema="tessera.salad.campaign-profiles.v1", profiles=[profile])
    receipts = root / "receipts"
    receipts.mkdir(mode=0o700)
    return dict(
        root=root,
        profiles=private(root / "profiles.json", json.dumps(document).encode()),
        contract=private(root / "contract.json", json.dumps(contract_data()).encode()),
        credential=private(root / "credential", TOKEN + b"\n"),
        receipts=receipts,
        document=document,
    )


def argv(files, *extra, image=IMAGE, cents=None):
    base = [
        "check",
        "--profiles",
        str(files["profiles"]),
        "--profile",
        "cpu-check",
        "--contract",
        str(files["contract"]),
        "--image",
        image,
    ]
    if cents is not None:
        base += ["--max-cents", str(cents)]
    return base + list(extra)


def plan_for(files, cents=5, **over):
    import argparse

    values = dict(
        profiles=files["profiles"],
        profile="cpu-check",
        contract=files["contract"],
        image=IMAGE,
        max_cents=cents,
    )
    return tool.build_plan(argparse.Namespace(**(values | over)))


# -- a stateful fake of Salad's container-group API --


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSalad:
    """Serves the few container-group routes the transport uses, with a billed-time model."""

    def __init__(
        self, clock, *, run_seconds=20, never_ends=False, gpu_in_group=False, stop_delay=0
    ):
        self.clock, self.run_seconds, self.never_ends = clock, run_seconds, never_ends
        self.stop_delay = stop_delay  # seconds a stop request takes to take effect (still billed)
        self.stop_requested_at = None
        self.gpu_in_group = gpu_in_group
        self.spec = None
        self.status = "absent"
        self.started_at = None
        self.requests = []
        self.fail = {}  # (method, suffix) -> status to answer instead
        self.closed = 0

    # transport connection factory protocol
    def __call__(self, host, *, port, timeout):
        fake = self

        class Connection:
            sock = None

            def connect(self):
                pass

            def request(self, method, path, *, body, headers):
                fake.requests.append((method, path, body))
                self.answer = fake.route(method, path, body)

            def getresponse(self):
                status, data = self.answer
                return Response(status, data)

            def close(self):
                fake.closed += 1

        return Connection()

    def state(self):
        if self.status == "stopping":
            if self.clock.now - self.stop_requested_at >= self.stop_delay:
                return "stopped"
            return "running"  # the instance is still up, and still billed
        if self.status in {"stopped", "absent"}:
            return "stopped"
        elapsed = self.clock.now - self.started_at
        if elapsed < 4:
            return "deploying"
        if self.never_ends or elapsed < 4 + self.run_seconds:
            return "running"
        return "succeeded"

    def group(self):
        state = self.state()
        container = json.loads(json.dumps(self.spec["container"]))
        if self.gpu_in_group:
            container["resources"]["gpu_classes"] = ["550e8400-e29b-41d4-a716-446655440000"]
        counts = {k + "_count": 0 for k in ("allocating", "creating", "running", "stopping")}
        if state == "running":
            counts["running_count"] = 1
        return dict(
            id=GROUP,
            name=self.spec["name"],
            organization_name="example-org",
            project_name="tessera-research",
            autostart_policy=False,
            replicas=1,
            restart_policy="never",
            country_codes=self.spec["country_codes"],
            priority="high",
            pending_change=state == "deploying",
            version=1,
            **{"scheduled-scaling-enabled": False, "scaling-actions": []},
            container=container,
            current_state=dict(status=state, instance_status_counts=counts),
        )

    def route(self, method, path, body):
        for (m, suffix), status in self.fail.items():
            if method == m and path.endswith(suffix):
                return status, {"title": "synthetic failure"}
        if path.endswith("/quotas"):
            quota = dict(container_replicas_quota=10, container_replicas_used=0)
            return 200, dict(container_groups_quotas=quota)
        if method == "GET" and path.endswith("/containers"):
            return 200, dict(items=[] if self.status == "absent" else [self.group()])
        if method == "POST" and path.endswith("/containers"):
            self.spec, self.status = json.loads(body), "stopped"
            return 201, self.group()
        if method == "GET":
            return 200, self.group()
        if method == "POST" and path.endswith("/start"):
            self.status, self.started_at = "started", self.clock.now
            return 202, {}
        if method == "POST" and path.endswith("/stop"):
            self.status, self.stop_requested_at = "stopping", self.clock.now
            return 202, {}
        if method == "DELETE":
            self.status = "absent"
            return 202, {}
        raise AssertionError((method, path))

    def calls(self):
        return [(m, p.rsplit("/", 1)[-1]) for m, p, _ in self.requests]


def run(files, fake, clock, cents=5):
    return tool.run_live(
        plan_for(files, cents),
        bytearray(TOKEN),
        factory=fake,
        clock=clock,
        sleep=clock.sleep,
    )


# -- the dry run is the default and does nothing --


def test_the_default_is_a_dry_run_with_no_network_and_no_credential(files, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not reach the transport or read a credential")

    monkeypatch.setattr(tool, "run_live", forbidden)
    monkeypatch.setattr(tool, "_read_credential", forbidden)
    assert tool.main(argv(files)) == 0
    out = capsys.readouterr()
    plan = json.loads(out.out)
    assert plan["mode"] == "plan" and plan["shape"]["gpu_count"] == 0
    assert plan["shape"]["vcpu"] == 1 and plan["hourly_usd"] == "0.05"
    assert plan["ceiling_cents"] == 5 and plan["max_run_seconds"] == 300
    assert Decimal(plan["worst_case_cents"]) <= plan["ceiling_cents"]
    assert plan["confirm_to_run"] == "spend-up-to-5c"
    assert "no network call" in out.err
    assert TOKEN.decode() not in out.out + out.err


def test_a_run_needs_the_exact_confirmation_and_its_files(files, monkeypatch, capsys):
    monkeypatch.setattr(
        tool, "run_live", lambda *a, **k: pytest.fail("must not run without confirmation")
    )
    extra = ["--run", "--credential-file", str(files["credential"])]
    extra += ["--receipt-directory", str(files["receipts"])]
    for confirm in (None, "yes", "spend-up-to-9c", "spend-up-to-5c "):
        flags = extra + ([] if confirm is None else ["--confirm", confirm])
        assert tool.main(argv(files, *flags)) == 2, confirm
    assert tool.main(argv(files, "--run", "--confirm", "spend-up-to-5c")) == 2  # no files
    assert "refused" in capsys.readouterr().err


def test_the_hard_ceiling_is_a_constant_under_ten_cents(files):
    assert tool.HARD_CEILING_CENTS == 9 < 10
    for cents in (0, 10, 50, -1):
        assert tool.main(argv(files, cents=cents)) == 2
    assert tool.main(argv(files, cents=9)) == 0


def test_the_plan_refuses_a_gpu_profile_an_unlisted_image_and_an_unbuyable_run(files):
    import argparse

    with pytest.raises(tool.CheckRefused, match="CPU-only"):
        gpu = profile_data(name="cpu-check")
        document = dict(schema="tessera.salad.campaign-profiles.v1", profiles=[gpu])
        private(files["profiles"], json.dumps(document).encode())
        plan_for(files)
    document = files["document"]
    private(files["profiles"], json.dumps(document).encode())
    with pytest.raises(tool.CheckRefused, match="allowlisted"):
        plan_for(files, image="ghcr.io/example/other@sha256:" + "9" * 64)
    # A rate so high that the ceiling buys under 30 s of running is refused, not shortened.
    pricey = profile_data(
        name="cpu-check",
        gpu_class=None,
        cpu_rate=dict(vcpu_hour_usd="9.99", memory_gb_hour_usd="9.99"),
    )
    private(
        files["profiles"],
        json.dumps(dict(schema="tessera.salad.campaign-profiles.v1", profiles=[pricey])).encode(),
    )
    with pytest.raises(tool.CheckRefused, match="after reserving 30 s"):
        plan_for(files)
    # A workload that declares more running time than the bound allows is refused.
    private(files["profiles"], json.dumps(document).encode())
    long_contract = contract_data(workload_seconds=400)
    private(files["contract"], json.dumps(long_contract).encode())
    with pytest.raises(tool.CheckRefused):
        plan_for(files)
    with pytest.raises(tool.CheckRefused, match="no such profile"):
        tool.build_plan(
            argparse.Namespace(
                profiles=files["profiles"],
                profile="missing",
                contract=files["contract"],
                image=IMAGE,
                max_cents=5,
            )
        )


# -- a live run against the fake: shape, billing estimate, cleanup --


def test_a_bounded_run_checks_the_cpu_shape_estimates_billing_and_cleans_up(files):
    clock = Clock()
    fake = FakeSalad(clock, run_seconds=20)
    receipt = run(files, fake, clock)
    assert receipt["shape_ok"] is True and "aborted" not in receipt
    assert receipt["final_state"] == "succeeded"
    assert receipt["cleanup"] == dict(stopped=True, deleted=True)
    assert fake.status == "absent"  # nothing is left behind to bill
    # 20 s of running at $0.05/hour is 0.0278 cents, far under the 5-cent ceiling.
    assert receipt["running_seconds"] >= 20
    estimate = Decimal(receipt["estimated_cents"])
    assert Decimal("0.0277") <= estimate <= Decimal("0.05") and receipt["within_ceiling"] is True
    assert receipt["rate_basis"] == "operator_cpu_rate_not_actual_cost"
    assert "manual" in receipt["billing_verification"]
    # The only calls are the CPU ones: no GPU class read, no availability read.
    paths = [p for _, p, _ in fake.requests]
    assert not any("gpu" in p or "availability" in p for p in paths)
    body = json.loads(next(b for m, _, b in fake.requests if m == "POST" and b))
    assert body["container"]["resources"]["gpu_classes"] == []
    assert TOKEN.decode() not in json.dumps(receipt)


def test_a_run_that_never_ends_is_stopped_at_the_bound_and_stays_within_the_ceiling(files):
    clock = Clock()
    fake = FakeSalad(clock, never_ends=True)
    receipt = run(files, fake, clock)
    plan = plan_for(files)
    assert receipt["final_state"] == "running"
    assert plan.max_run_seconds <= receipt["running_seconds"] <= plan.max_run_seconds + 10
    assert receipt["within_ceiling"] is True
    assert Decimal(receipt["estimated_cents"]) <= plan.ceiling_cents
    assert ("POST", "stop") in fake.calls() and fake.status == "absent"
    assert receipt["cleanup"] == dict(stopped=True, deleted=True)


def test_the_spend_bound_covers_the_time_the_stop_takes_to_take_effect(files):
    # A dear profile (1 vCPU + 1 GB at $1/hour) makes the bound bite: 5 cents buy 180 s, of which
    # 30 s are reserved for the stop, so the run is cut at 150 s and the worst case is 180 s.
    dear = profile_data(
        name="cpu-check",
        gpu_class=None,
        cpu_rate=dict(vcpu_hour_usd="0.5", memory_gb_hour_usd="0.5"),
    )
    document = dict(schema="tessera.salad.campaign-profiles.v1", profiles=[dear])
    private(files["profiles"], json.dumps(document).encode())
    plan = plan_for(files)
    assert (plan.max_run_seconds, plan.settle_reserve_seconds) == (150, 30)
    assert plan.worst_case_cents == Decimal("1.0") * 180 / 36 == Decimal(5)
    assert plan.worst_case_cents <= plan.ceiling_cents
    assert plan.summary()["settle_reserve_seconds"] == 30


def test_billing_runs_until_the_stop_takes_effect_and_the_estimate_counts_it(files):
    clock = Clock()
    fake = FakeSalad(clock, never_ends=True, stop_delay=20)  # slow to stop: still billed 20 s
    receipt = run(files, fake, clock)
    plan = plan_for(files)
    # Cut at max_run, then 20 more seconds before the instance is really down: all of it counts.
    assert receipt["running_seconds"] >= plan.max_run_seconds + 20
    expected = plan.hourly_usd * Decimal(str(receipt["running_seconds"])) / 36
    assert Decimal(receipt["estimated_cents"]) == expected.quantize(Decimal("0.0001"))
    assert receipt["within_ceiling"] is True  # the 30 s reserve covers a 20 s stop
    assert receipt["cleanup"] == dict(stopped=True, deleted=True)


def test_a_stop_that_never_settles_is_billed_through_the_whole_wait_and_flagged(files):
    clock = Clock()
    fake = FakeSalad(clock, never_ends=True, stop_delay=10_000)  # never observed stopped
    receipt = run(files, fake, clock)
    plan = plan_for(files)
    assert receipt["cleanup"]["stopped"] is False and "action_needed" in receipt["cleanup"]
    # Billing is counted through the end of the settle wait, not just to the stop request.
    assert receipt["running_seconds"] >= plan.max_run_seconds + tool.SETTLE_WAIT_SECONDS
    assert Decimal(receipt["estimated_cents"]) > Decimal(
        str(plan.hourly_usd * plan.max_run_seconds / 36)
    )


def test_a_group_that_reports_a_gpu_class_fails_the_shape_check_and_is_never_started(files):
    clock = Clock()
    fake = FakeSalad(clock, gpu_in_group=True)
    receipt = run(files, fake, clock)
    assert receipt["shape_ok"] is False and "could not be created as asked" in receipt["aborted"]
    assert ("POST", "start") not in fake.calls()  # nothing ever ran or billed
    assert receipt["running_seconds"] == 0 and receipt["estimated_cents"] == "0.0000"
    # The transport refused the reply and surfaced no id, so the group is named for a person.
    assert "stop and delete it by hand" in receipt["cleanup"]["action_needed"]
    assert receipt["group"].startswith("attempt-")


def test_a_failing_start_still_stops_and_deletes_the_group(files):
    clock = Clock()
    fake = FakeSalad(clock)
    fake.fail[("POST", "/start")] = 500
    receipt = run(files, fake, clock)
    assert receipt["aborted"] == "Salad did not accept the start; nothing ran"
    assert receipt["running_seconds"] == 0 and clock.now < 1000 + 60  # no long allocation wait
    assert receipt["cleanup"] == dict(stopped=True, deleted=True) and fake.status == "absent"


def test_a_cleanup_that_cannot_delete_says_so_loudly(files, tmp_path, capsys):
    clock = Clock()
    fake = FakeSalad(clock, run_seconds=5)
    fake.fail[("DELETE", "")] = 500  # every DELETE fails
    receipt = run(files, fake, clock)
    assert receipt["cleanup"]["deleted"] is False
    assert "stop and delete the container group" in receipt["cleanup"]["action_needed"]
    assert receipt["group"].startswith("attempt-")


def test_a_credential_is_wiped_after_the_run(files):
    clock = Clock()
    credential = bytearray(TOKEN)
    tool.run_live(
        plan_for(files), credential, factory=FakeSalad(clock, run_seconds=5), clock=clock,
        sleep=clock.sleep,
    )  # fmt: skip
    assert not any(credential)


def test_main_runs_the_confirmed_check_writes_a_private_receipt_and_prints_no_secret(
    files, monkeypatch, capsys
):
    clock = Clock()
    fake = FakeSalad(clock, run_seconds=10)
    real = tool.run_live
    monkeypatch.setattr(
        tool,
        "run_live",
        lambda plan, credential: real(
            plan, credential, factory=fake, clock=clock, sleep=clock.sleep
        ),
    )
    flags = ["--run", "--confirm", "spend-up-to-5c", "--credential-file", str(files["credential"])]
    flags += ["--receipt-directory", str(files["receipts"])]
    assert tool.main(argv(files, *flags)) == 0
    out = capsys.readouterr()
    (written,) = list(files["receipts"].iterdir())
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert json.loads(written.read_text())["shape_ok"] is True
    assert TOKEN.decode() not in out.out + out.err + written.read_text()
    with pytest.raises(FileExistsError):
        tool._write_receipt(files["receipts"], json.loads(written.read_text()))  # never overwritten


def test_the_credential_file_must_be_private(files):
    files["credential"].chmod(0o644)
    with pytest.raises(tool.CheckRefused, match="owner-only"):
        tool._read_credential(files["credential"])
    private(files["credential"], b"two words\n")
    with pytest.raises(tool.CheckRefused, match="printable token"):
        tool._read_credential(files["credential"])


# -- the manual billing comparison --


def make_receipt(files, estimated="0.4000"):
    receipt = dict(schema=tool.RECEIPT_SCHEMA, estimated_cents=estimated)
    return private(files["root"] / "receipt.json", json.dumps(receipt).encode())


@pytest.mark.parametrize(
    "portal,agrees",
    [("0.40", True), ("0.9", True), ("1.4", True), ("2.0", False), ("0", True), ("9", False)],
)
def test_the_billing_comparison_accepts_a_figure_near_the_estimate(files, capsys, portal, agrees):
    receipt = make_receipt(files)
    code = tool.main(["billing", "--receipt", str(receipt), "--portal-cents", portal])
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["agrees"] is agrees and code == (0 if agrees else 1)


def test_the_billing_comparison_refuses_bad_inputs(files):
    receipt = make_receipt(files)
    for portal in ("-1", "abc", "nan"):
        assert tool.main(["billing", "--receipt", str(receipt), "--portal-cents", portal]) == 2
    other = private(files["root"] / "other.json", json.dumps({"schema": "x"}).encode())
    assert tool.main(["billing", "--receipt", str(other), "--portal-cents", "1"]) == 2


def test_importing_the_tool_has_no_effects_and_no_pytest_collectable_live_test():
    # The module defines no test functions and nothing runs at import; only main() with an
    # exact confirmation can spend, and no test passes one to a real transport.
    assert not [name for name in dir(tool) if name.startswith("test_")]
    assert os.access(tool.__file__, os.R_OK)


# -- an unexpected error never loses the receipt ------------------------------------------------


def test_an_unexpected_error_before_anything_exists_keeps_the_group_name_and_no_message(
    files, monkeypatch
):
    clock = Clock()
    fake = FakeSalad(clock)

    def boom(self, selection, secret):
        raise RuntimeError("detail-that-must-not-be-kept " + TOKEN.decode())

    monkeypatch.setattr(tool_transport(), "preflight", boom)
    receipt = run(files, fake, clock)
    assert receipt["aborted"] == "unexpected RuntimeError; see the cleanup outcome"
    assert receipt["group"].startswith("attempt-") and receipt["running_seconds"] == 0
    assert "detail-that-must-not-be-kept" not in json.dumps(receipt)
    assert TOKEN.decode() not in json.dumps(receipt)
    assert fake.requests == []  # nothing was created


def test_an_unexpected_error_mid_run_still_stops_deletes_and_records_the_outcome(files):
    clock = Clock()
    fake = FakeSalad(clock, never_ends=True)
    calls = []

    def sleep_that_fails_once(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            raise RuntimeError("unexpected in the poll loop")
        clock.sleep(seconds)

    receipt = tool.run_live(
        plan_for(files), bytearray(TOKEN), factory=fake, clock=clock, sleep=sleep_that_fails_once
    )
    assert receipt["aborted"] == "unexpected RuntimeError; see the cleanup outcome"
    assert receipt["cleanup"] == dict(stopped=True, deleted=True) and fake.status == "absent"
    assert receipt["group"].startswith("attempt-") and receipt["started"] is True
    assert "unexpected in the poll loop" not in json.dumps(receipt)


def test_main_exits_nonzero_and_writes_the_receipt_after_an_unexpected_error(
    files, monkeypatch, capsys
):
    clock = Clock()
    fake = FakeSalad(clock)
    real = tool.run_live

    def failing_sleep(seconds):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        tool,
        "run_live",
        lambda plan, credential: real(
            plan, credential, factory=fake, clock=clock, sleep=failing_sleep
        ),
    )
    flags = ["--run", "--confirm", "spend-up-to-5c", "--credential-file", str(files["credential"])]
    flags += ["--receipt-directory", str(files["receipts"])]
    assert tool.main(argv(files, *flags)) == 1
    (written,) = list(files["receipts"].iterdir())  # the receipt exists though the run failed
    assert "aborted" in json.loads(written.read_text())


def tool_transport():
    from tessera_control.salad_http import SaladHTTPTransport

    return SaladHTTPTransport
