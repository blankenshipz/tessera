"""Verify/extract exact licensed sparse originals, apply patch, verify all targets.

No downloads, credential discovery, dependency installation or native effects.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def sha(data):
    return hashlib.sha256(data).hexdigest()


def index(files):
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items())).encode()


def safe(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name:
        raise ValueError("unsafe archive/target path")
    return name


def reconstruct(bundle, destination):
    manifest = json.loads((bundle / "baseline.json").read_text())
    if manifest["schema_version"] != "tessera-opencode-observer-runtime-v3":
        raise ValueError("unsupported runtime bundle schema")
    originals, targets = manifest["baseline_files"], manifest["target_files"]
    for name in set(originals) | set(targets):
        safe(name)
        path = PurePosixPath(name)
        if any(
            part.startswith(".")
            or part in {"node_modules", "auth.json", "financial-verification.json"}
            for part in path.parts
        ):
            raise ValueError("private/state/dependency member denied")
        if name not in {
            "package.json",
            "LICENSE.opencode",
            "harness/LICENSE",
            "harness/bin/codex-quota",
        }:
            if not name.startswith(
                (
                    "packages/core/",
                    "packages/plugin/",
                    "packages/sdk/",
                    "packages/ai/",
                    "packages/schema/",
                    "harness/",
                )
            ) or path.suffix not in {".ts", ".mjs", ".py", ".json"}:
                raise ValueError("nonpublic runtime source member denied")
        if not re.fullmatch("[a-f0-9]{64}", targets.get(name, "")):
            raise ValueError("invalid source byte pin")
    if len(originals) != 50 or len(targets) != 79 or not set(originals) <= set(targets):
        raise ValueError("incomplete sparse native closure")
    if sha(index(targets)) != manifest["target_namespace_sha256"]:
        raise ValueError("incorrect target namespace")
    if set(manifest["licenses"]) != {"LICENSE.opencode", "harness/LICENSE"}:
        raise ValueError("both original licenses required")
    for name, expected in manifest["bundle_files"].items():
        if sha((bundle / safe(name)).read_bytes()) != expected:
            raise ValueError("bundle file drift")
    if destination.exists() or destination.is_symlink():
        raise ValueError("fresh reconstruction directory required")
    destination.mkdir(parents=True)
    archive = bundle / "baseline.tar.gz"
    if archive.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("archive too large")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) != len(originals) or len({m.name for m in members}) != len(members):
            raise ValueError("archive member mismatch")
        total = 0
        for member in members:
            name = safe(member.name)
            expected_mode = 0o755 if name == "harness/bin/codex-quota" else 0o644
            if (
                name not in originals
                or not member.isfile()
                or member.size > 1024 * 1024
                or member.uid != 0
                or member.gid != 0
                or member.mtime != 0
                or member.pax_headers
                or member.uname
                or member.gname
                or member.mode != expected_mode
            ):
                raise ValueError("archive metadata/path denied")
            total += member.size
            if total > 8 * 1024 * 1024:
                raise ValueError("archive expanded size denied")
            data = tar.extractfile(member).read()
            if sha(data) != originals[name]:
                raise ValueError("original source drift")
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            output.chmod(expected_mode)
    subprocess.run(
        ["git", "apply", "--check", str((bundle / "observer.patch").resolve())],
        cwd=destination,
        check=True,
    )
    subprocess.run(
        ["git", "apply", str((bundle / "observer.patch").resolve())], cwd=destination, check=True
    )
    actual = {
        str(p.relative_to(destination)): sha(p.read_bytes())
        for p in destination.rglob("*")
        if p.is_file()
    }
    if actual != targets:
        raise ValueError("patched full target inventory mismatch")
    for name, expected in manifest["licenses"].items():
        if actual.get(name) != expected:
            raise ValueError("license drift")
    return {
        "originals": len(originals),
        "targets": len(actual),
        "target_namespace_sha256": sha(index(actual)),
        "claim": (
            "exact sparse public source reconstruction, not full OpenCode build or native admission"
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                reconstruct(Path(__file__).resolve().parent, args.destination), sort_keys=True
            )
        )
    except Exception:
        raise SystemExit("public runtime reconstruction failed; no admission granted") from None
