"""The reasoning admission as a person's recorded delegation (ADR 0163).

The admission record lives on the control plane's receipts (events) and is made only by a person's
``human_session`` credential; the ``reasoning.admitted`` marker references it; the readiness
``admission`` check fails a revoked, expired, altered or exceeded record, and reports a bare
marker as ``unrecorded`` (never as a recorded admission). Synthetic only: the in-process control
plane, temporary state, no network, no keys.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import _owner

from tessera_agent import reasoning_install
from tessera_agent.reasoning_admission import (
    admission_check,
    marker_bytes,
    parse_marker,
    startup_refusal,
)
from tessera_agent.reasoning_admit_cli import run_admit, run_revoke
from tessera_agent.reasoning_install_cli import add_parser
from tessera_control.api import create_app
from tessera_control.reasoning_admissions import RECORDED, record_digest
from tessera_control.store import Store, initialize_state

MODEL = "claude-test-model"
DESTINATION = "lab"
CAPS = {"max_cost_cents_per_call": 10, "daily_cost_cap_cents": 50, "daily_token_cap": 100_000}


def later(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**(delta or {"days": 2}))).isoformat()


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        initialize_state(self.state)
        human = _owner(self.state)
        header = (self.state / "owner.header").read_text().strip().split(": ", 1)[1]
        self.owner = {"Authorization": header}
        self.project = Store(self.state).create_project(
            human, "project", "project", {"name": "bounded"}
        )[1]["id"]
        self.client = TestClient(create_app(self.state))
        self.session_token = "synthetic-person-session-" + secrets.token_hex(16)
        self.post(
            self.owner,
            "/credentials",
            {
                "token": self.session_token,
                "kind": "human_session",
                "audience": "control",
                "expires_at": later(hours=3),
            },
        )
        self.person = {"Authorization": "Bearer " + self.session_token}
        self.agent_token = "synthetic-reasoning-agent-" + secrets.token_hex(16)
        self.post(
            self.owner,
            "/credentials",
            {
                "token": self.agent_token,
                "kind": "agent",
                "audience": "control",
                "project_id": self.project,
                "expires_at": later(hours=3),
                "external_agent": {
                    "destination": DESTINATION,
                    "actions": ["authority:consume", "campaign:read", "model_spend:record"],
                },
            },
        )
        self.agent = {
            "Authorization": "Bearer " + self.agent_token,
            "X-Agent-Destination": DESTINATION,
        }
        self.config = tmp_path / "root" / "etc" / "tessera"
        self.config.mkdir(parents=True, mode=0o755)
        os.chmod(tmp_path / "root", 0o755)
        os.chmod(tmp_path / "root" / "etc", 0o755)
        self.marker = self.config / "reasoning.admitted"

    def post(self, headers, path, body, key=None):
        response = self.client.post(
            path, json=body, headers={**headers, "Idempotency-Key": key or secrets.token_hex(8)}
        )
        assert response.status_code in (200, 201), response.text
        return response.json()

    def body(self, **over):
        body = {
            "provider_id": "anthropic",
            "model": MODEL,
            "caps": dict(CAPS),
            "expires_at": later(),
        }
        return body | over

    def admit(self, headers=None, key=None, **over):
        return self.client.post(
            f"/projects/{self.project}/reasoning-admissions",
            json=self.body(**over),
            headers={**(headers or self.person), "Idempotency-Key": key or secrets.token_hex(8)},
        )

    def revoke(self, admission_id, headers=None, revision=1):
        return self.client.post(
            f"/projects/{self.project}/reasoning-admissions/{admission_id}/revoke",
            json={"expected_revision": revision},
            headers={**(headers or self.person), "Idempotency-Key": secrets.token_hex(8)},
        )

    def recorded(self) -> int:
        with sqlite3.connect(self.state / "tessera.sqlite3") as db:
            return db.execute(
                "SELECT count(*) FROM events WHERE event_type=?", (RECORDED,)
            ).fetchone()[0]

    def profile(self, **over):
        profile = {
            "model": {"providerID": "anthropic", "id": MODEL},
            "limits": dict(CAPS),
            "model_spend": {"project_id": self.project},
        }
        return profile | over

    def check(self, profile=None, control=True, **kwargs):
        return admission_check(
            self.marker,
            profile or self.profile(),
            self.state if control else None,
            self.project,
            **kwargs,
        )

    def write_marker(self, admission):
        self.marker.write_bytes(marker_bytes(admission))
        os.chmod(self.marker, 0o600)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def code(result):
    assert result["name"] == "admission", result
    return result["detail"].split(":", 1)[0]


# -- the record: only a person makes it -------------------------------------------------------


def test_a_person_records_an_admission_on_receipts(world):
    response = world.admit(caps=CAPS | {"max_calls": 3})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["id"].startswith("radm_")
    assert record["decision_class"] == "human_session"
    assert record["state"] == "active" and record["status"] == "active" and record["integrity"]
    assert record["caps"]["max_calls"] == 3 and record["provider_id"] == "anthropic"
    assert record["admitted_by_principal_id"]
    with sqlite3.connect(world.state / "tessera.sqlite3") as db:
        (payload,) = db.execute(
            "SELECT payload_json FROM events WHERE event_type=?", (RECORDED,)
        ).fetchone()
    stored = json.loads(payload)
    assert stored["record_digest"] == record_digest(stored["record"]) == record["record_digest"]
    assert stored["decision_class"] == "human_session" and stored["automation"] is False
    got = world.client.get(
        f"/projects/{world.project}/reasoning-admissions/{record['id']}", headers=world.person
    )
    assert got.status_code == 200 and got.json()["record_digest"] == record["record_digest"]
    listed = world.client.get(
        f"/projects/{world.project}/reasoning-admissions", headers=world.agent
    )
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()["reasoning_admissions"]] == [record["id"]]


def test_a_retry_with_the_same_key_replays_the_same_record(world):
    body = world.body()
    first = world.client.post(
        f"/projects/{world.project}/reasoning-admissions",
        json=body,
        headers={**world.person, "Idempotency-Key": "same"},
    )
    second = world.client.post(
        f"/projects/{world.project}/reasoning-admissions",
        json=body,
        headers={**world.person, "Idempotency-Key": "same"},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert world.recorded() == 1


@pytest.mark.parametrize("who", ["owner", "labelled_owner", "agent"])
def test_no_agent_or_automation_identity_can_admit(world, who):
    headers = {
        "owner": world.owner,
        "labelled_owner": {**world.owner, "X-Tessera-Automation": "ops-script"},
        "agent": world.agent,
    }[who]
    response = world.admit(headers=headers)
    assert response.status_code == 403, response.text
    assert world.recorded() == 0


@pytest.mark.parametrize(
    "over",
    [
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"expires_at": later(days=31)},
        {"expires_at": "2030-01-01T00:00:00"},
        {"caps": CAPS | {"max_cost_cents_per_call": 60}},
        {"caps": CAPS | {"daily_token_cap": 0}},
        {"model": "anthropic/" + MODEL},
    ],
)
def test_bad_bounds_are_refused_and_nothing_is_recorded(world, over):
    response = world.admit(**over)
    assert 400 <= response.status_code < 500 and response.status_code != 403, response.text
    assert world.recorded() == 0


def test_an_unknown_project_is_not_found(world):
    response = world.client.post(
        "/projects/proj_missing/reasoning-admissions",
        json=world.body(),
        headers={**world.person, "Idempotency-Key": "x"},
    )
    assert response.status_code == 404


def test_revocation_is_human_only_once_and_at_the_current_revision(world):
    record = world.admit().json()
    assert world.revoke(record["id"], headers=world.agent).status_code == 403
    assert world.revoke(record["id"], revision=2).status_code == 409
    # Revocation only reduces what may run: the owner token may revoke.
    revoked = world.revoke(record["id"], headers=world.owner)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "revoked" and revoked.json()["revision"] == 2
    assert world.revoke(record["id"], revision=2).status_code == 409
    assert world.revoke("radm_missing").status_code == 404


# -- the readiness check ------------------------------------------------------------------------


def test_no_marker_is_a_warning_not_a_failure(world):
    result = world.check()
    assert result["status"] == "warn" and code(result) == "not_admitted"


@pytest.mark.parametrize("content", [b"", b"admitted by hand\n"])
def test_a_bare_marker_still_starts_the_unit_but_is_reported_unrecorded(world, content):
    world.marker.write_bytes(content)
    os.chmod(world.marker, 0o600)
    result = world.check()
    assert result["status"] == "warn" and code(result) == "unrecorded"
    assert result["status"] != "ok"
    assert startup_refusal(content) is None


def test_a_recorded_active_admission_is_ok_and_the_database_is_untouched(world):
    record = world.admit().json()
    world.write_marker(record)
    before = (world.state / "tessera.sqlite3").read_bytes()
    result = world.check()
    assert result["status"] == "ok", result
    assert record["id"] in result["detail"] and "human_session" in result["detail"]
    assert (world.state / "tessera.sqlite3").read_bytes() == before


def test_a_revoked_admission_fails(world):
    record = world.admit().json()
    world.write_marker(record)
    assert world.revoke(record["id"]).status_code == 200
    result = world.check()
    assert result["status"] == "fail" and code(result) == "admission_revoked"
    assert result["fix"]


def test_an_expired_admission_fails_with_or_without_the_record(world):
    record = world.admit().json()
    world.write_marker(record)
    future = datetime.now(UTC) + timedelta(days=3)
    assert code(world.check(now=future)) == "admission_expired"
    assert code(world.check(control=False, now=future)) == "admission_expired"
    assert startup_refusal(world.marker.read_bytes(), now=future) == "admission_expired"
    assert startup_refusal(world.marker.read_bytes()) is None


def test_a_recorded_marker_without_the_control_database_is_unchecked(world):
    world.write_marker(world.admit().json())
    result = world.check(control=False)
    assert result["status"] == "unchecked" and code(result) == "record_unchecked"


def test_an_altered_record_or_marker_fails(world):
    record = world.admit().json()
    world.write_marker(record)
    # The marker names a different digest.
    tampered = json.loads(world.marker.read_bytes())
    tampered["record_digest"] = "0" * 64
    world.marker.write_bytes(json.dumps(tampered).encode())
    assert code(world.check()) == "admission_digest_mismatch"
    # The stored record is edited after signing (caps raised): its digest no longer matches.
    world.write_marker(record)
    with sqlite3.connect(world.state / "tessera.sqlite3") as db:
        (cursor, payload) = db.execute(
            "SELECT cursor, payload_json FROM events WHERE event_type=?", (RECORDED,)
        ).fetchone()
        stored = json.loads(payload)
        stored["record"]["caps"]["daily_cost_cap_cents"] = 10_000
        db.execute("UPDATE events SET payload_json=? WHERE cursor=?", (json.dumps(stored), cursor))
    assert code(world.check()) == "admission_record_invalid"


def test_a_marker_naming_no_record_fails(world):
    record = world.admit().json() | {"id": "radm_" + "0" * 32}
    world.write_marker(record)
    assert code(world.check()) == "admission_record_missing"


def test_scope_and_caps_must_cover_the_profile(world):
    world.write_marker(world.admit().json())
    other_model = world.profile(model={"providerID": "anthropic", "id": "other-model"})
    assert code(world.check(other_model)) == "admission_scope_mismatch"
    over = world.profile(limits=CAPS | {"daily_cost_cap_cents": 51})
    result = world.check(over)
    assert code(result) == "admission_caps_exceeded" and "daily_cost_cap_cents" in result["detail"]
    lower = world.profile(limits=CAPS | {"max_cost_cents_per_call": 1})
    assert world.check(lower)["status"] == "ok"
    elsewhere = admission_check(world.marker, world.profile(), world.state, "proj_other")
    assert code(elsewhere) == "admission_scope_mismatch"


def test_max_calls_counts_the_ledger_since_the_admission(world):
    record = world.admit(caps=CAPS | {"max_calls": 1}).json()
    world.write_marker(record)
    assert world.check()["status"] == "ok"
    with sqlite3.connect(world.state / "tessera.sqlite3") as db:
        org, principal = db.execute("SELECT org_id, id FROM principals LIMIT 1").fetchone()
        db.execute(
            """INSERT INTO model_spend_calls (id, org_id, project_id, run_id, seq, provider_id,
            model, status, utc_day, reserved_micro, actor_principal_id, created_at)
            VALUES ('c1', ?, ?, 'r', 1, 'anthropic', ?, 'settled', ?, 1, ?, ?)""",
            (
                org,
                world.project,
                "anthropic/" + MODEL,
                datetime.now(UTC).date().isoformat(),
                principal,
                (datetime.now(UTC) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            ),
        )
    assert code(world.check()) == "admission_calls_exhausted"


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b'{"kind": "tessera.reasoning_admission"}',
        json.dumps(
            {
                "kind": "tessera.reasoning_admission",
                "version": 1,
                "admission_id": "radm_x",
                "record_digest": "z" * 64,
                "project_id": "p",
                "expires_at": "2030-01-01T00:00:00Z",
            }
        ).encode(),
    ],
)
def test_a_damaged_recorded_marker_fails_rather_than_reading_as_bare(world, content):
    world.marker.write_bytes(content)
    os.chmod(world.marker, 0o600)
    assert code(world.check()) == "marker_malformed"
    assert startup_refusal(content) == "admission_marker_malformed"


def test_an_unsafe_marker_fails(world):
    target = world.tmp / "elsewhere"
    target.write_bytes(b"")
    world.marker.symlink_to(target)
    assert code(world.check()) == "marker_unsafe"
    world.marker.unlink()
    world.marker.write_bytes(b"")
    os.chmod(world.marker, 0o622)
    assert code(world.check()) == "marker_unsafe"


# -- the command --------------------------------------------------------------------------------


class Chown:
    def __init__(self):
        self.calls = []

    def chown(self, path, uid, gid):
        self.calls.append((Path(path).name, uid, gid))


def transport_for(world, calls):
    def factory(origin, authorization):
        assert origin == "http://127.0.0.1:8765"

        def send(method, path, body=None, key=None):
            calls.append((method, path))
            headers = {"Authorization": authorization, "Idempotency-Key": key}
            response = world.client.request(method, path, json=body, headers=headers)
            return response.status_code, response.json()

        return send

    return factory


def header_file(world, token, name):
    path = world.tmp / name
    path.write_text(f"Authorization: Bearer {token}\n")
    os.chmod(path, 0o600)
    return path


def parse(world, *argv):
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command")
    add_parser(subcommands)
    return parser.parse_args(
        ["reasoning", *argv, "--root", str(world.tmp / "root"), "--config-dir", "/etc/tessera"]
    )


def write_profile(world):
    path = world.config / "reasoning.json"
    path.write_text(json.dumps(world.profile()))
    return path


def test_admit_records_then_writes_a_private_marker_that_readiness_accepts(world, capsys):
    write_profile(world)
    session = header_file(world, world.session_token, "session.header")
    calls, chown = [], Chown()
    args = parse(world, "admit", "--session-header", str(session), "--expires-in", "3600")
    assert run_admit(args, transport_factory=transport_for(world, calls), accounts=chown) == 0
    assert calls == [("POST", f"/projects/{world.project}/reasoning-admissions")]
    out = json.loads(capsys.readouterr().out)
    assert out["admission"]["decision_class"] == "human_session"
    assert out["admission"]["caps"] == CAPS | {"max_calls": None}
    info = os.lstat(world.marker)
    assert oct(info.st_mode & 0o777) == "0o600"
    assert chown.calls == [(chown.calls[0][0], 0, 0)] and chown.calls[0][0].endswith(".tmp")
    marker = parse_marker(world.marker.read_bytes())
    assert marker["admission_id"] == out["admission"]["id"]
    assert not [p for p in world.config.iterdir() if p.name.endswith(".tmp")]
    assert world.check()["status"] == "ok"


def test_admit_refuses_an_existing_marker_before_recording_anything(world):
    write_profile(world)
    world.marker.write_bytes(b"")
    session = header_file(world, world.session_token, "session.header")
    calls = []
    args = parse(world, "admit", "--session-header", str(session), "--expires-in", "3600")
    assert run_admit(args, transport_factory=transport_for(world, calls), accounts=Chown()) == 2
    assert calls == [] and world.recorded() == 0
    assert world.marker.read_bytes() == b""
    # --replace upgrades a bare marker to a recorded one.
    args = parse(
        world, "admit", "--session-header", str(session), "--expires-in", "3600", "--replace"
    )
    assert run_admit(args, transport_factory=transport_for(world, calls), accounts=Chown()) == 0
    assert world.check()["status"] == "ok"


def test_admit_with_the_owner_token_is_refused_and_writes_no_marker(world, capsys):
    write_profile(world)
    owner_token = world.owner["Authorization"].removeprefix("Bearer ")
    owner = header_file(world, owner_token, "owner.header")
    args = parse(world, "admit", "--session-header", str(owner), "--expires-in", "3600")
    assert run_admit(args, transport_factory=transport_for(world, []), accounts=Chown()) == 1
    assert json.loads(capsys.readouterr().out)["status"] == 403
    assert not world.marker.exists() and world.recorded() == 0


def test_revoke_admission_revokes_the_record_and_removes_its_marker(world, capsys):
    write_profile(world)
    session = header_file(world, world.session_token, "session.header")
    args = parse(world, "admit", "--session-header", str(session), "--expires-in", "3600")
    assert run_admit(args, transport_factory=transport_for(world, []), accounts=Chown()) == 0
    admission_id = json.loads(capsys.readouterr().out)["admission"]["id"]
    args = parse(
        world,
        "revoke-admission",
        "--session-header",
        str(session),
        "--project-id",
        world.project,
        "--admission-id",
        admission_id,
    )
    assert run_revoke(args, transport_factory=transport_for(world, [])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"admission_id": admission_id, "state": "revoked", "marker_removed": True}
    assert not world.marker.exists()


def test_the_installer_still_never_admits_and_names_the_command(world):
    options = reasoning_install.Options(root=world.tmp / "root")
    pending = {p["name"]: p for p in reasoning_install.pending_operator(options)}
    assert "tessera reasoning admit" in pending["admission"]["action"]
    with pytest.raises(reasoning_install.InstallRefused):
        reasoning_install.write_admitted_marker(options, b"x")  # no injected accounts
    world.marker.symlink_to(world.tmp / "elsewhere")
    with pytest.raises(reasoning_install.InstallRefused):
        reasoning_install.write_admitted_marker(options, b"x", replace=True, accounts=Chown())
