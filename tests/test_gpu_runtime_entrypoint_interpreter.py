"""The fixed runtime entrypoint must exec a python3 on both runtime bases (ADR 0076).

Official Python images (the CPU recipe's base) ship ``/usr/local/bin/python3`` and may
have no ``/usr/bin/python3``; apt-based images (the GPU recipe) ship ``/usr/bin/python3``.
No Docker here: each case builds a temporary root with stand-in interpreters, rewrites
the script's absolute paths under that root, and runs it with ``/bin/sh``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "workloads/gpu_runtime_v1"
RUN = RUNTIME_DIR / "run"
LOCAL_PY = "/usr/local/bin/python3"
APT_PY = "/usr/bin/python3"
RUNTIME_PY = "/tessera/lib/runtime.py"
# The pre-fix entrypoint: hard-requires the apt path, so an official Python base exits 127.
OLD_RUN = (
    "#!/bin/sh\n"
    "# Fixed entrypoint: no shell expansion of arguments, no environment lookups.\n"
    'exec /usr/bin/python3 -I /tessera/lib/runtime.py "$@"\n'
)
ARGS = ["v1", "op 1", "$HOME", "*", "-c", "x"]


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _stand_in(path: Path, record: Path, label: str) -> None:
    # Records which interpreter ran and its argv, and requires runtime.py to exist.
    _executable(
        path,
        f"#!/bin/sh\ntest -f \"$2\" || exit 3\nprintf '%s\\n' {label} \"$@\" > '{record}'\n",
    )


def _rooted(script: str, root: Path) -> str:
    out = script
    for p in (LOCAL_PY, APT_PY, RUNTIME_PY):
        out = out.replace(p, f"{root}{p}")
    # Every absolute path the script touches must have been moved under the fixture root.
    body = "\n".join(line for line in out.splitlines()[1:] if not line.lstrip().startswith("#"))
    stray = [m for m in re.findall(r"(?<![\w.-])/[\w./-]+", body) if not m.startswith(str(root))]
    assert stray == [], stray
    return out


def _fixture(tmp_path: Path, interpreters: tuple[str, ...]) -> tuple[Path, Path]:
    root = tmp_path / "root"
    record = tmp_path / "record"
    (root / RUNTIME_PY.lstrip("/")).parent.mkdir(parents=True)
    (root / RUNTIME_PY.lstrip("/")).write_text("raise SystemExit(0)\n")
    for interp in interpreters:
        _stand_in(root / interp.lstrip("/"), record, interp)
    # A decoy python3 on PATH must never be used: the entrypoint does no PATH lookup.
    _stand_in(tmp_path / "decoy/python3", record, "decoy")
    return root, record


def _run(script: str, tmp_path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    entry = tmp_path / "run"
    _executable(entry, _rooted(script, root))
    env = {"PATH": f"{tmp_path / 'decoy'}:/usr/bin:/bin"}
    return subprocess.run(
        ["/bin/sh", str(entry), *ARGS], env=env, capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize(
    ("interpreters", "chosen"),
    [
        ((LOCAL_PY,), LOCAL_PY),  # official python:3.x base
        ((APT_PY,), APT_PY),  # apt-installed python3 (GPU recipe)
        ((LOCAL_PY, APT_PY), LOCAL_PY),  # full Debian python image: the pip target wins
    ],
)
def test_entrypoint_execs_fixed_interpreter(tmp_path, interpreters, chosen):
    root, record = _fixture(tmp_path, interpreters)
    result = _run(RUN.read_text(), tmp_path, root)
    assert result.returncode == 0, (result.returncode, result.stderr)
    lines = record.read_text().splitlines()
    assert lines == [chosen, "-I", f"{root}{RUNTIME_PY}", *ARGS]


def test_entrypoint_without_python_fails_closed(tmp_path):
    root, record = _fixture(tmp_path, ())
    result = _run(RUN.read_text(), tmp_path, root)
    assert result.returncode == 127 and not record.exists()


def test_old_entrypoint_exits_127_on_official_python_base(tmp_path):
    # Regression witness: the one-liner this replaced never reached runtime.py.
    root, record = _fixture(tmp_path, (LOCAL_PY,))
    result = _run(OLD_RUN, tmp_path, root)
    assert result.returncode == 127 and not record.exists()


def test_entrypoint_has_no_environment_lookups():
    code = [
        line
        for line in RUN.read_text().splitlines()[1:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(code)
    assert "PATH" not in joined and "command -v" not in joined and "which" not in joined
    assert set(re.findall(r"\$\{?(\w+|@)", joined)) == {"@"}
    assert RUN.stat().st_mode & 0o111


def _cpu_interpreter_check() -> str:
    text = (RUNTIME_DIR / "Dockerfile.cpu").read_text()
    lines = [ln for ln in text.splitlines() if "py=" in ln or 'readlink -f "$py"' in ln]
    assert len(lines) == 2, lines
    return " && ".join(ln.strip().lstrip("&").strip().rstrip("\\").strip() for ln in lines)


@pytest.mark.parametrize(
    ("interpreters", "path_dir", "ok"),
    [
        ((LOCAL_PY,), "/usr/local/bin", True),
        ((APT_PY,), "/usr/bin", True),
        ((LOCAL_PY, APT_PY), "/usr/local/bin", True),
        # pip would install wheels for /usr/bin/python3 while run picks /usr/local/bin.
        ((LOCAL_PY, APT_PY), "/usr/bin", False),
        ((), "/usr/bin", False),
    ],
)
def test_cpu_recipe_checks_entrypoint_interpreter(tmp_path, interpreters, path_dir, ok):
    root, _ = _fixture(tmp_path, interpreters)
    snippet = _cpu_interpreter_check()
    for p in (LOCAL_PY, APT_PY):
        snippet = snippet.replace(p, f"{root}{p}")
    env = {"PATH": f"{root}{path_dir}:/usr/bin:/bin"}
    result = subprocess.run(["/bin/sh", "-c", snippet], env=env, capture_output=True, timeout=30)
    assert (result.returncode == 0) is ok, result.stderr


def test_cpu_recipe_adds_no_package_manager_python():
    text = (RUNTIME_DIR / "Dockerfile.cpu").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for tool in ("apt-get", "apt ", "apk ", "yum ", "dnf ", "microdnf"):
        assert tool not in code
    assert 'ENTRYPOINT ["/tessera/bin/run"]' in text and os.sep == "/"
