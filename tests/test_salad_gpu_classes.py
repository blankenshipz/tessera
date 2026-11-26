"""Read-only Salad GPU class listing: eligible classes, recorded data only, no secrets."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_provider_registration_api import ROUTE, World
from test_provider_registration_api import profile as api_profile

from tessera_control import salad_gpu_classes as gpu
from tessera_control.cli import main, parser
from tessera_control.salad_campaign_profiles import SaladCampaignProfile

GPU_A = "550e8400-e29b-41d4-a716-446655440000"
GPU_B = "650e8400-e29b-41d4-a716-446655440001"
CREDENTIAL = "credential-ref:very-private-ref"
HOST = "custody.example-tailnet.ts.net"
RATE = {"vcpu_hour_usd": "0.04", "memory_gb_hour_usd": "0.01"}


def make(name, gpu_class=GPU_A, org="org-1", run=50, **over):
    value = dict(
        name=name,
        org_id=org,
        project_id="project-1",
        worker_principal_id="worker-1",
        salad_organization="example-org",
        salad_project="example-project",
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
        custody_host=HOST,
        credential_reference=CREDENTIAL,
        credential_version=1,
        allowed_image_digests=["sha256:" + "1" * 64],
        max_run_cents=run,
        max_task_cents=run * 2,
    )
    value.update(gpu_class=gpu_class) if gpu_class else value.update(cpu_rate=RATE)
    value.update(over)
    return SaladCampaignProfile.model_validate(value)


def final(**over):
    value = dict(
        status="stopped",
        reason="done",
        orphan=False,
        allocation_uncertain=False,
        escalation_pending=False,
        budget_fenced=False,
        rate_unknown=False,
        published_hourly_rate_usd=None,
        observed_at="2026-01-01T00:00:00Z",
    )
    value.update(over)
    return json.dumps(value)


def database(path, rows, org="org-1"):
    """Synthetic receipts: (profile, guardian_final_json, updated_at)."""
    db = sqlite3.connect(path)
    db.executescript(
        """CREATE TABLE campaigns (id TEXT PRIMARY KEY, org_id TEXT);
        CREATE TABLE campaign_stage_attempts (
            id TEXT PRIMARY KEY, campaign_id TEXT, stage_key TEXT);
        CREATE TABLE campaign_salad_stages (campaign_id TEXT, stage_key TEXT, profile TEXT);
        CREATE TABLE campaign_salad_attempts (attempt_id TEXT, guardian_final_json TEXT,
            updated_at TEXT);"""
    )
    for index, (name, guardian, at) in enumerate(rows):
        cid = f"c{index}"
        db.execute("INSERT INTO campaigns VALUES (?,?)", (cid, org))
        db.execute("INSERT INTO campaign_stage_attempts VALUES (?,?,?)", (f"a{index}", cid, "s"))
        db.execute("INSERT INTO campaign_salad_stages VALUES (?,?,?)", (cid, "s", name))
        db.execute(
            "INSERT INTO campaign_salad_attempts VALUES (?,?,?)", (f"a{index}", guardian, at)
        )
    db.commit()
    db.close()
    return path


def now_text(days=0):
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_lists_only_gpu_classes_the_profiles_name_and_no_price_is_invented():
    listing = gpu.build_listing(
        [make("a-one"), make("a-two", run=80), make("b-one", GPU_B), make("cpu", None)], None
    )
    assert [c["id"] for c in listing["classes"]] == [GPU_A, GPU_B]
    first = listing["classes"][0]
    assert first["profiles"] == ["a-one", "a-two"] and first["max_run_cents"] == 80
    assert first["name"] is None and first["name_status"] == "not_recorded"
    assert first["published_hourly_rate_usd"] is None and first["price_status"] == "not_recorded"
    assert listing["cpu_only_profiles"] == 1


def test_instability_counts_come_from_receipts_and_a_recorded_rate_is_reported(tmp_path):
    rows = [
        ("a-one", final(prestart_rebinds=None), now_text(1)),
        (
            "a-one",
            final(
                allocation_uncertain=True,
                published_hourly_rate_usd="0.35",
                prestart_rebinds={
                    "count": 2,
                    "events": [
                        {
                            "at": "2026-01-01T00:00:00Z",
                            "from_allocation": "x",
                            "to_allocation": "y",
                            "reason": "prestart_reallocation",
                        }
                    ],
                },
            ),
            now_text(0),
        ),
        ("a-one", final(), now_text(40)),  # outside the window
        ("a-one", None, now_text(0)),  # no guardian record: not evidence either way
    ]
    path = database(tmp_path / "db.sqlite3", rows)
    listing = gpu.list_gpu_classes([make("a-one"), make("b-one", GPU_B)], path)
    first, second = listing["classes"]
    assert first["allocation_instability"] == {
        "status": "recorded",
        "window_days": 14,
        "attempts_with_guardian_record": 2,
        "prestart_rebinds": 2,
        "allocation_uncertain_attempts": 1,
    }
    assert first["published_hourly_rate_usd"] == "0.35"
    # Nothing recorded for this class: null, never zero.
    assert second["allocation_instability"]["status"] == "not_recorded"
    assert second["allocation_instability"]["prestart_rebinds"] is None
    assert second["allocation_instability"]["attempts_with_guardian_record"] is None


def test_no_database_means_not_recorded_and_other_orgs_are_excluded(tmp_path):
    listing = gpu.list_gpu_classes([make("a-one")], None)
    assert listing["classes"][0]["allocation_instability"]["status"] == "not_recorded"
    path = database(tmp_path / "db.sqlite3", [("a-one", final(), now_text(0))], org="org-2")
    mine = gpu.list_gpu_classes([make("a-one")], path, org_id="org-1")
    assert mine["classes"][0]["allocation_instability"]["status"] == "not_recorded"
    other = gpu.list_gpu_classes([make("a-one", org="org-2")], path, org_id="org-1")
    assert other["classes"] == []


def test_the_database_is_opened_read_only_and_a_bad_window_or_database_fails_closed(tmp_path):
    path = database(tmp_path / "db.sqlite3", [])
    before = path.read_bytes()
    gpu.list_gpu_classes([make("a-one")], path)
    assert path.read_bytes() == before
    with pytest.raises(gpu.GpuClassListingError):
        gpu.list_gpu_classes([make("a-one")], path, days=0)
    with pytest.raises(gpu.GpuClassListingError):
        gpu.list_gpu_classes([make("a-one")], tmp_path / "missing.sqlite3")
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    with pytest.raises(gpu.GpuClassListingError):
        gpu.list_gpu_classes([make("a-one")], empty)


def test_no_secret_shaped_value_reaches_the_output(tmp_path):
    path = database(tmp_path / "db.sqlite3", [("a-one", final(), now_text(0))])
    listing = gpu.list_gpu_classes([make("a-one")], path)
    text = json.dumps(listing) + gpu.render_text(listing)
    for forbidden in (CREDENTIAL, "credential-ref", HOST, "ts.net", "example-org"):
        assert forbidden not in text


def _providers_file(tmp_path, *profiles, mode=0o600):
    path = tmp_path / "providers.json"
    document = {
        "schema": "tessera.campaign-providers.v1",
        "salad_profiles": {
            "schema": "tessera.salad.campaign-profiles.v1",
            "profiles": [json.loads(p.model_dump_json()) for p in profiles],
        },
    }
    path.write_text(json.dumps(document))
    path.chmod(mode)
    return path


def test_cli_json_and_text(tmp_path, capsys):
    path = _providers_file(tmp_path, make("a-one"), make("b-one", GPU_B))
    state = tmp_path / "state"
    state.mkdir()
    database(state / "tessera.sqlite3", [("a-one", final(), now_text(0))])
    assert main(["providers", "gpu-classes", "--file", str(path), "--state-dir", str(state)]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["schema"] == gpu.SCHEMA and len(body["classes"]) == 2
    assert body["classes"][0]["allocation_instability"]["status"] == "recorded"
    assert CREDENTIAL not in json.dumps(body)
    assert main(["providers", "gpu-classes", "--file", str(path), "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert GPU_A in text and GPU_B in text and "not_recorded" in text and CREDENTIAL not in text


def test_cli_fails_closed_on_an_unreadable_or_invalid_file(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    assert main(["providers", "gpu-classes", "--file", str(missing)]) == 2
    assert "refused" in capsys.readouterr().err
    open_mode = _providers_file(tmp_path, make("a-one"), mode=0o644)
    assert main(["providers", "gpu-classes", "--file", str(open_mode)]) == 2
    err = capsys.readouterr()
    assert "refused" in err.err and err.out == ""
    assert main(["providers", "gpu-classes", "--file", "relative.json"]) == 2
    capsys.readouterr()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    bad.chmod(0o600)
    assert main(["providers", "gpu-classes", "--file", str(bad)]) == 2


def test_parser_registers_gpu_classes_under_providers():
    arguments = parser().parse_args(["providers", "gpu-classes", "--file", "/x"])
    assert arguments.providers_action == "gpu-classes" and arguments.format == "json"


# -- control API route ---------------------------------------------------------------------------


def _gpu_profile(world, name, gpu_class=GPU_A, **over):
    entry = api_profile(world.env, name=name, gpu_class=gpu_class, **over)
    entry.pop("cpu_rate")
    return entry


def test_route_lists_the_callers_classes_with_the_read_scope_only(tmp_path):
    world = World(tmp_path)
    body = {"action": "add", "section": "salad-profile", "entry": _gpu_profile(world, "gpu-main")}
    assert world.post(world.human(), body, key="k-gpu").status_code == 200
    document = json.loads(world.path.read_text())
    other = api_profile(world.env, name="theirs", org_id="org_of_another_tenant", gpu_class=GPU_B)
    other.pop("cpu_rate")
    document["salad_profiles"]["profiles"].append(other)
    world.path.write_text(json.dumps(document))
    world.path.chmod(0o600)
    url = ROUTE + "/salad-gpu-classes"
    reader = world.agent(["provider_trust:read"])
    response = world.client.get(url, headers=reader)
    assert response.status_code == 200
    listing = response.json()
    assert [c["id"] for c in listing["classes"]] == [GPU_A]
    assert listing["classes"][0]["allocation_instability"]["status"] == "not_recorded"
    assert GPU_B not in response.text and "credential-ref" not in response.text
    assert world.client.get(url, headers=world.human()).status_code == 200
    assert world.client.get(url, headers=world.agent(["campaign:read"])).status_code == 403
    assert world.client.get(url).status_code == 401
    assert world.client.get(url + "?days=0", headers=reader).status_code == 422


def test_route_says_so_when_no_file_is_configured_and_fails_closed_on_a_bad_one(tmp_path):
    url = ROUTE + "/salad-gpu-classes"
    world = World(tmp_path, configured=False)
    body = world.client.get(url, headers=world.human()).json()
    assert body["configured"] is False
    (tmp_path / "second").mkdir()
    world = World(tmp_path / "second")
    world.path.write_text("{broken")
    world.path.chmod(0o600)
    assert world.client.get(url, headers=world.human()).status_code == 409


def test_a_read_at_the_row_cap_is_marked_truncated(tmp_path, monkeypatch):
    rows = [("a-one", final(), now_text(0)), ("a-one", final(), now_text(1))]
    path = database(tmp_path / "db.sqlite3", rows)
    full = gpu.list_gpu_classes([make("a-one")], path)
    assert "truncated" not in full["classes"][0]["allocation_instability"]
    monkeypatch.setattr(gpu, "MAX_ROWS", 2)
    capped = gpu.list_gpu_classes([make("a-one")], path)
    assert capped["classes"][0]["allocation_instability"]["truncated"] is True
    assert len(capped["notes"]) == len(full["notes"]) + 1
    assert "(truncated)" in gpu.render_text(capped)
    assert "(truncated)" not in gpu.render_text(full)


def test_cli_refuses_a_window_outside_range_with_or_without_a_state_dir(tmp_path, capsys):
    path = _providers_file(tmp_path, make("a-one"))
    state = tmp_path / "state"
    state.mkdir()
    database(state / "tessera.sqlite3", [])
    for extra in ([], ["--state-dir", str(state)]):
        for days in ("0", "91"):
            argv = ["providers", "gpu-classes", "--file", str(path), "--days", days, *extra]
            assert main(argv) == 2
            captured = capsys.readouterr()
            assert "refused" in json.loads(captured.err) and captured.out == ""
