"""The private_derived release class: local CPU only, never egress or provider tickets."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from campaign_fixture import Env
from test_execution import _grant_payload, _task

from tessera_control.provider_inputs import ProviderInputStore
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.store import Forbidden, Store, StoreError, _connect, canonical_digest

SRC = Path(__file__).parent.parent / "src" / "tessera_control"
EGRESS = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "example.com", "port": 443}],
    "max_egress_bytes": 1000,
}


def _private_release(env: Env) -> dict:
    """Replace the public release of the fixture snapshot with a private_derived one."""
    releases = SnapshotReleaseStore(env.state)
    revision = env.query("SELECT revision FROM snapshot_releases")[0]["revision"]
    releases.revoke_snapshot_release(env.owner, env.release_id, "revoke-public", "r", revision)
    payload = {
        "expected_digest": env.snapshot_digest,
        "worker_principal_id": env.worker["id"],
        "classification": "private_derived",
        "permits_worker_transfer": True,
        "rationale": "Synthetic private-derived release.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    status, release = releases.release_snapshot(
        env.owner, env.snapshot_id, "release-private", canonical_digest(payload), payload
    )
    assert status == 201 and release["classification"] == "private_derived"
    return release


def _plan_task(env: Env, name: str, egress: dict | None) -> dict:
    task = _task(env.state, env.owner, env.project["id"], env.snapshot_digest, name)
    if egress is None:
        return task
    raise AssertionError("egress tasks are built by _egress_task")


def _egress_task(env: Env) -> dict:
    store = Store(env.state)
    _, task = store.create_task(
        env.owner,
        env.project["id"],
        "egress-task",
        canonical_digest({"n": "egress"}),
        {
            "title": "egress",
            "purpose": "egress plan",
            "budget_cents": 100,
            "max_duration_seconds": 60,
        },
    )
    revision = {
        "expected_revision": task["revision"],
        "title": "egress",
        "purpose": "egress plan",
        "budget_cents": 100,
        "max_duration_seconds": 60,
        "execution_spec": {
            "argv": ["python", "-c", "print(1)"],
            "input_snapshot_digest": env.snapshot_digest,
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1024,
            "egress": EGRESS,
        },
    }
    _, revised = store.revise_task_plan(
        env.owner, task["id"], "egress-plan", canonical_digest(revision), revision
    )
    _, approved = store.approve_task(
        env.owner,
        revised["id"],
        "egress-approve",
        canonical_digest({"r": 1}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    return approved


def test_private_derived_release_serves_a_local_cpu_grant_and_lease(tmp_path):
    env = Env(tmp_path)
    release = _private_release(env)
    task = _plan_task(env, "private", None)
    payload = _grant_payload(task, env.worker, {"id": env.snapshot_id})
    status, grant = env.execution.issue_execution_grant(
        env.owner, env.project["id"], "grant", canonical_digest(payload), payload
    )
    assert status == 201 and grant["snapshot_release_id"] == release["id"]
    status, lease = env.execution.acquire_execution_lease(env.worker, grant["id"], "lease", "lease")
    assert status in {200, 201} and lease["grant_id"] == grant["id"]


def test_private_derived_release_is_refused_under_the_default_and_for_unbound_workers(tmp_path):
    """ADR 0040 section 4: no principal is bound by default, so a local CPU grant gets
    public releases only; a principal listed under another provider is unbound here."""
    from provider_trust_fixture import DERIVED, install_trust

    from tessera_control.provider_trust import reset_policy

    env = Env(tmp_path)  # installs a trust fixture binding the worker to local-docker
    _private_release(env)
    task = _plan_task(env, "private", None)
    payload = _grant_payload(task, env.worker, {"id": env.snapshot_id})

    def issue(key):
        return env.execution.issue_execution_grant(
            env.owner, env.project["id"], key, canonical_digest(payload), payload
        )

    reset_policy()
    with pytest.raises(Forbidden, match="not eligible"):
        issue("default")
    # Bound to another provider's instance still means: not bound for local grants' class.
    install_trust({"salad": (DERIVED, ["someone-else"])})
    with pytest.raises(Forbidden, match="not eligible"):
        issue("unbound")
    assert env.query("SELECT count(*) AS n FROM execution_grants")[0]["n"] == 0
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    assert issue("bound")[0] == 201


def test_private_derived_release_is_refused_for_an_egress_plan(tmp_path):
    # Control: the same egress plan is grantable against a public release.
    (tmp_path / "control").mkdir(mode=0o700)
    (tmp_path / "private").mkdir(mode=0o700)
    control = Env(tmp_path / "control")
    task = _egress_task(control)
    payload = _grant_payload(task, control.worker, {"id": control.snapshot_id})
    status, _ = control.execution.issue_execution_grant(
        control.owner, control.project["id"], "grant-public", canonical_digest(payload), payload
    )
    assert status == 201
    env = Env(tmp_path / "private")
    task = _egress_task(env)
    _private_release(env)
    payload = _grant_payload(task, env.worker, {"id": env.snapshot_id})
    with pytest.raises(Forbidden, match="not eligible"):
        env.execution.issue_execution_grant(
            env.owner, env.project["id"], "grant-private", canonical_digest(payload), payload
        )
    assert env.query("SELECT count(*) AS n FROM execution_grants")[0]["n"] == 0


def test_provider_input_tickets_keep_requiring_a_public_release(tmp_path):
    env = Env(tmp_path)
    tickets = ProviderInputStore(env.state)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        worker = Store._live_principal(
            connection, {"id": env.worker["id"], "org_id": env.owner["org_id"]}, audience="worker"
        )
        public = tickets._authorize_snapshot(
            connection, worker, env.snapshot_id, worker["id"], env.snapshot_digest
        )
        assert public["classification"] == "public"
    finally:
        connection.close()
    _private_release(env)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        worker = Store._live_principal(
            connection, {"id": env.worker["id"], "org_id": env.owner["org_id"]}, audience="worker"
        )
        with pytest.raises(Forbidden):
            tickets._authorize_snapshot(
                connection, worker, env.snapshot_id, worker["id"], env.snapshot_digest
            )
    finally:
        connection.close()
    # Neither provider-ticket call site opts in to the private class.
    assert "allow_private_derived" not in (SRC / "provider_inputs.py").read_text()


def test_release_classification_is_closed(tmp_path):
    env = Env(tmp_path)
    releases = SnapshotReleaseStore(env.state)
    revision = env.query("SELECT revision FROM snapshot_releases")[0]["revision"]
    releases.revoke_snapshot_release(env.owner, env.release_id, "revoke", "r", revision)
    payload = {
        "expected_digest": env.snapshot_digest,
        "worker_principal_id": env.worker["id"],
        "classification": "secret",
        "permits_worker_transfer": True,
        "rationale": "x",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    with pytest.raises(StoreError):
        releases.release_snapshot(
            env.owner, env.snapshot_id, "bad", canonical_digest(payload), payload
        )
    with pytest.raises(sqlite3.IntegrityError):
        env.execute("UPDATE snapshot_releases SET classification='secret'")


def test_low_level_authorize_refuses_private_derived_by_default(tmp_path):
    from tessera_control.snapshot_releases import authorize_snapshot

    env = Env(tmp_path)
    _private_release(env)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        worker = Store._live_principal(
            connection, {"id": env.worker["id"], "org_id": env.owner["org_id"]}, audience="worker"
        )
        args = (connection, worker, env.snapshot_id, worker["id"], env.snapshot_digest)
        with pytest.raises(Forbidden):
            authorize_snapshot(*args)
        release = authorize_snapshot(*args, allow_private_derived=True)
        assert release["classification"] == "private_derived"
    finally:
        connection.close()
