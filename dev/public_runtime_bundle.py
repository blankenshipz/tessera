"""Closed, licensed v8 public runtime distribution; never loads private factories."""

import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

FILES = frozenset(
    {
        "baseline.json",
        "baseline.tar.gz",
        "observer.patch",
        "LICENSE",
        "BASELINE.sha256",
        "TARGET.sha256",
        "origins.json",
        "README.md",
        "reconstruct.py",
        "link_runtime.py",
        "verify_runtime.mjs",
        "verify_python.py",
        "runtime-deps/package.json",
        "runtime-deps/bun.lock",
    }
)
FIELDS = frozenset(
    {
        "schema_version",
        "bundle_version",
        "mode",
        "source_origin",
        "source_version",
        "source_reference",
        "upstream_verification",
        "opencode_upstream_commit",
        "source_date_epoch",
        "baseline_files",
        "target_files",
        "licenses",
        "license_path",
        "baseline_archive_sha256",
        "patch_sha256",
        "target_namespace_sha256",
        "dependency_lock_sha256",
        "bundle_files",
        "runtime_dependencies",
        "scope",
    }
)
NAMESPACE = "5ee39d0311d8a4b4f0f59298f7591bd8849e772ac69ba629dd3ff4e264c800b7"
MANIFEST_SHA = "45d2645a5bb29a4b2ebe771eb0c9b8802110e4a00423f7baf844d0e10ed9f9a5"
HEX = re.compile(r"[a-f0-9]{64}\Z")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def index(entries):
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items())).encode()


def public_member(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", name):
        return False
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or str(path) != name
        or any(
            part.startswith(".")
            or part
            in {
                "node_modules",
                "auth",
                "credentials",
                "receipts",
                "private",
                "worktrees",
                "auth.json",
            }
            for part in path.parts
        )
    ):
        return False
    if name in {"LICENSE.opencode", "harness/LICENSE", "package.json", "harness/bin/codex-quota"}:
        return True
    return name.startswith(
        (
            "packages/core/",
            "packages/plugin/",
            "packages/sdk/",
            "packages/ai/",
            "packages/schema/",
            "harness/",
        )
    ) and path.suffix in {".ts", ".mjs", ".py", ".json"}


def validate(bundle: Path) -> dict:
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("Public runtime bundle missing or symlink")
    paths = list(bundle.rglob("*"))
    if any(path.is_symlink() for path in [bundle, *bundle.parents, *paths]):
        raise ValueError("Public runtime symlink denied")
    actual = {path.relative_to(bundle).as_posix(): path for path in paths if path.is_file()}
    if set(actual) != FILES:
        raise ValueError("Public runtime closed file inventory mismatch")
    if any(path.stat().st_size > 4 * 1024 * 1024 for path in actual.values()):
        raise ValueError("Public runtime file bound exceeded")
    data = {name: path.read_bytes() for name, path in actual.items()}
    manifest = json.loads(data["baseline.json"])
    if (
        set(manifest) != FIELDS
        or manifest["schema_version"] != "tessera-opencode-observer-runtime-v3"
        or manifest["bundle_version"] != 8
        or manifest["mode"] != "file-pinned-sparse-runtime-closure"
        or manifest["opencode_upstream_commit"] is not None
        or manifest["upstream_verification"] != "not_independently_verified"
        or manifest["source_date_epoch"] != 0
    ):
        raise ValueError("Unsupported public runtime closed schema")
    originals, targets = manifest["baseline_files"], manifest["target_files"]
    if (
        len(originals) != 50
        or len(targets) != 79
        or not originals.keys() <= targets.keys()
        or any(
            not public_member(name) or not HEX.fullmatch(digest)
            for mapping in (originals, targets)
            for name, digest in mapping.items()
        )
    ):
        raise ValueError("Unsafe or incomplete public runtime closure")
    if manifest["bundle_files"] != {
        name: sha(content) for name, content in data.items() if name != "baseline.json"
    }:
        raise ValueError("Public runtime file pin mismatch")
    for key, name in (
        ("baseline_archive_sha256", "baseline.tar.gz"),
        ("patch_sha256", "observer.patch"),
        ("dependency_lock_sha256", "runtime-deps/bun.lock"),
    ):
        if manifest[key] != sha(data[name]):
            raise ValueError("Public runtime artifact pin mismatch")
    if (
        data["BASELINE.sha256"] != index(originals)
        or data["TARGET.sha256"] != index(targets)
        or manifest["target_namespace_sha256"] != sha(index(targets))
    ):
        raise ValueError("Public runtime source namespace mismatch")
    licenses = manifest["licenses"]
    if (
        set(licenses) != {"LICENSE.opencode", "harness/LICENSE"}
        or any(
            originals.get(name) != digest or targets.get(name) != digest
            for name, digest in licenses.items()
        )
        or sha(data["LICENSE"]) != licenses["LICENSE.opencode"]
    ):
        raise ValueError("Both unchanged public runtime licenses required")
    with tarfile.open(actual["baseline.tar.gz"], "r:gz") as archive:
        members = archive.getmembers()
        if len(members) != 50 or {m.name for m in members} != set(originals):
            raise ValueError("Public runtime archive member mismatch")
        total = 0
        for member in members:
            total += member.size
            if (
                not member.isfile()
                or member.size > 1024 * 1024
                or total > 8 * 1024 * 1024
                or member.uid
                or member.gid
                or member.mtime
                or member.pax_headers
                or member.uname
                or member.gname
                or member.mode != (0o755 if member.name == "harness/bin/codex-quota" else 0o644)
            ):
                raise ValueError("Public runtime archive metadata denied")
            if sha(archive.extractfile(member).read()) != originals[member.name]:
                raise ValueError("Public runtime archive bytes mismatch")
    if (
        sha(data["baseline.json"]) != MANIFEST_SHA
        or sha(index({name: sha(content) for name, content in data.items()})) != NAMESPACE
    ):
        raise ValueError("Reviewed v8 public runtime identity mismatch")
    return {
        "schema_version": manifest["schema_version"],
        "bundle_version": 8,
        "files": sorted(FILES),
        "bundle_namespace_sha256": NAMESPACE,
        "manifest_sha256": MANIFEST_SHA,
        "target_namespace_sha256": manifest["target_namespace_sha256"],
        "claim": "public sparse closure, not admission",
    }


def stage(bundle: Path, destination: Path) -> dict:
    """Stage only the fourteen reviewed public inputs, never an ignored checkout."""
    proof = validate(bundle)
    if (
        destination.exists()
        or destination.is_symlink()
        or any(path.is_symlink() for path in destination.parents)
    ):
        raise ValueError("Fresh ordinary public runtime stage required")
    destination.mkdir(parents=True)
    for name in sorted(FILES):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((bundle / name).read_bytes())
    if validate(destination) != proof:
        raise ValueError("Public runtime staging parity mismatch")
    return proof
