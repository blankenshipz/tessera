"""Harmless generated wheels and bounded in-memory streams only; no actual downloads."""

import base64
import csv
import hashlib
import io
import json
import os
import stat
import zipfile
from types import SimpleNamespace

import pytest
from test_capabilities import declaration
from test_capability_activation import NOW, candidate  # noqa: F401
from test_capability_kubernetes_runner import bind_plan

from tessera_control.capability_package import BrokerPackage, SignedCapabilityPackage, encode
from tessera_control.store import canonical_digest
from tessera_worker.capability_effects import BackendBlocked, DownloadResponse
from tessera_worker.capability_installer import CapabilityInstaller
from tessera_worker.capability_journal import CapabilityJournal
from tessera_worker.capability_runner_contract import PackageVerifier


def wheel_bytes(*, fault=None):
    prefix = "fixture-1.0.dist-info/"
    entries = {
        "fixture/__init__.py": b"# harmless fixture; never imported\n",
        prefix + "METADATA": b"Metadata-Version: 2.1\nName: fixture\nVersion: 1.0\n",
        prefix + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if fault == "version":
        entries[prefix + "METADATA"] = b"Name: fixture\nVersion: 2.0\n"
    if fault == "dependency":
        entries[prefix + "METADATA"] += b"Requires-Dist: surprise\n"
    if fault == "native":
        entries[prefix + "WHEEL"] = b"Root-Is-Purelib: false\nTag: cp311-linux-x86_64\n"
    if fault in {"traversal", "absolute", "pth", "script", "data", "symlink", "duplicate", "bomb"}:
        name = {
            "traversal": "../escape",
            "absolute": "/escape",
            "pth": "injection.pth",
            "script": prefix + "entry_points.txt",
            "data": "fixture.data/purelib/escape.py",
            "symlink": "link",
            "duplicate": "fixture/__init__.py",
            "bomb": "large.py",
        }[fault]
        entries[name] = b"x" * 70000 if fault == "bomb" else b"fixture"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, content in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow(
            [name, "sha256=" + ("wrong" if fault == "record" else digest), str(len(content))]
        )
    writer.writerow([prefix + "RECORD", "", ""])
    entries[prefix + "RECORD"] = record.getvalue().encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            if fault == "symlink" and name == "link":
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"/escape")
            else:
                archive.writestr(name, content)
        if fault == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("fixture/__init__.py", b"duplicate")
    return out.getvalue()


class FixtureDownload:
    def __init__(self, action, destination, content):
        self.content = content
        self.response = DownloadResponse(
            action.url, destination.addresses, 200, action.content_type, action.expected_size
        )
        self.calls = []
        self.before = lambda: None

    def open(self, *, timeout):
        self.calls.append("open")
        self.before()
        return self.response

    def read(self, offset, *, limit, timeout):
        self.calls.append("read")
        self.before()
        return self.content[offset : offset + limit]

    def close(self):
        self.calls.append("close")


@pytest.fixture
def installer(candidate, tmp_path):  # noqa: F811 - imported pytest fixture
    journals = []

    def make(*, fault=None, quota=None, inodes=64):
        content = wheel_bytes(fault=fault)
        declared = declaration("package")
        declared["broker_basis"] = candidate.body.declaration.broker_basis.model_dump()
        declared["max_bytes"] = 65536
        declared["filesystem_roots"] = ["/workspace/packages/fixture"]
        action = declared["actions"][0]
        action.update(
            url=action["url"] + ".whl",
            sha256=hashlib.sha256(content).hexdigest(),
            expected_size=len(content),
            content_type="application/zip",
            max_bytes=65536,
            filesystem_root="/workspace/packages/fixture",
        )
        lock = json.dumps(
            [
                {
                    "name": action["name"],
                    "version": action["version"],
                    "artifact_sha256": action["sha256"],
                }
            ],
            sort_keys=True,
        ).encode()
        action["lockfile_sha256"] = hashlib.sha256(lock).hexdigest()
        wire = candidate.body.model_dump(mode="json")
        wire.update(declaration=declared, declaration_digest=canonical_digest(declared))
        wire["binding"]["action"] = action
        body = BrokerPackage.model_validate(wire)
        payload = encode(body)
        package = SignedCapabilityPackage(
            payload, hashlib.sha256(payload).hexdigest(), "fixture-signature"
        )
        m = candidate.manifest.model_copy(
            update={
                "package_digest": package.digest,
                "destination": candidate.manifest.destination.model_copy(
                    update={"url": action["url"]}
                ),
                "quota": candidate.manifest.quota.model_copy(
                    update={"output_bytes": quota or 65536, "output_inodes": inodes}
                ),
            }
        )
        plan, _ = bind_plan(m)
        verifier = PackageVerifier(
            authority=lambda _: True,
            identity=("org", "project", "worker"),
            approved_plan_digest=plan.digest,
            now=lambda: NOW,
        )
        verified = verifier.verify(package)
        journal = CapabilityJournal(tmp_path / f"installer-{len(journals)}")
        journals.append(journal)
        engine = CapabilityInstaller(journal=journal, verifier=verifier)
        transport = FixtureDownload(body.binding.action, m.destination, content)
        return SimpleNamespace(**locals())

    yield make
    for journal in journals:
        journal.close()


def run(h):
    return h.engine.install(h.verified, plan=h.plan, lock=h.lock, transport=h.transport)


def test_atomic_hook_free_inventory_install_and_no_repeat(installer, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "secret-proxy")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-access-key")
    h = installer()
    result = run(h)
    assert result["state"] == "installed"
    assert result["downloaded_sha256"] == hashlib.sha256(h.content).hexdigest()
    assert result["inventory"][0]["version"] == "1.0"
    root = h.journal.root / ("install-" + h.m.nonce)
    assert (root / "artifact.whl").read_bytes() == h.content
    assert (root / "tree/fixture/__init__.py").read_bytes().startswith(b"# harmless")
    assert not (h.journal.root / ("install-" + h.m.nonce + ".partial")).exists()
    assert result["custody"] == "quarantined_unreleased"
    assert "secret-" not in json.dumps(result)
    assert "fixture-signature" not in json.dumps(result)
    with pytest.raises(BackendBlocked):
        run(h)
    assert h.transport.calls.count("open") == 1


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "dependency",
        "native",
        "record",
        "traversal",
        "absolute",
        "pth",
        "script",
        "data",
        "symlink",
        "duplicate",
        "bomb",
    ],
)
def test_forbidden_wheel_or_inventory_fails_not_success(installer, fault):
    h = installer(fault=fault)
    result = run(h)
    assert result["state"] == "failed" and result["inventory"] is None
    assert not (h.journal.root / ("install-" + h.m.nonce)).exists()
    assert not (h.journal.root / "escape").exists()
    with pytest.raises(BackendBlocked):
        run(h)


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "short",
        "extra",
        "content-type",
        "url",
        "addresses",
        "size",
        "lock-hash",
        "lock-closure",
        "timeout",
        "authority",
        "lost-reply",
    ],
)
def test_bounded_download_and_lock_failures(installer, fault):
    h = installer()
    from dataclasses import replace

    if fault == "hash":
        h.transport.content = b"x" * len(h.content)
    elif fault == "short":
        h.transport.content = h.content[:-1]
    elif fault == "extra":
        h.transport.content += b"x"
    elif fault in {"content-type", "url", "addresses", "size"}:
        key = fault.replace("-", "_")
        value = {
            "content_type": "text/plain",
            "url": "https://evil.example.org/fixture.whl",
            "addresses": ("127.0.0.1",),
            "size": len(h.content) + 1,
        }[key]
        h.transport.response = replace(h.transport.response, **{key: value})
    elif fault.startswith("lock"):
        h.lock = b"[]"
    elif fault == "timeout":
        clock = [0.0]
        h.engine.clock = lambda: clock[0]
        h.transport.before = lambda: clock.__setitem__(0, 100.0)
    elif fault == "authority":
        h.transport.before = lambda: setattr(h.verifier, "authority", lambda _: False)
    else:

        def lost():
            raise OSError("SECRET-UPSTREAM-DIAGNOSTIC")

        h.transport.before = lost
    result = run(h)
    assert result["state"] == "failed"
    assert "SECRET" not in json.dumps(result)
    assert result["inventory"] is None


@pytest.mark.parametrize("quota_kind", ["bytes", "inodes"])
def test_output_enforcement_prevents_write_overshoot(installer, quota_kind):
    content_size = len(wheel_bytes())
    h = installer(
        quota=content_size if quota_kind == "bytes" else None,
        inodes=3 if quota_kind == "inodes" else 64,
    )
    result = run(h)
    assert result["state"] == "failed"
    stage = h.journal.root / ("install-" + h.m.nonce + ".partial")
    paths = list(stage.rglob("*"))
    assert sum(p.stat().st_size for p in paths if p.is_file()) <= h.m.quota.output_bytes
    assert len(paths) + 1 <= h.m.quota.output_inodes


def test_no_follow_existing_stage_symlink(installer, tmp_path):
    h = installer()
    outside = tmp_path / "outside"
    outside.mkdir()
    (h.journal.root / ("install-" + h.m.nonce + ".partial")).symlink_to(outside)
    assert run(h)["state"] == "failed"
    assert list(outside.iterdir()) == []
    assert "open" not in h.transport.calls


def test_post_rename_loss_is_unknown_and_preserves_artifacts(installer, monkeypatch):
    h = installer()
    real_rename = os.rename

    def rename_then_revoke(*args, **kwargs):
        real_rename(*args, **kwargs)
        h.verifier.authority = lambda _: False

    monkeypatch.setattr(os, "rename", rename_then_revoke)
    result = run(h)
    assert result["state"] == "unknown" and result["inventory"] is None
    assert (h.journal.root / ("install-" + h.m.nonce)).is_dir()


def test_installer_rejects_unverified_package_without_download(installer):
    h = installer()
    with pytest.raises(BackendBlocked):
        h.engine.install(h.package, plan=h.plan, lock=h.lock, transport=h.transport)
    assert h.transport.calls == []


def test_installed_inventory_is_read_back_not_assumed(installer, monkeypatch):
    h = installer()
    original = h.engine._write

    def corrupt(root, name, content, charge):
        original(root, name, b"x" * len(content), charge)

    monkeypatch.setattr(h.engine, "_write", corrupt)
    assert run(h)["state"] == "failed"


def test_rename_applied_lost_reply_remains_unknown(installer, monkeypatch):
    h = installer()
    original = os.rename

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("lost reply secret")

    monkeypatch.setattr(os, "rename", lost)
    result = run(h)
    assert result["state"] == "unknown"
    assert (h.journal.root / ("install-" + h.m.nonce)).is_dir()


def test_unsupported_package_and_project_quota_mechanism(installer):
    h = installer()
    bad = h.plan.activation.quota.model_copy(update={"mechanism": "requested-only"})
    h.plan = h.plan.model_copy(
        update={"activation": h.plan.activation.model_copy(update={"quota": bad})}
    )
    h.verifier.approved_plan_digest = h.plan.digest
    with pytest.raises(BackendBlocked):
        run(h)
    assert h.transport.calls == []
