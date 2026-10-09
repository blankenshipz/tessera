"""Explicit public input inventory; pending snapshots are never a freeze admission."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from build_release_candidate import inputs

REQUIRED = {"README.md", "DESIGN.md", "CONTRIBUTING.md", "LICENSE", "pyproject.toml", "uv.lock"}


def inventory(root: Path) -> dict:
    selected = inputs(root)
    missing = sorted(REQUIRED - selected.keys())
    if missing:
        raise ValueError("Required release contracts missing")
    entries = [
        {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in sorted(selected.items())
    ]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": 1, "input_revision": hashlib.sha256(canonical).hexdigest(), "inputs": entries}


def assert_unchanged(root: Path, expected: dict) -> None:
    if inventory(root) != expected:
        raise ValueError("Release input inventory changed")


def verify_wheel(root: Path, wheel: Path, resources: dict[str, str]) -> dict:
    """Check installed-code/resource byte parity without extracting archive paths."""
    selected = inputs(root)
    required = {
        name: name.removeprefix("src/")
        for name in selected
        if name.startswith("src/") and name.endswith(".py")
    }
    required.update(resources)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(
            name.startswith("/")
            or ".." in Path(name).parts
            or any(
                part in {".tessera", "credentials", "auth", "receipts", "node_modules"}
                for part in Path(name).parts
            )
            for name in names
        ):
            raise ValueError("Unsafe or duplicate wheel member")
        for source, destination in required.items():
            if source not in selected:
                raise ValueError("Required resource absent from public inputs")
            if archive.read(destination) != selected[source]:
                raise ValueError("Wheel/source byte parity mismatch")
    return {"mode": "local", "verified_files": len(required), "status": "passed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parent-frozen", action="store_true")
    args = parser.parse_args()
    result = inventory(args.root)
    result["admission"] = "parent_frozen" if args.parent_frozen else "pending_writers"
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.output.open("x") as stream:
        json.dump(result, stream, sort_keys=True)


if __name__ == "__main__":
    main()
