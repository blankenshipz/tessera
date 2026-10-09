"""Offline installed origins and exact wheel bytes; call with the installed Python."""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import sysconfig
import zipfile
from pathlib import Path

from public_runtime_bundle import validate
from release_resources import STARTER_FILES, STARTER_INSTALLED


def installed_starter_resources(data: Path) -> dict[str, Path]:
    directory = data / STARTER_INSTALLED
    paths = {name: directory / name for name in STARTER_FILES}
    if any(path.is_symlink() or not path.is_file() for path in paths.values()):
        raise ValueError("Required installed starter resources missing or symlink")
    return paths


def verify(wheel: Path) -> dict:
    site = Path(sysconfig.get_path("purelib")).resolve()
    data = Path(sysconfig.get_path("data")).resolve()
    distribution = importlib.metadata.distribution("tessera-control")
    expected_prefix = f"tessera_control-{distribution.version}.data/data/"
    count = 0
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename
            if name.startswith(expected_prefix):
                relative = name.removeprefix(expected_prefix)
                if ".." in Path(relative).parts:
                    raise ValueError("Unsafe resource path")
                installed = data / relative
            elif name.startswith("tessera_") and ".dist-info/" not in name:
                if ".." in Path(name).parts or ".data/" in name:
                    raise ValueError("Unsafe package path")
                installed = site / name
            else:
                continue
            if installed.is_symlink() or installed.read_bytes() != archive.read(member):
                raise ValueError("Installed wheel byte mismatch")
            count += 1
    origins = {}
    for module in (
        "tessera_control",
        "tessera_worker",
        "tessera_mcp.runtime_setup",
        "tessera_mcp.runtime_setup_models",
        "tessera_starter",
    ):
        origin = Path(importlib.import_module(module).__file__).resolve()
        origin.relative_to(site)
        origins[module] = str(origin)
    runtime = data / "share/tessera/release/opencode-observer/runtime-v8"
    required = (
        runtime / "baseline.json",
        runtime / "runtime-deps/bun.lock",
        data / "share/tessera/docs/runtime-setup.md",
        data / "share/tessera/docs/runtime-operator-setup.md",
    )
    if not all(path.is_file() for path in required):
        raise ValueError("Required installed runtime/setup resources missing")
    runtime_proof = validate(runtime)
    starter_resources = installed_starter_resources(data)
    return {
        "mode": "local",
        "status": "passed",
        "version": distribution.version,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "verified_files": count,
        "origins": origins,
        "public_runtime": runtime_proof,
        "starter_resource_count": len(starter_resources),
        "claim": "installed bytes, not admission",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.wheel), sort_keys=True))
