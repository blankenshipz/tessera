"""Offline allowlisted RC builder; packaging is not live release acceptance."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from observer_snapshot import validate_snapshot
from public_runtime_bundle import FILES as RUNTIME_FILES
from public_runtime_bundle import validate as validate_runtime_bundle
from release_resources import STARTER_FILES, STARTER_SOURCE

ROOT_FILES = {
    "pyproject.toml",
    "uv.lock",
    "MANIFEST.in",
    "LICENSE",
    "README.md",
    "DESIGN.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "Dockerfile",
    "Dockerfile.guard",
    "compose.yaml",
    ".dockerignore",
    "desktop/EXECUTION.md",
}
TREES = {
    "src": {".py"},
    "tests": {".py"},
    "docs": {".md", ".json", ".yaml"},
    "skills": {".md"},
    "scripts": {".py"},
    "dev": {".py"},
    "examples": {".go", ".md", ".py", ".json", ".yaml", ".csv"},
    "plugins": {".ts", ".json", ".md"},
    "deploy": {".py", ".md", ".json", ".yaml", ".service", ".dockerignore"},
}
DEV_FILES = {"dev/build_release_candidate.py", "dev/pytest_shard.py"}
OBSERVER_FILES = {"README.md", "baseline.json", "LICENSE", "observer.patch", "baseline.tar.gz"}
FORBIDDEN = {
    ".git",
    ".tessera",
    ".venv",
    "node_modules",
    "__pycache__",
    "worktrees",
    ".worktrees",
    "auth",
    "credentials",
    "receipts",
    "runtime",
    ".evidence",
}
AUTH_FILES = {
    "auth.json",
    "credentials.json",
    "token.json",
    "tokens.json",
    "owner.header",
    "worker.header",
    "credentials",
    "token",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_self_lock(text: str, project_name: str, version: str) -> str:
    """Change exactly one editable self-package version; preserve every other byte/pin."""
    original = tomllib.loads(text)
    packages = original.get("package", [])
    indexes = [
        i
        for i, item in enumerate(packages)
        if item.get("name") == project_name and item.get("source") == {"editable": "."}
    ]
    if len(indexes) != 1:
        raise ValueError("Lock requires exactly one editable self-package.")
    blocks = list(re.finditer(r"(?ms)^\[\[package\]\]\r?\n.*?(?=^\[\[package\]\]|\Z)", text))
    if len(blocks) != len(packages):
        raise ValueError("Unsupported package block layout; refusing broad lock rewrite.")
    block = blocks[indexes[0]]
    value = re.search(r'(?m)^version = "([^"\r\n]+)"\r?$', block.group())
    if value is None or value.group(1) != packages[indexes[0]].get("version"):
        raise ValueError("Editable self-package version cannot be pinned exactly.")
    start, end = block.start() + value.start(1), block.start() + value.end(1)
    result = text[:start] + version + text[end:]
    expected = copy.deepcopy(original)
    expected["package"][indexes[0]]["version"] = version
    if tomllib.loads(result) != expected:
        raise ValueError("Lock normalization altered dependency resolution pins.")
    return result


def safe(path: Path) -> bool:
    return not any(
        p in FORBIDDEN
        or p in AUTH_FILES
        or p.startswith(".env")
        or p.endswith((".header", ".pem", ".key", ".sqlite3", ".db"))
        for p in path.parts
    )


def inputs(root: Path) -> dict[str, bytes]:
    names = {name for name in ROOT_FILES | DEV_FILES if (root / name).is_file()}
    for tree, extensions in TREES.items():
        names.update(
            p.relative_to(root).as_posix()
            for p in (root / tree).rglob("*")
            if p.is_file()
            and (
                p.suffix in extensions
                or (tree == "examples" and p.name == "Dockerfile")
                or p.relative_to(root).as_posix()
                == "deploy/remote-agent-platform/docker/Dockerfile"
                or p.relative_to(root).as_posix()
                in {f"{STARTER_SOURCE}/{name}" for name in STARTER_FILES}
            )
            and safe(p.relative_to(root))
            and not any(part.endswith(".egg-info") for part in p.parts)
        )
    observer = root / "release/opencode-observer"
    names.update(
        f"release/opencode-observer/{name}"
        for name in OBSERVER_FILES
        if (observer / name).is_file()
    )
    runtime = observer / "runtime-v8"
    if runtime.exists() or runtime.is_symlink():
        validate_runtime_bundle(runtime)
        names.update(f"release/opencode-observer/runtime-v8/{name}" for name in RUNTIME_FILES)
    result = {}
    for name in sorted(names):
        path = root / name
        if not safe(Path(name)) or any(
            p.is_symlink() for p in [path, *path.parents] if p != root.parent
        ):
            raise ValueError("Candidate input is not an ordinary allowlisted file.")
        data = path.read_bytes()
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Candidate input exceeds bounded file size.")
        result[name] = data
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        if name not in result:
            raise ValueError("Required candidate input missing.")
    return result


def observer_bundle(root: Path) -> dict:
    names = ("baseline.json", "LICENSE", "observer.patch")
    if root.is_symlink() or any((root / name).is_symlink() for name in names):
        raise ValueError("Observer bundle must use ordinary files.")
    for name, bound in zip(names, (16384, 512 * 1024, 4 * 1024 * 1024), strict=True):
        if (root / name).stat().st_size > bound:
            raise ValueError("Observer bundle exceeds its bounds.")
    files = {name: (root / name).read_bytes() for name in names}
    if len(files["baseline.json"]) > 16384 or len(files["observer.patch"]) > 4 * 1024 * 1024:
        raise ValueError("Observer bundle exceeds its bounds.")

    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Observer bundle has duplicate JSON keys.")
            result[name] = value
        return result

    baseline = json.loads(files["baseline.json"], object_pairs_hook=unique)
    if baseline.get("schema_version") == "tessera-opencode-observer-baseline-v2":
        return validate_snapshot(root, baseline, files)
    if set(baseline) != {
        "schema_version",
        "repository",
        "commit",
        "license_sha256",
        "patch_sha256",
    }:
        raise ValueError("Observer baseline fields are closed.")
    uri = urlsplit(baseline["repository"])
    if (
        baseline["schema_version"] != "tessera-opencode-observer-baseline-v1"
        or uri.scheme != "https"
        or not uri.hostname
        or uri.username
        or uri.password
        or uri.query
        or uri.fragment
        or not re.fullmatch(r"[0-9a-f]{40}", baseline["commit"])
        or baseline["license_sha256"] != digest(files["LICENSE"])
        or baseline["patch_sha256"] != digest(files["observer.patch"])
    ):
        raise ValueError("Observer baseline/hash pin mismatch.")
    patch = files["observer.patch"].decode("utf-8")
    paths = re.findall(r"^(?:---|\+\+\+) (\S+)", patch, flags=re.MULTILINE)
    if not paths or not files["LICENSE"].strip():
        raise ValueError("Observer patch/license missing.")
    for name in paths:
        if name == "/dev/null":
            continue
        path = Path(name)
        if (
            not name.startswith(("a/", "b/"))
            or ".." in path.parts
            or not safe(path)
            or path.suffix in {".pem", ".key", ".header"}
        ):
            raise ValueError("Observer patch contains an unsafe path.")
    return baseline


def stage_observer(bundle: Path, destination: Path) -> None:
    """Explicit public patch handoff only; never discovers/reads a worktree or auth."""
    baseline = observer_bundle(bundle)
    if destination.exists():
        raise ValueError("Refusing to overwrite observer staging.")
    destination.mkdir(parents=True)
    for name in ("baseline.json", "LICENSE", "observer.patch"):
        shutil.copyfile(bundle / name, destination / name)
    if baseline.get("mode") == "file-pinned-snapshot":
        shutil.copyfile(bundle / "baseline.tar.gz", destination / "baseline.tar.gz")


def build(root: Path, output: Path, version: str, epoch: int, *, development: bool = False) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+rc\d+", version) or not 315532800 <= epoch <= 4354819199:
        raise ValueError("Use an explicit RC version and bounded SOURCE_DATE_EPOCH (1980-2107).")
    if output.exists():
        raise ValueError("Refusing to overwrite candidate artifacts.")
    selected = inputs(root)
    if not development and version != "0.1.0rc1":
        validate_runtime_bundle(root / "release/opencode-observer/runtime-v8")
    observer = None if development else observer_bundle(root / "release/opencode-observer")
    manifest = [
        {"path": name, "size": len(data), "sha256": digest(data)} for name, data in selected.items()
    ]
    revision = digest(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="stage-", dir=output) as directory:
        stage = Path(directory)
        for name, data in selected.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        config = (stage / "pyproject.toml").read_text()
        config, replaced = re.subn(
            r'^version = "[^"]+"$', f'version = "{version}"', config, count=1, flags=re.MULTILINE
        )
        if replaced != 1:
            raise ValueError("Package version cannot be pinned.")
        (stage / "pyproject.toml").write_text(config)
        if "uv.lock" in selected:
            project_name = tomllib.loads(config)["project"]["name"]
            normalized = normalize_self_lock(selected["uv.lock"].decode(), project_name, version)
            (stage / "uv.lock").write_bytes(normalized.encode())
        staged_manifest = [
            {
                "path": name,
                "size": (stage / name).stat().st_size,
                "sha256": digest((stage / name).read_bytes()),
            }
            for name in selected
        ]
        normalized_paths = {
            name: {
                "original_sha256": digest(selected[name]),
                "staged_sha256": digest((stage / name).read_bytes()),
            }
            for name in selected
            if (stage / name).read_bytes() != selected[name]
        }
        metadata = {
            "schema_version": "tessera-source-candidate-v1",
            "version": version,
            "source_date_epoch": epoch,
            "input_revision": revision,
            "inputs": manifest,
            "staged_inputs": staged_manifest,
            "staged_input_revision": digest(
                json.dumps(staged_manifest, sort_keys=True, separators=(",", ":")).encode()
            ),
            "normalized_paths": normalized_paths,
            "observer": observer,
            "public_runtime": (
                validate_runtime_bundle(root / "release/opencode-observer/runtime-v8")
                if "release/opencode-observer/runtime-v8/baseline.json" in selected
                else None
            ),
            "development_snapshot": development,
            "acceptance": "candidate_only_not_accepted_release",
            "build_backend": {
                "setuptools": importlib.metadata.version("setuptools"),
                "python": sys.version.split()[0],
                "wheel_timestamps": "normalized_SOURCE_DATE_EPOCH",
            },
        }
        (stage / "candidate-manifest.json").write_text(
            json.dumps(metadata, sort_keys=True, indent=2) + "\n"
        )
        archive = output / f"tessera-{version}-source.tar.gz"
        with (
            archive.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=epoch) as gz,
        ):
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for path in sorted(stage.rglob("*")):
                    if not path.is_file():
                        continue
                    data = path.read_bytes()
                    entry = tarfile.TarInfo(
                        f"tessera-{version}/{path.relative_to(stage).as_posix()}"
                    )
                    entry.size, entry.mtime, entry.mode = len(data), epoch, 0o644
                    tar.addfile(entry, io.BytesIO(data))
        env = {
            "PATH": os.defpath,
            "HOME": str(stage / "private-home"),
            "TMPDIR": str(stage),
            "SOURCE_DATE_EPOCH": str(epoch),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        Path(env["HOME"]).mkdir()
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from setuptools.build_meta import build_wheel; "
                "import sys; build_wheel(sys.argv[1])",
                str(output.resolve()),
            ],
            cwd=stage,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=180,
        )
        wheels = list(output.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected one candidate wheel.")
        wheel = wheels[0]
        temporary = wheel.with_suffix(".normalized")
        with (
            zipfile.ZipFile(wheel) as old,
            zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as new,
        ):
            for name in sorted(old.namelist()):
                if not safe(Path(name)) or Path(name).suffix in {".pyc", ".pem", ".key", ".header"}:
                    raise ValueError("Wheel includes forbidden state.")
                entry = zipfile.ZipInfo(name, time.gmtime(epoch)[:6])
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                new.writestr(entry, old.read(name))
        temporary.replace(wheel)
    if inputs(root) != selected:
        raise ValueError("Source changed during candidate build; artifacts not admitted.")
    metadata["artifacts"] = [
        {"path": p.name, "sha256": digest(p.read_bytes()), "size": p.stat().st_size}
        for p in sorted(output.iterdir())
        if p.is_file()
    ]
    (output / "manifest.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    candidate = sub.add_parser("build")
    candidate.add_argument("--root", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    candidate.add_argument("--version", required=True)
    candidate.add_argument("--source-date-epoch", type=int, required=True)
    candidate.add_argument("--development-snapshot", action="store_true")
    staging = sub.add_parser("stage-observer")
    staging.add_argument("--bundle", type=Path, required=True)
    staging.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "stage-observer":
        stage_observer(args.bundle, args.destination)
    else:
        result = build(
            args.root,
            args.output,
            args.version,
            args.source_date_epoch,
            development=args.development_snapshot,
        )
        print(
            json.dumps(
                {
                    "version": result["version"],
                    "input_revision": result["input_revision"],
                    "artifacts": result["artifacts"],
                    "acceptance": result["acceptance"],
                }
            )
        )


if __name__ == "__main__":
    main()
