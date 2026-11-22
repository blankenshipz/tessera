"""A stage's code as a pinned input (ADR 0095): the optional ``code_input`` field.

The field names one pinned snapshot input of the stage; it changes no digest when absent, is
refused when it cannot be a content-addressed bundle, and survives the Salad stage adapter.
Nothing here runs a provider or touches a network.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from campaign_fixture import Env
from salad_campaign_fixture import IMAGE, salad_provider_reset, salad_spec_v2  # noqa: F401
from test_salad_stage_adapter import cpu_world, near  # noqa: F401
from test_salad_stage_adapter import spec_of as adapter_spec_of

from tessera_control.campaign_models import (
    normalize,
    parse_spec,
    parse_spec_json,
    spec_digest,
    stage_on_candidate,
)
from tessera_control.store import Conflict, canonical_digest


def snapshot_input(env, name="code"):
    return {
        "kind": "snapshot",
        "name": name,
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }


def stage(env, **over):
    value = {
        "key": "s1",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "/inputs/main.py"],
        "max_duration_seconds": 120,
        "max_exposure_cents": 0,
        "inputs": [snapshot_input(env)],
        "outputs": [{"name": "out", "path": "out.json", "max_bytes": 1024}],
    }
    value.update(over)
    return value


@pytest.fixture
def env(tmp_path):
    from tessera_control import campaign_provider_worker as worker

    env = Env(tmp_path)
    # The built-in local-docker provider needs the operator's worker principal to admit a stage.
    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    yield env
    worker.register_from_config(None)


def spec_of(env, **over):
    return salad_spec_v2(env, [stage(env, **over)])


def submit(env, spec):
    body = {"spec": spec}
    return env.store.submit_campaign(
        env.owner, env.project["id"], "k", canonical_digest(body), body
    )


def test_an_existing_spec_keeps_its_digest_and_omits_the_field(env):
    plain = parse_spec(spec_of(env))
    assert "code_input" not in json.dumps(plain.model_dump(mode="json"))
    assert normalize(plain).stages[0].code_input is None
    with_field = parse_spec(spec_of(env, code_input="code"))
    assert spec_digest(with_field) != spec_digest(plain)
    assert with_field.model_dump(mode="json")["stages"][0]["code_input"] == "code"


def test_the_field_reaches_the_normal_stage_and_every_candidate(env):
    # Structural parse only: the alternate need not be registered to read the field's path.
    spec = parse_spec_json(
        json.dumps(
            spec_of(
                env,
                code_input="code",
                alternate_providers=[{"provider": "self-hosted", "runtime": "docker-cpu-v1"}],
            )
        )
    )
    normal = normalize(spec).stages[0]
    assert normal.code_input == "code"
    assert stage_on_candidate(normal, "self-hosted").code_input == "code"


@pytest.mark.parametrize(
    ("inputs_extra", "name", "match"),
    [
        ([], "nope", "names no input of the stage"),
        (
            [{"kind": "stage", "name": "up", "stage": "s0", "outputs": ["out"]}],
            "up",
            "must be a pinned snapshot input, not a stage input",
        ),
        (
            [
                {
                    "kind": "artifact",
                    "name": "art",
                    "campaign_id": "c1",
                    "stage": "s0",
                    "output": "out",
                    "sha256": "a" * 64,
                }
            ],
            "art",
            "must be a pinned snapshot input, not a artifact input",
        ),
    ],
)
def test_a_code_input_that_cannot_be_a_pinned_bundle_is_refused(env, inputs_extra, name, match):
    inputs = [snapshot_input(env), *inputs_extra]
    spec = salad_spec_v2(
        env,
        [
            stage(env, key="s0"),
            stage(env, key="s1", inputs=inputs, code_input=name),
        ],
    )
    with pytest.raises(ValueError, match=match):
        parse_spec(spec)


def test_a_snapshot_without_files_cannot_carry_code(env):
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        manifest = json.loads(
            db.execute(
                "SELECT manifest_json FROM source_snapshots WHERE id=?", (env.snapshot_id,)
            ).fetchone()[0]
        )
        manifest["entries"] = []
        db.execute(
            "UPDATE source_snapshots SET manifest_json=? WHERE id=?",
            (json.dumps(manifest), env.snapshot_id),
        )
    with pytest.raises(Conflict, match="holds no files"):
        submit(env, spec_of(env, code_input="code"))
    # The same spec without the field is not affected by this check.
    assert "holds no files" not in _refusal(env, spec_of(env))


def _refusal(env, spec) -> str:
    try:
        submit(env, spec)
    except Conflict as error:
        return str(error)
    return ""


def test_a_code_approval_that_no_longer_matches_is_refused(env):
    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        db.execute(
            """INSERT INTO snapshot_code_approvals (snapshot_id, files_json,
            actor_principal_id, decision_class, provenance_json, recorded_at)
            VALUES (?, '[{"relative_path": "main.py", "sha256": "0", "size": 1}]', ?,
            'human_session', '{}', 't')""",
            (env.snapshot_id, env.owner["id"]),
        )
    with pytest.raises(Conflict, match="code approval that does not match its files"):
        submit(env, spec_of(env, code_input="code"))


def test_a_valid_spec_with_a_code_input_is_accepted_at_submit(env):
    status, body = submit(env, spec_of(env, code_input="code"))
    assert status == 201
    assert body["spec"]["stages"][0]["code_input"] == "code"


def test_the_stage_adapter_keeps_the_code_input(tmp_path, near):  # noqa: F811
    # A real operator profile, so the expansion succeeds; the assertions read the expanded stage.
    from tessera_control.campaign_salad_adapter import expand_spec

    world = cpu_world(tmp_path, near)
    spec = parse_spec(adapter_spec_of(world, code_input="code"))
    result = expand_spec(spec, world.profiles, tenant=None)
    assert result is not spec
    assert len(result.stages[0].alternate_providers) == 1
    assert result.stages[0].alternate_providers[0].provider_config["adapted"] is True
    assert result.stages[0].code_input == "code"
    (code,) = [i for i in result.stages[0].inputs if i.name == "code"]
    assert code.kind == "snapshot"
    assert code.snapshot_id == world.env.snapshot_id
    assert code.digest == world.env.snapshot_digest
