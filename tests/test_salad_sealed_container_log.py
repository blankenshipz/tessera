"""A sealed run's container log is never read or stored (ADR 0196 amendment, ADR 0068).

Control marks a route sealed (a final holdout stage, or a sealed-partition input) and says so at
the guardian's reserve; the guardian then records the closed ``data_class_sealed`` token in place
of the one container-log read. Fakes only: no provider, no network, no credentials."""

from __future__ import annotations

import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_campaign_holdout import DIGEST, _pure
from test_salad_nonzero_exit import _events, _Guardian, _Service
from test_salad_safety_service import GROUP, Provider, make_service, reservation

from tessera_control.campaign_holdout import route_data_class_sealed
from tessera_control.salad_container_log import (
    closed_container_log,
    summarize_container_log,
    unavailable,
)
from tessera_control.salad_exit_watch import container_exit_receipt
from tessera_control.salad_server import SaladSafetyClient

SEALED = {"outcome": "unavailable", "reason": "data_class_sealed"}


# -- the closed token ----------------------------------------------------------------------


def test_the_sealed_token_is_a_closed_reason_and_reaches_the_receipt():
    assert unavailable("data_class_sealed") == SEALED
    assert closed_container_log(SEALED) == SEALED
    receipt = container_exit_receipt(_events([1]), SEALED)
    # The exit codes stay; only the tail is withheld.
    assert receipt == {"exit_codes": [1], "stderr_tail": SEALED}


# -- the exit watch's read gate --------------------------------------------------------------


def _watch(sealed):
    service = _Service(_Guardian(), [])
    service.row = dict(service.row, data_class_sealed=1 if sealed else 0)
    sends, claims, kept = [], [], []
    service._bounded_send = lambda row, action, provider_id: sends.append(action)
    service.guardian.claim_container_log_read = lambda op: claims.append(op) or len(claims) == 1
    service.guardian.record_container_log = lambda op, value: kept.append(value)
    return service, sends, claims, kept


def test_a_sealed_run_never_sends_the_log_query_and_records_the_token():
    service, sends, claims, kept = _watch(sealed=True)
    service._read_container_log(service.row)
    service._read_container_log(service.row)  # claimed once: never a second token
    service._drain_container_logs()
    assert sends == [] and claims == ["op", "op"]
    assert kept == [SEALED]


def test_a_sealed_run_without_a_provider_id_still_records_the_token():
    service, sends, _claims, kept = _watch(sealed=True)
    service.row["provider_id"] = None
    service._read_container_log(service.row)
    service._drain_container_logs()
    assert sends == [] and kept == [SEALED]


def test_a_run_that_is_not_sealed_still_sends_its_one_read():
    service, sends, _claims, _kept = _watch(sealed=False)
    service._read_container_log(service.row)
    import time

    deadline = time.monotonic() + 5
    while not service._container_log_done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sends == ["read_logs"]


# -- the real safety service ---------------------------------------------------------------


@pytest.fixture
def sealed_service(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    service = make_service(
        tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider
    )
    service.loop()
    yield service, provider
    service.close()


def _sealed_column(service, reservation_id):
    return service.db.execute(
        "SELECT data_class_sealed FROM reservations WHERE reservation=?", (reservation_id,)
    ).fetchone()[0]


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_the_reserve_flag_must_be_an_exact_boolean(sealed_service, value):
    service, provider = sealed_service
    body = reservation()
    with pytest.raises(ValueError, match="invalid_admin_request"):
        service.admin({**body, "data_class_sealed": value})
    assert not provider.calls


def test_a_reserve_without_the_flag_is_not_sealed(sealed_service):
    service, _ = sealed_service
    body = reservation()
    assert service.admin(body)["state"] == "ready"
    assert _sealed_column(service, body["reservation"]) == 0


def test_sealed_is_kept_and_a_later_reserve_never_unseals(sealed_service):
    service, _ = sealed_service
    body = reservation()
    assert service.admin({**body, "data_class_sealed": True})["state"] == "ready"
    assert _sealed_column(service, body["reservation"]) == 1
    service.admin({**body, "data_class_sealed": False})
    service.admin(body)
    assert _sealed_column(service, body["reservation"]) == 1


def test_a_later_reserve_may_seal_an_open_reservation(sealed_service):
    service, _ = sealed_service
    body = reservation()
    service.admin({**body, "data_class_sealed": False})
    assert _sealed_column(service, body["reservation"]) == 0
    service.admin({**body, "data_class_sealed": True})
    assert _sealed_column(service, body["reservation"]) == 1


def _bind(service, body):
    service.admin(
        dict(
            operation="bind", reservation=body["reservation"], provider_id=GROUP, allocation_id=None
        )
    )


def test_the_guardian_records_the_token_and_never_reads_a_sealed_log(sealed_service):
    service, provider = sealed_service
    body = reservation()
    service.admin({**body, "data_class_sealed": True})
    _bind(service, body)
    sends = []
    service._bounded_send = lambda row, action, *a, **k: sends.append(action)
    service._read_container_log(service._row(reservation=body["reservation"]))
    service._drain_container_logs()
    assert sends == []
    status = service.admin(dict(operation="status", reservation=body["reservation"]))
    assert status["container_log"] == SEALED


def test_a_tail_read_before_the_run_was_sealed_is_never_stored(sealed_service):
    service, _ = sealed_service
    body = reservation()
    service.admin({**body, "data_class_sealed": True})
    _bind(service, body)
    operation = service._row(reservation=body["reservation"])["operation"]
    tail = summarize_container_log([{"text_log": "held-out label 7"}])
    service._container_log_done.append((operation, tail))
    service._drain_container_logs()
    status = service.admin(dict(operation="status", reservation=body["reservation"]))
    assert status["container_log"] == SEALED
    assert "held-out" not in json.dumps(status)


# -- control: which routes are sealed --------------------------------------------------------


def _control_db(spec, *, approved=True, claimed=()):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE campaign_holdout_claims (project_id TEXT, partition_digest TEXT);
        CREATE TABLE campaigns (id TEXT, project_id TEXT, spec_json TEXT);
        CREATE TABLE campaign_approvals (campaign_id TEXT);
        CREATE TABLE campaign_stage_attempts (id TEXT, campaign_id TEXT, stage_key TEXT);
        CREATE TABLE campaign_salad_attempts (attempt_id TEXT, route_id TEXT);
        """
    )
    db.execute(
        "INSERT INTO campaigns VALUES('c1','p1',?)",
        (spec if isinstance(spec, str) else json.dumps(spec),),
    )
    if approved:
        db.execute("INSERT INTO campaign_approvals VALUES('c1')")
    for digest in claimed:
        db.execute("INSERT INTO campaign_holdout_claims VALUES('p1',?)", (digest,))
    for stage in ("select", "final"):
        db.execute("INSERT INTO campaign_stage_attempts VALUES(?,?,?)", (f"a-{stage}", "c1", stage))
        db.execute("INSERT INTO campaign_salad_attempts VALUES(?,?)", (f"a-{stage}", f"r-{stage}"))
    return db


def test_the_holdout_stage_route_is_sealed_and_its_selection_stage_is_not():
    db = _control_db(_pure())
    assert route_data_class_sealed(db, "p1", "r-final", None) is True
    assert route_data_class_sealed(db, "p1", "r-select", "c" * 64) is False


def test_a_route_reading_a_sealed_partition_is_sealed():
    db = _control_db(_pure(), approved=False, claimed=(DIGEST,))
    assert route_data_class_sealed(db, "p1", "r-other", DIGEST) is True
    assert route_data_class_sealed(db, "p1", "r-other", "d" * 64) is False


def test_a_spec_that_no_longer_parses_fails_closed_to_sealed():
    db = _control_db("{not json")
    assert route_data_class_sealed(db, "p1", "r-select", None) is True


def test_a_route_of_another_project_is_not_read_as_this_ones():
    db = _control_db(_pure())
    assert route_data_class_sealed(db, "p2", "r-final", None) is False


# -- the trusted server's reserve sends the flag, fail closed --------------------------------


@pytest.mark.parametrize(
    ("state_flag", "sent"), [(True, True), (False, False), ("missing", True), (None, True)]
)
def test_the_client_sends_the_flag_and_a_missing_one_is_sealed(state_flag, sent):
    client = SaladSafetyClient.__new__(SaladSafetyClient)
    bodies = []
    client._exchange = bodies.append
    state = dict(
        id="route",
        proposal_digest="d" * 64,
        org_id="o",
        project_id="p",
        proposal={},
        routing_package={},
        runtime_package={},
        budget_binding={},
        guardian_deadline=1.0,
    )
    if state_flag != "missing":
        state["data_class_sealed"] = state_flag
    client.reserve(state)
    assert bodies[0]["data_class_sealed"] is sent


# -- end to end through control's use-time create admission --------------------------------


@pytest.mark.parametrize("sealed", [False, True])
def test_create_admission_computes_the_flag_and_the_reserve_carries_it(
    tmp_path, monkeypatch, sealed
):
    from test_salad_server import fixture

    from tessera_control import campaign_holdout
    from tessera_control.remote_execution_models import RemoteProposal

    h = fixture(tmp_path, monkeypatch)
    proposal = RemoteProposal.model_validate(h.route["proposal"])
    if sealed:  # the route's data snapshot is a sealed partition of its project
        data = proposal.selection.manifests.data
        monkeypatch.setattr(campaign_holdout, "sealed_digests", lambda db, project: {data})
    admission = h.store.preflight_authority(h.worker, h.route["id"], proposal, "create")
    assert admission["data_class_sealed"] is sealed
    h.server.safety.reserve(admission)
    assert h.safety_calls[-1]["operation"] == "reserve"
    assert h.safety_calls[-1]["data_class_sealed"] is sealed
