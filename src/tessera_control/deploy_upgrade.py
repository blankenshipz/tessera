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

import hashlib
import json
import os
import sqlite3
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
) -> dict[str, Any]:
    """Run (or, by default, only plan) the upgrade and return its receipt document."""
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
        run.step("backup", False, f"{type(error).__name__}: {str(error)[:200]}", began)
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

    # -- config fixes and migrations --------------------------------------------------------
    began = clock()
    receipt_dir = Path(plan.receipt_dir)
    try:
        for path in _paths([*plan.executor_configs, *plan.live_configs]):
            receipt = config_fixes.apply_drop_removed_keys(path, receipt_dir)
            if receipt is not None:
                run.document["fixes"].append(
                    {"fix": receipt["fix"], "receipt_sha256": receipt["receipt_sha256"]}
                )
    except (ValueError, OSError) as error:
        run.step("config-fixes", False, f"{type(error).__name__}: {str(error)[:200]}", began)
        restore_services("config fix failed before the migration")
        return run.finish()
    run.step("config-fixes", True, f"{len(run.document['fixes'])} applied", began)

    began = clock()
    try:
        receipt = deploy_state.apply_migrate(
            Path(plan.state_dir), receipt_dir, backup_confirmed=True
        )
    except Exception as error:
        run.step(
            "migrate",
            False,
            f"{type(error).__name__}: {str(error)[:200]}; services left stopped, backup {name}",
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
    stoppable = [s.name for s in reversed(list(plan.services)) if s.stop_argv]
    if stoppable:
        steps.append("stop: " + ", ".join(stoppable))
    steps.append(
        "backup to the plan's backup directory"
        + ("" if plan.max_backup_bytes is None else f" (capped at {plan.max_backup_bytes} bytes)")
    )
    if any(i.fix is not None and i.fix.fix_id == "drop_removed_keys" for i in findings):
        steps.append("drop removed config keys (receipted)")
    schema = _schema(findings)
    if schema.get("current") != schema.get("latest"):
        steps.append(f"migrate schema {schema.get('current')} -> {schema.get('latest')}")
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
