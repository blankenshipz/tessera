"""Real-Docker check that a non-65532 container user can write the private output dir.

Skipped unless a rootful, non-remapped Docker daemon is reachable and a small local image is present
(``TESSERA_TEST_DOCKER_IMAGE``, default ``busybox``; never pulled by this test).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

IMAGE = os.environ.get("TESSERA_TEST_DOCKER_IMAGE", "busybox")


def _docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=60, check=False
    )


def _available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        # uid mappings (rootless, userns-remap) change who owns the bind mount: skip.
        info = _docker("info", "--format", "{{.SecurityOptions}}")
        if info.returncode != 0 or any(w in info.stdout for w in ("rootless", "userns")):
            return False
        return _docker("image", "inspect", IMAGE).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Linux only"),
    pytest.mark.skipif(os.geteuid() == 0, reason="needs a non-root owner of the 0700 directory"),
    pytest.mark.skipif(not _available(), reason="Docker daemon or local image unavailable"),
]


def _write_as(user: str, directory) -> subprocess.CompletedProcess[str]:
    return _docker(
        "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true", "--user", user,
        "--mount", f"type=bind,src={directory},dst=/outputs",
        "--entrypoint", "sh", IMAGE, "-c", "echo ok > /outputs/result.txt",
    )  # fmt: skip


def test_poller_uid_writes_the_private_output_dir_and_65532_cannot(tmp_path) -> None:
    outputs = tmp_path / "workspace"
    outputs.mkdir(mode=0o700)
    outputs.chmod(0o700)
    assert (outputs.stat().st_mode & 0o777) == 0o700
    own = f"{os.geteuid()}:{os.getegid()}"
    if own == "65532:65532":
        pytest.skip("poller already runs as 65532")
    assert _write_as("65532:65532", outputs).returncode != 0  # the gap on native Linux
    result = _write_as(own, outputs)
    assert result.returncode == 0, result.stderr
    assert (outputs / "result.txt").read_text() == "ok\n"
    assert (outputs.stat().st_mode & 0o777) == 0o700  # custody unchanged
