"""ADR 0036 phase 1: reviewed generic workload_v1 contract (synthetic, no network)."""

import copy
import datetime
import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError
from test_salad_http import selection
from test_salad_live_setup import inputs, private_file, setup_script  # noqa: F401
from test_salad_safety_service import Provider, make_service

from tessera_control.models import ExecutionSpec
from tessera_control.remote_execution_models import (
    RemoteProposal,
    SaladExecution,
    derived_salad_argv,
    route_window_limit,
)
from tessera_control.salad_safety_service import SaladSafetyService
from tessera_control.salad_workload_contract import (
    WorkloadContractV1,
    capability_network_matches,
)
from tessera_control.store import canonical_digest

OPERATION = "a" * 32


def contract(**over):
    value = dict(
        contract="tessera.salad.workload.v1",
        argv_template=["train", "--op", "{operation}", "--epoch", "{epoch}", "{custody_host}"],
        env={"OMP_NUM_THREADS": "4", "MODEL_REVISION": "abc123"},
        workload_seconds=20,
        outputs=[dict(name="output.json", max_bytes=4096)],
        egress=dict(
            kind="provider_broad",
            fetches=[
                dict(url="https://models.example.com/w.bin", sha256="c" * 64, max_bytes=1000),
                dict(url="https://models.example.com/x.bin", sha256="d" * 64, max_bytes=10),
                dict(url="https://wheels.example.org:8443/y.whl", sha256="e" * 64, max_bytes=5),
            ],
        ),
        custody_host="localhost",
    )
    value.update(over)
    return value


def execution(work=None, **over):
    work = contract() if work is None else work
    model = WorkloadContractV1.model_validate(work)
    value = dict(
        platform="linux/amd64",
        command="/tessera/bin/run",
        argv=model.derived_argv(OPERATION, 1)[1:],
        argv_binding="workload_v1",
        workload=work,
        env=work["env"],
        max_output_bytes=model.output_bytes,
        max_output_files=len(model.outputs),
        capability_digest="a" * 64,
        network_policy="provider_broad_egress_explicitly_accepted",
        data_policy="public_released_only",
        artifact_destination="artifact-ref:output",
        checkpoint_destination="checkpoint-ref:output",
        monitor_profile="a" * 64,
        watchdog_profile="a" * 64,
        max_cost_cents=10,
        credit_exposure="unknown_blocks_new_work_credit_not_hard_cap",
    )
    value.update(over)
    return value


# --- model validation -------------------------------------------------------


def test_valid_contract_round_trips_and_profile_requires_it_iff_workload_v1():
    SaladExecution.model_validate(execution())
    smoke = execution()
    smoke["argv_binding"] = "exact"
    with pytest.raises(ValidationError):  # workload forbidden for other bindings
        SaladExecution.model_validate(smoke)
    missing = execution()
    del missing["workload"]
    with pytest.raises(ValidationError):  # required for workload_v1
        SaladExecution.model_validate(missing)
    exact = execution()
    del exact["workload"]
    exact.update(argv_binding="exact", argv=["gpu"], env={})
    assert "workload" not in SaladExecution.model_validate(exact).model_dump(mode="json")


def test_legacy_argv_shape_still_strict_for_non_workload_bindings():
    bad = execution(argv=["a b"])
    del bad["workload"]
    bad.update(argv_binding="exact", env={})
    with pytest.raises(ValidationError):
        SaladExecution.model_validate(bad)
    bad = execution(argv=["x"], env={"MODEL_REVISION": "1"})
    del bad["workload"]
    bad["argv_binding"] = "exact"
    with pytest.raises(ValidationError):
        SaladExecution.model_validate(bad)


@pytest.mark.parametrize(
    "token", ["{other}", "--x={operation}", "{operation}{epoch}", "a{b", "}", "{{epoch}}"]
)
def test_only_exact_placeholders_allowed(token):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(argv_template=["run", token]))


@pytest.mark.parametrize("template", [[], ["x"] * 33, [""], ["x" * 257], ["bad\ttab"], ["é"]])
def test_argv_template_bounds_and_printable(template):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(argv_template=template))


@pytest.mark.parametrize(
    "env",
    [
        {"lower": "1"},
        {"1BAD": "1"},
        {"A" * 65: "1"},
        {"API_TOKEN": "x"},
        {"MY_SECRET": "x"},
        {"DB_PASSWORD": "x"},
        {"CLOUD_CREDENTIAL": "x"},
        {"SERVICE_KEY": "x"},
        {"OK": "x" * 513},
        {"OK": "bad\nvalue"},
        {f"V{i}": "1" for i in range(33)},
    ],
)
def test_env_rules_refuse_secret_like_and_malformed(env):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(env=env))


def test_env_allows_reviewed_non_secret_and_empty_value():
    env = {"HF_HUB_OFFLINE": "1", "EMPTY": "", "REV": "a" * 64}
    WorkloadContractV1.model_validate(contract(env=env))
    # Case-sensitive: the suffix rule is for upper-case names only, names must be upper anyway.
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(env={"api_key": "1"}))


def test_profile_env_must_equal_contract_env_and_limits_match_outputs():
    with pytest.raises(ValidationError):
        SaladExecution.model_validate(execution(env={"OMP_NUM_THREADS": "1"}))
    with pytest.raises(ValidationError):
        SaladExecution.model_validate(execution(max_output_bytes=1))
    with pytest.raises(ValidationError):
        SaladExecution.model_validate(execution(max_output_files=2))


@pytest.mark.parametrize(
    "outputs",
    [
        [],
        [dict(name="a", max_bytes=1)] * 2,
        [dict(name=f"f{i}", max_bytes=1) for i in range(17)],
        [dict(name="A.json", max_bytes=1)],
        [dict(name=".hidden", max_bytes=1)],
        [dict(name="a/b", max_bytes=1)],
        [dict(name="a", max_bytes=0)],
        [dict(name="a", max_bytes=67108865)],
        [dict(name="x" * 65, max_bytes=1)],
    ],
)
def test_output_bounds_and_uniqueness(outputs):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(outputs=outputs))


def test_multi_file_outputs_are_valid_models():
    WorkloadContractV1.model_validate(
        contract(
            outputs=[
                dict(name="output.json", max_bytes=1),
                dict(name="weights.safetensors", max_bytes=67108864),
            ]
        )
    )


@pytest.mark.parametrize(
    "fetch",
    [
        dict(url="http://models.example.com/w", sha256="c" * 64, max_bytes=1),
        dict(url="https://user:pw@models.example.com/w", sha256="c" * 64, max_bytes=1),
        dict(url="https://models.example.com/w#frag", sha256="c" * 64, max_bytes=1),
        dict(url="https://Models.Example.com/w", sha256="c" * 64, max_bytes=1),
        dict(url="https://localhost/w", sha256="c" * 64, max_bytes=1),
        dict(url="https://10.0.0.1/w", sha256="c" * 64, max_bytes=1),
        dict(url="https://m.example.com/" + "a" * 512, sha256="c" * 64, max_bytes=1),
        dict(url="https://m.example.com/w", sha256="C" * 64, max_bytes=1),
        dict(url="https://m.example.com/w", sha256="c" * 63, max_bytes=1),
        dict(url="https://m.example.com/w", sha256="c" * 64, max_bytes=0),
        dict(url="https://m.example.com/w", sha256="c" * 64, max_bytes=8589934593),
    ],
)
def test_egress_fetch_checks(fetch):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(
            contract(egress=dict(kind="provider_broad", fetches=[fetch]))
        )


def test_egress_kind_and_count_bounds():
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(egress=dict(kind="none", fetches=[])))
    WorkloadContractV1.model_validate(contract(egress=dict(kind="provider_broad", fetches=[])))
    many = [
        dict(url=f"https://h.example.com/{i}", sha256="c" * 64, max_bytes=1) for i in range(129)
    ]
    # 128 declared fetches (about 45 wheels plus model files with headroom) are allowed.
    WorkloadContractV1.model_validate(
        contract(egress=dict(kind="provider_broad", fetches=many[:128]))
    )
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(
            contract(egress=dict(kind="provider_broad", fetches=many))
        )


@pytest.mark.parametrize("seconds", [0, 3601])
def test_workload_seconds_bounds(seconds):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(workload_seconds=seconds))


@pytest.mark.parametrize("host", ["", "UPPER.example", "a..b", "-a", "a b", "x" * 65, "a/b"])
def test_custody_host_exact(host):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(custody_host=host))


def test_extra_fields_forbidden_everywhere():
    for bad in (
        contract(extra=1),
        contract(outputs=[dict(name="a", max_bytes=1, extra=1)]),
        contract(egress=dict(kind="provider_broad", fetches=[], extra=1)),
        contract(
            egress=dict(
                kind="provider_broad",
                fetches=[dict(url="https://a.example.com/", sha256="c" * 64, max_bytes=1, x=1)],
            )
        ),
    ):
        with pytest.raises(ValidationError):
            WorkloadContractV1.model_validate(bad)


# --- argv derivation ---------------------------------------------------------


def test_derived_argv_substitutes_only_the_three_placeholders():
    work = contract(argv_template=["{operation}", "{epoch}", "{custody_host}", "literal", "-x"])
    model = WorkloadContractV1.model_validate(work)
    assert model.derived_argv(OPERATION, 7) == [
        "/tessera/bin/run",
        OPERATION,
        "7",
        "localhost",
        "literal",
        "-x",
    ]
    profile = execution(work)
    assert (
        derived_salad_argv(profile, ["/tessera/bin/run", *work["argv_template"]], OPERATION, 1, 100)
        == model.derived_argv(OPERATION, 1)[1:]
    )


def test_derived_argv_rejects_drift_and_bad_identity():
    profile = execution()
    approved = ["/tessera/bin/run", *contract()["argv_template"]]
    ok = derived_salad_argv(profile, approved, OPERATION, 1, 100)
    assert ok == profile["argv"]
    for args in (
        (profile, [*approved[:-1], "other"], OPERATION, 1, 100),
        (profile, approved, "b" * 32, 1, 100),  # profile argv bound another operation
        (profile, approved, "short", 1, 100),
        (profile, approved, OPERATION, 0, 100),
        (profile, approved, OPERATION, 1, 19),  # workload_seconds > route window
    ):
        with pytest.raises(ValueError):
            derived_salad_argv(*args)


# --- capability network ------------------------------------------------------


def test_capability_network_must_equal_fetch_destinations():
    profile = execution()
    expected = [
        dict(host="models.example.com", port=443),
        dict(host="wheels.example.org", port=8443),
    ]
    assert WorkloadContractV1.model_validate(contract()).capability_network() == expected
    assert capability_network_matches(profile, dict(network=expected))
    assert capability_network_matches(profile, dict(network=list(reversed(expected))))
    assert not capability_network_matches(profile, dict(network=[]))
    assert not capability_network_matches(profile, dict(network=expected[:1]))
    assert not capability_network_matches(
        profile, dict(network=[*expected, dict(host="extra.example.com", port=443)])
    )
    assert not capability_network_matches(
        profile, dict(network=[expected[0], dict(host="wheels.example.org", port=443)])
    )


def test_smoke_profiles_keep_empty_network_requirement():
    exact = dict(argv_binding="exact")
    assert capability_network_matches(exact, dict(network=[]))
    assert not capability_network_matches(exact, dict(network=[dict(host="a.b.com", port=443)]))
    assert capability_network_matches({}, dict(network=[]))  # legacy shape without a binding


def test_no_egress_contract_requires_empty_network():
    work = contract(egress=dict(kind="provider_broad", fetches=[]))
    assert capability_network_matches(execution(work), dict(network=[]))
    assert not capability_network_matches(
        execution(work), dict(network=[dict(host="a.example.com", port=443)])
    )


# --- safety service ----------------------------------------------------------


def workload_reservation(work=None, seconds=300, now=100, operation=OPERATION, output_paths=None):
    work = contract() if work is None else work
    value = selection().model_dump(mode="json")
    value["resources"]["max_seconds"] = seconds
    value["salad"]["execution"] = execution(work)
    model = WorkloadContractV1.model_validate(work)
    plan = dict(
        budget_cents=10,
        max_duration_seconds=seconds,
        execution_spec=dict(
            argv=["/tessera/bin/run", *work["argv_template"]],
            max_output_bytes=model.output_bytes,
            input_snapshot_digest=value["manifests"]["data"],
            pinned_skill_digests=[],
            output_paths=output_paths or [o["name"] for o in work["outputs"]],
            profile="kubernetes-opencode",
            network_enabled=False,
        ),
    )
    plan["execution_spec"] = ExecutionSpec.model_validate(plan["execution_spec"]).model_dump(
        mode="json"
    )
    runtime = dict(
        operation_id=operation,
        epoch=1,
        lease_id="lease",
        org_id="tenant",
        project_id="project",
        task_id="task",
        plan=plan,
        plan_digest=canonical_digest(plan),
        pinned_image=value["manifests"]["image"],
        snapshot_digest=value["manifests"]["data"],
        hard_expires_at=datetime.datetime.fromtimestamp(now + 3600, datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    )
    p = RemoteProposal.model_validate(
        dict(
            operation_id=operation,
            package_digest=canonical_digest(runtime),
            attempt_id="attempt",
            plan_digest=canonical_digest(plan),
            lease_id="lease",
            epoch=1,
            selection=value,
            study_id="study",
            program_review_id="review",
            source_review_id="source",
        )
    )
    proposal = p.model_dump(mode="json")
    package = dict(
        version=1,
        org_id="tenant",
        project_id="project",
        task_id="task",
        proposal=proposal,
        mode="offline",
        capability_expansion=False,
    )
    digest = canonical_digest(package)
    return dict(
        operation="reserve",
        reservation=canonical_digest(["route", digest]),
        route_id="route",
        org_id="tenant",
        project_id="project",
        proposal=proposal,
        routing_package=package,
        runtime_package=runtime,
        budget_binding=dict(
            experiment_id="experiment",
            admitted_attempt_id="attempt",
            grant_id="grant",
            authorization_revision=1,
            reservation_digest=canonical_digest(["experiment", "attempt", "grant", 1, 10]),
            operation_id=operation,
            lease_id="lease",
            epoch=1,
            package_digest=canonical_digest(runtime),
            max_exposure_cents=10,
            expires_at=runtime["hard_expires_at"],
        ),
        deadline=now + seconds,
        route_digest=digest,
    )


def proposal_of(body):
    return RemoteProposal.model_validate(body["proposal"])


def test_request_derivation_for_workload_v1_and_smoke_dispatch_unchanged():
    body = workload_reservation()
    assert SaladSafetyService._request_for(proposal_of(body)) == dict(
        operation=OPERATION,
        epoch="1",
        destination="localhost",
        watchdog="a" * 64,
        max_seconds=20,
        max_output_bytes=4096,
        max_output_files=1,
    )
    smoke = selection().model_dump(mode="json")
    smoke["salad"]["execution"]["argv"] = [
        "v1", "public-integers-v1", OPERATION, "1", "localhost", "a" * 64, "5", "1024", "1", "4",
    ]  # fmt: skip
    smoke_body = copy.deepcopy(body)
    smoke_body["proposal"]["selection"] = smoke
    assert SaladSafetyService._request_for(proposal_of(smoke_body))["dimension"] == 4


def test_reserve_admits_single_output_json_and_binds_workload_window(tmp_path):
    tmp_path.chmod(0o700)
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        s.loop()
        body = workload_reservation()
        assert s.admin(body)["state"] == "ready"
        # workload_seconds (20) + 120 upload grace, below the 300 s route window.
        assert s._row(body["reservation"])["running_seconds"] == 140
    finally:
        s.close()


def test_running_window_is_capped_by_route_window(tmp_path):
    tmp_path.chmod(0o700)
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        s.loop()
        body = workload_reservation(contract(workload_seconds=60), seconds=100)
        s.admin(body)
        assert s._row(body["reservation"])["running_seconds"] == 100  # min(180, 100)
    finally:
        s.close()


@pytest.mark.parametrize(
    "outputs",
    [
        [dict(name="output.json", max_bytes=4096), dict(name="extra.bin", max_bytes=10)],
        [dict(name="weights.bin", max_bytes=10)],
        [dict(name="output.json", max_bytes=65537)],
        [dict(name=f"f{i}.bin", max_bytes=1024 * 1024) for i in range(16)],
    ],
)
def test_reserve_admits_declared_outputs_and_binds_them_in_custody(tmp_path, outputs):
    tmp_path.chmod(0o700)
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        s.loop()
        body = workload_reservation(contract(outputs=outputs))
        assert s.admin(body)["state"] == "ready"
        row = s.custody.db.execute(
            "SELECT byte_cap,file_cap,expected_files,outputs FROM inventories"
        ).fetchone()
        assert row[:3] == (sum(o["max_bytes"] for o in outputs), len(outputs), len(outputs))
        assert dict(json.loads(row[3])) == {o["name"]: o["max_bytes"] for o in outputs}
    finally:
        s.close()


def test_reserve_refuses_output_paths_that_differ_from_declared_outputs(tmp_path):
    tmp_path.chmod(0o700)
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        s.loop()
        work = contract(
            outputs=[dict(name="a.bin", max_bytes=10), dict(name="b.bin", max_bytes=10)]
        )
        body = workload_reservation(work, output_paths=["b.bin", "a.bin"])
        with pytest.raises(ValueError, match="approved_spec_mismatch"):
            s.admin(body)
        assert not s.db.execute("SELECT 1 FROM reservations").fetchone()
    finally:
        s.close()


def test_outputs_supported_bounds_the_custody_total():
    many = [dict(name=f"f{i}.bin", max_bytes=2 * 1024 * 1024) for i in range(8)]
    assert WorkloadContractV1.model_validate(contract(outputs=many)).outputs_supported()
    many.append(dict(name="extra.bin", max_bytes=1))
    work = WorkloadContractV1.model_validate(contract(outputs=many))
    assert not work.outputs_supported()
    assert not work.legacy_single_output()
    assert WorkloadContractV1.model_validate(contract()).legacy_single_output()
    big = contract(outputs=[dict(name="output.json", max_bytes=65537)])
    assert not WorkloadContractV1.model_validate(big).legacy_single_output()


def test_reserve_refuses_custody_host_other_than_service_destination(tmp_path):
    tmp_path.chmod(0o700)
    provider = Provider()
    s = make_service(tmp_path, lambda: 100, ec.generate_private_key(ec.SECP256R1()), provider)
    try:
        s.loop()
        with pytest.raises(ValueError):
            s.admin(workload_reservation(contract(custody_host="other.example.com")))
    finally:
        s.close()


# --- setup script ------------------------------------------------------------


def test_setup_builds_workload_v1_route_from_contract_file(inputs, capsys):  # noqa: F811
    work = contract(
        custody_host="custody.example-tailnet.ts.net",
        workload_seconds=900,
        egress=dict(
            kind="provider_broad",
            fetches=[dict(url="https://models.example.com/w.bin", sha256="c" * 64, max_bytes=9)],
        ),
    )
    path = private_file(inputs["root"] / "contract.json", json.dumps(work).encode())
    inputs["argv"] += ["--workload-contract", str(path), "--max-seconds", "7200"]
    inputs["argv"] += ["--max-polls", "720"]
    assert setup_script.main(inputs["argv"]) == 0, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    config = json.loads(Path(summary["config_file"]).read_text())
    execution_ = config["policy"]["target"]["selection"]["salad"]["execution"]
    assert execution_["argv_binding"] == "workload_v1"
    assert execution_["workload"] == WorkloadContractV1.model_validate(work).model_dump(mode="json")
    assert execution_["argv"][0] == "train"
    assert execution_["env"] == work["env"]
    declaration = next(
        r["value"] for r in summary["review_required"] if r["field"] == "capability.declaration"
    )
    assert declaration["network"] == [dict(host="models.example.com", port=443)]


def test_setup_contract_requires_matching_custody_host_and_forbids_workload_seconds(
    inputs,  # noqa: F811
    capsys,
):
    work = contract(custody_host="other.example-tailnet.ts.net")
    path = private_file(inputs["root"] / "contract.json", json.dumps(work).encode())
    bad = inputs["argv"] + ["--workload-contract", str(path)]
    assert setup_script.main(bad) == 1
    assert "custody_host" in capsys.readouterr().err
    work = contract(custody_host="custody.example-tailnet.ts.net")
    path.write_bytes(json.dumps(work).encode())
    bad = inputs["argv"] + ["--workload-contract", str(path), "--workload-seconds", "10"]
    assert setup_script.main(bad) == 1
    assert "--workload-seconds" in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_setup_rejects_contract_with_loose_permissions(inputs, capsys):  # noqa: F811
    work = contract(custody_host="custody.example-tailnet.ts.net")
    path = private_file(inputs["root"] / "contract.json", json.dumps(work).encode())
    path.chmod(0o644)
    assert setup_script.main(inputs["argv"] + ["--workload-contract", str(path)]) == 1
    assert "--workload-contract" in capsys.readouterr().err
    assert not inputs["output"].exists()


@pytest.mark.parametrize(
    "env",
    [
        {"LD_PRELOAD": "x"}, {"LD_LIBRARY_PATH": "x"}, {"PATH": "/x"}, {"PYTHONPATH": "/x"},
        {"PYTHONHOME": "/x"}, {"PYTHONSTARTUP": "/x"}, {"BASH_ENV": "/x"}, {"ENV": "/x"},
        {"HF_AUTH": "1"}, {"DB_PASS": "1"}, {"DB_PASSWD": "1"}, {"PRIVATE_THING": "1"},
        {"API_KEY": "1"}, {"KEYBOARD": "1"}, {"AWS_REGION": "us"}, {"AZURE_X": "1"},
        {"GOOGLE_APPLICATION_CREDENTIALS": "/x"},
        {"A": "-----BEGIN PRIVATE KEY-----"}, {"A": "sk-abc"}, {"A": "hf_abc"},
        {"A": "ghp_abc"}, {"A": "AKIAABCDEFG"},
        {"A": "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"},
    ],
)  # fmt: skip
def test_env_refusal_classes(env):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(contract(env=env))


def test_env_allows_pythonunbuffered_and_hex_digests():
    WorkloadContractV1.model_validate(
        contract(env={"PYTHONUNBUFFERED": "1", "REV": "0123456789abcdef" * 4})
    )


def test_salad_route_window_exceeds_plan_only_by_provisioning_allowance():
    assert route_window_limit("salad_container", 3600) == 7200
    assert route_window_limit("kubernetes_job", 3600) == 3600


def test_kubernetes_plan_over_3600_refused_at_packaging(inputs, capsys, monkeypatch):  # noqa: F811
    original = setup_script.Control.http

    def widened(self, step, method, path, body=None, **kw):
        if step in {"task", "task-plan"}:
            body = dict(body, max_duration_seconds=3601)
        return original(self, step, method, path, body, **kw)

    monkeypatch.setattr(setup_script.Control, "http", widened)
    assert setup_script.main(inputs["argv"]) == 1
    assert "adapter allowlist" in capsys.readouterr().err


def test_salad_7200_route_builds_with_3600_plan(inputs, capsys):  # noqa: F811
    inputs["argv"] += ["--max-seconds", "7200", "--max-polls", "720"]
    assert setup_script.main(inputs["argv"]) == 0, capsys.readouterr().err
    config = json.loads(Path(json.loads(capsys.readouterr().out)["config_file"]).read_text())
    selection_ = config["policy"]["target"]["selection"]
    assert selection_["resources"]["max_seconds"] == 7200


def test_setup_refuses_unsupported_outputs_before_any_write(inputs, capsys):  # noqa: F811
    work = contract(
        custody_host="custody.example-tailnet.ts.net",
        outputs=[dict(name="a.bin", max_bytes=16 * 1024 * 1024), dict(name="b.bin", max_bytes=1)],
    )
    path = private_file(inputs["root"] / "contract.json", json.dumps(work).encode())
    assert setup_script.main(inputs["argv"] + ["--workload-contract", str(path)]) == 1
    assert "outputs_unsupported" in capsys.readouterr().err
    assert not inputs["output"].exists()
