"""ADR 0034 phase 2: governed allowlist egress on the direct Sandbox (synthetic cluster only)."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_agent_authority import enroll, prepared  # noqa: F401
from test_agent_sandbox import (
    FakeCluster,
    approved_plan,
    create,
    observe,
    resource,
    stop,
)
from test_execution import _grant_payload, _setup

from tessera_control.agent_sandbox import AgentSandboxStore
from tessera_control.agent_sandbox_configuration import (
    EGRESS_PROFILE,
    NONE_PROFILE,
    required_cases,
    validate_native_pair,
)
from tessera_control.agent_sandbox_models import SandboxPlan, SandboxTarget
from tessera_control.egress_journal import EVENT_TYPE, egress_journal_for_operation
from tessera_control.store import Conflict, Store, _connect, canonical_digest
from tessera_worker.agent_sandbox import AgentSandboxBackend, SandboxUnsafe, egress_manifests

PROXY_IMAGE = "registry.invalid/tessera-proxy@sha256:" + "9" * 64
POLICY = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "example.org", "port": 443}],
    "max_egress_bytes": 1000,
}
ORDER = ("sandbox_policy", "proxy_policy", "service", "proxy_pod")


class EgressCluster(FakeCluster):
    """Adds NetworkPolicy/Service/Pod creation and the raw proxy log."""

    def __init__(self, plan):
        super().__init__(plan)
        self.log = b""
        self.lost_kind = None
        self.created = []

    def request_raw(self, path, max_bytes):
        self.calls.append(("GET", path))
        assert path.endswith("/log?container=proxy&limitBytes=" + str(max_bytes))
        return self.log

    def request(self, method, path, body=None, **kwargs):
        kinds = {self.path(kind): kind for kind in ("policy", "service", "pod")}
        if method == "POST" and path in kinds:
            kind = kinds[path]
            self.calls.append((method, path))
            value = deepcopy(body)
            value["metadata"].update(
                uid=f"{kind}-uid-{len(self.created)}", resourceVersion="1", generation=1
            )
            self.created.append(value["metadata"]["name"])
            self.values[path + "/" + value["metadata"]["name"]] = value
            if kind == "policy":
                self.policies.append(value)
            if self.lost_kind == value["kind"] + value["metadata"]["name"]:
                self.lost_kind = None
                raise TimeoutError("synthetic lost egress create response")
            return deepcopy(value)
        if method == "DELETE" and path.startswith(self.path("policy") + "/"):
            old = self.values[path]
            assert body["preconditions"]["uid"] == old["metadata"]["uid"]
            self.policies[:] = [
                p for p in self.policies if p["metadata"]["uid"] != old["metadata"]["uid"]
            ]
        return super().request(method, path, body, **kwargs)

    def egress_object(self, f, name):
        wanted = egress_manifests(f.package)[name]
        kind = {"sandbox_policy": "policy", "proxy_policy": "policy"}.get(name, name.split("_")[-1])
        return self.values.get(self.path(kind, wanted["metadata"]["name"]))


def setup_egress(tmp_path, *, pin=True, egress=True):
    state, execution, owner, worker, project, task = _setup(tmp_path)
    plan, witness = approved_plan(owner["org_id"], project["id"])
    target = plan.target.model_dump() | ({"egress_proxy_image": PROXY_IMAGE} if pin else {})
    plan = SandboxPlan.model_validate(plan.model_dump() | {"target": target})
    store = Store(state)
    current = store.get_current_task_plan(owner, task["id"], task["current_plan_digest"])
    spec = current["execution_spec"] | {"profile": "agent-sandbox", "sandbox": plan.model_dump()}
    if egress:
        spec["egress"] = POLICY
    payload = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": 100,
        "max_duration_seconds": 60,
        "execution_spec": spec,
    }
    task = store.revise_task_plan(
        owner, task["id"], "sandbox-plan", canonical_digest(payload), payload
    )[1]
    task = store.approve_task(
        owner, task["id"], "sandbox-approval", "approval", task["revision"],
        task["current_plan_digest"],
    )[1]  # fmt: skip
    with _connect(state / "tessera.sqlite3") as connection:
        snapshot_id = connection.execute(
            "SELECT id FROM source_snapshots WHERE project_id=?", (project["id"],)
        ).fetchone()[0]
    grant_body = _grant_payload(task, worker, {"id": snapshot_id})
    grant = execution.issue_execution_grant(
        owner, project["id"], "sandbox-grant", canonical_digest(grant_body), grant_body
    )[1]
    lease = execution.acquire_execution_lease(worker, grant["id"], "sandbox-lease", "lease")[1]
    cluster = EgressCluster(plan)
    backend = AgentSandboxBackend(cluster, lambda target: deepcopy(witness))
    cluster.backend = backend
    return SimpleNamespace(
        state=state,
        execution=execution,
        owner=owner,
        worker=worker,
        project=project,
        grant=grant,
        lease=lease,
        cluster=cluster,
        backend=backend,
        adapter=AgentSandboxStore(state, backend),
        body={"expected_epoch": lease["epoch"]},
        package=None,
    )


def created(f):
    result = create(f)
    with _connect(f.state / "tessera.sqlite3") as connection:
        row = connection.execute(
            "SELECT package_json FROM execution_runtime_packages WHERE operation_id=?",
            (f.operation_id,),
        ).fetchone()
    f.package = json.loads(row["package_json"])
    return result


def journal(f, key="journal"):
    return f.adapter.egress_journal(f.worker, f.operation_id, key, f.body)


def finish(f):
    """Run to a terminal, stopped, quiescent Sandbox."""
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
    f.cluster.terminal(f.operation_id)
    observe(f)
    stop(f)
    assert observe(f, "stopped")["quiescent"] is True


LOG = (
    json.dumps(
        {"host": "example.org", "port": 443, "decision": "allow", "bytes_up": 10, "bytes_down": 90}
    )
    + "\n"
    + json.dumps(
        {"host": "evil.example", "port": 443, "decision": "deny", "bytes_up": 0, "bytes_down": 0}
    )
    + "\n"
).encode()


def test_network_none_binding_and_target_are_unchanged(tmp_path):
    from test_agent_sandbox import setup

    f = setup(tmp_path)
    create(f)
    assert "egress_proxy_image" not in f.cluster.target.model_dump()
    assert "egress_proxy_image" not in f.cluster.target.model_dump(mode="json")
    with _connect(f.state / "tessera.sqlite3") as connection:
        package = json.loads(
            connection.execute(
                "SELECT package_json FROM execution_runtime_packages WHERE operation_id=?",
                (f.operation_id,),
            ).fetchone()["package_json"]
        )
        blueprint = connection.execute(
            "SELECT blueprint_json, state_json FROM agent_sandbox_bindings"
        ).fetchone()
    assert package["profile_version"] == "agent-sandbox-network-none-v1"
    assert "network_profile" not in package and "egress" not in package
    assert "tessera_egress" not in blueprint["blueprint_json"]
    assert "egress_objects" not in blueprint["state_json"]
    assert "egress_proxy_image" not in json.dumps(package["sandbox"])
    pinned = SandboxTarget.model_validate(
        f.cluster.target.model_dump() | {"egress_proxy_image": PROXY_IMAGE}
    )
    assert canonical_digest(pinned.model_dump()) != canonical_digest(f.cluster.target.model_dump())
    assert pinned.model_dump()["egress_proxy_image"] == PROXY_IMAGE
    assert not any(
        method == "POST" and "networkpolicies" in path for method, path in f.cluster.calls
    )


def test_egress_plan_without_target_proxy_pin_is_refused(tmp_path):
    f = setup_egress(tmp_path, pin=False)
    with pytest.raises(Conflict, match="egress proxy image"):
        create(f)
    assert f.cluster.calls == []


def test_egress_create_makes_four_objects_before_the_sandbox(tmp_path):
    f = setup_egress(tmp_path)
    assert created(f)["phase"] == "created_suspended"
    assert f.package["profile_version"] == "agent-sandbox-egress-allowlist-v1"
    assert f.package["network_profile"] == "allowlist-proxy-v1"
    assert f.package["egress"] == POLICY
    posts = [path for method, path in f.cluster.calls if method == "POST"]
    c = f.cluster
    assert posts == [
        c.path("policy"), c.path("policy"), c.path("service"), c.path("pod"), c.path("sandbox")
    ]  # fmt: skip
    assert f.cluster.created == [
        egress_manifests(f.package)[name]["metadata"]["name"] for name in ORDER
    ]
    sandbox = c.values[c.path("sandbox", "tessera-sb-" + f.operation_id)]
    env = {
        e["name"]: e["value"]
        for e in sandbox["spec"]["podTemplate"]["spec"]["containers"][0]["env"]
    }
    assert (
        env["HTTPS_PROXY"]
        == f"http://tessera-egress-{f.operation_id}:{env['HTTPS_PROXY'].rsplit(':', 1)[1]}"
    )
    state = f.adapter.status(f.owner, f.operation_id)
    assert all(state["egress_objects"][name]["uid"] for name in ORDER)
    assert all(state["egress_objects"][name]["attempted"] for name in ORDER)
    with _connect(f.state / "tessera.sqlite3") as connection:
        stored = json.loads(
            connection.execute("SELECT blueprint_json FROM agent_sandbox_bindings").fetchone()[0]
        )
    assert stored["tessera_egress"] == egress_manifests(f.package)


def test_lost_egress_object_create_permits_lookup_only(tmp_path):
    f = setup_egress(tmp_path)
    op = hashlib.sha256(f"tessera-agent-sandbox-v1:{f.lease['id']}".encode()).hexdigest()[:32]
    f.cluster.lost_kind = "Servicetessera-egress-" + op
    assert create(f)["phase"] == "uncertain"
    assert not any(path == f.cluster.path("sandbox") for _, path in f.cluster.calls)
    before = sum(method == "POST" for method, _ in f.cluster.calls)
    result = f.adapter.create(f.worker, f.lease["id"], "retry-create", f.body)[1]
    assert sum(method == "POST" for method, _ in f.cluster.calls) == before
    # The retry only looks up; the not-yet-created proxy Pod is never POSTed.
    assert result["phase"] == "uncertain"
    assert f.adapter.status(f.owner, f.operation_id)["egress_objects"]["proxy_pod"]["uid"] is None


@pytest.mark.parametrize("fault", ["missing", "drift", "extra-allow-all"])
def test_release_refused_on_sandbox_policy_missing_drift_or_extra_allow_all(tmp_path, fault):
    f = setup_egress(tmp_path)
    created(f)
    f.adapter.action(f.worker, f.operation_id, "run", "run", f.body)
    policy = f.cluster.egress_object(f, "sandbox_policy")
    if fault == "missing":
        f.cluster.policies.remove(policy)
    elif fault == "drift":
        policy["spec"]["egress"].append({})
    else:
        f.cluster.policies.append(
            resource(
                "NetworkPolicy",
                "allow",
                "allow",
                {"podSelector": {}, "egress": [{}], "policyTypes": ["Egress"]},
            )
        )
    puts = sum(method == "PUT" for method, _ in f.cluster.calls)
    with pytest.raises(SandboxUnsafe):
        f.adapter.action(f.worker, f.operation_id, "release", "release", f.body)
    sandbox_pod = f.cluster.values[f.cluster.path("pod", "tessera-sb-" + f.operation_id)]
    assert sandbox_pod["spec"]["schedulingGates"]
    assert sum(method == "PUT" for method, _ in f.cluster.calls) == puts


def test_egress_journal_records_once_and_is_fenced(tmp_path):
    f = setup_egress(tmp_path)
    created(f)
    f.cluster.log = LOG
    with pytest.raises(Conflict, match="terminal"):
        journal(f, "too-early")
    finish(f)
    status, body = journal(f)
    assert status == 201 and body["recorded"] is True
    assert body["network_profile"] == "allowlist-proxy-v1"
    assert body["journal_sha256"] == hashlib.sha256(LOG).hexdigest()
    assert (
        body["summary"]["connections_allowed"] == 1 and body["summary"]["connections_denied"] == 1
    )
    assert body["cap_exceeded"] is False
    with _connect(f.state / "tessera.sqlite3") as connection:
        recorded = egress_journal_for_operation(connection, f.operation_id)
        count = connection.execute(
            "SELECT count(*) FROM events WHERE event_type=?", (EVENT_TYPE,)
        ).fetchone()[0]
    assert recorded["journal_sha256"] == body["journal_sha256"] and count == 1
    # Idempotent replay is the same answer; a new key cannot record a second journal.
    assert journal(f)[1] == body
    with pytest.raises(Conflict, match="already recorded"):
        journal(f, "second")
    # Humans cannot report a journal.
    with pytest.raises(Exception, match="worker"):
        f.adapter.egress_journal(f.owner, f.operation_id, "human", f.body)


def test_egress_journal_refuses_non_egress_binding(tmp_path):
    from test_agent_sandbox import setup

    f = setup(tmp_path)
    create(f)
    with pytest.raises(Conflict, match="egress profile"):
        f.adapter.egress_journal(f.worker, f.operation_id, "journal", f.body)


def test_egress_journal_refuses_non_allowlisted_allow(tmp_path):
    f = setup_egress(tmp_path)
    created(f)
    f.cluster.log = (
        json.dumps(
            {
                "host": "other.example",
                "port": 443,
                "decision": "allow",
                "bytes_up": 1,
                "bytes_down": 1,
            }
        )
        + "\n"
    ).encode()
    finish(f)
    with pytest.raises(Conflict, match="outside the frozen allowlist"):
        journal(f)
    with _connect(f.state / "tessera.sqlite3") as connection:
        assert egress_journal_for_operation(connection, f.operation_id) is None


def test_cleanup_deletes_the_four_objects_with_uid_preconditions(tmp_path):
    from test_agent_sandbox import durable_outputs

    f = setup_egress(tmp_path)
    created(f)
    f.cluster.log = LOG
    finish(f)
    body, _ = durable_outputs(f)
    with pytest.raises(Conflict, match="Record the egress journal"):
        f.adapter.cleanup(f.owner, f.operation_id, "early-cleanup", body)
    assert not [method for method, _ in f.cluster.calls if method == "DELETE"]
    journal(f)
    uids = {name: f.cluster.egress_object(f, name)["metadata"]["uid"] for name in ORDER}
    result = f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)[1]
    assert result["cleanup_acknowledged"] is True
    deletes = [path for method, path in f.cluster.calls if method == "DELETE"]
    names = [egress_manifests(f.package)[n]["metadata"]["name"] for n in ORDER]
    for name in names:
        assert any(path.endswith("/" + name) for path in deletes)
    assert all(f.cluster.egress_object(f, name) is None for name in ORDER)
    assert len(set(uids.values())) == 4


def test_cleanup_with_drifted_egress_object_is_uncertain_and_deletes_nothing_more(tmp_path):
    from test_agent_sandbox import durable_outputs

    f = setup_egress(tmp_path)
    created(f)
    f.cluster.log = LOG
    finish(f)
    body, _ = durable_outputs(f)
    journal(f)
    f.cluster.egress_object(f, "service")["metadata"]["uid"] = "replacement"
    result = f.adapter.cleanup(f.owner, f.operation_id, "cleanup", body)[1]
    assert result["phase"] == "cleanup_uncertain"
    assert f.cluster.egress_object(f, "service") is not None


def native(case, control, isolated):
    return {
        "case": case,
        "control_uid": "control",
        "subject_uid": "subject",
        "node_uid": "node",
        "control": {"native_receipt": {"request": request(case, "control"), "response": control}},
        "isolated": {"native_receipt": {"request": request(case, "subject"), "response": isolated}},
    }


def request(case, uid):
    return {"case": case, "subject_uid": uid, "node_uid": "node"}


def test_witness_requires_egress_cases_only_for_egress_targets(tmp_path):
    plan, _ = approved_plan()
    plain = plan.target
    pinned = SandboxTarget.model_validate(plain.model_dump() | {"egress_proxy_image": PROXY_IMAGE})
    base = {"ingress", "egress", "dns"}
    assert required_cases("cni", plain) == base
    assert required_cases("cni", pinned) == base | {"egress-proxy-allow", "egress-proxy-deny"}
    assert required_cases("quota", pinned) == {"cpu", "memory", "storage"}
    assert EGRESS_PROFILE != NONE_PROFILE
    allow_ok = {"connected": True, "bytes_received": 5}
    validate_native_pair("cni", native("egress-proxy-allow", allow_ok, {}), pinned)
    with pytest.raises(ValueError, match="native observations"):
        validate_native_pair(
            "cni",
            native("egress-proxy-allow", {"connected": True, "bytes_received": 0}, {}),
            pinned,
        )
    for refused in (
        {"connected": False, "errno": "ECONNREFUSED", "elapsed_ms": 10},
        {"connected": False, "http_status": 403, "elapsed_ms": 10},
    ):
        validate_native_pair("cni", native("egress-proxy-deny", allow_ok, refused), pinned)
    with pytest.raises(ValueError, match="native observations"):
        validate_native_pair(
            "cni",
            native(
                "egress-proxy-deny",
                allow_ok,
                {"connected": True, "bytes_received": 3, "elapsed_ms": 10},
            ),
            pinned,
        )
    with pytest.raises(ValueError, match="native observations"):
        validate_native_pair(
            "cni",
            native(
                "egress-proxy-deny",
                allow_ok,
                {"connected": False, "errno": "ENOENT", "elapsed_ms": 10},
            ),
            pinned,
        )
    # Existing network-none semantics still refuse ECONNREFUSED for the plain cases.
    with pytest.raises(ValueError, match="native observations"):
        validate_native_pair(
            "cni",
            native(
                "egress", allow_ok, {"connected": False, "errno": "ECONNREFUSED", "elapsed_ms": 10}
            ),
            plain,
        )


def test_verifier_rejects_egress_target_witness_without_new_cases(tmp_path):
    from test_agent_sandbox import setup
    from test_agent_sandbox_repairs import operator_config

    from tessera_control.agent_sandbox_configuration import (
        SandboxOperatorConfig,
        SignedSandboxWitnessVerifier,
    )

    f = setup(tmp_path)
    _, _, value = operator_config(tmp_path, f)
    plain = SandboxOperatorConfig.model_validate(value)
    # The measured four-kind witness covers the plain cases only. It still verifies
    # for the plain target and does not for the same target pinned to a proxy image.
    SignedSandboxWitnessVerifier(plain)(plain.target)
    pinned_target = plain.target.model_copy(update={"egress_proxy_image": PROXY_IMAGE})
    with pytest.raises(ValueError):
        SignedSandboxWitnessVerifier(plain.model_copy(update={"target": pinned_target}))(
            pinned_target
        )


def test_agent_credential_cannot_use_new_sandbox_authority(prepared):  # noqa: F811
    _, client, owner, agent, project, _, _ = prepared
    headers = {**agent, "Idempotency-Key": "agent-attempt"}
    for path in (
        "/worker/agent-sandboxes/anything/egress-journal",
        "/worker/agent-sandbox/leases/anything",
    ):
        response = client.post(path, headers=headers, json={"expected_epoch": 1})
        assert response.status_code in {401, 403}, (path, response.text)
    response = client.post(
        "/agent-sandboxes/anything/cleanup",
        headers=headers,
        json={"manifest_id": "m", "manifest_digest": "a" * 64},
    )
    assert response.status_code == 403
