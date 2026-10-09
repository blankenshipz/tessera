"""Clean wheel install smoke, offline using explicit existing dependency references."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path


def smoke(wheel: Path, dependencies: Path) -> dict:
    wheel = wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if any(
            set(Path(name).parts) & {".tessera", "auth", "credentials", "__pycache__"}
            or name.endswith((".pem", ".key", ".header", ".pyc"))
            for name in names
        ):
            raise ValueError("Forbidden wheel state.")
    temporary = "/private/tmp/opencode" if sys.platform == "darwin" else None
    with tempfile.TemporaryDirectory(prefix="tessera-rc-install-", dir=temporary) as directory:
        root = Path(directory)
        venv.EnvBuilder(with_pip=False).create(root / "env")
        python = root / "env/bin/python"
        env = {
            "PATH": os.defpath,
            "HOME": str(root / "home"),
            "TMPDIR": str(root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        (root / "home").mkdir()

        def run(*args):
            return subprocess.check_output(
                args, cwd=root, env=env, text=True, stderr=subprocess.PIPE, timeout=120
            )

        site = Path(
            run(str(python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))").strip()
        )
        # Reference third-party code only. Never import the checkout or process its
        # editable .pth files, installed Tessera code, environment or auth state.
        for path in dependencies.iterdir():
            if (
                path.name.startswith(
                    ("tessera", "__editable__", "pip", "setuptools", "pkg_resources")
                )
                or path.suffix == ".pth"
                or path.name == "__pycache__"
            ):
                continue
            if not (site / path.name).exists():
                (site / path.name).symlink_to(path.resolve(), target_is_directory=path.is_dir())
        run(
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(python),
            "install",
            "--no-deps",
            "--no-index",
            "--quiet",
            str(wheel),
        )
        details = json.loads(
            run(
                str(python),
                "-c",
                "import importlib.metadata as m,json; "
                "import tessera_control; print(json.dumps({"
                "'version':m.version('tessera-control'),"
                "'module':tessera_control.__file__}))",
            )
        )
        if not Path(details["module"]).is_relative_to(site):
            raise ValueError("Smoke imported Tessera outside its clean install.")
        entrypoints = [
            "tessera",
            "tessera-worker",
            "tessera-mcp",
            "tessera-reasoning",
            "tessera-a2a-peer",
        ]
        for name in entrypoints:
            if not run(str(root / "env/bin" / name), "--help").strip():
                raise ValueError("Installed entrypoint has no help contract.")
        share = root / "env/share/tessera"
        skill = share / "skills/tessera-research-bench/SKILL.md"
        if not skill.read_text().startswith("---\nname: tessera-research-bench\n"):
            raise ValueError("Installed portable skill invalid.")
        for name in ("README.md", "DESIGN.md", "CONTRIBUTING.md", "SECURITY.md"):
            if not (share / "docs" / name).is_file():
                raise ValueError("Installed operator docs missing.")
        if not (share / "release/opencode-observer/README.md").is_file():
            raise ValueError("Installed observer handoff missing.")
        catalog = json.loads(
            run(
                str(python),
                "-c",
                "import asyncio,json; "
                "from tessera_mcp.server import create_server; "
                "s=create_server(None); print(json.dumps({"
                "'tools':len(asyncio.run(s.list_tools())),"
                "'resources':len(asyncio.run(s.list_resources()))}))",
            )
        )
        if not catalog["tools"] or not catalog["resources"]:
            raise ValueError("Installed permission-scoped MCP resources missing.")
        return {
            "version": details["version"],
            "clean_install": True,
            "entrypoints": entrypoints,
            "skill": True,
            "docs": True,
            "observer_handoff": True,
            "mcp_catalog": catalog,
            "dependency_mode": "explicit existing local references; no downloads",
            "acceptance": "packaging_smoke_only_not_live_admission",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--dependencies", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.wheel, args.dependencies)))
