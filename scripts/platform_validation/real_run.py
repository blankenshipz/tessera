"""Reusable driver for REAL local-Docker runs through the public surface only.

Everything here goes through what an operator or agent can reach:

* a real `tessera serve` (the `Context` from `common`) and HTTP control calls,
* the real worker CLI as a subprocess (`python -m tessera_worker.cli development-run`),
* the local Docker daemon, only to discover a local image and to check/remove the
  exact resources this driver's runs created (never pull, build or prune).

The workloads are tiny generic `python -c` programs; nothing here is experiment
specific. The driver does not make provider calls, and records only what it observed.

This is partly white-box, not purely public surface: `expected_staged_digest` imports
`tessera_control` internals (`staged_inventory`, `inventory_digest`) and computes the
expected digest in-process; everything else is the public HTTP surface, the worker CLI
and the local Docker CLI.

Ownership of Docker resources: a baseline of `tessera-op-*`/`tessera-egress-proxy-*`
containers, `tessera-egress-*` networks and `tessera-*` volumes is taken before each worker
run; anything NEW afterwards (set difference) is owned by that run for `residue()` and
`cleanup()`, in addition to the recorded operation ids. Pre-existing resources are never
touched. A new resource is adopted only when this run's operation id is in its name or in its
`tessera.operation` label, so a concurrent unrelated Tessera process's resources are never
removed (a run whose operation id is unknown adopts nothing). Cleanup is exact-name and
never forced.

Public API (extend here, not in the checks):

    RealRun(ctx, image)            one project, one worker credential, one temp root
      .setup()                     project, worker credential file, enrol, seed snapshot,
                                   execution limits
      .run_task(label, code, ...)  task + plan + approve + release + grant + worker CLI
                                   -> RunRecord (worker JSON, operation id, ids)
      .labbook(event_type)         all project events, optionally filtered
      .egress_event(operation_id)  the recorded `execution.egress_journal_recorded`
      .manifest_for(operation_id)  the artifact manifest of a run (retained by the worker)
      .retain_and_release(m)       human disposition retain_quarantined + custody release
      .fetch_artifact(m, path)     released artifact bytes (via the human content route)
      .expected_staged_digest(m, paths)   inventory digest, reusing control's own function
      .stage(m, paths)             POST artifact-snapshots, then release it to the worker
      .staging_record(snapshot_id) GET /snapshots/{id}/staging
      .plan_experiment(code, ...)  baseline+intervention local-cpu tasks and an experiment
      .admit_run(kind)             approve (once) + grant + human admission -> RunRecord
                                   with .attempt (NOT yet run; bind a split first if wanted)
      .execute(record)             the worker CLI for an admitted/planned record
      .attempt_view(experiment_id, attempt_id)   current admitted-attempt row (revision, state)
      .grant_view(grant_id)        GET /execution-grants/{id}
      .residue()                   docker resources still named after this driver's operations
      .cleanup()                   stop (if running) and remove exactly those resources by name,
                                   never `--force`; removal failures stay reported as residue

Decision POSTs (approve, grant, snapshot release) all go through `ctx.request`, which
labels them as automation (`common.is_decision_call`). Disposition, custody release and
artifact staging are not in `approval_trust.DECISION_EVENT_TYPES`, so they carry no label.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .common import REPO, Context, _later, db_rows, scrub

IMAGE_PREFIX = "python@sha256:"


def local_python_image() -> str | None:
    """A local `python@sha256:` reference from the host's images; never pulls."""
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
            if digest.startswith(IMAGE_PREFIX):
                return digest
    return None


_OWN_PREFIXES = {
    "containers": ("tessera-op-", "tessera-egress-proxy-"),
    "networks": ("tessera-egress-",),
    "volumes": ("tessera-",),
}
_LISTINGS = {
    "containers": ("ps", "-a", "--format", "{{.Names}}"),
    "networks": ("network", "ls", "--format", "{{.Name}}"),
    "volumes": ("volume", "ls", "--format", "{{.Name}}"),
}


def _tessera_resources() -> dict[str, set[str]]:
    """Names of tessera-prefixed containers, networks and volumes currently on the host."""
    found: dict[str, set[str]] = {}
    for kind, args in _LISTINGS.items():
        done = _docker(*args)
        if done.returncode != 0:
            raise RuntimeError(f"docker {args[0]} listing failed: {done.stderr.strip()[-80:]}")
        found[kind] = {n for n in done.stdout.split() if n.startswith(_OWN_PREFIXES[kind])}
    return found


_INSPECT_LABELS = {
    "containers": ("inspect", "--format", "{{json .Config.Labels}}"),
    "networks": ("network", "inspect", "--format", "{{json .Labels}}"),
    "volumes": ("volume", "inspect", "--format", "{{json .Labels}}"),
}


def _exact_names(kind: str, operation_id: str) -> frozenset[str]:
    """The exact resource names the worker derives from an operation id."""
    return {
        "containers": frozenset(
            {f"tessera-op-{operation_id}", f"tessera-egress-proxy-{operation_id}"}
        ),
        "networks": frozenset({f"tessera-egress-{operation_id}"}),
        "volumes": frozenset(),
    }[kind]


def resource_belongs_to(kind: str, name: str, operation_id: str) -> bool | None:
    """Whether the resource belongs to this operation, by exact name or exact label.

    True: its name is exactly one derived from the operation id, or its
    `tessera.operation` label equals the id. False: it carries a different label (or
    none). None: the label could not be read, so ownership is unknown and the caller
    must neither adopt nor silently ignore it.
    """
    if not operation_id:
        return False
    if name in _exact_names(kind, operation_id):
        return True
    done = _docker(*_INSPECT_LABELS[kind], name)
    if done.returncode != 0:
        return None
    try:
        labels = json.loads(done.stdout) or {}
    except ValueError:
        return None
    return isinstance(labels, dict) and labels.get("tessera.operation") == operation_id


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _writable_tree(root: Path) -> None:
    """Staged inputs are 0444/0555; make only our own temp tree removable."""
    for path in [root, *root.rglob("*")]:
        try:
            if not path.is_symlink():
                os.chmod(path, stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


@dataclass
class WorkerHandle:
    """A started, not yet collected worker CLI subprocess."""

    record: RunRecord
    before: dict[str, set[str]]
    out: Path
    err: Path
    process: subprocess.Popen


@dataclass
class RunRecord:
    label: str
    task_id: str
    grant_id: str
    snapshot_id: str
    worker_returncode: int = -1
    worker: dict = field(default_factory=dict)
    worker_stderr: str = ""
    attempt: dict = field(default_factory=dict)
    experiment_id: str = ""

    @property
    def operation_id(self) -> str:
        return str((self.worker.get("receipt") or {}).get("operation_id", ""))

    @property
    def quarantined(self) -> bool:
        return self.worker.get("state") == "exited" and self.worker.get("quarantined") is True


class RealRun:
    def __init__(self, ctx: Context, image: str) -> None:
        self.ctx, self.image = ctx, image
        self.root = Path(tempfile.mkdtemp(prefix="tessera-pv-real-")).resolve()
        os.chmod(self.root, 0o700)
        self.operations: list[str] = []
        # Docker resources that appeared during a worker run (kind -> names), owned by this driver.
        # Resources that appeared during a run whose ownership could not be established.
        self.unattributed: list[str] = []
        # Removals that failed or timed out during cleanup().
        self.cleanup_errors: list[str] = []
        self.adopted: dict[str, set[str]] = {
            "containers": set(),
            "networks": set(),
            "volumes": set(),
        }
        self._seed_released = False
        self.runs: list[RunRecord] = []
        self.project: dict = {}
        self.worker_principal = ""
        self.seed: dict = {}
        self._token = secrets.token_urlsafe(48)
        self.credential_file = self.root / "worker.credential"

    # -- setup --------------------------------------------------------------
    def setup(self) -> None:
        call = self.ctx.call
        self.project = call("POST", "/projects", {"name": "Platform validation real run"})
        pid = self.project["id"]
        credential = call(
            "POST",
            "/credentials",
            {
                "kind": "worker",
                "audience": "worker",
                "token": self._token,
                "project_id": pid,
                "expires_at": _later(60),
            },
        )
        self.worker_principal = credential["principal_id"]
        self.credential_file.write_text(self._token)
        self.credential_file.chmod(0o600)
        call(
            "POST",
            f"/projects/{pid}/workers",
            {
                "worker_principal_id": self.worker_principal,
                "approved_build_identifier": "synthetic-pv-real-run",
                "approved_build_digest": "c" * 64,
                "trust_class": "test",
                "region": "local",
            },
        )
        source = self.root / "seed"
        source.mkdir()
        (source / "seed.txt").write_text("public synthetic seed\n")
        inventory = json.loads(self.ctx.cli("workspace", "inventory", "--root", str(source)))
        self.seed = json.loads(
            self.ctx.cli(
                "workspace",
                "adopt",
                "--root",
                str(source),
                "--state-dir",
                str(self.ctx.state_dir),
                "--project-id",
                pid,
                "--expected-digest",
                inventory["digest"],
                "--idempotency-key",
                "pv-real-seed",
            )
        )
        call(
            "PUT",
            f"/projects/{pid}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 0,
                "max_concurrency": 4,  # finished grants stay active until released
            },
        )

    # -- one run ------------------------------------------------------------
    def release_snapshot(self, snapshot: dict) -> dict:
        return self.ctx.call(
            "POST",
            f"/snapshots/{snapshot['id']}/releases",
            {
                "expected_digest": snapshot["digest"],
                "worker_principal_id": self.worker_principal,
                "classification": "public",
                "permits_worker_transfer": True,
                "rationale": "Synthetic generic bytes for a local platform-validation run.",
                "expires_at": _later(30),
            },
        )

    def run_task(
        self,
        label: str,
        code: str,
        *,
        output_paths: list[str],
        snapshot: dict | None = None,
        egress: dict | None = None,
        max_output_bytes: int = 65536,
        max_duration_seconds: int = 120,
        max_ticks: int = 400,
        proxy_image: str | None = None,
        timeout: int = 300,
    ) -> RunRecord:
        """Plan, approve, release, grant and run one `python -c` workload on Docker."""
        record = self.prepare_task(
            label,
            code,
            output_paths=output_paths,
            snapshot=snapshot,
            egress=egress,
            max_output_bytes=max_output_bytes,
            max_duration_seconds=max_duration_seconds,
        )
        self._worker(record, proxy_image, max_ticks, timeout)
        if record.operation_id and record.operation_id not in self.operations:
            self.operations.append(record.operation_id)
        return record

    def prepare_task(
        self,
        label: str,
        code: str,
        *,
        output_paths: list[str],
        snapshot: dict | None = None,
        egress: dict | None = None,
        max_output_bytes: int = 65536,
        max_duration_seconds: int = 120,
    ) -> RunRecord:
        """Plan, approve, release and grant one workload without starting a worker."""
        call, pid = self.ctx.call, self.project["id"]
        snapshot = snapshot or self.seed
        if snapshot is self.seed and not self._seed_released:
            # Control refuses a second release of a snapshot already released to the worker
            # (it must be revoked first), so the seed is released once per driver.
            self.release_snapshot(snapshot)
            self._seed_released = True
        spec: dict = {
            "argv": ["python", "-c", code],
            "input_snapshot_digest": snapshot["digest"],
            "pinned_skill_digests": [],
            "output_paths": output_paths,
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": max_output_bytes,
        }
        if egress is not None:
            spec["egress"] = egress
        draft = {
            "title": label,
            "purpose": "Synthetic platform validation, not science.",
            "budget_cents": 0,
            "max_duration_seconds": max_duration_seconds,
        }
        task = call("POST", f"/projects/{pid}/tasks", draft)
        task = call(
            "PUT",
            f"/tasks/{task['id']}/plan",
            {**draft, "expected_revision": task["revision"], "execution_spec": spec},
        )
        approved = call(
            "POST",
            f"/tasks/{task['id']}/approve",
            {
                "expected_revision": task["revision"],
                "expected_plan_digest": task["current_plan_digest"],
            },
        )
        grant = call(
            "POST",
            f"/projects/{pid}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": approved["revision"],
                "expected_plan_digest": task["current_plan_digest"],
                "worker_principal_id": self.worker_principal,
                "snapshot_id": snapshot["id"],
                "pinned_image": self.image,
                "expires_at": _later(15),
                "max_exposure_cents": 0,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
        )
        record = RunRecord(label, task["id"], grant["id"], snapshot["id"])
        self.runs.append(record)
        return record

    # -- experiment-admitted runs ---------------------------------------------
    def plan_experiment(
        self,
        code: str,
        *,
        output_paths: list[str],
        snapshot: dict | None = None,
        max_output_bytes: int = 65536,
        max_duration_seconds: int = 120,
    ) -> dict:
        """A baseline and an intervention variant, each with its own planned local-cpu task.

        Tasks are planned (immutable plan digest) but not approved; `admit_run` approves
        a variant's task once and issues one grant per run.
        """
        call, pid = self.ctx.call, self.project["id"]
        snapshot = snapshot or self.seed
        if snapshot is self.seed:
            self.release_snapshot(snapshot)
        spec = {
            "argv": ["python", "-c", code],
            "input_snapshot_digest": snapshot["digest"],
            "pinned_skill_digests": [],
            "output_paths": output_paths,
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": max_output_bytes,
        }
        self._argv = spec["argv"]
        self._snapshot = snapshot
        self.tasks: dict[str, dict] = {}
        self.approved: dict[str, dict] = {}
        variants = []
        for kind in ("baseline", "intervention"):
            draft = {
                "title": f"{kind} generic program",
                "purpose": "Synthetic platform validation, not science.",
                "budget_cents": 0,
                "max_duration_seconds": max_duration_seconds,
            }
            task = call("POST", f"/projects/{pid}/tasks", draft)
            task = call(
                "PUT",
                f"/tasks/{task['id']}/plan",
                {**draft, "expected_revision": task["revision"], "execution_spec": spec},
            )
            self.tasks[kind] = task
            variants.append(
                {
                    "kind": kind,
                    "name": kind,
                    "method_digest": task["current_plan_digest"],
                    "input_digest": snapshot["digest"],
                    "metadata": {"synthetic": True},
                    "execution_task_id": task["id"],
                    "execution_plan_digest": task["current_plan_digest"],
                }
            )
        self.experiment = call(
            "POST",
            f"/projects/{pid}/experiments",
            {
                "goal": "Synthetic platform validation.",
                "success_criterion": "none",
                "protocol": "Local Docker only; no provider.",
                "variants": variants,
            },
        )
        return self.experiment

    @property
    def argv(self) -> list[str]:
        """The exact argv submitted in the plan (for command_digest checks)."""
        return list(self._argv)

    def exp_variant(self, kind: str) -> dict:
        fresh = self.ctx.call("GET", f"/experiments/{self.experiment['id']}")
        design = fresh.get("design", fresh)
        return next(v for v in design["variants"] if v["kind"] == kind)

    def admit_run(self, kind: str = "baseline") -> RunRecord:
        """Approve the variant's task (once), issue a grant, and admit an attempt."""
        call, pid = self.ctx.call, self.project["id"]
        task = self.tasks[kind]
        if kind not in self.approved:
            self.approved[kind] = call(
                "POST",
                f"/tasks/{task['id']}/approve",
                {
                    "expected_revision": task["revision"],
                    "expected_plan_digest": task["current_plan_digest"],
                },
            )
        approved = self.approved[kind]
        grant = call(
            "POST",
            f"/projects/{pid}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": approved["revision"],
                "expected_plan_digest": task["current_plan_digest"],
                "worker_principal_id": self.worker_principal,
                "snapshot_id": self._snapshot["id"],
                "pinned_image": self.image,
                "expires_at": _later(15),
                "max_exposure_cents": 0,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
        )
        experiment = call("GET", f"/experiments/{self.experiment['id']}")
        variant = self.exp_variant(kind)
        attempt = call(
            "POST",
            f"/experiment-variants/{variant['id']}/admitted-attempts",
            {
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": experiment.get(
                    "current_design_revision", self.experiment["current_design_revision"]
                ),
                "grant_id": grant["id"],
            },
        )
        record = RunRecord(
            f"admitted {kind}",
            task["id"],
            grant["id"],
            self._snapshot["id"],
            attempt=attempt,
            experiment_id=self.experiment["id"],
        )
        self.runs.append(record)
        return record

    def execute(self, record: RunRecord, *, max_ticks: int = 400, timeout: int = 300) -> RunRecord:
        """Run the real worker CLI for a record created by `admit_run`."""
        self._worker(record, None, max_ticks, timeout)
        if record.operation_id and record.operation_id not in self.operations:
            self.operations.append(record.operation_id)
        return record

    def attempt_view(self, experiment_id: str, attempt_id: str) -> dict:
        export = self.ctx.call("GET", f"/experiments/{experiment_id}/export")
        return next(a for a in export["admitted_attempts"] if a["id"] == attempt_id)

    def grant_view(self, grant_id: str) -> dict:
        return self.ctx.call("GET", f"/execution-grants/{grant_id}")

    def _worker_invocation(
        self, record: RunRecord, proxy_image: str | None, max_ticks: int
    ) -> tuple[list[str], dict[str, str]]:
        base = self.root / f"worker-{len(self.runs)}"
        workspace, outputs = base / "workspace", base / "outputs"
        for path in (base, workspace, outputs):
            path.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        command = [
            sys.executable,
            "-m",
            "tessera_worker.cli",
            "development-run",
            "--control-url",
            self.ctx.base_url,
            "--grant-id",
            record.grant_id,
            "--credential-file",
            str(self.credential_file),
            "--workspace-root",
            str(workspace),
            "--development-output-root",
            str(outputs),
            "--allowed-image",
            self.image,
            "--poll-seconds",
            "0.5",
            "--max-ticks",
            str(max_ticks),
        ]
        if proxy_image:
            command += ["--egress-proxy-image", proxy_image]
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return command, env

    def _worker(
        self, record: RunRecord, proxy_image: str | None, max_ticks: int, timeout: int
    ) -> None:
        command, env = self._worker_invocation(record, proxy_image, max_ticks)
        before = _tessera_resources()
        new: dict[str, set[str]] = {}
        done = None
        timed_out = False
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, cwd=REPO, env=env, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills only the worker process; its container keeps running, so
            # resources are still adopted below and the run is reported as failed.
            timed_out = True
        finally:
            # Anything new after the run (even from a crashed or timed-out worker) that
            # belongs to an operation control recorded for this grant is ours. The ids come
            # from control's records, not from the worker's output, so a crashed worker
            # (which prints nothing) is still adopted.
            after = self._snapshot_after(before)
            new = {kind: after[kind] - before[kind] for kind in after}
            operations = self._control_operations(record.grant_id)
            for operation_id in operations:
                if operation_id not in self.operations:
                    self.operations.append(operation_id)
            self._adopt(new, operations)
        if timed_out or done is None:
            record.worker_returncode = -1
            record.worker_stderr = f"worker timed out after {timeout}s"
            record.worker = {}
            return
        record.worker_returncode = done.returncode
        record.worker_stderr = scrub(done.stderr.strip()[-400:], 400)
        try:
            record.worker = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            record.worker = {}

    # -- non-blocking worker (control may be killed while it runs) ----------------
    def start_worker(
        self, record: RunRecord, *, max_ticks: int = 400, proxy_image: str | None = None
    ) -> WorkerHandle:
        """Start the real worker CLI without waiting for it; `finish_worker` collects it."""
        command, env = self._worker_invocation(record, proxy_image, max_ticks)
        out = self.root / f"w{len(self.runs)}-{time.monotonic_ns()}.out"
        err = out.with_suffix(".err")
        handle = WorkerHandle(
            record,
            before=_tessera_resources(),
            out=out,
            err=err,
            process=None,  # type: ignore[arg-type]
        )
        with out.open("wb") as o, err.open("wb") as e:
            handle.process = subprocess.Popen(command, stdout=o, stderr=e, cwd=REPO, env=env)
        return handle

    def finish_worker(self, handle: WorkerHandle, *, timeout: int = 120) -> RunRecord:
        """Wait (bounded) for a started worker, fill the record, adopt owned resources."""
        record = handle.record
        timed_out = False
        try:
            handle.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            handle.process.kill()  # kills the worker process only; its container is cleaned by name
            handle.process.wait(timeout=10)
            timed_out = True
        after = self._snapshot_after(handle.before)
        new = {kind: after[kind] - handle.before[kind] for kind in after}
        operations = self._control_operations(record.grant_id)
        for operation_id in operations:
            if operation_id not in self.operations:
                self.operations.append(operation_id)
        self._adopt(new, operations)
        stdout = handle.out.read_text(errors="replace") if handle.out.exists() else ""
        stderr = handle.err.read_text(errors="replace") if handle.err.exists() else ""
        record.worker_returncode = -1 if timed_out else handle.process.returncode
        record.worker_stderr = scrub(
            f"worker timed out after {timeout}s" if timed_out else stderr.strip()[-400:], 400
        )
        try:
            record.worker = {} if timed_out else json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            record.worker = {}
        return record

    def _snapshot_after(self, before: dict[str, set[str]]) -> dict[str, set[str]]:
        """Resources present after a worker run. If the listing fails nothing can be
        adopted, so the failure is recorded as unattributed (the residue case then reports
        GAP instead of a clean PASS)."""
        try:
            return _tessera_resources()
        except RuntimeError as error:
            self.unattributed.append(f"post-run resource listing failed: {error}")
            return before

    def _control_operations(self, grant_id: str) -> list[str]:
        """Operation ids control recorded for a grant (read-only state read)."""
        if not grant_id or not re.fullmatch(r"[A-Za-z0-9_-]+", grant_id):
            return []
        try:
            rows = db_rows(
                self.ctx.state_dir,
                f"SELECT operation_id FROM external_operations WHERE grant_id='{grant_id}'",
            )
        except Exception:  # noqa: BLE001 - an unreadable record means nothing is adopted
            self.unattributed.append(f"control operations for {grant_id} unreadable")
            return []
        return [str(row[0]) for row in rows]

    def _adopt(self, new: dict[str, set[str]], operation_ids: list[str]) -> None:
        """Own only new resources that belong to one of this run's operations (exact name
        or exact `tessera.operation` label). A concurrent Tessera process's resources are
        never adopted; a resource whose label cannot be read is listed in `unattributed`
        so the evidence says so."""
        for kind, names in new.items():
            for name in sorted(names):
                verdicts = [resource_belongs_to(kind, name, op) for op in operation_ids]
                if any(v is True for v in verdicts):
                    self.adopted[kind].add(name)
                elif operation_ids and any(v is None for v in verdicts):
                    self.unattributed.append(f"{kind}:{name} (label unreadable)")

    # -- control-plane reads ------------------------------------------------
    def labbook(self, event_type: str | None = None) -> list[dict]:
        events, after = [], 0
        while True:
            page = self.ctx.call(
                "GET", f"/projects/{self.project['id']}/labbook?after={after}&limit=200"
            )["events"]
            events += page
            if len(page) < 200:
                break
            after = page[-1]["cursor"]
        return [e for e in events if event_type in (None, e["event_type"])]

    def egress_event(self, operation_id: str) -> dict | None:
        """The recorded `execution.egress_journal_recorded` payload for an operation."""
        for event in self.labbook("execution.egress_journal_recorded"):
            if event["payload"].get("operation_id") == operation_id:
                return event["payload"]
        return None

    def manifest_for(self, operation_id: str) -> dict | None:
        listed = self.ctx.call("GET", f"/projects/{self.project['id']}/artifact-manifests")
        for manifest in listed["artifact_manifests"]:
            if manifest.get("operation_id") == operation_id:
                artifacts = manifest.get("artifacts")
                if artifacts is None:
                    artifacts = json.loads(manifest["artifacts_json"])
                return {**manifest, "artifacts": artifacts}
        return None

    # -- custody and staging --------------------------------------------------
    def retain_and_release(self, manifest: dict) -> None:
        """Human retain_quarantined disposition, then custody release of that manifest."""
        digest, base = manifest["manifest_digest"], f"/artifact-manifests/{manifest['id']}"
        self.ctx.call(
            "POST",
            f"{base}/dispositions",
            {
                "expected_manifest_digest": digest,
                "expected_revision": 0,
                "disposition": "retain_quarantined",
                "rationale": "Synthetic platform-validation bytes; retained in quarantine.",
            },
        )
        self.ctx.call(
            "POST",
            f"{base}/custody-releases",
            {
                "expected_manifest_digest": digest,
                "expected_revision": 0,
                "action": "release",
                "rationale": "Release synthetic bytes for human-composed staging.",
            },
        )

    def fetch_artifact(self, manifest: dict, relative_path: str) -> bytes:
        """Released bytes via the human content route (needs custody release)."""
        import urllib.request

        item = next(a for a in manifest["artifacts"] if a["relative_path"] == relative_path)
        name, value = self.ctx.owner_header()
        request = urllib.request.Request(
            f"{self.ctx.base_url}/artifact-manifests/{manifest['id']}/artifacts/{item['id']}/content",
            headers={name: value},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    @staticmethod
    def expected_staged_digest(manifest: dict, paths: list[str] | None = None) -> str:
        """Derived in-process with control's own `staged_inventory`/`inventory_digest`.

        The expected digest is the inventory digest of root `artifact-manifest:{id}` and the
        selected manifest entries; it is computed from the manifest the control plane lists,
        not copied from the stage response.
        """
        sys.path.insert(0, str(REPO / "src"))
        from tessera_control.dataset_staging import staged_inventory
        from tessera_control.workspace import inventory_digest

        entries = [a for a in manifest["artifacts"] if paths is None or a["relative_path"] in paths]
        return inventory_digest(staged_inventory(manifest["id"], entries))

    def stage(self, manifest: dict, paths: list[str] | None = None) -> dict:
        """Stage released artifacts as a snapshot, then human-release it to the worker."""
        body = {
            "manifest_id": manifest["id"],
            "expected_manifest_digest": manifest["manifest_digest"],
            "expected_snapshot_digest": self.expected_staged_digest(manifest, paths),
        }
        if paths is not None:
            body["relative_paths"] = paths
        snapshot = self.ctx.call("POST", f"/projects/{self.project['id']}/artifact-snapshots", body)
        self.release_snapshot(snapshot)
        return snapshot

    def worker_auth(self) -> dict[str, str]:
        """Authorization header of the enrolled worker credential (for negative tests only)."""
        return {"Authorization": f"Bearer {self._token}"}

    def staging_record(self, snapshot_id: str) -> dict:
        return self.ctx.call("GET", f"/snapshots/{snapshot_id}/staging")

    # -- residue and cleanup --------------------------------------------------
    def _owned(self) -> dict[str, list[str]]:
        names = {kind: sorted(found) for kind, found in self.adopted.items()}
        for op in self.operations:
            names["containers"] += [f"tessera-op-{op}", f"tessera-egress-proxy-{op}"]
            names["networks"].append(f"tessera-egress-{op}")
        return {kind: sorted(set(v)) for kind, v in names.items()}

    def residue(self) -> list[str]:
        """Docker resources still present that this driver owns (by operation id or by
        having appeared during one of its worker runs)."""
        owned = self._owned()
        found = []
        for kind, args in _LISTINGS.items():
            done = _docker(*args)
            if done.returncode != 0:
                raise RuntimeError(f"docker {args[0]} listing failed: {done.stderr.strip()[-80:]}")
            present = set(done.stdout.split())
            found += [f"{kind}:{n}" for n in owned[kind] if n in present]
        return found

    def residue_report(self, left: list[str]) -> tuple[str, str]:
        """Status and evidence for the docker-residue case after `cleanup()` returned `left`.

        FAIL when anything this driver owns remains or a removal failed or timed out; GAP when
        a new resource appeared whose ownership could not be established (so absence of
        residue cannot be asserted); PASS otherwise.
        """
        try:
            after = self.residue()
        except RuntimeError as error:
            after = [f"listing failed: {error}"]
        retained = [x for x in left if x.startswith("containers:tessera-op-")]
        unexpected = [x for x in left if x not in retained]
        if unexpected or after or self.cleanup_errors:
            return (
                "FAIL",
                f"left by workers: {unexpected}; after cleanup: {after}; "
                f"cleanup errors: {self.cleanup_errors}",
            )
        if self.unattributed:
            return (
                "GAP",
                f"ownership of new resource(s) could not be established: {self.unattributed}",
            )
        return (
            "PASS",
            f"{len(self.operations)} operation(s) recorded by control; {len(retained)} exited "
            "workload container(s) (tessera-op-<operation>) retained by the worker were removed "
            "by exact name without force; other Tessera resources that appeared during a run "
            "belonged to no recorded operation and were not touched",
        )

    def cleanup(self) -> list[str]:
        """Remove exactly the leftover resources this driver owns, by exact name, no force.

        A running container is stopped by exact name first. A removal that fails is left in
        place, so `residue()` reports it afterwards. Returns what was found before removal.
        """
        left = self.residue()

        def attempt(label: str, *args: str) -> None:
            # One hung or failing docker call must not stop the removal of the others; it
            # is recorded, and residue() reports whatever is still present afterwards.
            try:
                done = _docker(*args)
            except subprocess.TimeoutExpired:
                self.cleanup_errors.append(f"{label}: docker {args[0]} timed out")
                return
            if done.returncode != 0:
                self.cleanup_errors.append(f"{label}: docker {args[0]} failed")

        for item in left:
            kind, name = item.split(":", 1)
            if kind == "containers":
                try:
                    state = _docker("inspect", "--format", "{{.State.Running}}", name)
                except subprocess.TimeoutExpired:
                    self.cleanup_errors.append(f"{item}: docker inspect timed out")
                    continue
                if state.returncode == 0 and state.stdout.strip() == "true":
                    attempt(item, "stop", name)
                attempt(item, "rm", name)
            elif kind == "networks":
                attempt(item, "network", "rm", name)
            else:
                attempt(item, "volume", "rm", name)
        _writable_tree(self.root)
        shutil.rmtree(self.root, ignore_errors=True)
        return left
