"""Install one wheel/sdist without inherited credentials and exercise its real CLI/API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def inventory(artifact: Path) -> list[str]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
    else:
        with tarfile.open(artifact, "r:gz") as archive:
            names = archive.getnames()
    forbidden = {".tessera", ".venv", "__pycache__", "node_modules", "owner.header"}
    for name in names:
        path = Path(name)
        if forbidden.intersection(path.parts) or path.suffix in {".pyc", ".sqlite3", ".pem"}:
            raise ValueError(f"unexpected runtime/state file in distribution: {name}")
    if not any(name.endswith("LICENSE") for name in names):
        raise ValueError("distribution omits LICENSE")
    if not any(name.endswith("skills/tessera-research-bench/SKILL.md") for name in names):
        raise ValueError("distribution omits portable research skill")
    return names


def run(artifact: Path) -> dict:
    names = inventory(artifact)
    temporary_root = "/private/tmp/opencode" if sys.platform == "darwin" else None
    with tempfile.TemporaryDirectory(prefix="tessera-install-", dir=temporary_root) as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        # Deliberate allowlist: no provider/control credentials, Python paths or runtime config.
        env = {
            "PATH": os.defpath,
            "HOME": str(root / "home"),
            "TMPDIR": str(root),
            "LANG": "en_US.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
        }
        Path(env["HOME"]).mkdir(mode=0o700)

        def command(*args: str) -> str:
            return subprocess.check_output(
                args, cwd=root, env=env, stderr=subprocess.PIPE, text=True, timeout=180
            )

        command(sys.executable, "-m", "venv", str(root / "env"))
        python = str(root / "env/bin/python")
        command(
            python,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--only-binary=:all:",
            str(artifact) + "[mcp]",
        )
        command(python, "-m", "pip", "check")
        cli = str(root / "env/bin/tessera")
        command(cli, "--help")
        tunnel_help = command(cli, "connection-tunnel", "--help")
        for option in (
            "--kubeconfig",
            "--context",
            "--namespace-uid",
            "--gateway-deployment-uid",
            "--public-ca-file",
            "--state-dir",
            "--max-seconds",
        ):
            if option not in tunnel_help:
                raise ValueError(
                    "installed connection tunnel help omits explicit operator contract"
                )
        command(str(root / "env/bin/tessera-worker"), "--help")
        mcp_help = command(str(root / "env/bin/tessera-mcp"), "--help")
        if "TESSERA_CONTROL_CA_FILE" not in mcp_help:
            raise ValueError("installed MCP help omits explicit verified public-CA support")
        skill = root / "env/share/tessera/skills/tessera-research-bench/SKILL.md"
        skill_text = skill.read_text()
        if not skill_text.startswith("---\nname: tessera-research-bench\n"):
            raise ValueError("installed skill frontmatter is invalid")
        if "2025-11-25" not in skill_text or "mcp==1.26.0" not in skill_text:
            raise ValueError("installed skill protocol/SDK contract is missing")
        mcp_contract = json.loads(
            command(
                python,
                "-c",
                "import asyncio,json,importlib.metadata as m; "
                "from tessera_mcp.server import create_server; "
                "s=create_server(None); "
                "print(json.dumps({'sdk':m.version('mcp'),"
                "'tools':[t.name for t in asyncio.run(s.list_tools())],"
                "'resources':[str(r.uri) for r in asyncio.run(s.list_resources())],"
                "'templates':[t.uriTemplate for t in asyncio.run(s.list_resource_templates())]}))",
            )
        )
        expected_tools = {
            "tessera_status",
            "tessera_list_contexts",
            "tessera_read_context",
            "tessera_list_proposals",
            "tessera_read_proposal",
            "tessera_propose_task_plan",
        }
        if mcp_contract["sdk"] != "1.26.0" or set(mcp_contract["tools"]) != expected_tools:
            raise ValueError("installed MCP SDK/tool surface mismatches contract")
        if mcp_contract["resources"] != ["tessera://status"] or set(mcp_contract["templates"]) != {
            "tessera://contexts/{context_id}",
            "tessera://proposals/{proposal_id}",
        }:
            raise ValueError("installed MCP resource surface mismatches contract")
        state = root / "state"
        command(cli, "init", "--state-dir", str(state))
        header = (state / "owner.header").read_text().strip()
        header_name, header_value = header.split(":", 1)
        if (state / "owner.header").stat().st_mode & 0o077:
            raise ValueError("owner header is not private")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # Only terminate the exact child started here. Never touch an existing listener.
        with (root / "service.log").open("w") as log:
            process = subprocess.Popen(
                [cli, "serve", "--state-dir", str(state), "--port", str(port)],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("installed control process exited before readiness")
                    try:
                        with urllib.request.urlopen(base + "/health", timeout=1) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError("installed control readiness timed out")
                try:
                    urllib.request.urlopen(base + "/projects", timeout=5)
                except urllib.error.HTTPError as error:
                    if error.code != 401:
                        raise
                else:
                    raise ValueError("unauthenticated projects read was accepted")
                request = urllib.request.Request(
                    base + "/projects",
                    data=b'{"name":"Clean installed bench"}',
                    headers={
                        header_name: header_value.strip(),
                        "Content-Type": "application/json",
                        "Idempotency-Key": "clean-install-project",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    project = json.load(response)
                    if response.status != 201 or not project.get("id"):
                        raise ValueError("installed API did not create a project")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        command(cli, "backup", "--state-dir", str(state), "--output", str(root / "backup"))
        command(cli, "restore", "--backup", str(root / "backup"), "--state-dir", str(root / "held"))
        manifest = json.loads((root / "backup/manifest.json").read_text())
        packages = json.loads(command(python, "-m", "pip", "list", "--format=json"))
        license_inventory = json.loads(
            command(
                python,
                "-c",
                "import importlib.metadata as m,json; "
                "print(json.dumps([{"
                "'name':d.metadata['Name'],'version':d.version,"
                "'license_expression':d.metadata.get('License-Expression'),"
                "'license_classifiers':[c for c in d.metadata.get_all('Classifier',[]) "
                "if c.startswith('License ::')],"
                "'license_text_present':bool(d.metadata.get('License'))"
                "} for d in m.distributions()]))",
            )
        )
        return {
            "artifact": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "files": len(names),
            "schema_version": manifest["schema_version"],
            "checks": [
                "inventory",
                "isolated-install",
                "pip-check",
                "cli-help",
                "mcp-extra-pin",
                "mcp-cli-help",
                "installed-skill",
                "installed-mcp-tool-resource-contract",
                "private-init",
                "ordinary-service",
                "unauthenticated-denial",
                "authenticated-project",
                "backup",
                "held-restore",
            ],
            "installed_packages": packages,
            "mcp_contract": mcp_contract,
            "skill_sha256": hashlib.sha256(skill.read_bytes()).hexdigest(),
            "declared_license_metadata": sorted(license_inventory, key=lambda d: d["name"].lower()),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.artifact.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
