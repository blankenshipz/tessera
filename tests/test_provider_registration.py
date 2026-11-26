"""Validated provider registration (ADR 0081): core operations and the `tessera providers` command.

Offline, temporary 0600 files only; no provider is registered in the process and nothing
contacts a network."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from provider_trust_fixture import build_policy

from tessera_control import provider_registration as pr
from tessera_control.cli import parser
from tessera_control.provider_registration_cli import run

GPU = "550e8400-e29b-41d4-a716-446655440000"
DIGEST = "sha256:" + "1" * 64
RATE = {"vcpu_hour_usd": "0.04", "memory_gb_hour_usd": "0.01"}
WORKER = "worker-salad-1"


def profile(name="cpu-main", gpu=False, **over):
    value = dict(
        name=name,
        org_id="org-1",
        project_id="project-1",
        worker_principal_id=WORKER,
        salad_organization="example-org",
        salad_project="tessera-research",
        country="US",
        cpu_millicores=2000,
        memory_mb=2048,
        disk_mb=1024,
        route_max_seconds=3600,
        capability_environment_id="salad-campaign",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        custody_host="custody.example-tailnet.ts.net",
        credential_reference="credential-ref:salad-management",
        credential_version=1,
        allowed_image_digests=[DIGEST],
        max_run_cents=50,
        max_task_cents=100,
    )
    value.update(gpu_class=GPU) if gpu else value.update(cpu_rate=RATE)
    value.update(over)
    return value


@pytest.fixture
def target(tmp_path):
    return tmp_path / "providers.json"


def add(target, entry, section="salad-profile", **kw):
    return pr.apply_change(target, "add", section, None, entry, actor="test", **kw)


def refused(match, *args, **kw):
    with pytest.raises(pr.RegistrationRefused, match=match):
        pr.apply_change(*args, actor="test", **kw)


# -- core ----------------------------------------------------------------------------------------


def test_add_cpu_and_gpu_profiles_creates_an_owner_only_file_and_lists_them(target):
    first = add(target, profile())
    add(target, profile("gpu-main", gpu=True))
    assert first["written"] and stat.S_IMODE(os.stat(target).st_mode) == 0o600
    document, digest, exists = pr.read_document(target)
    assert exists and digest == pr.digest_of(document)
    rows = {r["key"]: r for r in pr.list_document(document)}
    assert (
        rows["cpu-main"]["resource_class"] == "cpu" and rows["gpu-main"]["resource_class"] == "gpu"
    )
    assert rows["cpu-main"]["allowed_image_digests"] == [DIGEST]
    assert rows["cpu-main"]["max_run_cents"] == 50 and rows["gpu-main"]["max_task_cents"] == 100
    assert rows["cpu-main"]["credential_reference"] == "credential-ref:salad-management"


def test_update_changes_only_the_named_fields_and_refuses_a_rename(target):
    add(target, profile())
    result = pr.apply_change(
        target, "update", "salad-profile", "cpu-main", {"max_run_cents": 40}, actor="t"
    )
    assert result["changed_fields"] == ["max_run_cents"]
    (entry,) = pr.read_document(target)[0]["salad_profiles"]["profiles"]
    assert entry["max_run_cents"] == 40 and entry["memory_mb"] == 2048
    refused("cannot be changed", target, "update", "salad-profile", "cpu-main", {"name": "other"})


@pytest.mark.parametrize(
    ("section", "entry", "match"),
    [
        ("nope", {}, "Unknown section"),
        ("salad-profile", profile(allowed_image_digests=["latest"]), "allowed_image_digests"),
        ("salad-profile", profile(allowed_image_digests=["sha256:abc"]), "allowed_image_digests"),
        ("salad-profile", profile(max_run_cents=999999), "max_run_cents"),
        ("salad-profile", profile(extra_field=1), "extra_field"),
        ("salad-profile", profile(api_token="x"), "secret-like field name"),
        (
            "salad-profile",
            profile(salad_project="Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo"),
            "high-entropy",
        ),
        (
            "salad-profile",
            profile(credential_reference="plain-secret-value"),
            "credential_reference",
        ),
        ("salad-profile", profile(custody_host="evil.example.com"), "custody_host"),
        ("plugin", {}, "Plugin key|plugin's name"),
    ],
)
def test_refusals_leave_the_file_untouched(target, section, entry, match):
    add(target, profile("keep"))
    before = target.read_bytes()
    refused(match, target, "add", section, None, entry)
    assert target.read_bytes() == before


def test_a_credential_reference_is_accepted_and_a_secret_name_word_is_not_a_false_positive(target):
    add(target, profile(credential_reference="credential-ref:other"))  # reference keys pass
    assert pr.secret_findings({"quoted_passage": "text", "credential_reference": "x"}) == []


def test_unknown_plugin_duplicate_add_and_absent_update(target):
    refused("not an installed entry point", target, "add", "plugin", "no_such_vendor", {"a": 1})
    add(target, profile())
    refused("already exists", target, "add", "salad-profile", None, profile(max_run_cents=7))
    refused("does not exist", target, "update", "salad-profile", "ghost", {"max_run_cents": 1})


def test_a_stale_expected_digest_is_refused(target):
    add(target, profile())
    refused(
        "changed since it was read", target, "add", "salad-profile", None, profile("b"),
        expected_digest="0" * 64,
    )  # fmt: skip
    digest = pr.read_document(target)[1]
    assert add(target, profile("b"), expected_digest=digest)["written"]


def test_dry_run_writes_nothing_and_a_missing_file_stays_missing(target):
    result = add(target, profile(), dry_run=True)
    assert not result["written"] and not target.exists()
    assert not Path(str(target) + pr.AUDIT_SUFFIX).exists()


def test_a_non_owner_only_file_is_not_edited(target):
    add(target, profile())
    os.chmod(target, 0o644)
    with pytest.raises(ValueError):
        add(target, profile("b"))


def test_audit_chain_links_and_detects_tampering(target):
    add(target, profile())
    add(target, profile("b"))
    pr.apply_change(target, "update", "salad-profile", "b", {"max_run_cents": 10}, actor="t")
    records = pr.read_audit(target)
    assert len(records) == 3 and records[0]["previous_hash"] == "0" * 64
    for before, after in zip(records, records[1:], strict=False):
        assert after["previous_hash"] == before["hash"]
    for record in records:
        body = {k: v for k, v in record.items() if k != "hash"}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
        assert (
            record["hash"] == hashlib.sha256((record["previous_hash"] + raw).encode()).hexdigest()
        )
    forged = {**records[1], "actor": "someone-else"}
    body = {k: v for k, v in forged.items() if k != "hash"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert forged["hash"] != hashlib.sha256((forged["previous_hash"] + raw).encode()).hexdigest()
    assert stat.S_IMODE(os.stat(str(target) + pr.AUDIT_SUFFIX).st_mode) == 0o600


def test_trust_readout_names_the_exact_entry_when_unlisted_and_admits_when_listed(target):
    unlisted = build_policy({"local-docker": (["public"], ["someone"])})
    result = add(target, profile(), policy=unlisted, dry_run=True)
    trust = result["trust"]
    assert trust["checked"] and "private_derived" not in trust["admitted_classes"]
    assert (
        "merge" in trust["hint"]
        and WORKER in trust["hint"]
        and "all_registered" not in trust["hint"]
    )
    listed = build_policy({"salad": (["public", "private_derived"], [WORKER])})
    trust = add(target, profile(), policy=listed, dry_run=True)["trust"]
    assert "private_derived" in trust["admitted_classes"] and "hint" not in trust
    assert add(target, profile("c"), dry_run=True)["trust"]["checked"] is False


def test_self_hosted_add_and_reserved_ids(target):
    entry = {"id": "far-cpu", "worker_principal_id": "worker-far"}
    add(target, entry, section="self-hosted")
    refused("already exists", target, "add", "self-hosted", None, {**entry, "worker_slots": 2})
    for reserved in ("salad", "local-docker"):
        refused("reserved|unique", target, "add", "self-hosted", None, {**entry, "id": reserved})


def test_local_docker_singleton_add_then_update(target):
    add(target, {"worker_principal_id": "worker-local"}, section="local-docker")
    refused("already configured", target, "add", "local-docker", None, {"worker_principal_id": "x"})
    result = pr.apply_change(target, "update", "local-docker", None, {"worker_slots": 2}, actor="t")
    assert result["changed_fields"] == ["worker_slots"]
    assert pr.read_document(target)[0]["local_docker"]["worker_slots"] == 2


def test_a_written_file_still_loads_through_the_serve_loader_and_provider_validation(target):
    from tessera_control.campaign_providers import load_providers_file
    from tessera_control.salad_campaign_profiles import SaladCampaignProfiles

    add(target, profile())
    add(target, {"id": "far-cpu", "worker_principal_id": "w"}, section="self-hosted")
    document = load_providers_file(str(target))
    SaladCampaignProfiles.model_validate(document["salad_profiles"])
    assert document["self_hosted"][0]["id"] == "far-cpu"


# -- command ---------------------------------------------------------------------------------------


def cli(*argv):
    return run(parser().parse_args(["providers", *argv]))


def test_cli_add_list_update_audit_round_trip(target, capsys):
    entry = json.dumps(profile())
    assert (
        cli("add", "--file", str(target), "--section", "salad-profile", "--entry-json", entry) == 0
    )
    assert json.loads(capsys.readouterr().out)["written"] is True
    assert cli("list", "--file", str(target)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["exists"] and listed["entries"][0]["key"] == "cpu-main"
    assert "salad-profile" in listed["sections"] and "plugin" in listed["sections"]
    args = ["update", "--file", str(target), "--section", "salad-profile", "--key", "cpu-main"]
    assert (
        cli(*args, "--entry-json", '{"max_run_cents": 7}', "--expected-digest", listed["digest"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["changed_fields"] == ["max_run_cents"]
    assert cli("audit", "--file", str(target)) == 0
    assert [r["action"] for r in json.loads(capsys.readouterr().out)["records"]] == [
        "add",
        "update",
    ]


def test_cli_entry_file_trust_readout_and_dry_run(target, tmp_path, capsys):
    entry_file = tmp_path / "entry.json"
    entry_file.write_text(json.dumps(profile()))
    trust = tmp_path / "trust.json"
    trust.write_text(
        json.dumps(
            {
                "schema": "tessera.provider-trust.v1",
                "providers": {
                    "salad": {"data_classes": ["public"], "worker_principal_ids": [WORKER]}
                },
            }
        )
    )
    os.chmod(trust, 0o600)
    code = cli(
        "add", "--file", str(target), "--section", "salad-profile",
        "--entry-file", str(entry_file), "--provider-trust", str(trust), "--dry-run",
    )  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and not out["written"] and not target.exists()
    assert out["trust"]["admitted_classes"] == ["public"] and "hint" in out["trust"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--section", "nope", "--entry-json", "{}"],
        ["--section", "salad-profile", "--entry-json", "[1]"],
        ["--section", "salad-profile", "--entry-json", "not json"],
        ["--section", "salad-profile", "--entry-json", '{"api_token": "x"}'],
    ],
)
def test_cli_refusal_prints_json_on_stderr_and_exits_two(target, capsys, extra):
    assert cli("add", "--file", str(target), *extra) == 2
    captured = capsys.readouterr()
    assert "refused" in json.loads(captured.err) and captured.out == ""
    assert not target.exists()


def test_cli_requires_an_absolute_file(capsys):
    assert cli("list", "--file", "relative.json") == 2
    assert "absolute" in json.loads(capsys.readouterr().err)["refused"]


def test_generated_identifiers_are_names_not_secrets():
    from tessera_control.provider_registration import secret_findings

    ids = {"project_id": "project_" + "a1" * 16, "worker_principal_id": "worker-" + "b2" * 16}
    assert secret_findings(ids) == []
    assert secret_findings({"note": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo"})


# -- review follow-ups: path, audit, plugin, secrets, lock, size ---------------------------------


def audit_path(target):
    return Path(str(target) + pr.AUDIT_SUFFIX)


def test_an_identical_re_add_is_a_no_op_that_writes_and_audits_nothing(target):
    add(target, profile())
    before = (target.read_bytes(), audit_path(target).read_bytes())
    again = add(target, profile())
    assert again["written"] is False and again["noop"] is True and again["changed_fields"] == []
    assert (target.read_bytes(), audit_path(target).read_bytes()) == before


def test_update_reports_only_the_fields_that_really_changed(target):
    add(target, profile())
    result = pr.apply_change(
        target, "update", "salad-profile", "cpu-main", {"max_run_cents": 40, "country": "US"},
        actor="t",
    )  # fmt: skip
    assert result["changed_fields"] == ["max_run_cents"]


def test_a_tampered_audit_record_is_detected_and_refuses_the_next_change(target):
    for name in ("a", "b", "c"):
        add(target, profile(name))
    assert pr.verify_audit(target)["ok"] is True
    rows = audit_path(target).read_text().splitlines()
    middle = json.loads(rows[1])
    middle["actor"] = "someone-else"
    rows[1] = json.dumps(middle, sort_keys=True)
    audit_path(target).write_text("\n".join(rows) + "\n")
    state = pr.verify_audit(target)
    assert state["ok"] is False and state["broken_at"] == 1
    before = target.read_bytes()
    refused("audit chain is broken", target, "add", "salad-profile", None, profile("d"))
    assert target.read_bytes() == before


def test_a_corrupt_audit_line_refuses_the_change_and_leaves_the_file_alone(target):
    add(target, profile())
    with open(audit_path(target), "a") as handle:
        handle.write("garbage\n")
    before = target.read_bytes()
    refused("corrupt", target, "add", "salad-profile", None, profile("b"))
    assert target.read_bytes() == before
    with pytest.raises(pr.RegistrationRefused):
        pr.read_audit(target)


def test_an_audit_failure_after_the_write_is_reported_not_raised(target, monkeypatch):
    add(target, profile())

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pr, "append_audit", boom)
    result = add(target, profile("b"))
    assert result["written"] is True and "audit_error" in result and "audit_hash" not in result
    assert [e["key"] for e in pr.list_document(pr.read_document(target)[0])] == ["cpu-main", "b"]


def test_a_symlinked_ancestor_a_relative_path_and_a_dotdot_path_are_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    refused("symlink", tmp_path / "link" / "p.json", "add", "salad-profile", None, profile())
    assert not (real / "p.json").exists()
    with pytest.raises(pr.RegistrationRefused, match="absolute"):
        pr.apply_change(Path("p.json"), "add", "salad-profile", None, profile(), actor="t")
    refused("absolute", real / ".." / "real" / "p.json", "add", "salad-profile", None, profile())


def test_a_group_writable_directory_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o770)
    refused("writable", shared / "p.json", "add", "salad-profile", None, profile())
    assert not (shared / "p.json").exists()


def test_a_failed_replace_leaves_no_temp_file_and_no_providers_file(target, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no space")

    monkeypatch.setattr(pr.os, "replace", boom)
    refused("Cannot write", target, "add", "salad-profile", None, profile())
    assert not target.exists()
    assert [p.name for p in target.parent.iterdir() if p.name.startswith("providers.json.")] == [
        "providers.json.lock"
    ]


@pytest.mark.parametrize(
    "value",
    [
        {"clientSecret": "x"},
        {"accessKey": "x"},
        {"passphrase": "x"},
        {"note": "https://user:hunter2@host.example/x"},
        {"note": "Bearer abcdefghij1234"},
        {"note": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"},
        {"nested": [{"api_token": "x"}]},
    ],
)
def test_secret_shaped_names_and_values_are_found(value):
    assert pr.secret_findings(value)


@pytest.mark.parametrize(
    "value",
    [
        {"name": "my-quite-long-descriptive-profile-name-for-gpu-run-v2"},
        {"token_budget": 5, "max_tokens": 9},
        {"project_id": "project_" + "a1" * 16, "worker_principal_id": "worker-" + "b2" * 16},
        {"credential_reference": "credential-ref:x", "credential_version": 1},
        {"quoted_passage": "text", "allowed_image_digests": [DIGEST]},
    ],
)
def test_legitimate_names_and_values_are_not_secrets(value):
    assert pr.secret_findings(value) == []


def test_a_secret_shaped_key_is_refused(target):
    refused("plain name", target, "update", "salad-profile", "bad key!", {"max_run_cents": 1})


def test_a_user_supplied_field_name_is_not_echoed_in_a_refusal(target):
    with pytest.raises(pr.RegistrationRefused) as error:
        add(target, profile(**{"x" * 80: 1}))
    assert "x" * 80 not in str(error.value)


def test_a_module_without_register_from_config_is_not_a_plugin(target):
    refused("register_from_config", target, "add", "plugin", "json", {}, allow_module=True)
    assert not target.exists()


def test_the_api_path_does_not_import_a_bare_module(target):
    refused("not an installed entry point", target, "add", "plugin", "this_is_not_imported", {})


def test_an_already_imported_plugin_with_hooks_is_accepted_and_its_check_refuses(
    target, monkeypatch
):
    import sys
    import types

    seen = []
    module = types.ModuleType("fake_vendor_x")
    module.register_from_config = lambda section: None

    def check(section):
        seen.append(section)
        if section.get("bad"):
            raise ValueError("bad setting")

    module.validate_config = check
    monkeypatch.setitem(sys.modules, "fake_vendor_x", module)
    result = pr.apply_change(
        target, "add", "plugin", "fake_vendor_x", {"region": "r1"}, actor="t", allow_module=False
    )
    assert result["written"] and seen == [{"region": "r1"}]
    assert pr.read_document(target)[0]["plugins"]["fake_vendor_x"] == {"region": "r1"}
    refused("bad setting", target, "update", "plugin", "fake_vendor_x", {"bad": True})
    assert pr.read_document(target)[0]["plugins"]["fake_vendor_x"] == {"region": "r1"}


def test_a_document_over_the_serve_size_limit_is_refused(target, monkeypatch):
    add(target, profile())
    before = target.read_bytes()
    monkeypatch.setattr(pr.providers, "MAX_PROVIDERS_FILE_BYTES", len(before) + 50)
    refused("serve reads at most", target, "add", "salad-profile", None, profile("b"))
    assert target.read_bytes() == before


def test_a_held_lock_is_a_bounded_refusal(target):
    import fcntl

    add(target, profile())
    lock = Path(str(target) + ".lock")
    holder = os.open(lock, os.O_RDWR)
    other = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(pr.RegistrationRefused, match="locked"):
            pr._lock(other, seconds=0.1)
    finally:
        os.close(holder)
        os.close(other)


def test_self_hosted_list_and_readout_use_the_instance_id(target):
    entry = {"id": "lab-2", "worker_principal_id": "worker-lab"}
    policy = build_policy({"lab-2": (["public"], ["worker-lab"])})
    trust = add(target, entry, section="self-hosted", policy=policy)["trust"]
    assert (
        trust["provider"] == "lab-2"
        and '"lab-2"' in trust["hint"]
        and "self-hosted" not in (trust["hint"])
    )
    assert "next serve start" in trust["note"]
    (row,) = pr.list_document(pr.read_document(target)[0])
    assert row["provider"] == "lab-2" and row["scope"] == "deployment"


def _all_registered(listed):
    from tessera_control.provider_trust import policy_from_document

    return policy_from_document(
        {
            "schema": "tessera.provider-trust.v1",
            "mode": "all_registered_providers",
            "providers": {"lab-2": {"data_classes": [], "worker_principal_ids": [listed]}},
        },
        "file",
    )


def test_readout_says_what_owner_mode_will_do_for_a_provider_registered_only_after_restart(target):
    entry = {"id": "lab-2", "worker_principal_id": "worker-lab"}
    trust = add(target, entry, section="self-hosted", policy=_all_registered("worker-lab"))["trust"]
    assert trust["after_restart"].startswith("every data class")
    other = add(
        target, {**entry, "id": "lab-3"}, section="self-hosted", policy=_all_registered("x")
    )
    assert other["trust"]["after_restart"].startswith("public only")  # worker not listed there
    unlisted = add(
        target, {**entry, "id": "lab-4"}, section="self-hosted", policy=_all_registered("someone")
    )
    assert unlisted["trust"]["after_restart"].startswith("public only")
    sections = pr.sections()
    assert sections["salad-profile"].scope == "organization"
    assert {sections[n].scope for n in ("local-docker", "self-hosted")} == {"deployment"}


# -- authorize runs on the document read under the lock -------------------------------------------


def test_authorize_refusal_changes_nothing_and_sees_the_document_as_read_under_the_lock(target):
    add(target, profile())
    stale = json.loads(target.read_text())  # what a caller listed earlier
    edited = json.loads(target.read_text())
    edited["salad_profiles"]["profiles"][0]["org_id"] = "org-other"
    target.write_text(json.dumps(edited, indent=2, sort_keys=True) + "\n")  # a concurrent edit
    audit = Path(str(target) + pr.AUDIT_SUFFIX)
    before_file, before_audit = target.read_bytes(), audit.read_bytes()
    seen = []

    def authorize(document):
        seen.append(document["salad_profiles"]["profiles"][0]["org_id"])
        return "not yours" if seen[-1] != stale["salad_profiles"]["profiles"][0]["org_id"] else None

    with pytest.raises(pr.RegistrationRefused, match="not yours"):
        pr.apply_change(
            target,
            "update",
            "salad-profile",
            "cpu-main",
            {"max_run_cents": 20},
            actor="test",
            authorize=authorize,
        )
    assert seen == ["org-other"]  # the NEW content, not the stale listing
    assert target.read_bytes() == before_file and audit.read_bytes() == before_audit


def test_authorize_that_allows_lets_the_change_through(target):
    add(target, profile())
    result = pr.apply_change(
        target,
        "update",
        "salad-profile",
        "cpu-main",
        {"max_run_cents": 20},
        actor="test",
        authorize=lambda document: None,
    )
    assert result["written"] is True and result["changed_fields"] == ["max_run_cents"]
