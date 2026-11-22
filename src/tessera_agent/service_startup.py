"""Fixed installed-service startup mapping for systemd LoadCredential files.

No shell, arbitrary executable, provider authentication or credential discovery.
Only references in the manager-provided CREDENTIALS_DIRECTORY are read.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import stat
import sys
from pathlib import Path

from .model_destinations import (
    RUNTIME_ANTHROPIC_MESSAGES,
    DestinationRefused,
    destination,
    refuse_api_key_environment,
)
from .remote_reasoning_runtime import validate_profile
from .remote_reasoning_store import ReasoningStore, private_file

_FORWARDED = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
# Manager-provided file (systemd LoadCredential=model-credential:...) holding the
# profile's opaque anthropic-api-key:// reference. Tessera compares it in constant
# time and never forwards it into any output or the child environment.
MODEL_CREDENTIAL_FILE = "model-credential"
# Manager-provided Anthropic API key (LoadCredential=anthropic-api-key:...). Startup
# only checks it is a private non-empty regular file; the supervisor reads it at call
# time. It is never placed in the child environment, argv or any output.
ANTHROPIC_API_KEY_FILE = "anthropic-api-key"
_AGENT_REF = ("agent_token_env", "TESSERA_REASONING_AGENT_TOKEN", "agent-token")
_HARNESS_REF = ("harness_token_env", "TESSERA_REASONING_HARNESS_TOKEN", "harness-password")


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


def refuse_forbidden_environment(environment: dict[str, str]) -> None:
    """Fail closed on any API-key fallback variable before any profile is read."""
    try:
        refuse_api_key_environment(environment)
    except DestinationRefused:
        raise StartupBlocked("api_key_environment_present") from None


def require_model_credential(root: Path, profile: dict) -> None:
    """Anthropic profiles need the manager-provided reference file.

    The file must hold exactly the profile's opaque credential_ref (one trailing
    newline allowed). Neither value is ever echoed, logged or forwarded.
    """
    if profile["model"]["providerID"] != "anthropic":
        return
    try:
        content = private_file(root / MODEL_CREDENTIAL_FILE)
    except (OSError, ValueError):
        raise StartupBlocked("missing_or_invalid_model_credential_reference") from None
    if not content.strip():
        raise StartupBlocked("missing_or_invalid_model_credential_reference")
    if content.endswith(b"\n"):
        content = content[:-1]
    expected = profile.get("credential_ref")
    if (
        not isinstance(expected, str)
        or any(b <= 32 or b >= 127 for b in content)
        or not expected.isascii()
        or not hmac.compare_digest(content, expected.encode("ascii"))
    ):
        raise StartupBlocked("model_credential_reference_mismatch")


def require_api_key_file(root: Path) -> None:
    """Stat-level check only: a private, owned, non-empty regular file. Never echoed."""
    try:
        info = (root / ANTHROPIC_API_KEY_FILE).lstat()
    except OSError:
        raise StartupBlocked("missing_or_invalid_model_api_key_credential") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.getuid()
        or not 0 < info.st_size <= 64 * 1024
    ):
        raise StartupBlocked("missing_or_invalid_model_api_key_credential")


def token_reference(root: Path, name: str) -> str:
    token = private_file(root / name).decode("ascii").rstrip("\n")
    if not token or len(token) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
        raise StartupBlocked("invalid_runtime_credential")
    return token


def launch_spec(mode: str, state_dir: Path, environment: dict[str, str]):
    """Return only a fixed Python module argv and allowlisted child environment."""
    if mode not in {"reasoning", "a2a"} or not state_dir.is_absolute():
        raise StartupBlocked("unsupported_service_profile")
    refuse_forbidden_environment(environment)
    root = credential_root(environment)
    profile_path = root / "profile"
    profile = json.loads(private_file(profile_path))
    child = {name: environment[name] for name in _FORWARDED if name in environment}
    # Never forward arbitrary provider keys, PYTHONPATH or loader/shell overrides.
    child["PYTHONNOUSERSITE"] = "1"
    if mode == "reasoning":
        validate_profile(profile)
        require_model_credential(root, profile)
        found = destination(profile["model"]["providerID"], profile["account_mode"])
        references = [_AGENT_REF]
        if found.runtime == RUNTIME_ANTHROPIC_MESSAGES:
            # Direct Messages API: no harness password or harness token.
            require_api_key_file(root)
            # The child reads the key from the same manager directory at call time.
            child["CREDENTIALS_DIRECTORY"] = str(root)
        else:
            references.append(_HARNESS_REF)
        for field, variable, reference in references:
            if profile.get(field) != variable:
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
        refuse_forbidden_environment(dict(os.environ))
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
