from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.cli import main
from tessera_control.store import Store, canonical_digest, initialize_state


def test_review_adopt_restart_verify_and_metadata_api(tmp_path, monkeypatch, capsys):
    state, root = tmp_path / "state", tmp_path / "source"
    root.mkdir()
    (root / "analysis.py").write_text("raise RuntimeError('must never execute')\n")
    (root / ".env").write_text("FIXTURE_SECRET=should-not-be-captured\n")
    initialize_state(state)
    header = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    store = Store(state)
    actor = store.authenticate(header.removeprefix("Bearer "))
    body = {"name": "Adopted research"}
    _, project = store.create_project(actor, "project", canonical_digest(body), body)

    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["tessera", "workspace", *map(str, args)])
        code = main()
        output = capsys.readouterr()
        return code, output.out, output.err

    code, output, error = invoke("inventory", "--root", root)
    assert code == 0 and error == ""
    review = json.loads(output)
    assert "raise RuntimeError" not in output and "should-not-be-captured" not in output
    assert review["inventory"]["exclusions"] == [
        {"relative_path": ".env", "reason": "credential_path"}
    ]
    adoption_args = (
        "adopt",
        "--root",
        root,
        "--state-dir",
        state,
        "--project-id",
        project["id"],
        "--expected-digest",
        review["digest"],
        "--idempotency-key",
        "adopt-once",
    )
    code, output, error = invoke(*adoption_args)
    assert code == 0 and error == ""
    adopted = json.loads(output)
    assert adopted["transfer_authorized"] is False
    assert json.loads(invoke(*adoption_args)[1]) == adopted
    (root / "analysis.py").write_text("Changed after adoption")
    code, output, error = invoke("verify", "--state-dir", state, "--snapshot-id", adopted["id"])
    assert code == 0 and error == ""
    assert json.loads(output)["integrity"] == "verified"
    assert "Changed after adoption" not in output
    with TestClient(create_app(state)) as client:
        response = client.get(f"/snapshots/{adopted['id']}", headers={"Authorization": header})
        assert response.status_code == 200
        assert response.json()["digest"] == review["digest"]
        assert "raise RuntimeError" not in response.text
        listed = client.get(
            f"/projects/{project['id']}/snapshots", headers={"Authorization": header}
        )
        assert len(listed.json()["snapshots"]) == 1
        assert client.get(f"/snapshots/{adopted['id']}").status_code == 401
        assert (
            client.post(
                f"/projects/{project['id']}/snapshots",
                json={"root": str(root)},
                headers={"Authorization": header},
            ).status_code
            == 405
        )
    code, output, error = invoke(*adoption_args)
    assert code == 1 and output == "" and "changed since inventory" in error


def test_cli_rejects_sources_containing_private_state(tmp_path: Path, monkeypatch, capsys):
    state = tmp_path / "state"
    initialize_state(state)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "workspace",
            "adopt",
            "--root",
            str(tmp_path),
            "--state-dir",
            str(state),
            "--project-id",
            "unused",
            "--expected-digest",
            "a" * 64,
            "--idempotency-key",
            "adopt",
        ],
    )
    assert main() == 1
    output = capsys.readouterr()
    assert not output.out and "must not contain control-plane state" in output.err
