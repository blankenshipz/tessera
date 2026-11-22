"""ADR 0034 sections 1 and 3: frozen egress policy and worker journal route."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import test_execution as fx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tessera_control.api import create_app
from tessera_control.egress_journal import egress_journal_for_operation
from tessera_control.egress_models import EgressPolicy
from tessera_control.models import ExecutionSpec
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Store, _connect, canonical_digest, initialize_state

DIGEST = "b" * 64
BASE_SPEC = {
    "argv": ["python", "-c", "print('x')"],
    "input_snapshot_digest": DIGEST,
    "pinned_skill_digests": [],
    "output_paths": ["result.txt"],
    "profile": "local-cpu",
    "network_enabled": False,
    "max_output_bytes": 1024,
}
POLICY = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "example.org", "port": 443}],
    "max_egress_bytes": 1000,
}


def test_spec_without_egress_serializes_exactly_as_before() -> None:
    dumped = ExecutionSpec.model_validate(BASE_SPEC).model_dump(mode="json")
    assert dumped == {
        "argv": ["python", "-c", "print('x')"],
        "input_snapshot_digest": DIGEST,
        "pinned_skill_digests": [],
        "output_paths": ["result.txt"],
        "profile": "local-cpu",
        "docker": None,
        "network_enabled": False,
        "max_output_bytes": 1024,
    }
    with_egress = ExecutionSpec.model_validate(BASE_SPEC | {"egress": POLICY})
    assert with_egress.model_dump(mode="json")["egress"] == POLICY
    assert canonical_digest(dumped) != canonical_digest(with_egress.model_dump(mode="json"))


@pytest.mark.parametrize("profile", ["agent-sandbox", "kubernetes-opencode", "ordinary-docker"])
def test_non_local_profiles_refuse_egress(profile: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(BASE_SPEC | {"profile": profile, "egress": POLICY})


def test_other_profile_refusal_mentions_adr() -> None:
    with pytest.raises(ValidationError, match="ADR 0034"):
        ExecutionSpec.model_validate(
            BASE_SPEC | {"profile": "kubernetes-opencode", "egress": POLICY}
        )


def test_network_enabled_stays_false() -> None:
    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(BASE_SPEC | {"network_enabled": True, "egress": POLICY})


@pytest.mark.parametrize(
    "host", ["localhost", "10.0.0.1", "169.254.169.254", "foo.internal", "127.0.0.1", "x.local"]
)
def test_non_public_destinations_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        EgressPolicy.model_validate(POLICY | {"destinations": [{"host": host, "port": 443}]})


def test_policy_bounds() -> None:
    dup = [{"host": "example.org", "port": 443}] * 2
    for bad in (
        POLICY | {"destinations": []},
        POLICY | {"destinations": dup},
        POLICY | {"destinations": [{"host": f"h{i}.example.org", "port": 443} for i in range(33)]},
        POLICY | {"max_egress_bytes": 0},
        POLICY | {"max_egress_bytes": 2**31 + 1},
        POLICY | {"profile": "other"},
        POLICY | {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            EgressPolicy.model_validate(bad)
    assert EgressPolicy.model_validate(POLICY | {"max_egress_bytes": 2**31})


def _approved_task(state: Path, owner: Any, project_id: str, digest: str, spec_extra: dict):
    store = Store(state)
    _, task = store.create_task(
        owner,
        project_id,
        "task-egress",
        canonical_digest({"n": "egress"}),
        {
            "title": "egress",
            "purpose": "Exercise egress.",
            "budget_cents": 100,
            "max_duration_seconds": 60,
        },
    )
    revision = {
        "expected_revision": task["revision"],
        "title": "egress",
        "purpose": "Exercise egress.",
        "budget_cents": 100,
        "max_duration_seconds": 60,
        "execution_spec": BASE_SPEC | {"input_snapshot_digest": digest} | spec_extra,
    }
    _, revised = store.revise_task_plan(
        owner, task["id"], "rev", canonical_digest(revision), revision
    )
    _, approved = store.approve_task(
        owner,
        revised["id"],
        "appr",
        canonical_digest({"r": revised["revision"]}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    return approved


def _world(tmp_path: Path, spec_extra: dict):
    tmp_path.mkdir(exist_ok=True)
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    initialize_state(state)
    owner = fx._owner(state)
    _, project = Store(state).create_project(
        owner, "p", canonical_digest({"n": "P"}), {"name": "P"}
    )
    worker = fx._worker(state, owner, project["id"], "worker-token")
    snapshot, _ = fx._snapshot_release(state, owner, project["id"], worker["id"])
    task = _approved_task(state, owner, project["id"], snapshot["digest"], spec_extra)
    execution = RuntimePackageStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    execution.configure_project_execution(
        owner, project["id"], "lim", canonical_digest(limits), limits
    )
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", "grant", fx._grant_payload(task, worker, snapshot)
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    _, op = execution.execution_package(worker, lease["id"], "pkg", "pkg", lease["epoch"])
    return state, owner, worker, project, task, lease, op


def _package_of(state: Path, op: dict) -> dict:
    # local-cpu packages are returned, not stored; re-fetch through the same call.
    return op["package"] if "package" in op else op


def test_package_network_profile_none_and_allowlist(tmp_path: Path) -> None:
    *_, op = _world(tmp_path / "a", {})
    pkg = _package_of(tmp_path, op)
    assert pkg["network_profile"] == "none" and "egress" not in pkg
    *_, op = _world(tmp_path / "b", {"egress": POLICY})
    pkg = _package_of(tmp_path, op)
    assert pkg["network_profile"] == "allowlist-proxy-v1"
    assert pkg["egress"] == POLICY


def _journal(destinations=None, **overrides):
    destinations = destinations or [
        {
            "host": "example.org",
            "port": 443,
            "decision": "allow",
            "connections": 2,
            "bytes_up": 10,
            "bytes_down": 90,
        },
        {
            "host": "evil.example.net",
            "port": 443,
            "decision": "deny",
            "connections": 1,
            "bytes_up": 0,
            "bytes_down": 0,
        },
    ]
    summary = {
        "connections_allowed": sum(
            d["connections"] for d in destinations if d["decision"] == "allow"
        ),
        "connections_denied": sum(
            d["connections"] for d in destinations if d["decision"] == "deny"
        ),
        "bytes_up": sum(d["bytes_up"] for d in destinations),
        "bytes_down": sum(d["bytes_down"] for d in destinations),
        "connections_unclosed": sum(d.get("unclosed", 0) for d in destinations),
        "destinations": destinations,
    }
    return {"journal_sha256": "c" * 64, "summary": summary} | overrides


class Harness:
    def __init__(self, tmp_path: Path, spec_extra: dict) -> None:
        self.state, self.owner, self.worker, self.project, self.task, self.lease, op = _world(
            tmp_path, spec_extra
        )
        self.operation_id = _package_of(tmp_path, op)["operation_id"]
        self.client = TestClient(create_app(self.state))
        self.owner_header = {
            "Authorization": (self.state / "owner.header").read_text().strip().split(": ", 1)[1]
        }

    def post(self, body, *, key="k1", headers=None, operation_id=None, lease=True):
        body = dict(body)
        if lease:
            body.setdefault("lease_id", self.lease["id"])
            body.setdefault("expected_epoch", self.lease["epoch"])
        return self.client.post(
            f"/worker/operations/{operation_id or self.operation_id}/egress-journal",
            json=body,
            headers=(headers or {"Authorization": "Bearer worker-token"})
            | {"Idempotency-Key": key},
        )

    def events(self):
        with _connect(self.state / "tessera.sqlite3") as connection:
            return egress_journal_for_operation(connection, self.operation_id)


@pytest.fixture
def egress(tmp_path: Path) -> Harness:
    return Harness(tmp_path, {"egress": POLICY})


def test_journal_happy_path_replay_and_second_summary(egress: Harness) -> None:
    first = egress.post(_journal())
    assert first.status_code == 201, first.text
    recorded = egress.events()
    assert recorded["network_profile"] == "allowlist-proxy-v1"
    assert recorded["cap_exceeded"] is False
    assert recorded["journal_sha256"] == "c" * 64
    replay = egress.post(_journal())
    assert replay.status_code == 201 and replay.json() == first.json()
    different = _journal(journal_sha256="d" * 64)
    assert egress.post(different, key="k2").status_code == 409
    with _connect(egress.state / "tessera.sqlite3") as connection:
        count = connection.execute(
            "SELECT count(*) FROM events WHERE event_type='execution.egress_journal_recorded'"
        ).fetchone()[0]
    assert count == 1


def test_wrong_audience_refused(egress: Harness) -> None:
    for headers in (egress.owner_header,):
        response = egress.post(_journal(), headers=headers)
        assert response.status_code in {401, 403}
    agent = "a" * 43
    created = egress.client.post(
        "/credentials",
        headers=egress.owner_header | {"Idempotency-Key": "agent"},
        json={
            "token": agent,
            "kind": "agent",
            "audience": "control",
            "project_id": egress.project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    agent_headers = {"Authorization": f"Bearer {agent}"}
    assert egress.post(_journal(), headers=agent_headers, key="k3").status_code in {401, 403}
    approve = egress.client.post(
        f"/tasks/{egress.task['id']}/approve",
        json={
            "expected_revision": egress.task["revision"],
            "expected_plan_digest": egress.task["current_plan_digest"],
        },
        headers=agent_headers | {"Idempotency-Key": "ag-approve"},
    )
    assert approve.status_code == 403
    assert egress.events() is None


def test_wrong_lease_or_epoch_conflicts(egress: Harness) -> None:
    assert egress.post(_journal() | {"lease_id": "other-lease"}).status_code == 409
    assert (
        egress.post(
            _journal() | {"expected_epoch": egress.lease["epoch"] + 1}, key="k2"
        ).status_code
        == 409
    )
    assert egress.events() is None


def test_unknown_operation_not_found(egress: Harness) -> None:
    assert egress.post(_journal(), operation_id="0" * 32).status_code == 404


def test_non_egress_package_conflicts(tmp_path: Path) -> None:
    plain = Harness(tmp_path, {})
    assert plain.post(_journal()).status_code == 409
    assert plain.events() is None


def test_allowed_destination_outside_allowlist_conflicts(egress: Harness) -> None:
    destinations = [
        {
            "host": "example.org",
            "port": 8443,
            "decision": "allow",
            "connections": 1,
            "bytes_up": 1,
            "bytes_down": 1,
        }
    ]
    assert egress.post(_journal(destinations)).status_code == 409
    assert egress.events() is None


def test_totals_mismatch_rejected(egress: Harness) -> None:
    body = _journal()
    body["summary"]["bytes_up"] += 1
    assert egress.post(body).status_code in {400, 409, 422}
    body = _journal()
    body["summary"]["connections_denied"] += 1
    assert egress.post(body, key="k2").status_code in {400, 409, 422}
    assert egress.events() is None


def test_cap_exceeded_is_recorded_not_rejected(egress: Harness) -> None:
    destinations = [
        {
            "host": "example.org",
            "port": 443,
            "decision": "allow",
            "connections": 1,
            "bytes_up": 600,
            "bytes_down": 600,
        }
    ]
    response = egress.post(_journal(destinations))
    assert response.status_code == 201, response.text
    recorded = egress.events()
    assert recorded["cap_exceeded"] is True
    assert json.dumps(recorded["summary"])


def test_unclosed_tunnel_is_recorded_not_missing(egress: Harness) -> None:
    body = _journal()
    destinations = body["summary"]["destinations"]
    destinations[0] = destinations[0] | {"connections": 2, "unclosed": 1}
    body["summary"]["connections_unclosed"] = 1
    assert egress.post(body).status_code == 201
    recorded = egress.events()
    assert recorded["summary"]["connections_unclosed"] == 1
    assert recorded["summary"]["connections_allowed"] == 2


def test_unclosed_total_must_match_destinations(egress: Harness) -> None:
    body = _journal()
    body["summary"]["connections_unclosed"] = 1
    assert egress.post(body).status_code == 400
