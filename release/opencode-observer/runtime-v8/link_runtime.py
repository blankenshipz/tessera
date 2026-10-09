"""Link an explicitly installed pinned public dependency, never an auth path."""

import argparse
import hashlib
import json
from pathlib import Path


def link(tree, dependencies):
    bundle = Path(__file__).resolve().parent
    manifest = json.loads((bundle / "baseline.json").read_text())
    for name in ("package.json", "bun.lock"):
        expected = manifest["bundle_files"]["runtime-deps/" + name]
        if hashlib.sha256((dependencies / name).read_bytes()).hexdigest() != expected:
            raise ValueError("dependency manifest/lock mismatch")
    effect = dependencies / "node_modules/effect"
    if json.loads((effect / "package.json").read_text())["version"] != "4.0.0-rc.112":
        raise ValueError("wrong actual dependency version")
    for name, destination in [
        ("effect", effect),
        ("@opencode/schema", tree / "packages/schema"),
        ("@opencode/sdk", tree / "packages/sdk"),
    ]:
        output = tree / "node_modules" / name
        if output.exists() or output.is_symlink():
            raise ValueError("refuse existing runtime link")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.symlink_to(destination.resolve(), target_is_directory=True)
    (tree / ".cache").mkdir()
    return {
        "linked": ["effect", "@opencode/schema", "@opencode/sdk"],
        "dependency_version": "4.0.0-rc.112",
        "claim": "explicit frozen public dependency linkage, not production owner enrollment",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path)
    parser.add_argument("dependencies", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(link(args.tree.resolve(), args.dependencies.resolve()), sort_keys=True))
    except Exception:
        raise SystemExit(
            "public runtime linkage failed; missing prerequisites are not a skip"
        ) from None
