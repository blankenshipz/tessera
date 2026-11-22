"""Items 1 and 5: what local Docker sandboxing actually proves on this host.

Honest by construction. Three evidence classes are kept apart:

* the Tessera fixture (`scripts/check_local_docker_fixture.py`), the only path that
  exercises the supervisor; on macOS/LinuxKit it fails the namespace-proof preflight
  before any container exists, which is recorded as GAP, never PASS;
* direct `docker run` flags, NOT the Tessera supervisor path (a check that the
  flags behave, not a sandbox-product proof);
* ADR 0034 allowlisted egress: the in-tree synthetic tests (no live egress); live
  allowlisted egress on local Docker stays a GAP.

No pull, build, prune or provider call. Only `tessera-pv-` containers created here
are touched, by exact name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .common import REPO, Context, Result

CAPABILITY = "docker_sandbox"
PREFIX = "tessera-pv-"
EGRESS_TESTS = (
    "tests/test_sandbox_egress_control.py",
    "tests/test_sandbox_egress_worker.py",
    "tests/test_agent_sandbox_egress.py",
    "tests/test_agent_sandbox_egress_worker.py",
    "tests/test_dataset_staging.py",
)
DIRECT = "direct docker flags, not the Tessera supervisor path"

_PROBE = r"""
import socket, sys
results = {}
s = socket.socket(); s.settimeout(2)
try:
    s.connect(("1.1.1.1", 53)); results["ip"] = "connected"
except OSError as e:
    results["ip"] = "blocked:" + type(e).__name__
try:
    socket.getaddrinfo("example.com", 80); results["dns"] = "resolved"
except OSError as e:
    results["dns"] = "blocked:" + type(e).__name__
print(results)
sys.exit(0 if all(v.startswith("blocked") for v in results.values()) else 3)
"""


def _sh(args: list[str], timeout: int = 60, cwd: Path | None = None, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)


def _docker(*args: str, timeout: int = 60):
    return _sh(["docker", *args], timeout=timeout)


class _ListingError(RuntimeError):
    """A docker listing failed, so residue cannot be asserted either way."""


def _listing(*args: str) -> list[str]:
    done = _docker(*args)
    if done.returncode != 0:
        raise _ListingError(
            f"docker {' '.join(args[:2])} rc={done.returncode}: {done.stderr.strip()[-80:]}"
        )
    return done.stdout.split()


def _owned_resources() -> set[str]:
    """Fixture-owned containers (`tessera-local-<nonce>`) and output volumes.

    The /output volume is `tessera-local-out-<nonce>` and labelled `tessera.fixture`;
    both the name prefix and the label are scanned so a mislabelled leftover is seen.
    A failed listing raises `_ListingError`: it is a FAIL, never "residue=0".
    """
    containers = _listing("ps", "-a", "--format", "{{.Names}}")
    by_name = _listing("volume", "ls", "--format", "{{.Name}}")
    by_label = _listing(
        "volume", "ls", "--filter", "label=tessera.fixture", "--format", "{{.Name}}"
    )
    found = {f"container:{n}" for n in containers if n.startswith("tessera-local-")}
    found |= {f"volume:{n}" for n in by_name if n.startswith("tessera-local-out-")}
    return found | {f"volume:{n}" for n in by_label}


_VOLUME_PROBE = """
import json
from tessera_worker import local_docker_contract as contract
from tessera_worker.local_docker_contract import FixturePlan
from tessera_worker.local_docker_engine import output_volume_arguments
plan = FixturePlan(
    profile="local-networkless-fixture-engine-attested-v1",
    operation_id="0" * 64, nonce="0" * 64,
    image="python@sha256:" + "a" * 64, mode="success",
)
print(json.dumps(dict(
    args=output_volume_arguments(plan), name=plan.output_volume_name,
    prefix=contract.OUTPUT_VOLUME_PREFIX, origin=contract.__file__,
)))
"""


def _output_volume_check() -> tuple[bool, str]:
    """Exercise `output_volume_arguments` in the interpreter and tree the fixture runs.

    Without the labelled tmpfs-typed /output volume `docker cp` of the /output tmpfs
    returns an empty archive on Docker Desktop, so success/failure could never be
    proven and residue would go unscanned. The probe runs in a subprocess with the
    same interpreter, working directory and environment as the fixture subprocess.
    """
    try:
        done = _sh([sys.executable, "-c", _VOLUME_PROBE], cwd=REPO, timeout=60)
        probe = json.loads(done.stdout)
        args, name = probe["args"], probe["name"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        return False, f"output-volume probe failed: {type(error).__name__}: {error}"
    if done.returncode != 0:
        return False, f"output-volume probe rc={done.returncode}"
    opts = {a for i, a in enumerate(args[:-1]) if args[i - 1] == "--opt" and i}
    labels = {a for i, a in enumerate(args[:-1]) if args[i - 1] == "--label" and i}
    problems = []
    if args[:4] != ["volume", "create", "--driver", "local"]:
        problems.append("not a local-driver volume create")
    if not {"type=tmpfs", "device=tmpfs"} <= opts:
        problems.append("volume is not tmpfs-typed")
    if f"tessera.output_volume={name}" not in labels or not any(
        label.startswith("tessera.fixture=") for label in labels
    ):
        problems.append("volume is not labelled to the fixture and operation")
    if not name.startswith(probe.get("prefix", "") or "tessera-local-out-") or args[-1] != name:
        problems.append("volume name is not the exact owned name")
    if problems:
        return False, "; ".join(problems)
    origin = Path(probe.get("origin", "")).resolve()
    try:
        where = str(origin.relative_to(REPO))
    except ValueError:
        where = "outside the repository (installed package)"
    return True, (
        f"output_volume_arguments gives a labelled tmpfs-typed local volume; module {where}; "
        "residue scan covers containers and volumes"
    )


def _local_python_image() -> str | None:
    """A local `python@sha256:` reference; never pulls."""
    listed = _docker(
        "image", "ls", "--no-trunc", "--filter", "reference=python", "--format", "{{.ID}}"
    )
    for image_id in listed.stdout.split():
        info = _docker("image", "inspect", image_id, "--format", "{{json .RepoDigests}}")
        try:
            digests = json.loads(info.stdout)
        except ValueError:
            continue
        for digest in digests or []:
            if digest.startswith("python@sha256:"):
                return digest
    return None


_STRONG_CASES = (
    "network-none-success",
    "network-none-failure",
    "network-none-cancel",
    "network-none-deadline",
    "network-none-disconnect",
    "stop-cleanup",
)


def _fixture(out: list[Result], image: str | None) -> None:
    script = REPO / "scripts" / "check_local_docker_fixture.py"
    default = _sh([sys.executable, str(script)], cwd=REPO)
    ok = '"effects": 0' in default.stdout and '"disabled"' in default.stdout
    out.append(
        Result(
            CAPABILITY,
            "fixture-default-zero-effects",
            "PASS" if ok else "FAIL",
            f"[fixture] {default.stdout.strip()[:120]}",
        )
    )
    if image is None:
        out.append(
            Result(
                CAPABILITY,
                "fixture-networkless-run",
                "GAP",
                "[fixture] no local python@sha256 image; not run",
            )
        )
        return
    root = Path(tempfile.mkdtemp(prefix=PREFIX + "fx-")).resolve()
    evidence = root / "evidence"  # must not exist yet
    try:
        before = _owned_resources()
        run = _sh(
            [
                sys.executable,
                str(script),
                "--run",
                "--image",
                image,
                "--evidence-dir",
                str(evidence),
            ],
            timeout=300,
            cwd=REPO,
        )
        leftovers = sorted(_owned_resources() - before)
        try:
            report = json.loads(run.stdout)
        except ValueError:
            report = {}
        cases = report.get("cases", {})
        gates = set()
        top = evidence / "summary.json"
        if top.exists():
            for case in json.loads(top.read_text()).get("cases", {}).values():
                net = case.get("records", {}).get("network", {}).get("observation", {})
                if net.get("gate"):
                    gates.add(net["gate"])
        state = report.get("state")
        names = (
            "network-none-success",
            "network-none-failure",
            "network-none-cancel",
            "network-none-deadline",
            "network-none-disconnect",
            "stop-cleanup",
        )
        if leftovers:
            for case in names:
                out.append(
                    Result(
                        CAPABILITY,
                        case,
                        "FAIL",
                        f"[fixture] residue containers/volumes: {leftovers}",
                    )
                )
        elif state == "blocked" and gates:
            note = (
                f"[fixture] blocked before container creation: {sorted(gates)}; "
                "platform limitation (macOS/LinuxKit namespace proof); "
                f"cases_run={sorted(cases)} residue=0"
            )
            for case in names:
                out.append(Result(CAPABILITY, case, "GAP", note))
        elif state in ("passed", "succeeded", "completed") and run.returncode == 0:
            for case in names:
                out.append(
                    Result(
                        CAPABILITY,
                        case,
                        "PASS",
                        f"[fixture] state={state} cases={sorted(cases)} residue=0",
                    )
                )
        else:
            for case in names:
                out.append(
                    Result(
                        CAPABILITY,
                        case,
                        "FAIL",
                        f"[fixture] state={state} rc={run.returncode} {run.stderr[-120:]}",
                    )
                )
    except _ListingError as error:
        for case in _STRONG_CASES:
            out.append(Result(CAPABILITY, case, "FAIL", f"[fixture] residue unverifiable: {error}"))
    except subprocess.TimeoutExpired:
        out.append(
            Result(CAPABILITY, "fixture-networkless-run", "FAIL", "[fixture] timed out after 300s")
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


WEAK_CLASS = "engine_declared_workload_observed"
WEAK_ENV = "TESSERA_PV_WEAK_ATTESTATION"
_WEAK_CASES = {
    "network-none-success": ("success", "completed"),
    "network-none-failure": ("failure", "failed"),
    "network-none-cancel": ("cancel", "cancelled"),
    "network-none-deadline": ("timeout", "unknown"),
    "network-none-disconnect": ("broker-loss", "unknown"),
}


def _fixture_weak(out: list[Result], image: str | None) -> None:
    """Opt-in bounded weak-attestation run (ADR 0035): PASS-WEAK, never PASS.

    One invocation, no retry. The class is read back from the journal-derived
    summary, not assumed; a missing or mismatched class is a FAIL.
    """
    names = tuple(_WEAK_CASES)
    if os.environ.get(WEAK_ENV) != "1" or image is None:
        why = "no local python@sha256 image" if image is None else f"set {WEAK_ENV}=1 to run"
        for case in names:
            out.append(Result(CAPABILITY, f"{case}[weak]", "GAP", f"[fixture:weak] not run: {why}"))
        return
    ok, detail = _output_volume_check()
    out.append(Result(CAPABILITY, "output-volume-precondition", "PASS" if ok else "FAIL", detail))
    if not ok:
        return  # fail fast: the weak run would be unprovable and its residue unscanned
    script = REPO / "scripts" / "check_local_docker_fixture.py"
    root = Path(tempfile.mkdtemp(prefix=PREFIX + "fxw-")).resolve()
    evidence = root / "evidence"
    try:
        before = _owned_resources()
        run = _sh(
            [
                sys.executable,
                str(script),
                "--run",
                "--image",
                image,
                "--evidence-dir",
                str(evidence),
                "--attestation-class",
                WEAK_CLASS,
            ],
            timeout=600,
            cwd=REPO,
        )
        leftovers = sorted(_owned_resources() - before)
        try:
            report = json.loads(run.stdout)
        except ValueError:
            report = {}
        cases = report.get("cases", {})
        label = "[fixture:weak, engine-declared + workload-observed; not independent proof]"
        proven = (
            report.get("state") == "passed_weak_attestation"
            and report.get("attestation_class") == WEAK_CLASS
            and run.returncode == 0
            and not leftovers
        )
        for name, (key, state) in _WEAK_CASES.items():
            got = cases.get(key, {})
            ok = (
                proven
                and got.get("state") == state
                and got.get("attestation_class") == WEAK_CLASS
                and got.get("termination_confirmed") is True
                and got.get("cleanup_confirmed") is True
            )
            if ok:
                status = "PASS-WEAK"
                note = (
                    f"state={state} termination_confirmed=True cleanup_confirmed=True "
                    f"residue=0 (containers+output volume) {label}"
                )
            elif report.get("state") == "blocked" and not leftovers:
                status = "GAP"
                note = (
                    f"blocked: report_state={report.get('state')} cases={sorted(cases)} "
                    f"case_state={got.get('state')} reason={report.get('reason')} "
                    f"rc={run.returncode} "
                    f"stderr_tail={run.stderr[-100:]!r} {label}"
                )
            else:
                status = "FAIL"
                note = f"state={report.get('state')} rc={run.returncode} leftovers={leftovers}"
            out.append(Result(CAPABILITY, f"{name}[weak]", status, note))
    except _ListingError as error:
        for case in names:
            out.append(
                Result(CAPABILITY, f"{case}[weak]", "FAIL", f"residue unverifiable: {error}")
            )
    except subprocess.TimeoutExpired:
        out.append(Result(CAPABILITY, "fixture-weak-run", "FAIL", "timed out after 600s"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _direct(out: list[Result], image: str | None) -> None:
    if image is None:
        for case in ("direct-network-none", "direct-stop-cleanup"):
            out.append(
                Result(CAPABILITY, case, "GAP", "no local python@sha256 image; will not pull")
            )
        return
    hardening = [
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--user",
        "65532",
        "--pull",
        "never",
    ]
    name = f"{PREFIX}net-{uuid.uuid4().hex[:8]}"
    try:
        probe = _docker(
            "run", "--rm", "--name", name, *hardening, image, "python", "-c", _PROBE, timeout=60
        )
        blocked = probe.returncode == 0
        out.append(
            Result(
                CAPABILITY,
                "direct-network-none",
                "PASS" if blocked else "FAIL",
                f"[{DIRECT}] rc={probe.returncode} {(probe.stdout or probe.stderr).strip()[:160]}",
            )
        )
    finally:
        _docker("rm", "-f", name)  # exact name created above

    name = f"{PREFIX}stop-{uuid.uuid4().hex[:8]}"
    try:
        started = _docker(
            "run",
            "-d",
            "--name",
            name,
            *hardening,
            image,
            "python",
            "-c",
            "import time; time.sleep(300)",
        )
        if started.returncode:
            out.append(
                Result(
                    CAPABILITY,
                    "direct-stop-cleanup",
                    "FAIL",
                    f"[{DIRECT}] start failed: {started.stderr.strip()[:160]}",
                )
            )
            return
        running = _docker("inspect", name, "--format", "{{.State.Running}}").stdout.strip()
        _docker("stop", "-t", "1", name)
        stopped = _docker("inspect", name, "--format", "{{.State.Running}}").stdout.strip()
        removed = _docker("rm", name).returncode == 0
        residue = _docker(
            "ps", "-a", "--filter", f"name={PREFIX}", "--format", "{{.Names}}"
        ).stdout.split()
        good = running == "true" and stopped == "false" and removed and not residue
        out.append(
            Result(
                CAPABILITY,
                "direct-stop-cleanup",
                "PASS" if good else "FAIL",
                f"[{DIRECT}] running={running} after_stop={stopped} "
                f"removed={removed} {PREFIX}residue={len(residue)}",
            )
        )
    finally:
        _docker("rm", "-f", name)


def _egress(out: list[Result]) -> None:
    case = "adr0034-allowlisted-egress"
    present = [t for t in EGRESS_TESTS if (REPO / t).is_file()]
    missing = [t for t in EGRESS_TESTS if t not in present]
    if missing:
        out.append(Result(CAPABILITY, case, "FAIL", f"in-tree egress tests missing: {missing}"))
    elif _sh([sys.executable, "-m", "pytest", "--version"]).returncode:
        out.append(
            Result(
                CAPABILITY,
                case,
                "GAP",
                f"pytest not importable by {sys.executable}; in-tree egress tests not run",
            )
        )
    else:
        root = Path(tempfile.mkdtemp(prefix=PREFIX + "egress-")).resolve()
        try:
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            src = str(REPO / "src")
            env["PYTHONPATH"] = os.pathsep.join(
                [src, str(REPO / "tests"), *filter(None, [env.get("PYTHONPATH")])]
            )
            shown = _sh(
                [sys.executable, "-c", "import tessera_control as t;print(t.__file__)"],
                env=env,
                cwd=root,
            )
            if not shown.stdout.strip().startswith(src):
                out.append(
                    Result(
                        CAPABILITY,
                        case,
                        "FAIL",
                        f"tessera_control resolved to {shown.stdout.strip()}, not {src}",
                    )
                )
            else:
                done = _sh(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "--basetemp",
                        str(root / ".pytest-tmp"),
                        *EGRESS_TESTS,
                    ],
                    timeout=480,
                    cwd=REPO,
                    env=env,
                )
                tail = done.stdout.strip().splitlines()[-1:] or [""]
                out.append(
                    Result(
                        CAPABILITY,
                        case,
                        "FAIL" if done.returncode else "PASS",
                        f"{len(EGRESS_TESTS)} in-tree test files: {tail[0]} "
                        "(synthetic, no live egress)",
                    )
                )
        except (subprocess.SubprocessError, OSError) as error:
            out.append(Result(CAPABILITY, case, "FAIL", f"{type(error).__name__}: {error}"))
        finally:
            subprocess.run(["chmod", "-R", "u+rwX", str(root)], capture_output=True)
            shutil.rmtree(root, ignore_errors=True)
    # Live allowlisted egress is exercised by check_real_run.py (egress-live-run).


def run(ctx: Context) -> list[Result]:
    out: list[Result] = []
    if shutil.which("docker") is None or _docker("info", timeout=30).returncode:
        out.append(
            Result(
                CAPABILITY,
                "docker-available",
                "GAP",
                "docker daemon not reachable; nothing exercised",
            )
        )
    else:
        image = _local_python_image()
        _fixture(out, image)
        _fixture_weak(out, image)
        _direct(out, image)
    _egress(out)
    out.append(
        Result(
            CAPABILITY,
            "public-path-network-none-agent-sandbox-local-docker",
            "GAP",
            "no public CLI/MCP path runs a network-none agent sandbox on local Docker "
            "end-to-end: `tessera agent-sandbox` targets Kubernetes Agent Sandbox; "
            "ordinary-docker-v1 needs a control plane and guard image absent locally",
        )
    )
    return out
