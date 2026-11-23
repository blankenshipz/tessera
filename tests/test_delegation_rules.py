"""Evaluating an owner-signed delegation's written rules (ADR 0122). Fakes only."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from campaign_fixture import Env
from gate_evidence_fixture import GateWorld
from salad_campaign_fixture import salad_spec_v2, salad_stage_v2
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, NEAR
from test_gate_evidence_metrics import make_lock
from test_gate_paired_delta import arms, make_campaign, rule

from tessera_control import delegation_rules, program_budget
from tessera_control.campaign_models import LOCAL_DOCKER_PROVIDER, parse_spec_json
from tessera_control.delegation_rules import (
    committed_cents,
    evaluate_approval,
    evaluate_gate,
    terms_digest,
)
from tessera_control.delegation_terms import DelegationTerms, GateRule
from tessera_control.store import _connect, utc_now

NOW = datetime.now(UTC).replace(microsecond=0)
IMAGE_STAGE = {
    "key": "a",
    "provider": NEAR,
    "runtime": "docker-cpu-v1",
    "image": "python@sha256:" + "a" * 64,
    "argv": ["python", "-c", "print(1)"],
    "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
    "max_duration_seconds": 60,
    "inputs": [
        {
            "kind": "snapshot",
            "name": "code",
            "snapshot_id": "snap",
            "digest": "b" * 64,
            "data_class": "public",
        }
    ],
}


@pytest.fixture(autouse=True)
def _reset_budget():
    program_budget.reset()
    yield
    program_budget.reset()


def _ensure_column(connection: sqlite3.Connection, table: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "delegation_id" not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN delegation_id TEXT")


def _terms(**overrides) -> DelegationTerms:
    base = {
        "budget_ceiling_cents": 100,
        "per_run_cap_cents": 10,
        "allowed_providers": [LOCAL_DOCKER_PROVIDER],
    }
    return DelegationTerms.model_validate({**base, **overrides})


def _delegation(env: Env, creator: dict, terms: DelegationTerms | None = None, **overrides):
    terms = terms or _terms()
    return {
        "id": "deleg_" + uuid.uuid4().hex[:8],
        "org_id": creator["org_id"],
        "project_id": env.project["id"],
        "created_by_principal_id": creator["id"],
        "created_credential_id": creator["credential_id"],
        "terms_json": json.dumps(terms.model_dump(mode="json")),
        "terms_digest": terms_digest(terms),
        "expires_at": (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "status": "active",
        "revision": 1,
        **overrides,
    }


class Approvals:
    def __init__(self, tmp_path: Path) -> None:
        self.env = Env(tmp_path)
        self.creator = self.env.human()
        self.connection = _connect(self.env.state / "tessera.sqlite3")
        for table in ("campaign_approvals", "campaign_gate_decisions"):
            _ensure_column(self.connection, table)

    def campaign(self, cap: int = 10) -> dict:
        submitted = self.env.submit(spec=self.env.spec(budget_cap_cents=cap))
        return self.row(submitted["id"])

    def row(self, campaign_id: str) -> dict:
        return dict(
            self.connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        )

    def spec(self, campaign: dict):
        return parse_spec_json(campaign["spec_json"])

    def approve_under(self, delegation: dict, cap: int = 10) -> dict:
        submitted = self.env.submit(spec=self.env.spec(budget_cap_cents=cap))
        self.env.approve(submitted)
        self.connection.execute(
            "UPDATE campaign_approvals SET delegation_id=? WHERE campaign_id=?",
            (delegation["id"], submitted["id"]),
        )
        return self.row(submitted["id"])

    def evaluate(self, delegation, campaign, spec=None, **kw):
        return evaluate_approval(
            self.connection,
            delegation,
            kw.pop("campaign_view", campaign),
            spec or self.spec(campaign),
            now=kw.pop("now", NOW),
            **kw,
        )


@pytest.fixture
def ap(tmp_path):
    return Approvals(tmp_path)


def _only(result, code):
    assert result.reasons == (code,), result
    assert not result.ok


# ---- approval ----


def test_a_covered_campaign_passes_and_details_are_numbers_only(ap):
    campaign = ap.campaign()
    result = ap.evaluate(_delegation(ap.env, ap.creator), campaign)
    assert result.ok and result.reasons == ()
    assert result.details == {
        "committed_cents": 0,
        "cap": 10,
        "approvals": 0,
        "ceiling": 100,
        "reasons": [],
    }


def test_delegation_revoked_and_expired(ap):
    campaign = ap.campaign()
    _only(
        ap.evaluate(_delegation(ap.env, ap.creator, status="revoked"), campaign),
        "delegation_revoked",
    )
    past = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _only(
        ap.evaluate(_delegation(ap.env, ap.creator, expires_at=past), campaign),
        "delegation_expired",
    )
    exact = NOW.isoformat().replace("+00:00", "Z")
    _only(
        ap.evaluate(_delegation(ap.env, ap.creator, expires_at=exact), campaign),
        "delegation_expired",
    )


def test_terms_that_do_not_parse_or_match_their_digest_are_invalid(ap):
    campaign = ap.campaign()
    bad = _delegation(ap.env, ap.creator, terms_json="{}")
    _only(ap.evaluate(bad, campaign), "delegation_terms_invalid")
    mismatched = _delegation(ap.env, ap.creator, terms_digest="0" * 64)
    _only(ap.evaluate(mismatched, campaign), "delegation_terms_invalid")
    _only(
        ap.evaluate(_delegation(ap.env, ap.creator, terms_json="not json"), campaign),
        "delegation_terms_invalid",
    )


def test_creator_must_still_be_a_live_human_with_the_same_credential(ap):
    campaign = ap.campaign()
    gone = _delegation(ap.env, ap.creator, created_by_principal_id="principal_missing")
    _only(ap.evaluate(gone, campaign), "creator_not_live")
    delegation = _delegation(ap.env, ap.creator)
    ap.connection.execute(
        "UPDATE credentials SET revoked_at=? WHERE id=?", (utc_now(), ap.creator["credential_id"])
    )
    _only(ap.evaluate(delegation, campaign), "creator_not_live")


def test_creator_demoted_credential_rotated_or_outside_the_project(ap):
    campaign = ap.campaign()
    rotated = _delegation(ap.env, ap.creator, created_credential_id="credential_old")
    _only(ap.evaluate(rotated, campaign), "creator_credential_changed")
    outside = {**ap.creator, "project_id": "project_elsewhere"}
    _only(
        ap.evaluate(_delegation(ap.env, ap.creator), campaign, creator=outside),
        "creator_not_project_member",
    )
    demoted = {**ap.creator, "role": "agent"}
    _only(
        ap.evaluate(_delegation(ap.env, ap.creator), campaign, creator=demoted), "creator_not_human"
    )
    # Demoted in storage, the creator no longer resolves as a live principal at all.
    ap.connection.execute("UPDATE principals SET role='agent' WHERE id=?", (ap.creator["id"],))
    _only(ap.evaluate(_delegation(ap.env, ap.creator), campaign), "creator_not_live")


def test_campaign_must_be_in_the_delegations_project_and_org(ap):
    campaign = ap.campaign()
    delegation = _delegation(ap.env, ap.creator)
    other_project = {**campaign, "project_id": "project_other"}
    _only(ap.evaluate(delegation, campaign, campaign_view=other_project), "wrong_project")
    other_org = {**campaign, "org_id": "org_other"}
    _only(ap.evaluate(delegation, campaign, campaign_view=other_org), "wrong_org")


def test_only_a_submitted_campaign_can_be_first_approved(ap):
    campaign = ap.campaign()
    ap.connection.execute("UPDATE campaigns SET status='running' WHERE id=?", (campaign["id"],))
    _only(ap.evaluate(_delegation(ap.env, ap.creator), campaign), "campaign_not_submitted")


def test_per_run_cap(ap):
    campaign = ap.campaign(cap=10)
    delegation = _delegation(ap.env, ap.creator, _terms(per_run_cap_cents=9))
    result = ap.evaluate(delegation, campaign)
    _only(result, "per_run_cap_exceeded")
    assert result.details["cap"] == 10


def test_ceiling_arithmetic_across_campaigns_excludes_cancelled(ap):
    delegation = _delegation(ap.env, ap.creator, _terms(budget_ceiling_cents=15))
    first = ap.approve_under(delegation)
    assert committed_cents(ap.connection, delegation["id"]) == 10
    second = ap.campaign()
    result = ap.evaluate(delegation, second)
    _only(result, "ceiling_exceeded")
    assert (result.details["committed_cents"], result.details["ceiling"]) == (10, 15)
    # A campaign approved under another delegation or by a person does not count.
    other = _delegation(ap.env, ap.creator, _terms(budget_ceiling_cents=15))
    assert committed_cents(ap.connection, other["id"]) == 0
    wider = _delegation(ap.env, ap.creator, _terms(budget_ceiling_cents=20))
    wider["id"] = delegation["id"]
    assert ap.evaluate(wider, second).ok
    ap.connection.execute("UPDATE campaigns SET status='cancelled' WHERE id=?", (first["id"],))
    assert committed_cents(ap.connection, delegation["id"]) == 0
    assert ap.evaluate(delegation, second).ok


def test_max_approvals(ap):
    delegation = _delegation(ap.env, ap.creator, _terms(max_approvals=1))
    ap.approve_under(delegation)
    result = ap.evaluate(delegation, ap.campaign())
    _only(result, "max_approvals_reached")
    assert result.details["approvals"] == 1


def test_provider_must_be_allowed_including_alternates_and_reroutes(ap):
    campaign = ap.campaign()
    narrow = _delegation(ap.env, ap.creator, _terms(allowed_providers=["somewhere-else"]))
    _only(ap.evaluate(narrow, campaign), "provider_not_allowed")
    stage = {
        **IMAGE_STAGE,
        "alternate_providers": [{"provider": FAR, "runtime": "docker-cpu-v1"}],
        "retry": {"max_attempts": 2, "infra_retries": 1, "reroute_providers": ["spare-cpu"]},
    }
    spec = parse_spec_json(json.dumps(_spec2(stage)))
    for allowed in ([NEAR], [NEAR, FAR], [NEAR, "spare-cpu"]):
        delegation = _delegation(ap.env, ap.creator, _terms(allowed_providers=allowed))
        _only(ap.evaluate(delegation, campaign, spec), "provider_not_allowed")
    everything = _delegation(ap.env, ap.creator, _terms(allowed_providers=[NEAR, FAR, "spare-cpu"]))
    assert ap.evaluate(everything, campaign, spec).ok


def _configure_program(*project_ids):
    program = {"id": "prog", "cap_cents": 1000}
    if project_ids:
        program["project_ids"] = list(project_ids)
    program_budget.configure(
        program_budget.ProgramBudgetConfig.model_validate(
            {"schema": program_budget.PROGRAM_BUDGET_SCHEMA, "programs": [program]}
        )
    )


def test_program_must_exist_and_cover_the_project(ap):
    campaign = ap.campaign()
    delegation = _delegation(ap.env, ap.creator, _terms(program_id="prog"))
    _only(ap.evaluate(delegation, campaign), "program_not_covering")  # nothing configured
    _configure_program("project_elsewhere")
    _only(ap.evaluate(delegation, campaign), "program_not_covering")
    _configure_program(ap.env.project["id"])
    assert ap.evaluate(delegation, campaign).ok
    _configure_program()
    assert ap.evaluate(delegation, campaign).ok
    unnamed = _delegation(ap.env, ap.creator, _terms(program_id="other"))
    _only(ap.evaluate(unnamed, campaign), "program_not_covering")
    program_budget.mark_broken()
    _only(ap.evaluate(delegation, campaign), "program_budget_unavailable")
    # A delegation that names no program is unaffected.
    assert ap.evaluate(_delegation(ap.env, ap.creator), campaign).ok


def test_a_salad_stage_needs_a_human(ap):
    campaign = ap.campaign()
    delegation = _delegation(ap.env, ap.creator)
    for spec in (
        salad_spec_v2(ap.env, [salad_stage_v2(ap.env, "g1")]),
        _spec2({**IMAGE_STAGE, "alternate_providers": [_salad_alternate(ap.env)]}),
    ):
        parsed = parse_spec_json(json.dumps(spec))
        result = ap.evaluate(
            _delegation(ap.env, ap.creator, _terms(allowed_providers=["salad", NEAR])),
            campaign,
            parsed,
        )
        _only(result, "spec_needs_human")
    assert ap.evaluate(delegation, campaign).ok


def _salad_alternate(env) -> dict:
    stage = salad_stage_v2(env, "g1")
    return {
        "provider": "salad",
        "runtime": stage["runtime"],
        "provider_config": stage["provider_config"],
    }


def test_several_failures_are_collected_in_order(ap):
    campaign = ap.campaign()
    delegation = _delegation(
        ap.env,
        ap.creator,
        _terms(per_run_cap_cents=5, budget_ceiling_cents=5, allowed_providers=["x"]),
        status="revoked",
        created_credential_id="credential_old",
    )
    ap.connection.execute("UPDATE campaigns SET status='running' WHERE id=?", (campaign["id"],))
    result = ap.evaluate(delegation, campaign)
    assert result.reasons == (
        "delegation_revoked",
        "creator_credential_changed",
        "campaign_not_submitted",
        "per_run_cap_exceeded",
        "ceiling_exceeded",
        "provider_not_allowed",
    )
    assert result.details["reasons"] == list(result.reasons)


def test_an_exception_becomes_evaluation_error_without_leaking_it(ap, monkeypatch):
    campaign = ap.campaign()

    def boom(*args, **kwargs):
        raise RuntimeError("secret internals " + campaign["id"])

    monkeypatch.setattr(delegation_rules, "committed_cents", boom)
    result = ap.evaluate(_delegation(ap.env, ap.creator), campaign)
    assert (result.ok, result.reasons) == (False, ("evaluation_error",))
    assert "secret" not in repr(result) and campaign["id"] not in repr(result)
    monkeypatch.undo()
    # A missing approvals column (storage not migrated) also fails closed.
    ap.connection.execute("ALTER TABLE campaign_approvals RENAME COLUMN delegation_id TO moved")
    _only(ap.evaluate(_delegation(ap.env, ap.creator), campaign), "evaluation_error")


def test_nothing_in_reasons_or_details_names_the_campaign_spec_or_providers(ap):
    campaign = ap.campaign()
    delegation = _delegation(
        ap.env,
        ap.creator,
        _terms(allowed_providers=["somewhere-else"], per_run_cap_cents=5),
        status="revoked",
    )
    result = ap.evaluate(delegation, campaign)
    assert len(result.reasons) >= 3
    blob = json.dumps([list(result.reasons), result.details])
    for secret in (
        campaign["id"],
        campaign["spec_digest"],
        LOCAL_DOCKER_PROVIDER,
        ap.env.project["id"],
    ):
        assert secret not in blob
    assert all(code == code.lower() and " " not in code for code in result.reasons)
    assert all(isinstance(v, int | list) for v in result.details.values())


# ---- gate ----


@pytest.fixture
def world(tmp_path):
    world = GateWorld(tmp_path)
    with world.db() as connection:
        _ensure_column(connection, "campaign_gate_decisions")
    return world


def _gate_delegation(world, rules, **overrides):
    terms = _terms(gate_rules=rules)
    owner = world.owner
    return {
        "id": "deleg_gate",
        "org_id": owner["org_id"],
        "project_id": world.project["id"],
        "created_by_principal_id": owner["id"],
        "created_credential_id": owner["credential_id"],
        "terms_json": json.dumps(terms.model_dump(mode="json")),
        "terms_digest": terms_digest(terms),
        "expires_at": (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "status": "active",
        "revision": 1,
        **overrides,
    }


def _rule(world, lock, name="improves", on_pass="go", on_fail="no_go", **kw):
    kw.setdefault("allowed_outcomes", ["pass", "fail"])
    return GateRule(
        name=name, bound_outcome=rule(world, lock, **kw), on_pass=on_pass, on_fail=on_fail
    )


def _finished(world):
    lock = make_lock(world)
    attempts = arms(world, lock, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])  # deltas 2, 3, 4
    return lock, make_campaign(world, attempts)


def _gate(world, delegation, campaign_id, name="improves", **kw):
    with world.db() as connection:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return evaluate_gate(connection, delegation, row, name, now=kw.pop("now", NOW))


def _vote(world, campaign_id, decision):
    with world.db() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO campaign_gate_decisions (id, org_id, project_id, campaign_id,
            campaign_spec_digest, decision, note, decided_by_principal_id, decided_credential_id,
            decision_class, automation_label, created_at, delegation_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "gatedec_" + uuid.uuid4().hex,
                world.owner["org_id"],
                world.project["id"],
                campaign_id,
                "a" * 64,
                decision,
                "n",
                world.owner["id"],
                None,
                "human_session",
                None,
                utc_now(),
                None,
            ),
        )


def test_a_passing_rule_records_go_and_a_failing_one_no_go(world):
    lock, campaign = _finished(world)
    delegation = _gate_delegation(world, [_rule(world, lock)])
    passing = _gate(world, delegation, campaign)
    assert (passing.ok, passing.decision, passing.reasons) == (True, "go", ())
    assert passing.rule_name == "improves" and passing.basis["outcome"] == "pass"
    assert len(passing.basis["result_digest"]) == 64
    failing = _gate(world, _gate_delegation(world, [_rule(world, lock, threshold=10.0)]), campaign)
    assert (failing.ok, failing.decision) == (True, "no_go")
    assert failing.basis["outcome"] == "fail"


def test_an_unmapped_outcome_or_unevaluable_result_never_guesses(world):
    lock, campaign = _finished(world)
    unmapped = _gate(world, _gate_delegation(world, [_rule(world, lock, on_pass=None)]), campaign)
    assert (unmapped.ok, unmapped.decision, unmapped.reasons) == (
        False,
        None,
        ("outcome_pass_unmapped",),
    )
    unmapped_fail = _gate(
        world, _gate_delegation(world, [_rule(world, lock, threshold=10.0, on_fail=None)]), campaign
    )
    assert unmapped_fail.reasons == ("outcome_fail_unmapped",) and unmapped_fail.decision is None
    # The minimum-pairs rule is not weakened: three pairs against a minimum of six.
    few = _gate(world, _gate_delegation(world, [_rule(world, lock, minimum_pairs=6)]), campaign)
    assert (few.ok, few.decision, few.reasons) == (False, None, ("unevaluable_insufficient_pairs",))
    sources = _gate(
        world,
        _gate_delegation(world, [_rule(world, lock, allowed_sources=["derived"])]),
        campaign,
    )
    assert sources.reasons == ("unevaluable_disallowed_measurement_source",)
    assert sources.decision is None
    # An outcome the rule's own allowed set excludes is not decided either.
    narrow = _gate(
        world,
        _gate_delegation(world, [_rule(world, lock, threshold=10.0, allowed_outcomes=["pass"])]),
        campaign,
    )
    assert narrow.reasons == ("outcome_fail_not_allowed",) and narrow.decision is None
    # Attempts that are not all the campaign's own are unevaluable.
    lock2 = make_lock(world)
    attempts = arms(world, lock2, [3.0, 5.0, 7.0], [1.0, 2.0, 3.0])
    partial = make_campaign(world, attempts[:-1])
    mixed = _gate(world, _gate_delegation(world, [_rule(world, lock2)]), partial)
    assert mixed.reasons == ("unevaluable_attempts_not_from_campaign",)


def test_gate_refusals_each_alone(world):
    lock, campaign = _finished(world)
    good = _gate_delegation(world, [_rule(world, lock)])
    assert _gate(world, good, campaign, name="nope").reasons == ("rule_unknown",)
    assert _gate(world, {**good, "status": "revoked"}, campaign).reasons == ("delegation_revoked",)
    past = (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert _gate(world, {**good, "expires_at": past}, campaign).reasons == ("delegation_expired",)
    assert _gate(world, {**good, "terms_digest": "0" * 64}, campaign).reasons == (
        "delegation_terms_invalid",
    )
    assert _gate(world, {**good, "created_by_principal_id": "nobody"}, campaign).reasons == (
        "creator_not_live",
    )
    assert _gate(world, {**good, "project_id": "project_other"}, campaign).reasons == (
        "creator_not_project_member",
        "wrong_project",
    )
    running = make_campaign(world, [], status="running")
    assert _gate(world, good, running).reasons == ("campaign_not_finished",)
    held = make_campaign(world, [], held=3)
    assert _gate(world, good, held).reasons == ("exposure_unresolved",)
    for result in (_gate(world, good, held), _gate(world, good, running)):
        assert result.ok is False and result.decision is None


def test_a_prior_no_go_vetoes_unless_the_rule_is_itself_a_no_go(world):
    lock, campaign = _finished(world)
    _vote(world, campaign, "no_go")
    vetoed = _gate(world, _gate_delegation(world, [_rule(world, lock)]), campaign)
    assert (vetoed.ok, vetoed.decision, vetoed.reasons) == (False, None, ("already_vetoed",))
    only_no_go = _gate_delegation(world, [_rule(world, lock, on_pass=None)])
    still = _gate(world, only_no_go, campaign)
    assert (still.ok, still.decision) == (False, None)  # pass is unmapped, never a go
    vetoing = _gate(
        world, _gate_delegation(world, [_rule(world, lock, threshold=10.0, on_pass=None)]), campaign
    )
    assert (vetoing.ok, vetoing.decision) == (True, "no_go")
    # A later go retracts the veto.
    _vote(world, campaign, "go")
    assert _gate(world, _gate_delegation(world, [_rule(world, lock)]), campaign).decision == "go"


def test_gate_exception_becomes_evaluation_error(world, monkeypatch):
    lock, campaign = _finished(world)

    def boom(*args, **kwargs):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(delegation_rules, "compute_paired_delta", boom)
    result = _gate(world, _gate_delegation(world, [_rule(world, lock)]), campaign)
    assert (result.ok, result.decision, result.reasons) == (False, None, ("evaluation_error",))
    assert "secret" not in repr(result) and campaign not in repr(result)
