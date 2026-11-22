"""Work runs with no session: the supervised worker daemon, driven as boot would.

Local only: a real `tessera serve` on loopback and, for the live case, network-none
local Docker. No provider, no external network, no pull or build.

* daemon-credential-backoff / daemon-status-health: the REAL daemon supervising the REAL
  worker CLI against the REAL control with a credential control rejects. Needs no grant
  poller. Proves end to end that a 401 makes the CLI exit 3 (credential_rejected), that
  the daemon backs off by the authentication schedule instead of crash-looping, that
  `status` reports health, and that SIGTERM stops daemon and child.
* work-runs-with-no-session / control-restart-no-session: needs the grant poller
  (`development-poll`). The daemon is started ONCE; the harness then only submits jobs and
  watches control. It never invokes the worker CLI. If this tree has no poller the cases
  are an honest GAP.

The daemon is the only process this module signals (SIGTERM, then its process group).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tessera_worker.exit_codes import EXIT_CREDENTIAL_REJECTED

from .common import REPO, Context, Result
from .real_run import RealRun, local_python_image

CAPABILITY = "no-session"
STATUS = "status.json"
OBSERVE_SECONDS = 16.0
WAIT_RECEIPT_SECONDS = 150.0
NO_POLLER = (
    "not run: this tree has no work source (the `development-poll` command, a grant poller "
    "that lists a worker's pending grants, is not present); the supervised daemon accepts "
    "it unchanged"
)
WORKLOAD = "open('/outputs/result.txt','w').write('no-session\\n')"
OUTPUT_PATHS = ["result.txt"]


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _read_status(state: Path) -> dict | None:
    try:
        document = json.loads((state / STATUS).read_text())
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _status_cli(state: Path) -> tuple[int, dict | None]:
    done = subprocess.run(
        [sys.executable, "-m", "tessera_worker.daemon", "status", "--state-dir", str(state)],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=_env(),
        timeout=30,
    )
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        return done.returncode, None


class Daemon:
    """One `tessera_worker.daemon run` subprocess in its own process group."""

    def __init__(self, state: Path, daemon_args: list[str], worker_args: list[str]) -> None:
        self.state = state
        command = [
            sys.executable,
            "-m",
            "tessera_worker.daemon",
            "run",
            "--state-dir",
            str(state),
            *daemon_args,
            "--",
            *worker_args,
        ]
        self.log = state.parent / (state.name + ".log")
        with self.log.open("wb") as log:
            self.process = subprocess.Popen(
                command,
                stdout=log,
                stderr=log,
                cwd=REPO,
                env=_env(),
                start_new_session=True,
            )

    def wait_status(self, timeout: float = 20.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            document = _read_status(self.state)
            if document is not None and document.get("pid") == self.process.pid:
                return document
            if self.process.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    def stop(self, grace: float) -> tuple[int | None, bool]:
        """SIGTERM the daemon. Returns (exit code or None if it had to be killed, killed)."""
        if self.process.poll() is not None:
            return self.process.returncode, False
        self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=grace + 10), False
        except subprocess.TimeoutExpired:
            self.kill_group()
            return None, True

    def kill_group(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=10)


def run(ctx: Context) -> list[Result]:
    out: list[Result] = []

    def add(case: str, status: str, evidence: str) -> None:
        out.append(Result(CAPABILITY, case, status, evidence))

    started = time.monotonic()
    ctx.start_service()
    image = local_python_image() or "python@sha256:" + "0" * 64
    work = Path(tempfile.mkdtemp(prefix="tessera-pv-nosession-")).resolve()
    os.chmod(work, 0o700)
    try:
        _backoff_cases(ctx, work, image, add)
        if not _poller_supported():
            add("work-runs-with-no-session", "GAP", NO_POLLER)
            add("control-restart-no-session", "GAP", NO_POLLER)
        else:
            _live_cases(ctx, work, started, add)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# -- cases 1 and 2 -------------------------------------------------------------------
def _backoff_cases(ctx: Context, work: Path, image: str, add) -> None:
    state = work / "bad-state"
    workspace, outputs = _private(work / "bad-ws"), _private(work / "bad-out")
    bad = work / "bad.credential"
    bad.write_text(secrets.token_urlsafe(48))  # well formed, unknown to control
    bad.chmod(0o600)
    daemon = Daemon(
        state,
        [
            "--auth-backoff-min",
            "4",
            "--auth-backoff-max",
            "8",
            "--backoff-min",
            "1",
            "--backoff-max",
            "4",
            "--grace-seconds",
            "10",
        ],
        [
            "development-run",
            "--control-url",
            ctx.base_url,
            "--grant-id",
            "grant_unused",
            "--credential-file",
            str(bad),
            "--workspace-root",
            str(workspace),
            "--development-output-root",
            str(outputs),
            "--allowed-image",
            image,
        ],
    )
    try:
        if daemon.wait_status() is None:
            add(
                "daemon-credential-backoff",
                "FAIL",
                f"daemon wrote no status (exit {daemon.process.poll()})",
            )
            add("daemon-status-health", "GAP", "daemon did not start")
            return
        starts: list[float] = []
        child_pids: set[int] = set()
        states: list[str] = []
        kinds: set[str] = set()
        codes: set[int] = set()
        samples: list[dict] = []
        health: tuple[int, dict | None] | None = None
        deadline = time.monotonic() + OBSERVE_SECONDS
        while time.monotonic() < deadline:
            document = _read_status(state)
            if document:
                if document.get("last_start") and document["last_start"] not in starts:
                    starts.append(document["last_start"])
                if document.get("child_pid"):
                    child_pids.add(document["child_pid"])
                if not states or states[-1] != document.get("state"):
                    states.append(str(document.get("state")))
                    samples.append({k: document.get(k) for k in _SAMPLE_KEYS})
                if document.get("last_exit_kind"):
                    kinds.add(document["last_exit_kind"])
                    codes.add(document.get("last_exit_code"))
                if health is None and document.get("last_exit_kind"):
                    health = _status_cli(state)
            time.sleep(0.5)
        code, killed = daemon.stop(10)
        final = _read_status(state) or {}
        after = _status_cli(state)
        leftover = [pid for pid in child_pids if _alive(pid)]
        spacing = [round(b - a, 1) for a, b in zip(starts, starts[1:], strict=False)]

        problems = []
        if kinds != {"credential_rejected"} or codes != {3}:
            problems.append(f"exit kinds={sorted(kinds)} codes={sorted(codes, key=str)}")
        if "credential_backoff" not in states:
            problems.append(f"never in credential_backoff (states={states})")
        if len(starts) < 2:
            problems.append(f"only {len(starts)} start(s) observed")
        if len(starts) > 4:
            problems.append(f"{len(starts)} starts in {OBSERVE_SECONDS:.0f}s (crash loop)")
        if any(gap < 4.0 for gap in spacing):
            problems.append(f"starts closer than the 4s auth minimum: {spacing}")
        if code != 0 or killed:
            problems.append(f"daemon exit on SIGTERM={code} killed={killed}")
        if final.get("state") != "stopped":
            problems.append(f"final state={final.get('state')}")
        if leftover:
            problems.append("child process still alive after stop")
        add(
            "daemon-credential-backoff",
            "FAIL" if problems else "PASS",
            "; ".join(problems)
            or (
                f"real 401 -> worker CLI exit 3 (kind credential_rejected); states={states}; "
                f"{len(starts)} start(s) in {OBSERVE_SECONDS:.0f}s, gaps={spacing}s "
                f"(auth min 4s, no crash loop); SIGTERM -> daemon exit 0, state stopped, "
                f"{len(child_pids)} child pid(s) seen, none alive"
            ),
        )
        problems = []
        # Alive but waiting out a rejected credential is degraded (exit 3); a running child is
        # healthy (exit 0). Both must report a fresh status file.
        health_state = (health[1] or {}).get("state") if health else None
        want = EXIT_CREDENTIAL_REJECTED if health_state == "credential_backoff" else 0
        if health is None or health[0] != want or not (health[1] or {}).get("fresh"):
            problems.append(f"status while supervising (state {health_state}): {health}")
        if after[0] != 1 or (after[1] or {}).get("state") != "stopped":
            problems.append(
                f"status after stop: exit {after[0]} state {(after[1] or {}).get('state')}"
            )
        add(
            "daemon-status-health",
            "FAIL" if problems else "PASS",
            "; ".join(problems)
            or (
                f"while supervising: exit {health[0]} (expected {want}), fresh="
                f"{(health[1] or {}).get('fresh')}, state={health[1].get('state')}; "
                f"after stop: exit {after[0]}, state={after[1].get('state')}; "
                f"status.json samples={json.dumps(samples, sort_keys=True)}"
            ),
        )
    finally:
        daemon.kill_group()
        shutil.rmtree(state, ignore_errors=True)


_SAMPLE_KEYS = (
    "state",
    "restarts",
    "consecutive_failures",
    "last_exit_code",
    "last_exit_kind",
    "subcommand",
)


# -- cases 3 and 4 -------------------------------------------------------------------
def _poller_supported() -> bool:
    try:
        done = subprocess.run(
            [sys.executable, "-m", "tessera_worker.cli", "development-poll", "--help"],
            capture_output=True,
            cwd=REPO,
            env=_env(),
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _await_receipt(ctx: Context, driver: RealRun, record, timeout: float) -> tuple[bool, str]:
    """Wait for control to hold an observed run receipt for the grant's operation."""
    deadline = time.monotonic() + timeout
    operation = ""
    receipt: dict | None = None
    while time.monotonic() < deadline:
        operations = driver._control_operations(record.grant_id)
        if operations:
            operation = operations[0]
            status, body = ctx.request("GET", f"/external-operations/{operation}/run-receipt")
            if status == 200 and isinstance(body, dict):
                receipt = body
                break
        time.sleep(1.0)
    if operation and operation not in driver.operations:
        driver.operations.append(operation)
    if receipt is None:
        return (
            False,
            f"no run receipt within {timeout:.0f}s (operation={'set' if operation else 'none'})",
        )
    manifest = driver.manifest_for(operation)
    want = {
        "provider_kind": "local_docker_cpu",
        "exit_code": 0,
        "command_digest": _sha(_canon(["python", "-c", WORKLOAD])),
        "output_digest": manifest["manifest_digest"] if manifest else None,
        "receipt_evidence": "observed_provider_execution",
    }
    problems = [f"{k} mismatch" for k, v in want.items() if receipt.get(k) != v]
    if manifest is None:
        problems.append("control has no artifact manifest for the operation")
    return not problems, "; ".join(problems) or (
        f"receipt observed: provider_kind={receipt['provider_kind']} exit_code=0 "
        "output_digest == control manifest digest, command_digest == sha256(canonical argv)"
    )


def _live_cases(ctx: Context, work: Path, started: float, add) -> None:
    image = local_python_image()
    if image is None or shutil.which("docker") is None:
        reason = "not run: no local python@sha256 image / docker CLI (the harness never pulls)"
        add("work-runs-with-no-session", "GAP", reason)
        add("control-restart-no-session", "GAP", reason)
        return
    driver = RealRun(ctx, image)
    daemon: Daemon | None = None
    state = work / "daemon-state"
    try:
        driver.setup()
        workspace, outputs = _private(work / "ws"), _private(work / "out")
        daemon = Daemon(
            state,
            [
                "--backoff-min",
                "1",
                "--backoff-max",
                "6",
                "--restart-delay",
                "1",
                "--grace-seconds",
                "15",
            ],
            [
                "development-poll",
                "--control-url",
                ctx.base_url,
                "--credential-file",
                str(driver.credential_file),
                "--workspace-root",
                str(workspace),
                "--development-output-root",
                str(outputs),
                "--allowed-image",
                image,
                "--poll-seconds",
                "1",
            ],
        )
        up = daemon.wait_status()
        if up is None:
            add(
                "work-runs-with-no-session",
                "FAIL",
                f"daemon did not start ({daemon.process.poll()})",
            )
            add("control-restart-no-session", "GAP", "daemon did not start")
            return
        # The daemon is up; from here the harness only submits jobs and reads control.
        first = driver.prepare_task("no-session-1", WORKLOAD, output_paths=OUTPUT_PATHS)
        ok, evidence = _await_receipt(ctx, driver, first, WAIT_RECEIPT_SECONDS)
        running = _read_status(state) or {}
        child_alive = _alive(running.get("child_pid"))
        before = running.get("restarts")
        healthy = running.get("state") == "running" and child_alive
        add(
            "work-runs-with-no-session",
            "PASS" if ok and healthy else "FAIL",
            f"{evidence}; daemon state={running.get('state')} child_alive={child_alive} "
            f"restarts={before}; the harness made 0 worker CLI invocations (daemon started once)",
        )
        if not ok:
            add("control-restart-no-session", "GAP", "not run: the first job did not complete")
            return
        ctx.stop_service(kill=True)
        ctx.restart_service(reuse_port=True)
        second = driver.prepare_task("no-session-2", WORKLOAD, output_paths=OUTPUT_PATHS)
        ok2, evidence2 = _await_receipt(ctx, driver, second, WAIT_RECEIPT_SECONDS)
        after = (_read_status(state) or {}).get("restarts")
        add(
            "control-restart-no-session",
            "PASS" if ok2 else "FAIL",
            f"control SIGKILLed and restarted on the same port; {evidence2}; daemon restarts "
            f"{before} -> {after}; nothing started by hand",
        )
    except Exception as error:  # noqa: BLE001 - a crash in the live section is a FAIL
        add("no-session-live", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if daemon is not None:
            code, killed = daemon.stop(15)
            final = _read_status(state) or {}
            add(
                "daemon-sigterm-stop",
                "PASS" if code == 0 and not killed and final.get("state") == "stopped" else "FAIL",
                f"exit={code} killed={killed} state={final.get('state')}",
            )
            daemon.kill_group()
            shutil.rmtree(state, ignore_errors=True)
        try:
            left = driver.cleanup()
            status, evidence = driver.residue_report(left)
        except Exception as error:  # noqa: BLE001
            status, evidence = "FAIL", f"{type(error).__name__}: {error}"
        add("docker-residue", status, evidence)
