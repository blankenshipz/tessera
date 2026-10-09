"""Explicit reviewed source staging only; never runtime state or auth discovery."""

import argparse
import shutil
from pathlib import Path

FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "MANIFEST.in")
SKILLS = ("tessera-research-bench", "tessera-governed-research")
DOCKER_FILES = ("Dockerfile", "Dockerfile.dockerignore")


def selected(root):
    paths = [Path(name) for name in FILES]
    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root)
        if any(
            part.startswith(".")
            or part in {"__pycache__", "credentials", "auth", "cache", "caches", "secrets", "state"}
            for part in relative.parts[:-1]
        ):
            continue
        paths.append(relative)
    paths += [Path("skills") / name / "SKILL.md" for name in SKILLS]
    paths += [Path("deploy/remote-agent-platform/docker") / name for name in DOCKER_FILES]
    for relative in paths:
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Source staging refuses symlinks.")
        if not current.is_file():
            raise ValueError("Required selected source file is missing.")
    return sorted(paths)


def stage(root, destination):
    paths = selected(root)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    stage(args.source, args.destination)


if __name__ == "__main__":
    main()
