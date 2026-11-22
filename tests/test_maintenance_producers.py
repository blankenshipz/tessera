"""Scripted structured API Status, real private claims; not live admission proof."""

import copy

import pytest
from maintenance_hold_fixture import fixture_hold
from test_maintenance_handoff import fixture

from tessera_operator.maintenance_producers import QuotaEnforcement
from tessera_operator.schema_supervised import SupervisedDenied


def ready(tmp_path, monkeypatch):
    handoff, plan, _, _ = fixture(tmp_path / "input", monkeypatch)
    hold, api, _, _ = fixture_hold(tmp_path / "hold", plan, handoff=handoff)
    hold.prepare("hold", plan)
    hold.apply("hold", plan)
    return hold, api, plan


def rejection(request):
    name = request["body"]["metadata"]["name"]
    return dict(
        code=403,
        body=dict(
            apiVersion="v1",
            kind="Status",
            code=403,
            reason="Forbidden",
            status="Failure",
            details=dict(name=name, kind="pods"),
            message=(
                f'pods "{name}" is forbidden: exceeded quota: tessera-maintenance-hold, '
                "requested: pods=1, used: pods=1, limited: pods=0"
            ),
        ),
    )


def test_probe_claim_before_single_dryrun_and_join_hold(tmp_path, monkeypatch):
    hold, api, plan = ready(tmp_path, monkeypatch)
    requests = []

    def transport(request):
        assert (hold.directory / "enforcement.claim.json").exists()
        assert request["method"] == "POST"
        assert request["query"]["dryRun"] == "All"
        assert request["body"]["spec"]["schedulingGates"]
        requests.append(copy.deepcopy(request))
        return rejection(request)

    producer = QuotaEnforcement(hold, transport)
    producer.collect(plan)
    hold.held(plan)  # the existing real evidence consumer accepts the producer
    with pytest.raises(SupervisedDenied, match="no replay"):
        producer.collect(plan)
    assert len(requests) == 1
    assert api.quota["spec"] == {"hard": {"pods": "0"}}


@pytest.mark.parametrize(
    "fault",
    ["rbac", "auth", "network", "lostack", "success", "otherquota", "wronguid", "drift", "expired"],
)
def test_probe_rejects_nonproof_and_never_replays(tmp_path, monkeypatch, fault):
    hold, api, plan = ready(tmp_path, monkeypatch)
    calls = []

    def transport(request):
        calls.append(1)
        result = rejection(request)
        if fault in {"network", "lostack"}:
            raise TimeoutError("SECRET must never be saved")
        if fault == "rbac":
            result["body"]["message"] = (
                "User cannot create pods: exceeded quota: tessera-maintenance-hold"
            )
        if fault == "auth":
            result["code"] = 401
        if fault == "success":
            result = {"code": 201, "body": request["body"]}
        if fault == "otherquota":
            result["body"]["message"] = result["body"]["message"].replace(
                "tessera-maintenance-hold", "other"
            )
        if fault == "wronguid":
            api.quota["metadata"]["uid"] = "replacement"
        if fault == "drift":
            api.quota["spec"]["hard"]["pods"] = "1"
        if fault == "expired":
            hold.clock = lambda: plan["deadline"]
        return result

    producer = QuotaEnforcement(hold, transport)
    with pytest.raises(SupervisedDenied):
        producer.collect(plan)
    assert not (hold.directory / "enforcement.evidence.json").exists()
    assert "SECRET" not in (hold.directory / "enforcement.result.json").read_text()
    with pytest.raises(SupervisedDenied):
        producer.collect(plan)
    assert len(calls) == 1


def test_wrong_hold_uid_and_expired_plan_do_not_request(tmp_path, monkeypatch):
    hold, api, plan = ready(tmp_path, monkeypatch)
    api.quota["metadata"]["uid"] = "wrong"
    producer = QuotaEnforcement(hold, lambda request: pytest.fail("must not dispatch"))
    with pytest.raises(SupervisedDenied):
        producer.collect(plan)
    hold.clock = lambda: plan["deadline"]
    with pytest.raises(SupervisedDenied):
        producer.collect(plan)
