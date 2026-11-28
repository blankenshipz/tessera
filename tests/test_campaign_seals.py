"""Role-sealed campaign inputs and artifacts (ADR 0164): refused at parse, recorded on receipts.

Parse tests go through the real ``parse_spec_json`` path (structure checks, which include the
seal check); the receipt test runs an approved campaign through the in-process runner with a
fake worker.
"""

from __future__ import annotations

import copy
import json

import pytest
from campaign_runner_harness import IMAGE, World, dump
from pydantic import ValidationError

from tessera_control.campaign_models import normalize, parse_spec_json, spec_digest
from tessera_control.campaign_provider_worker import register_from_config
from tessera_control.campaign_rerun import build_retry_spec
from tessera_control.campaign_seals import sealed_access

RAW = "a" * 64
LABELS = "b" * 64
PINNED = "c" * 64


@pytest.fixture(autouse=True)
def _restore_local_docker():
    yield
    register_from_config(None)


def _snapshot(name: str, digest: str, data_class: str = "private_derived") -> dict:
    return {
        "kind": "snapshot",
        "name": name,
        "snapshot_id": f"snap-{name}",
        "digest": digest,
        "data_class": data_class,
    }


def _from(name: str, stage: str, *outputs: str) -> dict:
    return {"kind": "stage", "name": name, "stage": stage, "outputs": list(outputs)}


def _stage(key: str, inputs: list[dict], outputs=("result",), **extra) -> dict:
    return {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [
            {"name": name, "path": f"{name}.json", "kind": "other", "max_bytes": 1024}
            for name in outputs
        ],
        "max_duration_seconds": 60,
        "inputs": inputs,
        **extra,
    }


def _spec(*stages: dict, seals: list[dict] | None = None) -> dict:
    spec = {
        "version": 2,
        "name": "sealed inputs",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": list(stages),
    }
    if seals is not None:
        spec["seals"] = seals
    return spec


def _parse(spec: dict):
    return parse_spec_json(json.dumps(spec))


def _refusal(spec: dict) -> str:
    with pytest.raises(ValidationError) as caught:
        _parse(spec)
    return str(caught.value)


RECEIPT_SQL = "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?"
LABEL_SEAL = {"name": "heldout", "role": "evaluation", "digest": LABELS}


def test_a_fit_stage_mounting_a_sealed_input_is_refused_naming_stage_and_artifact():
    spec = _spec(
        _stage("fit", [_snapshot("train", RAW), _snapshot("labels", LABELS)]),
        seals=[LABEL_SEAL],
    )
    message = _refusal(spec)
    assert "Stage fit" in message and "'labels'" in message and "'heldout'" in message
    assert "'evaluation'" in message


def test_a_sealed_artifact_input_is_refused_like_a_snapshot():
    artifact = {
        "kind": "artifact",
        "name": "scores",
        "campaign_id": "camp-1",
        "stage": "score",
        "output": "result",
        "sha256": PINNED,
    }
    spec = _spec(
        _stage("fit", [_snapshot("train", RAW), artifact]),
        seals=[{"name": "prior-scores", "role": "evaluation", "digest": PINNED}],
    )
    assert "Stage fit" in _refusal(spec) and "'prior-scores'" in _refusal(spec)
    spec["stages"][0]["roles"] = ["evaluation"]
    _parse(spec)


def test_an_evaluation_stage_with_the_role_is_accepted_and_its_access_is_listed():
    spec = _spec(
        _stage("fit", [_snapshot("train", RAW)]),
        _stage(
            "score",
            [_from("model", "fit", "result"), _snapshot("labels", LABELS)],
            roles=["evaluation"],
        ),
        seals=[LABEL_SEAL],
    )
    normal = normalize(_parse(spec))
    assert normal.stage("score").roles == ("evaluation",)
    assert sealed_access(normal, "fit") == []
    assert sealed_access(normal, "score") == [
        {"seal": "heldout", "role": "evaluation", "input": "labels", "via": "direct"}
    ]


def test_a_derived_artifact_inherits_the_seal_through_every_hop():
    prep = _stage("prep", [_snapshot("labels", LABELS)], roles=["evaluation"])
    mid = _stage("mid", [_from("feats", "prep", "result")], roles=["evaluation"])
    fit = _stage("fit", [_snapshot("train", RAW), _from("leak", "mid", "result")])
    message = _refusal(_spec(prep, mid, fit, seals=[LABEL_SEAL]))
    assert "Stage fit" in message and "'leak'" in message and "'heldout'" in message
    assert "derived through stage mid" in message
    # One hop is refused too, and the stage in the middle needs the role as well.
    message = _refusal(_spec(prep, {**mid, "roles": None}, seals=[LABEL_SEAL]))
    assert "Stage mid" in message and "derived through stage prep" in message
    # With the role, the whole chain is accepted and the receipt record says how.
    normal = normalize(
        _parse(_spec(prep, mid, {**fit, "roles": ["evaluation"]}, seals=[LABEL_SEAL]))
    )
    assert sealed_access(normal, "fit") == [
        {
            "seal": "heldout",
            "role": "evaluation",
            "input": "leak",
            "via": "lineage",
            "from_stage": "mid",
        }
    ]


def test_a_sealed_stage_output_is_readable_only_by_the_role():
    split = _stage("split", [_snapshot("raw", RAW)], outputs=("train", "test"))
    seal = {"name": "test-split", "role": "evaluation", "stage": "split", "output": "test"}
    fit_ok = _stage("fit", [_from("train", "split", "train")])
    # The producing stage and a stage reading only the training slice need no role.
    _parse(_spec(split, fit_ok, seals=[seal]))
    fit_bad = _stage("fit", [_from("rows", "split", "train", "test")])
    message = _refusal(_spec(split, fit_bad, seals=[seal]))
    assert "Stage fit" in message and "'test-split'" in message
    score = _stage(
        "score",
        [_from("model", "fit", "result"), _from("test", "split", "test")],
        roles=["evaluation"],
    )
    normal = normalize(_parse(_spec(split, fit_ok, score, seals=[seal])))
    assert sealed_access(normal, "score") == [
        {"seal": "test-split", "role": "evaluation", "input": "test", "via": "direct"}
    ]


def test_a_stage_reading_two_seals_needs_both_roles():
    seals = [LABEL_SEAL, {"name": "audit", "role": "audit", "digest": RAW}]
    stage = _stage("both", [_snapshot("raw", RAW), _snapshot("labels", LABELS)])
    message = _refusal(_spec({**stage, "roles": ["evaluation"]}, seals=seals))
    assert "'audit'" in message
    _parse(_spec({**stage, "roles": ["evaluation", "audit"]}, seals=seals))


@pytest.mark.parametrize(
    ("seal", "why"),
    [
        ({"name": "x", "role": "evaluation", "digest": "d" * 64}, "matches no"),
        ({"name": "x", "role": "evaluation", "stage": "nope", "output": "result"}, "unknown"),
        ({"name": "x", "role": "evaluation", "stage": "fit", "output": "nope"}, "undeclared"),
        (
            {"name": "x", "role": "evaluation", "digest": RAW, "stage": "fit", "output": "result"},
            "exactly one",
        ),
        ({"name": "x", "role": "evaluation", "stage": "fit"}, "go together"),
        ({"name": "x", "role": "Evaluation", "digest": RAW}, "pattern"),
    ],
)
def test_a_malformed_seal_is_refused(seal, why):
    spec = _spec(_stage("fit", [_snapshot("train", RAW)], roles=["evaluation"]), seals=[seal])
    assert why in _refusal(spec)


def test_duplicate_seal_names_and_duplicate_roles_are_refused():
    stage = _stage("fit", [_snapshot("train", RAW)], roles=["evaluation"])
    twice = {"name": "heldout", "role": "evaluation", "digest": RAW}
    assert "unique" in _refusal(_spec(stage, seals=[twice, {**twice, "role": "audit"}]))
    assert "sealed twice" in _refusal(_spec(stage, seals=[twice, {**twice, "name": "again"}]))
    assert "distinct" in _refusal(_spec({**stage, "roles": ["evaluation", "evaluation"]}))


def test_specs_without_seals_are_unchanged():
    spec = _spec(
        _stage("fit", [_snapshot("train", RAW)]),
        _stage("score", [_from("model", "fit", "result"), _snapshot("labels", LABELS)]),
    )
    parsed = _parse(spec)
    dumped = parsed.model_dump(mode="json")
    assert "seals" not in dumped and all("roles" not in s for s in dumped["stages"])
    normal = normalize(parsed)
    assert normal.seals == () and all(stage.roles == () for stage in normal.stages)
    assert sealed_access(normal, "score") == []
    # Same digest as before the feature: the canonical dump has no new keys.
    from tessera_control.store import canonical_digest

    assert spec_digest(parsed) == canonical_digest(dumped)
    assert json.loads(json.dumps(dumped)) == _parse(json.loads(json.dumps(dumped))).model_dump(
        mode="json"
    )
    # A declared role with no seals is accepted and is part of the digest.
    with_role = copy.deepcopy(spec)
    with_role["stages"][1]["roles"] = ["evaluation"]
    assert spec_digest(_parse(with_role)) != spec_digest(parsed)


def _finished(spec: dict, status: dict[str, str]) -> dict:
    return {
        "id": "camp-old",
        "status": "failed",
        "spec": spec,
        "stages": [{"stage_key": key, "status": value} for key, value in status.items()],
    }


def test_a_rerun_keeps_the_seals_its_pinned_outputs_carried():
    split = _stage("split", [_snapshot("raw", RAW)], outputs=("train", "test"))
    prep = _stage("prep", [_snapshot("labels", LABELS)], roles=["evaluation"])
    fit = _stage("fit", [_from("train", "split", "train")])
    score = _stage(
        "score",
        [
            _from("model", "fit", "result"),
            _from("test", "split", "test"),
            _from("feats", "prep", "result"),
        ],
        roles=["evaluation"],
    )
    seals = [
        {"name": "test-split", "role": "evaluation", "stage": "split", "output": "test"},
        LABEL_SEAL,
    ]
    spec = _spec(split, prep, fit, score, seals=seals)
    _parse(spec)
    campaign = _finished(
        spec, {"split": "succeeded", "prep": "succeeded", "fit": "succeeded", "score": "failed"}
    )
    outputs = {
        "outputs": [
            {"stage_key": key, "name": name, "sha256": sha, "custody_released": True}
            for key, name, sha in (
                ("split", "train", "1" * 64),
                ("split", "test", "2" * 64),
                ("prep", "result", "3" * 64),
                ("fit", "result", "4" * 64),
            )
        ]
    }
    plan = build_retry_spec(campaign, outputs)
    retry = plan["spec"]
    assert [s["key"] for s in retry["stages"]] == ["score"]
    # The sealed test slice and the output derived from the sealed labels stay sealed by digest;
    # the raw-labels seal has no reader left, and the unsealed model output is not sealed.
    assert [(s["role"], s["digest"]) for s in retry["seals"]] == [
        ("evaluation", "2" * 64),
        ("evaluation", "3" * 64),
    ]
    assert retry["seals"][0]["name"].startswith("test-split-")
    assert retry["seals"][1]["name"].startswith("heldout-")
    normal = normalize(_parse(retry))
    assert {item["input"] for item in sealed_access(normal, "score")} == {"test", "feats"}
    # Without the role the planned re-run would be refused like the original.
    stripped = copy.deepcopy(retry)
    del stripped["stages"][0]["roles"]
    assert "Stage score" in _refusal(stripped)


def test_a_rerun_of_a_spec_without_seals_adds_none():
    spec = _spec(
        _stage("fit", [_snapshot("train", RAW)]),
        _stage("score", [_from("model", "fit", "result")]),
    )
    plan = build_retry_spec(
        _finished(spec, {"fit": "succeeded", "score": "failed"}),
        {
            "outputs": [
                {"stage_key": "fit", "name": "result", "sha256": "4" * 64, "custody_released": True}
            ]
        },
    )
    assert "seals" not in plan["spec"]


def test_the_receipt_records_sealed_access_per_stage(tmp_path):
    world = World(tmp_path)
    env = world.env
    register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    labels = {
        "kind": "snapshot",
        "name": "labels",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    result = [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}]
    score = {**_stage("score", [labels], roles=["evaluation"]), "outputs": result}
    report = {
        **_stage("report", [_from("scores", "score", "result")], roles=["evaluation"]),
        "outputs": result,
    }
    spec = _spec(
        score,
        report,
        seals=[{"name": "heldout", "role": "evaluation", "digest": env.snapshot_digest}],
    )
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    row = world.row(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"]
    )
    stages = {item["stage_key"]: item for item in json.loads(row["receipt_json"])["stages"]}
    assert stages["score"]["sealed_access"] == [
        {"seal": "heldout", "role": "evaluation", "input": "labels", "via": "direct"}
    ]
    assert stages["report"]["sealed_access"] == [
        {
            "seal": "heldout",
            "role": "evaluation",
            "input": "scores",
            "via": "lineage",
            "from_stage": "score",
        }
    ]


def test_a_receipt_without_seals_has_no_sealed_access(tmp_path):
    world = World(tmp_path)
    env = world.env
    register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    code = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }
    result = [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}]
    campaign = world.campaign(_spec({**_stage("s1", [code]), "outputs": result}))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    row = world.row(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"]
    )
    (stage,) = json.loads(row["receipt_json"])["stages"]
    assert "sealed_access" not in stage
