"""Shared harness for opt-in platform validation checks.

Everything here is local and synthetic: a real `tessera serve` subprocess on a
loopback port with a temporary state directory. No provider, network or paid call.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_BOOT = "import sys; from tessera_control.cli import main; sys.argv[0]='tessera'; sys.exit(main())"
# PASS-WEAK is a weak-attestation result (ADR 0035): counted separately, never as PASS.
AUTOMATION_HEADER = "X-Tessera-Automation"
AUTOMATION_LABEL = "platform-validation"
# Routes whose events are decisions in approval_trust.DECISION_EVENT_TYPES and that this
# harness calls: task approval, grant issue, attempt admission, snapshot release,
# budget authority and the resolution of an interaction decision. Anything else is not a
# decision and carries no label.
_DECISION_ROUTES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/tasks/[^/]+/approve$",
        r"^/projects/[^/]+/execution-grants$",
        r"^/experiment-variants/[^/]+/admitted-attempts$",
        r"^/snapshots/[^/]+/releases$",
        r"^/experiments/[^/]+/budget/(authorization|charges|bounds)$",
        r"^/interaction/decisions/resolve$",
    )
)


def is_decision_call(method: str, path: str) -> bool:
    return method.upper() == "POST" and any(
        r.match(path.split("?", 1)[0]) for r in _DECISION_ROUTES
    )


STATUSES = ("PASS", "PASS-WEAK", "FAIL", "GAP")


def scrub(text: str, limit: int = 300) -> str:
    """Bound evidence and drop absolute home paths."""
    text = str(text).replace(str(Path.home()), "~").replace(str(REPO), ".")
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class Result:
    capability: str
    case: str
    status: str
    evidence: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"bad status {self.status}")
        self.evidence = scrub(self.evidence)

    def to_dict(self) -> dict[str, str]:
        return {
            "capability": self.capability,
            "case": self.case,
            "status": self.status,
            "evidence": self.evidence,
        }


@dataclass
class Context:
    state_dir: Path
    base_url: str = ""
    owner_header_path: Path = field(default=Path())
    process: subprocess.Popen | None = None
    port: int = 0
    _log: Path | None = None

    @classmethod
    def create(cls, root: Path | None = None) -> Context:
        base = Path(root) if root else Path(tempfile.mkdtemp(prefix="tessera-pv-")).resolve()
        state = base / "state"
        return cls(state_dir=state, owner_header_path=state / "owner.header")

    # -- process management -------------------------------------------------
    def _cli(self, *args: str) -> list[str]:
        return [sys.executable, "-c", _BOOT, *args]

    def init_state(self) -> None:
        if not (self.state_dir / "owner.header").exists():
            subprocess.run(
                self._cli("init", "--state-dir", str(self.state_dir)),
                check=True,
                capture_output=True,
                cwd=REPO,
                timeout=120,
            )

    def cli(self, *args: str) -> str:
        """Run a tessera CLI subcommand (e.g. workspace adopt) and return stdout."""
        done = subprocess.run(
            self._cli(*args), capture_output=True, text=True, cwd=REPO, timeout=120
        )
        if done.returncode:
            raise RuntimeError(scrub(f"cli {args[0]} failed: {done.stderr[-200:]}"))
        return done.stdout

    def start_service(self, reuse_port: bool = False) -> None:
        """Start `tessera serve`; reuse_port keeps the previous port (and base_url) so a
        worker started earlier can reconnect to the same address after a restart."""
        if self.process and self.process.poll() is None:
            raise RuntimeError("service already running")
        self.init_state()
        if not (reuse_port and self.port):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._log = self.state_dir.parent / "serve.log"
        log = self._log.open("ab")
        self.process = subprocess.Popen(
            self._cli("serve", "--state-dir", str(self.state_dir), "--port", str(self.port)),
            stdout=log,
            stderr=log,
            cwd=REPO,
        )
        log.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(scrub(f"serve exited {self.process.returncode}: {self._tail()}"))
            try:
                with urllib.request.urlopen(self.base_url + "/health/ready", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        self.stop_service(kill=True)
        raise RuntimeError("serve did not become ready in 60s")

    def _tail(self) -> str:
        try:
            return self._log.read_text(errors="replace")[-200:] if self._log else ""
        except OSError:
            return ""

    def stop_service(self, kill: bool = False) -> None:
        proc, self.process = self.process, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def restart_service(self, reuse_port: bool = False) -> None:
        self.stop_service()
        self.start_service(reuse_port=reuse_port)

    def cleanup(self) -> None:
        self.stop_service(kill=True)
        shutil.rmtree(self.state_dir.parent, ignore_errors=True)

    # -- HTTP ---------------------------------------------------------------
    def owner_header(self) -> tuple[str, str]:
        name, value = self.owner_header_path.read_text().strip().split(": ", 1)
        return name, value

    def new_key(self) -> str:
        return "pv-" + uuid.uuid4().hex

    def request(
        self,
        method: str,
        path: str,
        json: object = None,  # noqa: A002 - mirrors the requests-style name
        auth: str = "human",
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object]:
        """auth: 'human' sends the owner header; 'none' sends no credential."""
        import json as jsonlib

        hdrs = dict(headers or {})
        if auth == "human":
            name, value = self.owner_header()
            hdrs.setdefault(name, value)
            # Owner-operated automation is declared only on decision routes, so the
            # recorded decision_class says so (approval-trust policy); every other
            # call sends no label.
            if is_decision_call(method, path):
                hdrs.setdefault(AUTOMATION_HEADER, AUTOMATION_LABEL)
        elif auth != "none":
            raise ValueError("auth must be 'human' or 'none'")
        if idempotency_key:
            hdrs["Idempotency-Key"] = idempotency_key
        data = None
        if json is not None:
            data = jsonlib.dumps(json).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            return status, jsonlib.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")

    def call(
        self, method, path, body=None, *, key=None, expected=(200, 201), headers=None, auth="human"
    ):
        """Mutating call that asserts the status; returns the JSON body."""
        key = key or (self.new_key() if method != "GET" else None)
        status, payload = self.request(method, path, body, auth, key, headers)
        if status not in expected:
            raise RuntimeError(scrub(f"{method} {path} -> {status}: {payload}"))
        return payload


class Journey:
    """Synthetic project -> two-variant experiment -> grants -> admission, over HTTP.

    Creates a real snapshot (public synthetic file) via the CLI, a worker
    credential, and execution limits; nothing touches a provider.
    """

    def __init__(self, ctx: Context, limit_cents: int = 20, grant_cents: int = 5) -> None:
        self.ctx, self.grant_cents = ctx, grant_cents
        self.last_admit: tuple[str, dict] | None = None
        self.worker = "Bearer synthetic-worker-" + "x" * 48
        call = ctx.call
        self.project = call("POST", "/projects", {"name": "Platform validation"})
        source = ctx.state_dir.parent / "source"
        source.mkdir(exist_ok=True)
        (source / "input.txt").write_text("public synthetic input\n")
        inventory = json.loads(ctx.cli("workspace", "inventory", "--root", str(source)))
        self.snapshot = json.loads(
            ctx.cli(
                "workspace",
                "adopt",
                "--root",
                str(source),
                "--state-dir",
                str(ctx.state_dir),
                "--project-id",
                self.project["id"],
                "--expected-digest",
                inventory["digest"],
                "--idempotency-key",
                "pv-adopt",
            )
        )
        self.credential = call(
            "POST",
            "/credentials",
            {
                "kind": "worker",
                "audience": "worker",
                "token": self.worker.removeprefix("Bearer "),
                "project_id": self.project["id"],
                "expires_at": _later(60),
            },
        )
        call(
            "POST",
            f"/projects/{self.project['id']}/workers",
            {
                "worker_principal_id": self.credential["principal_id"],
                "approved_build_identifier": "synthetic-pv-worker",
                "approved_build_digest": "a" * 64,
                "trust_class": "test",
                "region": "local",
            },
        )
        call(
            "POST",
            f"/snapshots/{self.snapshot['id']}/releases",
            {
                "expected_digest": self.snapshot["digest"],
                "worker_principal_id": self.credential["principal_id"],
                "classification": "public",
                "permits_worker_transfer": True,
                "rationale": "Synthetic input only.",
                "expires_at": _later(30),
            },
        )
        call(
            "PUT",
            f"/projects/{self.project['id']}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": limit_cents,
                "max_concurrency": 2,
            },
        )
        self.tasks: dict[str, dict] = {}
        variants = []
        for kind in ("baseline", "intervention"):
            task = call(
                "POST",
                f"/projects/{self.project['id']}/tasks",
                {
                    "title": kind,
                    "purpose": "Synthetic platform validation, not science.",
                    "budget_cents": 10,
                    "max_duration_seconds": 300,
                },
            )
            task = call(
                "PUT",
                f"/tasks/{task['id']}/plan",
                {
                    "expected_revision": task["revision"],
                    "title": task["title"],
                    "purpose": task["purpose"],
                    "budget_cents": 10,
                    "max_duration_seconds": 300,
                    "execution_spec": {
                        "argv": ["python", "-c", "print(1)"],
                        "input_snapshot_digest": self.snapshot["digest"],
                        "pinned_skill_digests": [],
                        "output_paths": ["result.txt"],
                        "profile": "kubernetes-opencode",
                        "network_enabled": False,
                        "max_output_bytes": 1024,
                    },
                },
            )
            self.tasks[kind] = task
            variants.append(
                {
                    "kind": kind,
                    "name": kind,
                    "method_digest": task["current_plan_digest"],
                    "input_digest": self.snapshot["digest"],
                    "metadata": {"synthetic": True},
                    "execution_task_id": task["id"],
                    "execution_plan_digest": task["current_plan_digest"],
                }
            )
        self.experiment = call(
            "POST",
            f"/projects/{self.project['id']}/experiments",
            {
                "goal": "Synthetic platform validation.",
                "success_criterion": "none",
                "protocol": "No provider; local only.",
                "variants": variants,
            },
        )

    def worker_headers(self) -> dict[str, str]:
        return {"Authorization": self.worker}

    def admit(self, kind: str, key: str | None = None, expected=(200, 201)):
        """Approve task, create grant, admit attempt. Returns (grant, status, body, key)."""
        ctx, task = self.ctx, self.tasks[kind]
        approved = ctx.call(
            "POST",
            f"/tasks/{task['id']}/approve",
            {
                "expected_revision": task["revision"],
                "expected_plan_digest": task["current_plan_digest"],
            },
        )
        grant = ctx.call(
            "POST",
            f"/projects/{self.project['id']}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": approved["revision"],
                "expected_plan_digest": task["current_plan_digest"],
                "worker_principal_id": self.credential["principal_id"],
                "snapshot_id": self.snapshot["id"],
                "pinned_image": "python@sha256:" + "b" * 64,
                "expires_at": _later(10),
                "max_exposure_cents": self.grant_cents,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
        )
        experiment = self.experiment
        variant = next(v for v in experiment["design"]["variants"] if v["kind"] == kind)
        key = key or ctx.new_key()
        request = {
            "expected_variant_revision": variant["revision"],
            "expected_design_revision": experiment["current_design_revision"],
            "grant_id": grant["id"],
        }
        self.last_admit = (f"/experiment-variants/{variant['id']}/admitted-attempts", request)
        status, body = ctx.request("POST", self.last_admit[0], request, idempotency_key=key)
        if expected and status not in expected:
            raise RuntimeError(scrub(f"admit {kind} -> {status}: {body}"))
        return grant, status, body, key

    def export(self):
        return self.ctx.call("GET", f"/experiments/{self.experiment['id']}/export")


def _later(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def db_counts(state_dir: Path, queries: dict[str, str]) -> dict[str, int]:
    """Read-only counts straight from the state database (evidence, not a mutation)."""
    import sqlite3
    from contextlib import closing

    uri = f"file:{state_dir / 'tessera.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        return {name: db.execute(sql).fetchone()[0] for name, sql in queries.items()}


def env_ok() -> bool:
    return os.name == "posix"


def db_rows(state_dir: Path, sql: str) -> list[tuple]:
    import sqlite3
    from contextlib import closing

    uri = f"file:{state_dir / 'tessera.sqlite3'}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        return [tuple(r) for r in db.execute(sql).fetchall()]
