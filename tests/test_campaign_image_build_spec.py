"""An image-build stage's spec, admission and lineage (ADR 0123).

Nothing here builds an image or touches a network: the predecessor's recorded
``execution.image_built`` event is written as the report endpoint writes it, and its receipt is
rebuilt by ``build_receipt`` (never edited by hand).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from campaign_fixture import Env, record_snapshot_private
from campaign_runner_harness import World, stage_spec
from pydantic import ValidationError
from test_campaign_models_v2 import _spec2
from test_campaign_receipt_chaining import _finished

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import (
    BUILD_ARGV,
    BUILD_RUNTIME,
    normalize,
    parse_spec,
    parse_spec_json,
    spec_digest,
    stage_classes,
)
from tessera_control.store import Conflict, StoreError, canonical_digest

BASE = "python@sha256:" + "b" * 64


def snapshot(env, name="ctx", **over):
    return {
        "kind": "snapshot",
        "name": name,
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
        **over,
    }


def registered(env, repositories=("registry.example.org/team",)):
    """The operator file: local-docker with its registered push destinations (ADR 0123)."""
    return {
        "local_docker": {
            "worker_principal_id": env.worker["id"],
            "build_repositories": list(repositories),
        }
    }


def build_stage(env, /, **over):
    value = {
        "key": "build",
        "provider": "local-docker",
        "runtime": BUILD_RUNTIME,
        "image": BASE,
        "argv": list(BUILD_ARGV),
        "max_duration_seconds": 120,
        "inputs": [snapshot(env)],
        "outputs": [{"name": "log", "path": "build.log", "kind": "log", "max_bytes": 1024}],
        "build": {
            "context_input": "ctx",
            "dockerfile": "Dockerfile",
            "base_images": [BASE],
            "repository": "registry.example.org/team/app",
        },
    }
    value.update(over)
    return value


def invalid(env, match, /, **over):
    with pytest.raises(ValidationError, match=match):
        parse_spec_json(json.dumps(_spec2(build_stage(env, **over))))


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    worker.register_from_config(registered(env))
    env.trust()
    yield env
    worker.register_from_config(None)


# -- structure -------------------------------------------------------------------------


def test_a_valid_build_spec_parses_and_other_specs_keep_their_digest(env):
    spec = parse_spec(_spec2(build_stage(env)))
    stage = normalize(spec).stages[0]
    assert stage.build["repository"] == "registry.example.org/team/app"
    plain = _spec2(
        {k: v for k, v in build_stage(env).items() if k != "build"}
        | {"runtime": "docker-cpu-v1", "argv": ["python", "x.py"]}
    )
    dumped = json.dumps(parse_spec_json(json.dumps(plain)).model_dump(mode="json"))
    for absent in ("build", "image_from", "image_resolved_from"):
        assert f'"{absent}":' not in dumped
    assert spec_digest(parse_spec_json(json.dumps(plain))) == canonical_digest(json.loads(dumped))
    assert spec_digest(spec) != spec_digest(parse_spec_json(json.dumps(plain)))


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"runtime": "docker-cpu-v1"}, "runtime is docker-build-v1"),
        ({"argv": ["tessera-image-build", "--x"]}, "argv is exactly"),
        ({"env": {"A": "b"}}, "declares no env"),
        (
            {
                "egress": {
                    "profile": "allowlist-proxy-v1",
                    "destinations": [{"host": "a.example.org", "port": 443}],
                    "max_egress_bytes": 1,
                }
            },
            "must not declare egress",
        ),
        ({"code_input": "ctx"}, "no code_input"),
        (
            {"alternate_providers": [{"provider": "self-hosted", "runtime": BUILD_RUNTIME}]},
            "no alternate_providers",
        ),
        (
            {"outputs": [{"name": "log", "path": "b.log", "kind": "dataset", "max_bytes": 9}]},
            "exactly one output, of kind log",
        ),
        (
            {
                "outputs": [
                    {"name": "log", "path": "b.log", "kind": "log", "max_bytes": 9},
                    {"name": "two", "path": "c.log", "kind": "log", "max_bytes": 9},
                ]
            },
            "exactly one output, of kind log",
        ),
    ],
)
def test_each_build_stage_shape_rule_is_refused(env, over, match):
    invalid(env, match, **over)


def test_the_build_context_must_be_a_public_snapshot_input(env):
    invalid(env, "must be a public snapshot", inputs=[snapshot(env, data_class="private_derived")])
    invalid(env, "must name a pinned snapshot input", inputs=[snapshot(env, name="other")])
    art = {
        "kind": "artifact",
        "name": "ctx",
        "campaign_id": "c1",
        "stage": "s0",
        "output": "out",
        "sha256": "a" * 64,
    }
    invalid(env, "must name a pinned snapshot input", inputs=[art])


def test_a_build_stage_has_exactly_its_one_context_input(env):
    extra = {"kind": "stage", "name": "up", "stage": "other", "outputs": ["o"]}
    other = {
        "key": "other",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": BASE,
        "argv": ["python"],
        "max_duration_seconds": 60,
        "inputs": [snapshot(env, name="d")],
        "outputs": [{"name": "o", "path": "o.json", "max_bytes": 9}],
    }
    stage = build_stage(env, inputs=[snapshot(env), extra])
    with pytest.raises(ValidationError, match="exactly its one context input"):
        parse_spec_json(json.dumps(_spec2(other, stage)))


def test_the_build_runtime_requires_a_build_section(env):
    stage = {k: v for k, v in build_stage(env).items() if k != "build"}
    with pytest.raises(ValidationError, match="requires a build section"):
        parse_spec_json(json.dumps(_spec2(stage)))


def test_another_stages_code_input_must_not_name_the_build_context_snapshot(env):
    code = {
        "key": "run",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": BASE,
        "argv": ["python", "/inputs/main.py"],
        "max_duration_seconds": 60,
        "inputs": [snapshot(env, name="src")],
        "code_input": "src",
        "outputs": [{"name": "o", "path": "o.json", "max_bytes": 9}],
    }
    with pytest.raises(ValidationError, match="private code is never baked"):
        parse_spec_json(json.dumps(_spec2(build_stage(env), code)))


def test_stage_classes_of_a_build_stage(env):
    assert stage_classes(parse_spec(_spec2(build_stage(env)))) == {
        "build": ("public", "private_derived")
    }


# -- submit-time admission -------------------------------------------------------------


def submit(env, spec):
    body = {"spec": spec}
    return env.store.submit_campaign(
        env.owner, env.project["id"], "k", canonical_digest(body), body
    )


def set_entries(env, paths):
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        manifest = json.loads(
            db.execute(
                "SELECT manifest_json FROM source_snapshots WHERE id=?", (env.snapshot_id,)
            ).fetchone()[0]
        )
        manifest["entries"] = [{"relative_path": p, "sha256": "0" * 64, "size": 1} for p in paths]
        db.execute(
            "UPDATE source_snapshots SET manifest_json=? WHERE id=?",
            (json.dumps(manifest), env.snapshot_id),
        )


def test_an_ordinary_public_context_with_its_dockerfile_is_admitted(env):
    set_entries(env, ["Dockerfile", "requirements.txt", "app/main.py"])
    status, body = submit(env, _spec2(build_stage(env)))
    assert status == 201
    assert body["spec"]["stages"][0]["build"]["dockerfile"] == "Dockerfile"


@pytest.mark.parametrize(
    "repository",
    [
        "registry.example.org/elsewhere/app",  # same host, a path the operator did not register
        "registry.example.org/team2/app",  # a sibling that only shares a name prefix
        "other.example.org/team/app",  # another public host
    ],
)
def test_a_push_to_an_unregistered_repository_is_refused_at_submit(env, repository):
    set_entries(env, ["Dockerfile"])
    stage = build_stage(env)
    stage["build"]["repository"] = repository
    with pytest.raises(StoreError, match="build_repository_not_registered"):
        submit(env, _spec2(stage))


def test_without_any_registered_repository_no_pushing_build_is_admitted(env):
    worker.register_from_config(registered(env, repositories=()))
    set_entries(env, ["Dockerfile"])
    with pytest.raises(StoreError, match="build_repository_not_registered"):
        submit(env, _spec2(build_stage(env)))
    stage = build_stage(env)
    stage["build"]["push"] = False  # nothing is pushed, so no destination is needed
    assert submit(env, _spec2(stage))[0] == 201


@pytest.mark.parametrize("path", ["a//Dockerfile", "a/./Dockerfile", "a/", "a/../Dockerfile"])
def test_a_dockerfile_path_is_refused_at_submit_as_the_worker_would(env, path):
    build = {**build_stage(env)["build"], "dockerfile": path}
    invalid(env, "plain path inside the build context", build=build)


def test_a_context_without_the_dockerfile_is_refused(env):
    set_entries(env, ["requirements.txt"])
    with pytest.raises(Conflict, match="holds no file 'Dockerfile'"):
        submit(env, _spec2(build_stage(env)))
    set_entries(env, [])
    with pytest.raises(Conflict, match="holds no file 'Dockerfile'"):
        submit(env, _spec2(build_stage(env)))


def test_a_context_with_a_code_approval_is_refused(env):
    set_entries(env, ["Dockerfile", "main.py"])
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        db.execute(
            """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
            actor_principal_id, decision_class, provenance_json, recorded_at)
            VALUES (?, '[{"relative_path": "main.py", "sha256": "0", "size": 1}]', ?,
            'human_session', '{}', 't')""",
            (env.snapshot_id, env.owner["id"]),
        )
    with pytest.raises(Conflict, match="private code approval"):
        submit(env, _spec2(build_stage(env)))


def test_a_context_recorded_private_is_refused_even_when_declared_public(env):
    set_entries(env, ["Dockerfile"])
    record_snapshot_private(env.state, env.snapshot_id)
    with pytest.raises(Conflict, match="not recorded public"):
        submit(env, _spec2(build_stage(env)))


# -- lineage: image_from ---------------------------------------------------------------


def consumer(env, **ref):
    reference = {"kind": "receipt_image", "stage": "s1"} | ref
    stage = {
        "key": "use",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image_from": reference,
        "argv": ["python", "-c", "print(1)"],
        "max_duration_seconds": 60,
        "inputs": [snapshot(env, name="code")],
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
    }
    return _spec2(stage)


REPORTED_DIGEST = "e" * 64
REPORTED = f"registry.example.org/team/app@sha256:{REPORTED_DIGEST}"


def mark_built(world, campaign, *, event=(), spec_build=True):
    """Make the finished campaign's stage a build stage whose worker reported ``event``.

    Nothing is written into the receipt by hand: the stored spec gets a real build stage, the
    attempt's operation gets the ``execution.image_built`` event the report endpoint records
    (a dict in ``event`` overrides its fields; ``None`` records none), and the terminal receipt is
    rebuilt by ``build_receipt`` exactly as the platform builds it."""
    from tessera_control.campaign_receipts import build_receipt
    from tessera_control.image_build_report import EVENT_TYPE
    from tessera_control.store import _connect

    with _connect(world.env.state / "tessera.sqlite3") as db:
        spec = json.loads(
            db.execute("SELECT spec_json FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()[
                0
            ]
        )
        if spec_build:  # the world's campaigns are version 1; a build is a version 2 stage
            version_two = _spec2(
                build_stage(
                    world.env,
                    key="s1",
                    inputs=[snapshot(world.env, name="code")],
                    build={
                        "context_input": "code",
                        "dockerfile": "Dockerfile",
                        "base_images": [BASE],
                        "repository": "registry.example.org/team/app",
                    },
                )
            )
            spec = version_two | {
                k: spec[k] for k in ("budget_cap_cents", "deadline_seconds") if k in spec
            }
        db.execute(
            "UPDATE campaigns SET spec_json=? WHERE id=?", (json.dumps(spec), campaign["id"])
        )
        operation_id = f"op-{campaign['id']}"
        db.execute(
            "UPDATE campaign_stage_attempts SET operation_id=? WHERE campaign_id=?",
            (operation_id, campaign["id"]),
        )
        if event is not None:
            body = {
                "operation_id": operation_id,
                "image": REPORTED,
                "digest": REPORTED_DIGEST,  # the worker row's bare hex, as recorded
                "size_bytes": 4096,
                "pushed": True,
                "base_images": [BASE],
                "via_control": False,
            } | dict(event)
            world.env.store._event(
                db,
                world.env.worker,
                world.env.project["id"],
                None,
                EVENT_TYPE,
                1,
                body,
            )
        db.execute("DROP TRIGGER campaign_receipts_no_update")
        row = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        receipt, digest = build_receipt(db, row, "succeeded")
        db.execute(
            "UPDATE campaign_receipts SET receipt_json=?, receipt_digest=? WHERE campaign_id=?",
            (json.dumps(receipt), digest, campaign["id"]),
        )
    return digest


@pytest.fixture
def chained(tmp_path):
    world = World(tmp_path)
    first, _row = _finished(world)
    world.env.trust()
    worker.register_from_config(registered(world.env))
    try:
        yield world, first
    finally:
        worker.register_from_config(None)


def submit_world(world, spec, predecessors=None, key="k"):
    body = {"spec": spec}
    if predecessors is not None:
        body["predecessors"] = predecessors
    env = world.env
    return env.store.submit_campaign(
        env.owner, env.project["id"], key, canonical_digest(body), body
    )[1]


def test_image_from_needs_exactly_one_source_and_one_image_form(chained):
    world, first = chained
    for ref in ({}, {"campaign_id": "a", "predecessor": "p"}):
        with pytest.raises(ValidationError):
            parse_spec_json(json.dumps(consumer(world.env, **ref)))
    both = consumer(world.env, campaign_id="a")
    both["stages"][0]["image"] = BASE
    with pytest.raises(ValidationError, match="exactly one of image and image_from"):
        parse_spec_json(json.dumps(both))
    neither = consumer(world.env, campaign_id="a")
    del neither["stages"][0]["image_from"]
    with pytest.raises(ValidationError, match="exactly one of image and image_from"):
        parse_spec_json(json.dumps(neither))


def test_submit_resolves_the_built_image_and_records_lineage(chained):
    world, first = chained
    receipt_digest = mark_built(world, first)
    created = submit_world(world, consumer(world.env, predecessor="img"), {"img": first["id"]})
    stage = created["spec"]["stages"][0]
    assert stage["image"] == REPORTED and "image_from" not in stage
    assert stage["image_resolved_from"] == {
        "campaign_id": first["id"],
        "stage": "s1",
        "source_receipt_sha256": receipt_digest,
    }
    stored = world.row("SELECT spec_json FROM campaigns WHERE id=?", created["id"])["spec_json"]
    assert "image_from" not in stored and "receipt_image" not in stored
    parse_spec_json(stored)
    # By campaign id as well.
    by_id = submit_world(world, consumer(world.env, campaign_id=first["id"]), key="k2")
    assert by_id["spec"]["stages"][0]["image"] == REPORTED


def test_a_stored_spec_never_holds_an_unresolved_reference(chained):
    world, first = chained
    raw = consumer(world.env, campaign_id=first["id"])
    parse_spec(raw)  # a request may carry it
    with pytest.raises(ValueError, match="unresolved image_from"):
        parse_spec_json(json.dumps(raw))


def test_unused_predecessors_are_still_refused(chained):
    world, first = chained
    mark_built(world, first)
    spec = consumer(world.env, campaign_id=first["id"])
    with pytest.raises(Conflict, match="no input references one"):
        submit_world(world, spec, {"img": first["id"]})


def test_an_unreferenced_predecessor_is_refused_even_when_another_is_used(chained):
    world, first = chained
    mark_built(world, first)
    spec = consumer(world.env, predecessor="img")
    with pytest.raises(Conflict, match="no input references one"):
        submit_world(world, spec, {"img": first["id"], "idle": first["id"]})
    assert submit_world(world, spec, {"img": first["id"]})["spec"]["stages"][0]["image"] == REPORTED


def test_the_resolved_image_is_the_one_the_workers_event_reported(chained):
    """The receipt is built by ``build_receipt`` from the recorded event; the reference resolves
    through the succeeded attempt's ``build`` entry, with the digest as ``sha256:<hex>``."""
    world, first = chained
    mark_built(world, first)
    with sqlite3.connect(world.env.state / "tessera.sqlite3") as db:
        receipt = json.loads(
            db.execute(
                "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (first["id"],)
            ).fetchone()[0]
        )
    (attempt,) = receipt["stages"][0]["attempts"]
    assert "build" not in receipt["stages"][0]
    assert attempt["build"]["evidence"] == "provider_reported"
    assert attempt["build"]["digest"] == f"sha256:{REPORTED_DIGEST}"
    assert attempt["build"]["produced_image"] == REPORTED


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ({"spec_build": False}, "not an image-build stage"),
        ({"event": None}, "no provider-reported produced image"),
        ({"event": {"digest": "f" * 64}}, "no provider-reported produced image"),
        (
            {"event": {"image": "registry.example.org/team/other@sha256:" + "e" * 64}},
            "no provider-reported produced image",
        ),
        ({"event": {"size_bytes": "big"}}, "no provider-reported produced image"),
    ],
)
def test_a_receipt_that_does_not_prove_the_build_is_refused(chained, setup, match):
    world, first = chained
    mark_built(world, first, **setup)
    with pytest.raises(Conflict, match=match):
        submit_world(world, consumer(world.env, campaign_id=first["id"]))


def test_a_predecessor_that_has_not_succeeded_is_refused(chained):
    world, first = chained
    mark_built(world, first)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1")]
    pending = world.campaign(spec)
    with pytest.raises(Conflict, match="not succeeded"):
        submit_world(world, consumer(world.env, campaign_id=pending["id"]))


def test_a_predecessor_of_another_project_is_refused(chained):
    world, first = chained
    mark_built(world, first)
    with sqlite3.connect(world.env.state / "tessera.sqlite3") as db:
        db.execute("UPDATE campaigns SET project_id='other' WHERE id=?", (first["id"],))
    with pytest.raises(Conflict, match="no such campaign in this project"):
        submit_world(world, consumer(world.env, campaign_id=first["id"]))


def test_a_submitted_image_resolved_from_is_refused(chained):
    world, first = chained
    spec = consumer(world.env, campaign_id=first["id"])
    del spec["stages"][0]["image_from"]
    spec["stages"][0]["image"] = REPORTED
    spec["stages"][0]["image_resolved_from"] = {
        "campaign_id": first["id"],
        "stage": "s1",
        "source_receipt_sha256": "a" * 64,
    }
    with pytest.raises(Conflict, match="set by submit only"):
        submit_world(world, spec)


def test_validate_resolves_read_only(chained):
    world, first = chained
    mark_built(world, first)
    env = world.env
    body = {"spec": consumer(env, campaign_id=first["id"])}
    before = world.row("SELECT COUNT(*) AS n FROM campaigns")["n"]
    result = env.store.validate_campaign_spec(env.owner, env.project["id"], body)
    assert result["valid"] is True
    assert world.row("SELECT COUNT(*) AS n FROM campaigns")["n"] == before


def test_a_standing_approval_or_approved_submission_refuses_image_from(chained):
    from datetime import UTC, datetime, timedelta

    from tessera_control.campaign_submissions import CampaignSubmissionStore

    world, first = chained
    mark_built(world, first)
    env = world.env
    raw = consumer(env, campaign_id=first["id"])
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    standing = {
        "spec": raw,
        "expected_spec_digest": "0" * 64,
        "per_start_budget_cap_cents": 5,
        "max_starts": 3,
        "max_total_budget_cents": 15,
        "not_after": later,
    }
    with pytest.raises(Conflict, match="standing approval cannot use image_from"):
        env.store.create_standing_approval(
            env.human(), env.project["id"], "s1", canonical_digest(standing), standing
        )
    approval = {
        "kind": "submit",
        "spec": raw,
        "expected_spec_digest": "0" * 64,
        "per_run_budget_cap_cents": 8,
        "not_after": later,
    }
    store = CampaignSubmissionStore(env.state)
    with pytest.raises(Conflict, match="submission cannot use image_from"):
        store.create_submission_approval(
            env.human(), env.project["id"], "s2", canonical_digest(approval), approval
        )
