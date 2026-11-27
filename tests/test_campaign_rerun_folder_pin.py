"""A failed-stages re-run pins several outputs of one stage as one folder (ADR 0209). Fakes only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from campaign_runner_harness import IMAGE, World, dump
from pydantic import ValidationError

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import (
    normalize,
    parse_spec,
    stage_input_class,
)
from tessera_control.campaign_rerun import RerunRefused, build_retry_spec
from tessera_control.campaign_salad_adapter import input_plan
from tessera_control.campaign_store import CampaignStore
from tessera_control.salad_campaign_profiles import DATA_POLICY_TRUST, stage_data_policy
from tessera_control.snapshot_composition import composed_inventory, inventory_digest
from tessera_control.store import Conflict, _connect, canonical_digest

ACTIONS = ["campaign:submit", "campaign:read", "campaign:cancel", "artifact:read", "campaign:logs"]
FILES = {
    "cd_lock": "cd_lock.json",
    "surfactants": "surfactants.csv",
    "lab_16e84db3": "labels_16e84db3.json.gz",
}
MOUNTS = {f"prep/{path}" for path in FILES.values()}


@pytest.fixture(autouse=True)
def _local_docker_worker():
    yield
    worker.register_from_config(None)


def _spec(env) -> dict:
    code = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    prepare = {
        "key": "prepare",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": n, "path": p, "max_bytes": 1024} for n, p in FILES.items()],
        "max_duration_seconds": 60,
        "inputs": [code],
    }
    train = {
        **prepare,
        "key": "train",
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "inputs": [
            code,
            {"kind": "stage", "name": "prep", "stage": "prepare", "outputs": [*FILES]},
        ],
    }
    return {
        "version": 2,
        "name": "folder",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [prepare, train],
    }


def _failed_after_prepare(tmp_path):
    world = World(tmp_path)
    env = world.env
    env.trust()
    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    prepared = {"cd_lock.json": '{"lock": 1}', "surfactants.csv": "a,b\n"}
    prepared["labels_16e84db3.json.gz"] = "L"
    world.scenarios = [(0, prepared), (9, {"data.json": '{"score": 4}'})]
    campaign = world.campaign(_spec(env))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "failed", dump(world, campaign["id"])
    return world, campaign["id"]


def _rerun(world, actor, campaign_id, key, **body):
    world.env.execute(
        "UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?", actor["id"], campaign_id
    )
    return world.env.store.rerun_campaign(actor, campaign_id, key, canonical_digest(body), body)


def _recorded(world, cid):
    rows = world.env.query(
        "SELECT name, sha256, path FROM campaign_stage_io WHERE campaign_id=? "
        "AND stage_key='prepare' AND direction='output'",
        cid,
    )
    return {r["name"]: (r["path"], r["sha256"]) for r in rows}


def test_one_input_reading_three_outputs_is_rerun_as_one_reviewed_folder_pin(tmp_path):
    world, cid = _failed_after_prepare(tmp_path)
    env = world.env
    recorded = _recorded(world, cid)
    assert {name: path for name, (path, _sha) in recorded.items()} == FILES
    status, new = _rerun(
        world, env.agent(ACTIONS, "steerer"), cid, "folder-1", stage_keys=["train"]
    )
    assert status == 201 and new["status"] == "submitted"
    (stage,) = new["spec"]["stages"]
    assert stage["key"] == "train"
    pins = [i for i in stage["inputs"] if i["name"] == "prep"]
    assert len(pins) == 1  # one input name, one folder: never split into three inputs
    assert not any(i["kind"] == "artifact" for i in stage["inputs"])
    pin = pins[0]
    assert pin["kind"] == "artifact_folder"
    assert (pin["campaign_id"], pin["stage"]) == (cid, "prepare")
    assert pin["files"] == [
        {"output": name, "path": FILES[name], "sha256": recorded[name][1]} for name in FILES
    ]
    expected = [
        {
            "stage": "prepare",
            "output": name,
            "sha256": recorded[name][1],
            "input": "prep",
            "path": FILES[name],
        }
        for name in FILES
    ]
    assert new["rerun"]["reused_outputs"] == expected
    event = env.query(
        "SELECT payload_json FROM events WHERE event_type='campaign.rerun_submitted'"
    )[0]
    assert json.loads(event["payload_json"])["reused_outputs"] == expected

    # Planning (Salad input plan) and the runner's composition agree on prep/<declared path>.
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        live = connection.execute("SELECT * FROM campaigns WHERE id=?", (new["id"],)).fetchone()
        entries = CampaignStore.adapter_entries(connection, dict(live), live["project_id"])
        normal = normalize(parse_spec(new["spec"])).stages[0]
        folder = next(i for i in normal.inputs if i.name == "prep")
        assert {path for path, _sha, _size in entries(folder)} == set(FILES.values())
        profile = SimpleNamespace(
            name="p", disk_mb=1024, stage_adapter=SimpleNamespace(max_input_bytes=None)
        )
        planned, _cap = input_plan(normal, profile, entries)
        rows = []
        for source in normal.inputs:
            for path, sha, size in entries(source):
                rows.append((f"{source.name}/{path}", sha, size, "imported_unverified"))
        assert MOUNTS <= {row[0] for row in rows}
        assert planned == inventory_digest(composed_inventory(rows))
    finally:
        connection.close()

    # A person approves the new digest; only train runs, on the composed folder layout.
    runs = len(world.worker_runs)
    world.scenarios = [(0, {"data.json": '{"score": 5}'})]
    env.approve(new)
    world.tick_all(new["id"])
    assert world.status(new["id"]) == "succeeded", dump(world, new["id"])
    assert len(world.worker_runs) == runs + 1
    snap = world.row(
        "SELECT s.digest, s.manifest_json FROM campaign_stages st JOIN source_snapshots s "
        "ON s.id = st.input_snapshot_id WHERE st.campaign_id=? AND st.stage_key='train'",
        new["id"],
    )
    paths = {e["relative_path"] for e in json.loads(snap["manifest_json"])["entries"]}
    assert MOUNTS <= paths and not any(p.startswith(("cd_lock/", "surfactants/")) for p in paths)
    assert snap["digest"] == planned  # the reviewed plan is exactly what was composed
    inputs = env.query(
        "SELECT name, sha256, path FROM campaign_stage_io WHERE campaign_id=? "
        "AND direction='input' AND name LIKE 'prep/%'",
        new["id"],
    )
    assert {r["name"] for r in inputs} == MOUNTS


def test_the_folder_is_refused_whole_when_custody_is_gone(tmp_path):
    world, cid = _failed_after_prepare(tmp_path)
    env = world.env
    manifest = world.row(
        "SELECT m.* FROM artifact_manifests m JOIN campaign_stage_io io ON io.manifest_id = m.id "
        "WHERE io.campaign_id=? AND io.stage_key='prepare' AND io.direction='output'",
        cid,
    )
    human = world.row("SELECT id FROM principals WHERE role='human'")["id"]
    env.execute(
        """INSERT INTO artifact_custody_releases (id, manifest_id, manifest_digest, revision,
        state, rationale, actor_principal_id, created_at)
        VALUES ('rel_revoked', ?, ?, 99, 'revoked', 'test', ?, '2026-01-01T00:00:00Z')""",
        manifest["id"],
        manifest["manifest_digest"],
        human,
    )
    with pytest.raises(Conflict, match="custody-released"):
        _rerun(world, env.agent(ACTIONS, "steerer"), cid, "folder-revoked")
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 1


def _hand_written(world, cid, mutate):
    """Submit a hand-written spec holding a folder pin changed by ``mutate``."""
    recorded = _recorded(world, cid)
    spec = _spec(world.env)
    train = spec["stages"][1]
    train["inputs"][1] = {
        "kind": "artifact_folder",
        "name": "prep",
        "campaign_id": cid,
        "stage": "prepare",
        "files": [{"output": n, "path": p, "sha256": recorded[n][1]} for n, p in FILES.items()],
    }
    mutate(train["inputs"][1])
    spec["stages"] = [train]
    return world.env.submit(spec=spec)


def test_submit_refuses_a_folder_path_that_is_not_the_declared_output_path(tmp_path):
    world, cid = _failed_after_prepare(tmp_path)

    def rename(pin):
        pin["files"][0]["path"] = "elsewhere.json"

    with pytest.raises(Conflict, match="not the path the producing stage declared"):
        _hand_written(world, cid, rename)

    def swap(pin):  # two real declared paths, exchanged between outputs
        pin["files"][0]["path"], pin["files"][1]["path"] = FILES["surfactants"], FILES["cd_lock"]

    with pytest.raises(Conflict, match="not the path the producing stage declared"):
        _hand_written(world, cid, swap)


def test_the_source_digest_approves_nothing_for_another_digest(tmp_path):
    world, cid = _failed_after_prepare(tmp_path)
    env = world.env
    # A folder file pinned to bytes the producing stage never recorded is refused at submit,
    # even though the source campaign (and its outputs' digests) were approved.
    other = "e" * 64

    def wrong_digest(pin):
        pin["files"][2]["sha256"] = other

    with pytest.raises(Conflict, match="does not resolve"):
        _hand_written(world, cid, wrong_digest)
    # The re-run is an ordinary submission: its own digest, no approval carried over, and the
    # folder is private_derived so only the provider trust policy can admit it.
    _status, new = _rerun(world, env.agent(ACTIONS, "steerer"), cid, "folder-policy")
    source = env.store.get_campaign(env.owner, cid)
    assert new["spec_digest"] != source["spec_digest"]
    assert env.query("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", new["id"]) == []
    assert new["status"] == "submitted"
    normal = normalize(parse_spec(new["spec"]))
    assert stage_input_class(normal, "train") == "private_derived"
    assert stage_data_policy(normal.stages[0]) == DATA_POLICY_TRUST


# -- the planner, offline -----------------------------------------------------------------


def _source(outputs, custody=None, digests=None, paths=None):
    snap = {"kind": "snapshot", "name": "code", "snapshot_id": "s", "digest": "b" * 64,
            "data_class": "public"}  # fmt: skip
    paths = paths or {n: FILES.get(n, n + ".json") for n in outputs}

    def stage(key, inputs, outs):
        return {
            "key": key,
            "provider": "local-docker",
            "runtime": "docker-cpu-v1",
            "image": "python@sha256:" + "a" * 64,
            "argv": ["true"],
            "max_duration_seconds": 60,
            "inputs": inputs,
            "outputs": [{"name": n, "path": paths[n], "max_bytes": 10} for n in outs]
            or [{"name": "r", "path": "r.json", "max_bytes": 10}],
        }

    campaign = {
        "id": "c1",
        "status": "failed",
        "settled_cents": 0,
        "spec": {
            "version": 2,
            "name": "n",
            "budget_cap_cents": 5,
            "deadline_seconds": 600,
            "stages": [
                stage("a", [snap], list(paths)),
                stage(
                    "b",
                    [snap, {"kind": "stage", "name": "prep", "stage": "a", "outputs": outputs}],
                    [],
                ),
            ],
        },
        "stages": [
            {"stage_key": "a", "status": "succeeded"},
            {"stage_key": "b", "status": "failed"},
        ],
    }
    digests = digests or {n: [f"{i:064x}"] for i, n in enumerate(outputs, start=1)}
    custody = custody or {}
    rows = [
        {"stage_key": "a", "name": n, "sha256": d, "custody_released": custody.get(n, True)}
        for n in outputs
        for d in digests.get(n, [])
    ]
    return campaign, {"outputs": rows}


def test_the_planned_folder_is_a_valid_spec_with_declared_paths():
    campaign, outputs = _source(list(FILES))
    plan = build_retry_spec(campaign, outputs)
    (pin,) = [i for i in plan["spec"]["stages"][0]["inputs"] if i["name"] == "prep"]
    assert pin["kind"] == "artifact_folder"
    assert [(f["output"], f["path"]) for f in pin["files"]] == list(FILES.items())
    normal = normalize(parse_spec(plan["spec"]))
    folder = next(i for i in normal.stages[0].inputs if i.name == "prep")
    assert [path for _o, path, _s in folder.files] == list(FILES.values())
    assert len(plan["reused_outputs"]) == 3


@pytest.mark.parametrize("broken", list(FILES))
def test_one_unreleased_file_refuses_the_whole_folder(broken):
    campaign, outputs = _source(list(FILES), custody={broken: False})
    with pytest.raises(RerunRefused, match=f"{broken}.*custody-released"):
        build_retry_spec(campaign, outputs)


@pytest.mark.parametrize("digests", [[], ["1" * 64, "2" * 64]])
def test_one_file_without_a_single_digest_refuses_the_whole_folder(digests):
    names = list(FILES)
    good = {n: [f"{i:064x}"] for i, n in enumerate(names, start=1)}
    good["surfactants"] = digests
    campaign, outputs = _source(names, digests=good)
    with pytest.raises(RerunRefused, match="no single recorded digest"):
        build_retry_spec(campaign, outputs)


def test_an_unsafe_declared_path_is_refused_not_pinned():
    paths = {"cd_lock": "../cd_lock.json", "surfactants": "surfactants.csv"}
    campaign, outputs = _source(list(paths), paths=paths)
    with pytest.raises(RerunRefused, match="cannot be pinned as a folder file"):
        build_retry_spec(campaign, outputs)


def test_a_single_output_stays_an_ordinary_artifact_pin():
    campaign, outputs = _source(["cd_lock"])
    plan = build_retry_spec(campaign, outputs)
    (pin,) = [i for i in plan["spec"]["stages"][0]["inputs"] if i["name"] == "prep"]
    assert pin == {
        "kind": "artifact",
        "name": "prep",
        "campaign_id": "c1",
        "stage": "a",
        "output": "cd_lock",
        "sha256": f"{1:064x}",
    }
    assert plan["reused_outputs"] == [{"stage": "a", "output": "cd_lock", "sha256": f"{1:064x}"}]


# -- the spec model -----------------------------------------------------------------------


def _folder(files):
    spec = {
        "version": 2,
        "name": "n",
        "budget_cap_cents": 5,
        "deadline_seconds": 600,
        "stages": [
            {
                "key": "b",
                "provider": "local-docker",
                "runtime": "docker-cpu-v1",
                "image": "python@sha256:" + "a" * 64,
                "argv": ["true"],
                "max_duration_seconds": 60,
                "inputs": [
                    {
                        "kind": "artifact_folder",
                        "name": "prep",
                        "campaign_id": "c1",
                        "stage": "a",
                        "files": files,
                    }
                ],
                "outputs": [{"name": "r", "path": "r.json", "max_bytes": 10}],
            }
        ],
    }
    return parse_spec(spec)


def _f(output, path):
    return {"output": output, "path": path, "sha256": "d" * 64}


@pytest.mark.parametrize(
    "files",
    [
        [_f("x", "/abs.json"), _f("y", "y.json")],
        [_f("x", "../x.json"), _f("y", "y.json")],
        [_f("x", "a/../x.json"), _f("y", "y.json")],
        [_f("x", ""), _f("y", "y.json")],
        [_f("x", "a//x.json"), _f("y", "y.json")],
        [_f("x", "same.json"), _f("y", "same.json")],
        [_f("x", "x.json"), _f("x", "y.json")],
        [_f("x", "d"), _f("y", "d/y.json")],
        [_f("x", "x.json")],  # one file is an ordinary artifact pin
    ],
)
def test_the_spec_refuses_an_ambiguous_or_escaping_folder(files):
    with pytest.raises(ValidationError):
        _folder(files)


def test_existing_artifact_pins_serialize_unchanged():
    pin = {
        "kind": "artifact",
        "name": "up",
        "campaign_id": "c1",
        "stage": "a",
        "output": "x",
        "sha256": "d" * 64,
    }
    spec = _folder([_f("x", "x.json"), _f("y", "y.json")]).model_dump(mode="json")
    spec["stages"][0]["inputs"] = [pin]
    assert parse_spec(spec).model_dump(mode="json")["stages"][0]["inputs"] == [pin]
