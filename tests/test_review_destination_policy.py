"""Operator-attested review destination policy (ADR 0084, ADR 0047 amendment). Synthetic only:
no model or provider is called."""

import json
import os
import sqlite3
import sys
import types
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient
from test_review_jobs import job, world  # noqa: F401

from tessera_control import cli
from tessera_control.api import create_app
from tessera_control.review_destination_policy import (
    ENVIRONMENT_VARIABLE,
    SCHEMA,
    EnvironmentReviewDestinationPolicy,
    ReviewDestinationPolicy,
    ReviewDestinationsRefused,
    load_review_destination_policy,
    policy_from_document,
)
from tessera_control.review_jobs import REFUSAL_REASONS, NoDestinationsPolicy


def document(**entry):
    base = {"kind": "model_api", "data_classes": ["public", "private_derived"]}
    return {"schema": SCHEMA, "destinations": {"anthropic": {**base, **entry}}}


def write_policy(path, value, mode=0o600):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    os.chmod(path, mode)
    return path


# --- check -------------------------------------------------------------------------------


def test_admits_listed_classes_and_refuses_unlisted_with_existing_codes():
    policy = policy_from_document(document(tiers=["sonnet", "opus"], max_budget_cents=500))
    assert policy.check("anthropic", "sonnet", ["private_derived"], 100) is None
    assert policy.check("anthropic", "opus", ["public", "private_derived"], 500) is None
    refusals = [
        policy.check("other", "sonnet", ["public"], 1),
        policy.check("anthropic", "haiku", ["public"], 1),
        policy.check("anthropic", "sonnet", ["private_code_approved"], 1),
        policy.check("anthropic", "sonnet", ["public", "private_code_approved"], 1),
        policy.check("anthropic", "sonnet", ["public"], 501),
    ]
    assert refusals == [
        "destination_not_enrolled",
        "tier_exceeds_policy",
        "data_class_not_allowed_for_destination",
        "data_class_not_allowed_for_destination",
        "budget_exceeds_policy",
    ]
    assert set(refusals) <= set(REFUSAL_REASONS)


def test_private_classes_need_explicit_listing():
    public_only = policy_from_document(document(data_classes=["public"]))
    assert public_only.check("anthropic", "sonnet", ["public"], 1) is None
    for private in ("private_derived", "private_code_approved"):
        assert (
            public_only.check("anthropic", "sonnet", [private], 1)
            == "data_class_not_allowed_for_destination"
        )


@pytest.mark.parametrize(
    "classes",
    [["secret"], ["PUBLIC"], [], "public", None, [None], ["public", 1]],
)
def test_unknown_missing_or_malformed_classes_are_refused(classes):
    policy = policy_from_document(document(data_classes=["public", "private_derived"]))
    assert policy.check("anthropic", "sonnet", classes, 1) == (
        "data_class_not_allowed_for_destination"
    )


def test_malformed_arguments_refuse_without_raising():
    policy = policy_from_document(document())
    assert policy.check(None, "sonnet", ["public"], 1) == "destination_not_enrolled"
    assert policy.check(["anthropic"], "sonnet", ["public"], 1) == "destination_not_enrolled"
    assert policy.check("anthropic", None, ["public"], 1) == "tier_exceeds_policy"
    for budget in (-1, True, 1.5, "1", None):
        assert policy.check("anthropic", "sonnet", ["public"], budget) == "budget_exceeds_policy"


def test_without_tiers_or_cap_only_classes_bind():
    policy = policy_from_document(document())
    for tier in ("haiku", "sonnet", "opus", "fable"):
        assert policy.check("anthropic", tier, ["public"], 10000) is None


def test_license_labels_restrictive_by_default():
    policy = policy_from_document(document())
    assert policy.license_labels_refusal("anthropic", []) is None
    assert policy.license_labels_refusal("anthropic", ["unlabelled"]) is None
    assert policy.license_labels_refusal("anthropic", ["non-commercial"]) == (
        "data_class_not_allowed_for_destination"
    )
    assert policy.license_labels_refusal("other", []) == "destination_not_enrolled"
    allowed = policy_from_document(document(allowed_license_labels=["non-commercial"]))
    assert allowed.license_labels_refusal("anthropic", ["non-commercial"]) is None
    # Listing labels replaces the default: unlabelled must then be listed too.
    assert allowed.license_labels_refusal("anthropic", ["unlabelled"]) is not None
    assert allowed.license_labels_refusal("anthropic", ["unknown-lineage"]) is not None


def test_digest_is_canonical():
    one = policy_from_document(document(data_classes=["private_derived", "public"]))
    two = policy_from_document(document(data_classes=["public", "private_derived"]))
    assert one.digest == two.digest and len(one.digest) == 64
    assert one.digest != policy_from_document(document(max_budget_cents=1)).digest
    assert one.show()["digest"] == one.digest


# --- document validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # Not registered at all (no public-internet or ad hoc destination can be named).
        {
            "schema": SCHEMA,
            "destinations": {"example-web": {"kind": "model_api", "data_classes": ["public"]}},
        },
        # Registered, but not reached through Tessera's own enforced model-runtime allowlist.
        {
            "schema": SCHEMA,
            "destinations": {"openai": {"kind": "model_api", "data_classes": ["public"]}},
        },
        {"schema": "tessera.review-destinations.v0", "destinations": {}},
        {"schema": SCHEMA},
        {"schema": SCHEMA, "destinations": {}, "mode": "open"},
        {"schema": SCHEMA, "destinations": []},
        [],
        document(kind="public_internet"),
        document(kind=None),
        document(data_classes=[]),
        document(data_classes=["public", "public"]),
        document(data_classes=["restricted"]),
        document(data_classes="public"),
        document(tiers=[]),
        document(tiers=["Sonnet"]),
        document(max_budget_cents=-1),
        document(max_budget_cents=10001),
        document(max_budget_cents=True),
        document(max_budget_cents=1.0),
        document(allowed_license_labels=["unknown-lineage"]),
        document(allowed_license_labels=["Not A Label"]),
        document(endpoint="https://example.invalid"),
    ],
)
def test_malformed_or_unregistered_documents_are_refused(value):
    with pytest.raises(ReviewDestinationsRefused):
        policy_from_document(value)


def test_missing_required_key_is_refused():
    value = document()
    del value["destinations"]["anthropic"]["data_classes"]
    with pytest.raises(ReviewDestinationsRefused):
        policy_from_document(value)


def test_empty_destinations_admit_nothing():
    policy = policy_from_document({"schema": SCHEMA, "destinations": {}})
    assert policy.check("anthropic", "sonnet", ["public"], 1) == "destination_not_enrolled"


# --- file checks ---------------------------------------------------------------------------


def test_loads_private_file(tmp_path):
    policy = load_review_destination_policy(write_policy(tmp_path / "d.json", document()))
    assert isinstance(policy, ReviewDestinationPolicy)
    assert policy.check("anthropic", "sonnet", ["private_derived"], 1) is None


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_group_or_world_writable_file_is_refused(tmp_path, mode):
    path = write_policy(tmp_path / "d.json", document(), mode)
    with pytest.raises(ReviewDestinationsRefused, match="writable"):
        load_review_destination_policy(path)


def test_symlink_directory_missing_foreign_oversized_and_bad_json_are_refused(
    tmp_path, monkeypatch
):
    real = write_policy(tmp_path / "real.json", document())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ReviewDestinationsRefused, match="symlink"):
        load_review_destination_policy(link)
    with pytest.raises(ReviewDestinationsRefused, match="regular file"):
        load_review_destination_policy(tmp_path)
    with pytest.raises(ReviewDestinationsRefused, match="cannot be read"):
        load_review_destination_policy(tmp_path / "missing.json")
    big = write_policy(tmp_path / "big.json", " " * (64 * 1024 + 1))
    with pytest.raises(ReviewDestinationsRefused, match="too large"):
        load_review_destination_policy(big)
    bad = write_policy(tmp_path / "bad.json", "{not json")
    with pytest.raises(ReviewDestinationsRefused, match="valid JSON"):
        load_review_destination_policy(bad)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ReviewDestinationsRefused, match="owned by the service user"):
        load_review_destination_policy(real)


def test_environment_policy_for_runner(tmp_path, monkeypatch):
    from tessera_control.code_review_runner import load_policy

    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    with pytest.raises(ReviewDestinationsRefused):
        EnvironmentReviewDestinationPolicy()
    path = write_policy(tmp_path / "d.json", document(data_classes=["public"]))
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, str(path))
    policy = load_policy(
        "tessera_control.review_destination_policy:EnvironmentReviewDestinationPolicy"
    )
    assert policy.check("anthropic", "sonnet", ["public"], 1) is None
    assert policy.check("anthropic", "sonnet", ["private_derived"], 1) is not None
    assert policy.digest == load_review_destination_policy(path).digest


# --- API and serve wiring ------------------------------------------------------------------


def _submit(client, w, body, key):
    return client.post(
        f"/projects/{w.project}/review-jobs",
        headers={**w.submitter, "Idempotency-Key": key},
        json=body,
    )


def test_default_without_flag_is_no_destinations(world):  # noqa: F811
    app = create_app(world.state)
    assert isinstance(app.state.review_policy, NoDestinationsPolicy)
    r = _submit(TestClient(app), world, job(destination_id="anthropic"), "k-default")
    assert r.status_code == 409 and r.json()["reason"] == "destination_not_enrolled"


def test_create_app_loads_file_and_policy_governs_submit(world, tmp_path):  # noqa: F811
    path = write_policy(tmp_path / "d.json", document(tiers=["sonnet"], max_budget_cents=200))
    app = create_app(world.state, review_destinations=path)
    assert isinstance(app.state.review_policy, ReviewDestinationPolicy)
    client = TestClient(app)
    # The fixture's input has no recorded class, so it is derived as private_derived.
    r = _submit(client, world, job(destination_id="anthropic"), "k-ok")
    assert r.status_code == 201, r.text
    assert r.json()["destination_id"] == "anthropic"
    assert r.json()["data_classes"] == ["private_derived"]
    for over, reason in [
        ({"destination_id": "dest-1"}, "destination_not_enrolled"),
        ({"tier": "opus"}, "tier_exceeds_policy"),
        ({"budget_cents": 201}, "budget_exceeds_policy"),
    ]:
        r = _submit(client, world, job(**{"destination_id": "anthropic", **over}), f"k-{reason}")
        assert r.status_code == 409 and r.json()["reason"] == reason
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 1


def test_private_input_refused_when_destination_lists_public_only(world, tmp_path):  # noqa: F811
    path = write_policy(tmp_path / "d.json", document(data_classes=["public"]))
    client = TestClient(create_app(world.state, review_destinations=path))
    r = _submit(client, world, job(destination_id="anthropic"), "k-private")
    assert r.status_code == 409
    assert r.json()["reason"] == "data_class_not_allowed_for_destination"


def test_create_app_fails_closed_on_refused_file(world, tmp_path):  # noqa: F811
    with pytest.raises(ValueError):
        create_app(world.state, review_destinations=tmp_path / "missing.json")
    bad = write_policy(tmp_path / "loose.json", document(), 0o666)
    with pytest.raises(ValueError):
        create_app(world.state, review_destinations=bad)


def _serve(monkeypatch, *flags):
    built = []

    def fake_create_app(state_dir, deployment=None, **kwargs):
        built.append(kwargs)
        return types.SimpleNamespace(state=types.SimpleNamespace())

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr("tessera_control.provider_trust.record_at_start", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: ran.append(True))
    monkeypatch.setattr(cli, "require_sqlite_runtime", lambda: None)
    monkeypatch.setattr(cli.maintenance_lock, "shared", lambda state_dir: nullcontext())
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--state-dir", "/x", "--port", "9", *flags]
    )
    return cli.main(), built, ran


def test_serve_without_flag_keeps_default(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    code, built, ran = _serve(monkeypatch)
    assert code == 0 and ran == [True]
    assert built[0]["review_destinations"] is None


def test_serve_flag_loads_policy(monkeypatch, tmp_path):
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    path = write_policy(tmp_path / "d.json", document())
    code, built, ran = _serve(monkeypatch, "--review-destinations", str(path))
    assert code == 0 and ran == [True]
    policy = built[0]["review_destinations"]
    assert policy.digest == load_review_destination_policy(path).digest


@pytest.mark.parametrize("via_env", [False, True])
def test_serve_refuses_to_start_on_bad_file(monkeypatch, tmp_path, capsys, via_env):
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    bad = write_policy(tmp_path / "d.json", document(data_classes=["restricted"]))
    if via_env:
        monkeypatch.setenv(ENVIRONMENT_VARIABLE, str(bad))
        flags = ()
    else:
        flags = ("--review-destinations", str(bad))
    code, built, ran = _serve(monkeypatch, *flags)
    assert code == 2 and built == [] and ran == []
    assert "serve configuration error: review destination" in capsys.readouterr().err


def test_serve_refuses_to_start_on_missing_file(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    code, built, ran = _serve(monkeypatch, "--review-destinations", str(tmp_path / "none.json"))
    assert code == 2 and built == [] and ran == []
    assert "review destinations file cannot be read" in capsys.readouterr().err
