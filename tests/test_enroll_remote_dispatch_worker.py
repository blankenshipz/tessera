"""No network or cluster: durable owner enrollment and witness fail-closed checks."""

import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from enroll_remote_dispatch_worker import EnrollmentError, enroll
from test_kubernetes_remote_service import _fixture


class FakeOwner:
    origin = "https://control.example.test"

    def __init__(self):
        self.calls = []
        self.lose_reply = False

    def post(self, path, body, key):
        self.calls.append((path, body.copy(), key))
        if self.lose_reply and path == "/credentials":
            self.lose_reply = False
            raise ConnectionError("lost reply")
        if path == "/credentials":
            return {"principal_id": "principal_worker"}
        return {"worker_principal_id": "principal_worker", "id": "worker_registered"}


def target(tmp_path, *, denial="denied", old=False, profile="development-minikube-opencode-v1"):
    client, output, _ = _fixture(tmp_path)
    cluster = asdict(client.cluster)
    cluster["profile"] = profile
    network = cluster.pop("network_none_evidence")
    if old:
        network["observed_at"] = datetime.now(UTC) - timedelta(minutes=16)
    network["denied_connectivity"]["outcome"] = denial
    path = tmp_path / "target.json"
    path.write_text(
        json.dumps(
            {"cluster": cluster, "network_evidence": network, "output": asdict(output)}, default=str
        )
    )
    return path


def test_scoped_enrollment_replays_lost_reply_without_issuing_new_token(tmp_path):
    binding = target(tmp_path)
    channel = FakeOwner()
    channel.lose_reply = True
    kwargs = dict(
        project_id="project_one",
        build_digest="a" * 64,
        target_file=binding,
        private_dir=tmp_path / "private",
    )
    with pytest.raises(ConnectionError, match="lost reply"):
        enroll(channel, **kwargs)
    assert not (kwargs["private_dir"] / "worker-token").exists()
    first = channel.calls[0]
    assert enroll(channel, **kwargs) == {
        "worker_principal_id": "principal_worker",
        "worker_registration_id": "worker_registered",
    }
    assert channel.calls[1] == first
    assert (kwargs["private_dir"] / "worker-token").stat().st_mode & 0o777 == 0o600
    assert first[1]["token"] not in json.dumps(enroll(channel, **kwargs))
    with pytest.raises(EnrollmentError, match="differs"):
        enroll(channel, **(kwargs | {"project_id": "project_other"}))


@pytest.mark.parametrize("denial,old", [("allowed", False), ("denied", True), ("timeout", False)])
def test_no_enrollment_without_fresh_real_denial(tmp_path, denial, old):
    channel = FakeOwner()
    with pytest.raises((EnrollmentError, ValueError)):
        enroll(
            channel,
            project_id="project_one",
            build_digest="a" * 64,
            target_file=target(tmp_path, denial=denial, old=old),
            private_dir=tmp_path / "private",
        )
    assert channel.calls == []
    assert not (tmp_path / "private").exists()


def test_conformance_only_profile_cannot_enroll_executable_dispatch_worker(tmp_path):
    channel = FakeOwner()
    with pytest.raises(EnrollmentError, match="profile is not supported"):
        enroll(
            channel,
            project_id="project_one",
            build_digest="a" * 64,
            target_file=target(tmp_path, profile="development-kubernetes-conformance-v1"),
            private_dir=tmp_path / "private",
        )
    assert channel.calls == []
    assert not (tmp_path / "private").exists()
