"""Fixed installed-service startup mapping for systemd LoadCredential files.

No shell, arbitrary executable, provider authentication or credential discovery.
Only references in the manager-provided CREDENTIALS_DIRECTORY are read.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from .remote_reasoning_runtime import validate_profile
from .remote_reasoning_store import ReasoningStore, private_file

_FORWARDED = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
_REASONING_REFS = {
    "agent_token_env": ("TESSERA_REASONING_AGENT_TOKEN", "agent-token"),
    "harness_token_env": ("TESSERA_REASONING_HARNESS_TOKEN", "harness-password"),
}


class StartupBlocked(ValueError):
    """Sanitized startup denial; no underlying exception or file contents logged."""


def credential_root(environment: dict[str, str]) -> Path:
    value = environment.get("CREDENTIALS_DIRECTORY", "")
    if not value or not Path(value).is_absolute():
        raise StartupBlocked("missing_manager_credential_directory")
    path = Path(value)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise StartupBlocked("invalid_manager_credential_directory")
    return path


def token_reference(root: Path, name: str) -> str:
    token = private_file(root / name).decode("ascii").rstrip("\n")
    if not token or len(token) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
        raise StartupBlocked("invalid_runtime_credential")
    return token


def launch_spec(mode: str, state_dir: Path, environment: dict[str, str]):
    """Return only a fixed Python module argv and allowlisted child environment."""
    if mode not in {"reasoning", "a2a"} or not state_dir.is_absolute():
        raise StartupBlocked("unsupported_service_profile")
    root = credential_root(environment)
    profile_path = root / "profile"
    profile = json.loads(private_file(profile_path))
    child = {name: environment[name] for name in _FORWARDED if name in environment}
    # Never forward arbitrary provider keys, PYTHONPATH or loader/shell overrides.
    child["PYTHONNOUSERSITE"] = "1"
    if mode == "reasoning":
        validate_profile(profile)
        for field, (variable, reference) in _REASONING_REFS.items():
            if profile[field] != variable:
                raise StartupBlocked("unsupported_credential_environment_name")
            child[variable] = token_reference(root, reference)
        private_file(root / "prompt")  # Runtime enforces prompt digest/length.
        command = [
            sys.executable,
            "-m",
            "tessera_agent.remote_reasoning_runtime",
            "run",
            "--profile",
            str(profile_path),
            "--state-dir",
            str(state_dir),
            "--prompt-file",
            str(root / "prompt"),
        ]
    else:
        if profile.get("control_token_env") != "TESSERA_A2A_CONTROL_TOKEN":
            raise StartupBlocked("unsupported_credential_environment_name")
        if profile.get("database") != str(state_dir / "peer.sqlite3"):
            raise StartupBlocked("peer_database_outside_service_state")
        port = profile.get("listen_port", 8771)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise StartupBlocked("invalid_unprivileged_loopback_peer_port")
        child["TESSERA_A2A_CONTROL_TOKEN"] = token_reference(root, "agent-token")
        # Listener policy is fixed here, never inherited from caller/config.
        command = [
            sys.executable,
            "-m",
            "tessera_control.a2a_peer",
            "--config",
            str(profile_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    return command, child


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reasoning", "reasoning-stop", "a2a", "research"))
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.state_dir.is_absolute():
            raise StartupBlocked("absolute_service_state_required")
        if args.mode == "research":
            # Packaging may expose this fixed entrypoint, but no source manifest
            # or environment switch can substitute for installed admission.
            from .research_service import main as research_main

            # Use only the operator-supplied private manager directory, with the
            # same mapper validation as other modes. No inferred default path.
            reference = credential_root(dict(os.environ)) / "profile"
            return research_main(["--config", str(reference), "--state-dir", str(args.state_dir)])
        if args.mode == "reasoning-stop":
            root = credential_root(dict(os.environ))
            profile = json.loads(private_file(root / "profile"))
            validate_profile(profile)
            store = ReasoningStore(args.state_dir)
            try:
                store.stop(profile["run_id"])
            finally:
                store.close()
            return 0
        command, child = launch_spec(args.mode, args.state_dir, dict(os.environ))
        os.execve(sys.executable, command, child)
    except Exception:
        # Do not expose parser/decoder/upstream contents or secret-bearing exceptions.
        print(
            "Tessera service startup blocked; inspect private references and profile.",
            file=sys.stderr,
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
