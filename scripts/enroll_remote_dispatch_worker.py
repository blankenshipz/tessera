"""Opt-in owner enrollment for one pre-witnessed Kubernetes dispatch target.

No Kubernetes calls, Secret creation, grant issuance, or dispatch. Keep the
private directory for idempotent recovery if a control response is lost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import ssl
import stat
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tessera_worker.control_channel import HttpControlChannel
from tessera_worker.kubernetes_contracts import KubernetesClusterConfig
from tessera_worker.kubernetes_remote_service import _network, _NoRedirect, _private_json
from tessera_worker.service import read_private_credential

_ID = re.compile(r"[A-Za-z0-9_:-]{1,200}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class EnrollmentError(RuntimeError):
    pass


def _private_file(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as destination:
        destination.write(value)
        destination.flush()
        os.fsync(destination.fileno())


def _target(path: Path) -> str:
    data = _private_json(path)
    evidence = _network(data["network_evidence"])
    cluster = KubernetesClusterConfig(**data["cluster"], network_none_evidence=evidence)
    # The standalone conformance target is not an executable control outbox
    # profile. The normal worker claim schema and admitted package are pinned to
    # this profile; reject an incompatible target before issuing credentials.
    if cluster.profile != "development-minikube-opencode-v1":
        raise EnrollmentError("dispatch target profile is not supported by control admission")
    if not evidence.is_fresh_for(cluster.cluster_uid, cluster.namespace_uid):
        raise EnrollmentError("fresh allowed and denied network witness is required")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def enroll(
    channel: HttpControlChannel,
    *,
    project_id: str,
    build_digest: str,
    target_file: Path,
    private_dir: Path,
    duration_minutes: int = 60,
) -> dict[str, str]:
    """Enroll one project worker; replay the exact intent after an uncertain reply."""
    if not _ID.fullmatch(project_id) or not _DIGEST.fullmatch(build_digest):
        raise EnrollmentError("project or image build digest invalid")
    if not 1 <= duration_minutes <= 120:
        raise EnrollmentError("credential lifetime must be between 1 and 120 minutes")
    target_digest = _target(target_file)
    if not private_dir.is_absolute():
        raise EnrollmentError("private directory must be absolute")
    private_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = private_dir.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise EnrollmentError("enrollment directory must be private 0700")
    intent_file = private_dir / "enrollment.json"
    if not intent_file.exists():
        if any(private_dir.iterdir()):
            raise EnrollmentError("new enrollment directory must be empty")
        intent = {
            "project_id": project_id,
            "build_digest": build_digest,
            "target_digest": target_digest,
            "control_origin": channel.origin,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=duration_minutes)).isoformat(),
            "credential_key": secrets.token_hex(24),
            "register_key": secrets.token_hex(24),
            "token": secrets.token_urlsafe(48),
        }
        _private_file(intent_file, json.dumps(intent).encode())
    else:
        if (
            intent_file.is_symlink()
            or stat.S_IMODE(intent_file.stat().st_mode) != 0o600
            or intent_file.stat().st_uid != os.geteuid()
            or intent_file.stat().st_nlink != 1
        ):
            raise EnrollmentError("enrollment intent must be private 0600")
        intent = _private_json(intent_file)
    if any(
        intent[field] != expected
        for field, expected in (
            ("project_id", project_id),
            ("build_digest", build_digest),
            ("target_digest", target_digest),
            ("control_origin", channel.origin),
        )
    ):
        raise EnrollmentError("enrollment intent differs from explicit target or project")
    if datetime.fromisoformat(intent["expires_at"]) <= datetime.now(UTC):
        raise EnrollmentError("worker credential intent expired; reconcile before new enrollment")
    credential = channel.post(
        "/credentials",
        {
            "token": intent["token"],
            "kind": "worker",
            "audience": "worker",
            "project_id": project_id,
            "expires_at": intent["expires_at"],
        },
        intent["credential_key"],
    )
    principal_id = credential["principal_id"]
    if not _ID.fullmatch(principal_id):
        raise EnrollmentError("control returned invalid worker identity")
    registration = channel.post(
        f"/projects/{project_id}/workers",
        {
            "worker_principal_id": principal_id,
            "approved_build_identifier": "remote-dispatch-worker-v1",
            "approved_build_digest": build_digest,
            "trust_class": "development",
            "region": "local-docker-desktop",
        },
        intent["register_key"],
    )
    if registration.get("worker_principal_id") != principal_id:
        raise EnrollmentError("control registration worker identity mismatch")
    token_file = private_dir / "worker-token"
    if token_file.exists():
        if read_private_credential(token_file) != intent["token"]:
            raise EnrollmentError("existing worker token differs from durable enrollment")
    else:
        _private_file(token_file, intent["token"].encode())
    return {"worker_principal_id": principal_id, "worker_registration_id": registration["id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--control-ca-file", type=Path, required=True)
    parser.add_argument("--owner-token-file", type=Path, required=True)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--build-digest", required=True)
    args = parser.parse_args(argv)
    if not args.control_url.startswith("https://"):
        parser.error("owner enrollment requires an HTTPS control origin")
    context = ssl.create_default_context(cafile=str(args.control_ca_file))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )
    channel = HttpControlChannel(
        args.control_url,
        read_private_credential(args.owner_token_file),
        opener=lambda request, timeout: opener.open(request, timeout=timeout),
    )
    result = enroll(
        channel,
        project_id=args.project_id,
        build_digest=args.build_digest,
        target_file=args.target_file,
        private_dir=args.private_dir,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
