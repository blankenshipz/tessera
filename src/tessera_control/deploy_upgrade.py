"""``tessera deploy-upgrade``: one checked upgrade, dry-run unless told to execute (ADR 0099).

The steps a deploy took by hand, in order, each recorded in one receipt:

1. *preflight*: the deploy check over the plan's areas. Anything that fails and is not fixed by
   this command (a pending migration, a removed config key) blocks the upgrade before a service
   is touched.
2. *stop*: the services that name a ``stop_argv``, last to first.
3. *backup*: a capped, SQLite-consistent backup of the state directory into the plan's backup
   directory (a new directory per run).
4. *config fixes*: the removed keys dropped, each with the deploy-check receipt.
5. *migrate*: the existing explicit migration; the backup above is the confirmation it needs.
6. *start*: the services in plan order, each restarted and waited on until healthy.
7. *smoke*: the fixed cheap set: a second deploy check must be ready (schema current, configs
   clean, providers valid) and, if the plan asks, the synthetic networkless CPU smoke passes.
8. *receipt*: the outcome, the schema before and after, the backup, each step's duration.

A failure stops the run at that step and the receipt says which. Nothing is rolled back for the
operator: before the migration a failure restarts the services that were stopped (nothing has
changed), after it the services stay as they are and the receipt names the backup to restore.
The command never reads a secret, never contacts a provider and spends nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import deploy_check, deploy_check_cli, deploy_services, deploy_state, recovery
from . import deploy_config as config_fixes
from .migrations import LATEST_SCHEMA_VERSION
from .store import DATABASE_NAME

RECEIPT_SCHEMA = "tessera.deploy-receipt.v1"
# Fixes this command makes itself; every other failing check blocks the upgrade.
SELF_FIXED = {"migrate", "drop_removed_keys"}


SNAPSHOT_NAME = "snapshot.sqlite3"
SNAPSHOT_MANIFEST = "snapshot-manifest.json"


def take_backup(state_dir: Path, output: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    """The backup this command takes before it migrates.

    ``tessera backup`` only accepts a database already at this release's schema, so it cannot
    back up the old-schema database an upgrade starts from. A current database gets that validated
    backup; an older one gets a pre-migration snapshot instead: SQLite's online backup of the
    database file alone (migrations change only the database), bounded by the same size cap, with
    a manifest that records its schema version and digest. It is restored by stopping the services
    and putting ``snapshot.sqlite3`` back as the database with the release that wrote it, not with
    ``tessera restore``."""
    current = deploy_state._read_schema_version(state_dir)
    if current == LATEST_SCHEMA_VERSION:
        manifest = recovery.backup_state(state_dir, output, max_bytes=max_bytes)
        return {"kind": "validated", "schema_version": current, "manifest": bool(manifest)}
    limit = recovery.backup_byte_limit(max_bytes)
    database = state_dir / DATABASE_NAME
    size = os.lstat(database).st_size
    if size > limit:
        raise ValueError(f"The database ({size} bytes) exceeds the backup bound of {limit} bytes.")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("The backup output must be a new directory under an existing directory.")
    output.mkdir(mode=0o700)
    destination = output / SNAPSHOT_NAME
    source = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(destination, 0o600)
    data = destination.read_bytes()
    manifest = {
        "kind": "pre-migration-snapshot",
        "schema_version": current,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    deploy_check.write_private_json(output / SNAPSHOT_MANIFEST, manifest)
    return manifest


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def package_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("tessera")
        except PackageNotFoundError:
            return None
    except ImportError:  # pragma: no cover
        return None


def _paths(values: list[str]) -> list[Path]:
    return [Path(value) for value in values]


def preflight(plan: Any, *, smoke: bool) -> tuple[list[deploy_check.Finding], list[str]]:
    """The deploy-check findings and the ids that block this command from going on."""
    findings = deploy_check_cli.gather(
        state_dir=Path(plan.state_dir),
        executor_configs=_paths(plan.executor_configs),
        live_configs=_paths(plan.live_configs),
        providers_file=None if plan.providers_file is None else Path(plan.providers_file),
        max_backup_bytes=plan.max_backup_bytes,
        smoke=smoke,
    )
    blocking = [
        item.check_id
        for item in findings
        if item.status == "fail" and (item.fix is None or item.fix.fix_id not in SELF_FIXED)
    ]
    return findings, blocking


FocusedRunner = Callable[..., int]


def _default_focused_runner(files: list[str], root: Path, jobs: int) -> int:
    """Run the files with :func:`focused_tests.run`; pytest's output goes to stderr so the
    command's own report (``--format json``) stays the only thing on stdout."""
    from . import focused_tests

    def popen(command: list[str], **options: Any) -> subprocess.Popen:
        options.setdefault("stdout", sys.stderr)
        return subprocess.Popen(command, **options)

    with contextlib.redirect_stdout(sys.stderr):
        return focused_tests.run(files, root, jobs=jobs, popen=popen)


def checkout_head(root: Path) -> str | None:
    """The commit checked out at ``root``, or None when git cannot say."""
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    head = done.stdout.strip()
    return head if done.returncode == 0 and head else None


def _focused_finding(status: str, summary: str, **detail: Any) -> deploy_check.Finding:
    return deploy_check.Finding("tests.focused", "tests", status, summary, detail)


def focused_preflight(
    plan: Any,
    *,
    execute: bool,
    blocked: bool,
    runner: FocusedRunner,
    clock: Callable[[], float],
) -> tuple[deploy_check.Finding, dict[str, Any]]:
    """The ``tests.focused`` preflight finding and its receipt entry.

    The focused tests for ``from_revision..source_revision`` are selected from the repository
    checkout at the plan's ``repo_root`` (default: the working directory). A dry run only lists
    them; an execute run runs them with the plan's concurrency cap before any service is stopped,
    and a failure blocks the upgrade. A selection that cannot map the changes blocks too: the
    upgrade never falls back to the full suite, and so does a checkout that is not at
    ``source_revision`` (its tests would not be the release's). Without a checkout the step is
    skipped and says so."""
    began = clock()
    jobs = plan.focused_tests_jobs
    entry: dict[str, Any] = {
        "from_revision": plan.from_revision,
        "to_revision": plan.source_revision,
        "jobs": jobs,
        "selected": 0,
        "files": [],
        "smoke": [],
        "unmapped": [],
        "reasons": {},
        "result": None,
        "duration_seconds": 0.0,
    }

    def done(status: str, result: str, summary: str) -> tuple[deploy_check.Finding, dict]:
        entry["result"] = result
        entry["duration_seconds"] = round(clock() - began, 3)
        keys = ("selected", "files", "smoke", "unmapped", "reasons", "jobs")
        detail = {key: entry[key] for key in keys}
        return _focused_finding(status, summary, result=result, **detail), entry

    root = Path(plan.repo_root) if plan.repo_root is not None else Path.cwd()
    if not (root / "tests").is_dir() or not (root / "src").is_dir():
        return done(
            "skipped",
            "skipped",
            "skipped: no repository checkout (no tests/ and src/ at the plan's repo_root)",
        )
    from . import focused_tests

    try:
        changed = focused_tests.changed_files(root, f"{plan.from_revision}..{plan.source_revision}")
        selection = focused_tests.select(
            root, changed, overrides=focused_tests.load_overrides(root), smoke=True
        )
    except Exception as error:  # git cannot resolve the range, bad overrides
        return done(
            "fail",
            "error",
            f"focused tests could not be selected: {type(error).__name__}: {str(error)[:200]}",
        )
    files = list(selection.all_files)
    entry.update(
        selected=len(files),
        files=files,
        smoke=sorted(selection.smoke),
        unmapped=sorted(selection.unmapped),
    )
    if selection.unmapped and not selection.files:
        return done(
            "fail",
            "unmapped",
            f"no focused test maps to {len(selection.unmapped)} changed file(s); "
            "the upgrade does not fall back to the full suite",
        )
    head = checkout_head(root)
    if head != plan.source_revision:
        return done(
            "fail",
            "wrong-checkout",
            "the repository checkout is not at the plan's source_revision",
        )
    entry["reasons"] = _reason_counts(selection)
    if not execute:
        summary = ", ".join(f"{why} {count}" for why, count in entry["reasons"].items())
        return done(
            "ok",
            "listed",
            f"{len(files)} focused test file(s) selected, not run in a dry run"
            + (f" ({summary})" if summary else ""),
        )
    if blocked:
        return done("skipped", "not-run", "not run: the preflight is already blocked")
    if not files:
        return done("ok", "nothing-selected", "no focused test is selected by the upgrade range")
    try:
        code = runner(files, root, jobs)
    except Exception as error:  # pytest could not be started
        return done("fail", "error", f"focused tests could not run: {type(error).__name__}")
    if code != 0:
        return done("fail", "failed", f"focused tests failed (exit {code}, {len(files)} file(s))")
    return done("ok", "passed", f"{len(files)} focused test file(s) passed (jobs {jobs})")


def _reason_counts(selection: Any) -> dict[str, int]:
    """How many selected files each kind of reason (named, imports, smoke, ...) picked."""
    counts: dict[str, int] = {}
    for path in selection.all_files:
        for why in selection.reasons.get(path, []):
            counts[why] = counts.get(why, 0) + 1
    return dict(sorted(counts.items()))


def _schema(finding_list: list[deploy_check.Finding]) -> dict[str, Any]:
    for item in finding_list:
        if item.check_id == "state.schema":
            return dict(item.detail)
    return {}


class _Run:
    """The receipt as it is built; every step lands here whether it passed or not."""

    def __init__(self, plan: Any, execute: bool, clock: Callable[[], float]) -> None:
        self.plan = plan
        self.clock = clock
        self.began = clock()
        self.started_at = datetime.now(UTC)
        self.steps: list[dict[str, Any]] = []
        self.document: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "mode": "execute" if execute else "dry-run",
            "source_revision": plan.source_revision,
            "package_version": package_version(),
            "result": "ok",
            "failed_step": None,
            "schema_version": {},
            "backup": None,
            "fixes": [],
            "services": [],
            "smoke": [],
        }

    def step(
        self,
        name: str,
        ok: bool,
        detail: str,
        began: float | None = None,
        *,
        seconds: float | None = None,
        **extra: Any,
    ):
        if seconds is None:
            seconds = self.clock() - (self.began if began is None else began)
        seconds = round(seconds, 3)
        entry = {"step": name, "ok": ok, "seconds": seconds, "detail": detail, **extra}
        self.steps.append(entry)
        if not ok and self.document["failed_step"] is None:
            self.document["result"] = "failed"
            self.document["failed_step"] = name
        return entry

    def finish(self) -> dict[str, Any]:
        finished = datetime.now(UTC)
        self.document.update(
            started_at=_stamp(self.started_at),
            finished_at=_stamp(finished),
            duration_seconds=round(self.clock() - self.began, 3),
            steps=self.steps,
        )
        return self.document


def _failure(code: str, error: BaseException) -> str:
    """A stable code and the exception type only: never the message, which can carry a path."""
    return f"{code} ({type(error).__name__})"


def _not_stopped(plan: Any) -> list[str]:
    """Services this run cannot stop: no ``stop_argv``."""
    return [s.name for s in plan.services if not s.stop_argv]


def _unsafe_unstopped(plan: Any) -> list[str]:
    return [s.name for s in plan.services if not s.stop_argv and getattr(s, "writes_state", True)]


def _config_paths(plan: Any) -> list[Path]:
    return _paths([*plan.executor_configs, *plan.live_configs])


def _backup_configs(plan: Any, directory: Path) -> list[dict[str, Any]]:
    """Copy each config (owner-only) before a fix; the receipt records index and digest only."""
    paths = _config_paths(plan)
    if not paths:
        return []
    directory.mkdir(mode=0o700)
    rows = []
    for index, path in enumerate(paths):
        data = path.read_bytes()
        copy = directory / f"config-{index}.json"
        copy.write_bytes(data)
        os.chmod(copy, 0o600)
        rows.append({"index": index, "sha256": hashlib.sha256(data).hexdigest()})
    return rows


def _service_rows(results: list[deploy_services.StepResult]) -> list[dict[str, Any]]:
    return [
        {
            "step": r.step,
            "name": r.name,
            "ok": r.ok,
            "seconds": round(r.seconds, 3),
            "detail": r.detail,
        }
        for r in results
    ]


def run_upgrade(
    plan: Any,
    *,
    execute: bool,
    runner: deploy_services.Runner = deploy_services.default_runner,
    probe: Callable[..., tuple[bool, str]] = deploy_services.probe_health,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    backup: Callable[..., dict[str, Any]] = take_backup,
    focused_runner: FocusedRunner = _default_focused_runner,
) -> dict[str, Any]:
    """Run (or, by default, only plan) the upgrade and return its receipt document.

    ``focused_runner(files, root, jobs) -> exit status`` runs the plan's focused tests (only
    when the plan enables ``focused_tests``)."""
    run = _Run(plan, execute, clock)
    began = clock()
    findings, blocking = preflight(plan, smoke=False)
    if execute:
        # The directories the run writes to are proven before a service is stopped: a run
        # never ends without its receipt or its backup because a path was wrong.
        for what, directory in (
            ("receipt directory", plan.receipt_dir),
            ("backup directory", plan.backup_dir),
        ):
            try:
                deploy_check.require_writable_directory(Path(directory), what)
            except ValueError as error:
                blocking.append(f"plan.{what.split()[0]}_dir ({error})")
    unsafe = _unsafe_unstopped(plan)
    run.document["not_stopped"] = _not_stopped(plan)
    if execute:
        for name in unsafe:
            blocking.append(
                f"service.{name} (no stop_argv: it may still write the database; add a "
                "stop_argv or declare writes_state false)"
            )
    if plan.focused_tests:
        # After the writable-directory proofs and before any service is stopped.
        finding, run.document["focused_tests"] = focused_preflight(
            plan, execute=execute, blocked=bool(blocking), runner=focused_runner, clock=clock
        )
        findings.append(finding)
        if finding.status == "fail":
            blocking.append(finding.check_id)
    before = _schema(findings)
    run.document["schema_version"] = {
        "before": before.get("current"),
        "latest": before.get("latest"),
    }
    run.document["preflight"] = {
        "counts": deploy_check.build_report(findings)["counts"],
        "blocking": blocking,
        "checks": [
            {"id": item.check_id, "status": item.status, "summary": item.summary}
            for item in findings
        ],
    }
    run.step(
        "preflight", not blocking, "blocked by: " + ", ".join(blocking) if blocking else "ok", began
    )
    if blocking:
        return run.finish()
    if not execute:
        run.document["planned"] = _planned(plan, findings)
        return run.finish()

    services = list(plan.services)
    pending = before.get("current") != before.get("latest")
    stopped = deploy_services.stop_services(services, runner, clock=clock)
    run.document["services"].extend(_service_rows(stopped))
    stop_failed = [r for r in stopped if not r.ok]
    for result in stopped:
        run.step(f"stop:{result.name}", result.ok, result.detail, seconds=result.seconds)
    # A stop that failed means the state may still be written to; do not copy or migrate it.
    if stop_failed:
        run.step("abort", False, "a service did not stop; nothing was changed")
        return run.finish()

    def restore_services(reason: str) -> None:
        """Nothing was changed yet: bring back the services this run stopped."""
        if not any(s.stop_argv for s in services):
            return
        if pending:
            # The installed release needs the new schema; starting it against the old one
            # fails exactly when this recovery is needed. Leave the services stopped.
            run.step(
                "restore-services",
                False,
                f"{reason}; services left stopped: a migration is pending, so start the "
                "previous release (or re-run this command)",
            )
            return
        results = deploy_services.start_services(
            services, runner, probe=probe, sleep=sleep, clock=clock
        )
        run.document["services"].extend(_service_rows(results))
        run.step("restore-services", all(r.ok for r in results), reason)

    # -- backup -------------------------------------------------------------------------
    began = clock()
    name = "backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        made = backup(
            Path(plan.state_dir), Path(plan.backup_dir) / name, max_bytes=plan.max_backup_bytes
        )
    except Exception as error:  # a size bound, a bad directory, a damaged database
        run.step("backup", False, _failure("backup_failed", error), began)
        restore_services("backup failed before any change")
        return run.finish()
    run.document["backup"] = {
        "name": name,
        "kind": made.get("kind"),
        "schema_version": made.get("schema_version"),
        "bytes": made.get("bytes"),
        "sha256": made.get("sha256"),
        "max_backup_bytes": plan.max_backup_bytes,
    }
    run.step("backup", True, name, began)

    # -- migrate, then config fixes ----------------------------------------------------------
    receipt_dir = Path(plan.receipt_dir)
    began = clock()
    try:
        receipt = deploy_state.apply_migrate(
            Path(plan.state_dir), receipt_dir, backup_confirmed=True
        )
    except Exception as error:
        run.step(
            "migrate",
            False,
            f"{_failure('migrate_failed', error)}; services left stopped, backup {name}",
            began,
        )
        return run.finish()
    if receipt is not None:
        run.document["fixes"].append(
            {"fix": receipt["fix"], "receipt_sha256": receipt["receipt_sha256"]}
        )
    after = deploy_state.check_schema(Path(plan.state_dir))
    run.document["schema_version"]["after"] = after.detail.get("current")
    run.step("migrate", after.status == "ok", after.summary, began)
    if after.status != "ok":
        return run.finish()

    # Config fixes run only after a successful migrate, so a failed migrate leaves the configs
    # (and the old release's keys) as they were. The originals are copied beside the backup
    # first so a rollback can put them back.
    began = clock()
    try:
        config_backups = _backup_configs(plan, Path(plan.backup_dir) / (name + "-configs"))
        run.document["backup"]["config_backups"] = config_backups
        for path in _paths([*plan.executor_configs, *plan.live_configs]):
            receipt = config_fixes.apply_drop_removed_keys(path, receipt_dir)
            if receipt is not None:
                run.document["fixes"].append(
                    {"fix": receipt["fix"], "receipt_sha256": receipt["receipt_sha256"]}
                )
    except (ValueError, OSError) as error:
        run.step(
            "config-fixes",
            False,
            f"{_failure('config_fix_failed', error)}; services left stopped, backup {name}",
            began,
        )
        return run.finish()
    run.step("config-fixes", True, f"{len(run.document['fixes'])} applied", began)

    # -- start in order, wait for health ------------------------------------------------------
    started = deploy_services.start_services(
        services, runner, probe=probe, sleep=sleep, clock=clock
    )
    run.document["services"].extend(_service_rows(started))
    expected = {(s.name, step) for s in services for step in ("restart", "health")}
    seen = {(r.name, r.step) for r in started if r.ok}
    for result in started:
        run.step(f"{result.step}:{result.name}", result.ok, result.detail, seconds=result.seconds)
    if seen != expected:
        run.step("start", False, "a service did not restart or did not become healthy")
        return run.finish()

    # -- the fixed cheap smoke set ------------------------------------------------------------
    began = clock()
    post, _blocking = preflight(plan, smoke=bool(plan.smoke_cpu))
    smoke = [
        {"check": item.check_id, "status": item.status, "summary": item.summary}
        for item in post
        if item.status != "ok" or item.check_id in {"state.schema", "smoke.cpu"}
    ]
    not_ready = [item.check_id for item in post if item.status == "fail"]
    run.document["smoke"] = smoke
    run.step(
        "smoke",
        not not_ready,
        "ready" if not not_ready else "failing after upgrade: " + ", ".join(not_ready),
        began,
    )
    return run.finish()


def _planned(plan: Any, findings: list[deploy_check.Finding]) -> list[str]:
    """What an execute run would do, from what the preflight found; nothing is changed."""
    steps = []
    if plan.focused_tests:
        steps.append(
            f"run the focused tests for the upgrade range (jobs {plan.focused_tests_jobs}) "
            "before any service is stopped"
        )
    stoppable = [s.name for s in reversed(list(plan.services)) if s.stop_argv]
    if stoppable:
        steps.append("stop: " + ", ".join(stoppable))
    unstopped = _not_stopped(plan)
    if unstopped:
        unsafe = _unsafe_unstopped(plan)
        steps.append(
            "NOT stopped (no stop_argv): "
            + ", ".join(unstopped)
            + ("; execute refuses until a stop_argv is set" if unsafe else "; declared read-only")
        )
    steps.append(
        "backup to the plan's backup directory"
        + ("" if plan.max_backup_bytes is None else f" (capped at {plan.max_backup_bytes} bytes)")
    )
    schema = _schema(findings)
    if schema.get("current") != schema.get("latest"):
        steps.append(f"migrate schema {schema.get('current')} -> {schema.get('latest')}")
    if any(i.fix is not None and i.fix.fix_id == "drop_removed_keys" for i in findings):
        steps.append("after the migrate: drop removed config keys (receipted; originals kept)")
    steps.append("start in order, waiting for health: " + ", ".join(s.name for s in plan.services))
    steps.append("smoke: deploy check ready" + (", CPU smoke" if plan.smoke_cpu else ""))
    steps.append("write the deploy receipt")
    return steps


def write_receipt(plan: Any, document: dict[str, Any]) -> Path:
    """Write the receipt to the plan's receipt directory (owner-only); returns its path."""
    receipt_dir = Path(plan.receipt_dir)
    if not receipt_dir.is_dir():
        raise ValueError("The receipt directory must exist.")
    body = json.dumps(document, sort_keys=True)
    digest = deploy_check.sha256_hex(body.encode())
    stamp = document["started_at"].replace(":", "").replace("-", "")
    path = receipt_dir / f"deploy-{stamp}-{digest[:12]}.json"
    deploy_check.write_private_json(path, {**document, "receipt_sha256": digest})
    return path


# -- rollback ---------------------------------------------------------------------------------

ROLLBACK_SCHEMA = "tessera.deploy-rollback-receipt.v1"
_BACKUP_NAME = re.compile(r"^backup-\d{8}T\d{6}Z$")
PRE_ROLLBACK_SUFFIX = ".pre-rollback"


class RollbackRefused(ValueError):
    """The rollback was refused before anything changed; the message names no path or value."""


def _load_deploy_receipt(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, ValueError):
        raise RollbackRefused("the receipt cannot be read as JSON") from None
    if not isinstance(document, dict) or document.get("schema") != RECEIPT_SCHEMA:
        raise RollbackRefused("not a deploy-upgrade receipt")
    claimed = document.pop("receipt_sha256", None)
    recomputed = deploy_check.sha256_hex(json.dumps(document, sort_keys=True).encode())
    if claimed != recomputed:
        raise RollbackRefused("the receipt digest does not match its content")
    document["receipt_sha256"] = claimed
    if document.get("mode") != "execute" or not isinstance(document.get("backup"), dict):
        raise RollbackRefused("the receipt records no backup to roll back to")
    return document


def _database_in_use(database: Path) -> bool:
    try:
        connection = sqlite3.connect(database, timeout=0.5, isolation_level=None)
    except sqlite3.Error:
        return True
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("ROLLBACK")
        return False
    except sqlite3.Error:
        return True
    finally:
        connection.close()


def rollback(
    plan: Any,
    receipt_path: Path,
    *,
    probe: Callable[..., tuple[bool, str]] = deploy_services.probe_health,
) -> dict[str, Any]:
    """Put the pre-migration snapshot a deploy receipt names back as the database.

    Refuses (changing nothing) unless: the receipt's own digest verifies, it names a
    pre-migration snapshot under the plan's backup directory whose digest matches both the
    receipt and the snapshot manifest, no plan service reports healthy and the database is not
    locked. The replaced database is kept beside the state as ``<database>.pre-rollback``. The
    config files the upgrade rewrote are restored from their copies when their digests match.
    Writes its own receipt; returns the document."""
    document = _load_deploy_receipt(receipt_path)
    backup = document["backup"]
    if backup.get("kind") != "pre-migration-snapshot":
        raise RollbackRefused("only a pre-migration snapshot is rolled back here; use `restore`")
    name = backup.get("name")
    if not isinstance(name, str) or not _BACKUP_NAME.match(name):
        raise RollbackRefused("the receipt's backup name is not valid")
    directory = Path(plan.backup_dir) / name
    snapshot = directory / SNAPSHOT_NAME
    try:
        data = snapshot.read_bytes()
        manifest = json.loads((directory / SNAPSHOT_MANIFEST).read_bytes())
    except (OSError, ValueError):
        raise RollbackRefused("the snapshot or its manifest cannot be read") from None
    digest = hashlib.sha256(data).hexdigest()
    if not (digest == backup.get("sha256") == manifest.get("sha256")):
        raise RollbackRefused("the snapshot digest does not match the receipt")
    for service in plan.services:
        if probe(service.health)[0]:
            raise RollbackRefused("a service reports healthy; stop the services first")
    state_dir = Path(plan.state_dir)
    database = state_dir / DATABASE_NAME
    aside = state_dir / (DATABASE_NAME + PRE_ROLLBACK_SUFFIX)
    if aside.exists() or aside.is_symlink():
        raise RollbackRefused("a previous rollback copy exists; move it away first")
    if database.exists() and _database_in_use(database):
        raise RollbackRefused("the database is locked by another process")
    require = deploy_check.require_writable_directory
    try:
        require(Path(plan.receipt_dir), "receipt directory")
    except ValueError:
        raise RollbackRefused("the receipt directory is not writable") from None

    staged = state_dir / (DATABASE_NAME + ".rollback-new")
    staged.write_bytes(data)
    os.chmod(staged, 0o600)
    if database.exists():
        os.replace(database, aside)
    for suffix in ("-wal", "-shm"):
        sidecar = state_dir / (DATABASE_NAME + suffix)
        if sidecar.exists():
            sidecar.unlink()
    os.replace(staged, database)

    restored_configs = []
    paths = _config_paths(plan)
    copies = Path(plan.backup_dir) / (name + "-configs")
    for row in backup.get("config_backups") or []:
        index = row.get("index")
        if not isinstance(index, int) or not 0 <= index < len(paths):
            continue
        try:
            original = (copies / f"config-{index}.json").read_bytes()
        except OSError:
            continue
        if hashlib.sha256(original).hexdigest() != row.get("sha256"):
            continue
        tmp = paths[index].with_name(paths[index].name + ".rollback-new")
        tmp.write_bytes(original)
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths[index])
        restored_configs.append(index)

    result = {
        "schema": ROLLBACK_SCHEMA,
        "result": "ok",
        "deploy_receipt_sha256": document["receipt_sha256"],
        "backup": {"name": name, "sha256": digest, "schema_version": backup.get("schema_version")},
        "configs_restored": restored_configs,
        "replaced_database_kept_as": DATABASE_NAME + PRE_ROLLBACK_SUFFIX,
        "started_at": _stamp(datetime.now(UTC)),
        "note": "services were not started; start the release that wrote the snapshot",
    }
    body = json.dumps(result, sort_keys=True)
    sha = deploy_check.sha256_hex(body.encode())
    out = (
        Path(plan.receipt_dir)
        / f"rollback-{result['started_at'].replace(':', '').replace('-', '')}-{sha[:12]}.json"
    )
    deploy_check.write_private_json(out, {**result, "receipt_sha256": sha})
    result["receipt_sha256"] = sha
    return result
