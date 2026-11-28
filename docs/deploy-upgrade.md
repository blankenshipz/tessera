# Deploy upgrade

`tessera deploy-upgrade` runs an upgrade as one checked command: preflight, backup, migrate,
restart the services in order, wait for health, a fixed cheap smoke and a receipt. It is a dry run
until you pass `--execute`. Design and limits: [ADR 0099](decisions/0099-deploy-upgrade.md); the
preflight is the [deploy check](deploy-check.md).

To run the upgrade once nothing blocks it (for example a remote route still in flight), record a
[deploy request](deploy-request.md) instead of watching for the blocker to clear.

```
tessera deploy-upgrade --plan PLAN            # preflight and what would happen; writes nothing
tessera deploy-upgrade --plan PLAN --execute  # do it and write the receipt
```

The plan is a closed, owner-only (0600) JSON file; every path is absolute:

```json
{
  "schema": "tessera.deploy-upgrade-plan.v1",
  "state_dir": "/var/lib/tessera/state",
  "backup_dir": "/var/lib/tessera/backups",
  "receipt_dir": "/var/lib/tessera/deploy-receipts",
  "max_backup_bytes": 4294967296,
  "source_revision": "0123456789abcdef0123456789abcdef01234567",
  "executor_configs": ["/etc/tessera/executor.json"],
  "providers_file": "/etc/tessera/providers.json",
  "smoke_cpu": false,
  "services": [
    {
      "name": "control",
      "stop_argv": ["systemctl", "stop", "tessera-control.service"],
      "restart_argv": ["systemctl", "start", "tessera-control.service"],
      "health": {"kind": "http", "url": "http://127.0.0.1:8765/health", "timeout_seconds": 5},
      "health_wait_seconds": 120
    },
    {
      "name": "executor",
      "stop_argv": ["systemctl", "stop", "tessera-salad-executor.service"],
      "restart_argv": ["systemctl", "start", "tessera-salad-executor.service"],
      "health": {"kind": "file", "path": "/run/tessera/executor-health.json", "max_age_seconds": 120}
    }
  ]
}
```

Services start in the order listed (control before the executor) and stop in the reverse order. A
command is an argument list run without a shell and with only `PATH` in its environment. Health is
the service-health file the units write (its `last_loop_at` must be at or after the restart) or a loopback
HTTP 2xx. A service without `stop_argv` is not stopped: execute refuses it unless the service sets
`"writes_state": false`; the dry run and receipt list what is not stopped.

### Salad routes in flight

Preflight refuses the upgrade, in the dry run and under `--execute` and before anything is stopped,
while any Salad campaign attempt is `executing`:
`salad.executing (N Salad attempt(s) executing: a restart loses the run; wait for it to finish or pass --allow-executing-salad)`.
Restarting the Salad executor while a route's child runs loses that run. The restarted executor
finishes only from a receipt already on disk or relays the guardian's confirmed group stop (ADR
0160), so the attempt ends `unknown` for a person to reconcile. Wait for the route to finish, or
record a [deploy request](deploy-request.md). `--allow-executing-salad` turns the refusal into a
warning when losing the run is accepted. The receipt records the count as `salad_executing`.
Reserved, preparing and queued (`route_started`) attempts do not block: after v0.104.0 a restart
while stages are only in those states is safe.

**Leases across the stop (ADR 0214).** A Salad lease lasts 150 s and the executor renews it; a
lapsed lease cannot be renewed, so a stop longer than a held lease's remaining time requeues the
stage and spends one of its requeues. Preflight therefore reads every lease-holding Salad attempt
(an attempt from `leased` through `executing` whose grant is still unresolved and whose lease has
not expired) and refuses with `salad.lease_lapse` when any held lease expires within the plan's
`expected_downtime_seconds` (5 to 3600, default 120). A lease is renewed to at most 150 s ahead,
so a downtime of 150 s or more refuses whenever any lease is held. `--allow-lease-lapse` turns
the refusal into a warning. The same read is taken again immediately before the first service is
stopped (after the focused tests); if it blocks then, the step `preflight:lease_recheck` fails and
nothing is stopped. A database that exists but cannot be read blocks (`salad.lease_state`) rather
than counting as no leases. The receipt records `salad_executing`, `salad_route_holding`,
`salad_lease_holding`, `salad_leases_lapsing` and `expected_downtime_seconds`.

**Startup checks (ADR 0214).** A service may name `"config_check": {"program": "serve" |
"salad-executor" | "salad-guardian", "args": [...]}` with the exact arguments its unit passes (not
`--check-config`). Preflight runs that program's own `--check-config` with them under the release
running `deploy-upgrade`, before anything is stopped, and the post-install smoke runs it again.
A refused configuration blocks the upgrade; see the [deploy check](deploy-check.md#startup-checks).
The stored-state check (`state.stored_specs`, `state.stored_attempts`) runs only at the current
schema: for a release that carries a migration it is skipped in preflight and first runs in the
post-migrate smoke, after the stop and restart. A spec it finds then blocks its campaign (not
failed) until a fix or a rollback.

**Service venvs (optional).** A service that runs from its own virtualenv (for example the
reasoning service) declares `"venv": "/abs/path/to/venv"`. Then the plan also needs
`install_source` (a checkout or built wheel path; default `repo_root`) and may set
`venv_install_timeout_seconds` (30 to 1800, default 600). Under `--execute`, after the services
stop, the backup, the migration and the config fixes, and before any restart, the release is
`pip install`ed with each distinct venv's own `bin/python`, and the version it reports is read
back (and must equal the control plane's, when known). The receipt gets `service_venvs`: per venv
the path, services, release (`source_revision`), installed version, `ok` and `error_class`
(`pip_failed`, `timeout`, `version_unreadable`, `version_mismatch`, `not_attempted`, or an OS
error name). A failed install stops the run before any restart and leaves the services stopped.
A venv without an executable `bin/python` blocks preflight, and the dry run lists each planned
install. Details: [ADR 0161](decisions/0161-deploy-upgrade-service-venvs.md).

**Focused tests (optional).** Four more plan fields add a test gate before anything stops
([ADR 0117](decisions/0117-focused-test-selection.md)). All are optional and the step is
disabled by default.

| Field | Meaning |
|---|---|
| `from_revision` | the revision being upgraded from; the range is `from_revision..source_revision` |
| `focused_tests` | `true` to enable the step (needs `from_revision` and `source_revision`) |
| `focused_tests_jobs` | concurrent pytest jobs, 1 to 4 (default 2) |
| `repo_root` | absolute path of the repository checkout (default: the working directory) |

When enabled, preflight adds the finding `tests.focused`. A dry run lists the selected test files
and runs nothing. Under `--execute` it runs them in preflight, before the backup, the migration
and any service stop, so a failure costs no downtime. A failing run, ANY changed file that no
focused test maps to (the upgrade never falls back to the full suite; files the overrides file
declares safe under `[unmapped] safe` are exempt and listed), a range git cannot resolve, or a
checkout that is not at `source_revision` blocks the upgrade. The range is the exact difference
between the two releases, also when `from_revision` is not an ancestor. With no repository checkout at
`repo_root` (no `tests/` and `src/`, as on a packaged install) the step is skipped and the finding
says so. The receipt gets a `focused_tests` key (range, jobs, selected files, smoke set, unmapped
and declared-safe files, what a cap cut, broad changes, selections narrowed by an override, result,
duration) only when the step is enabled.

**Backup.** A database already at the new schema gets the validated `tessera backup` under
`max_backup_bytes`. An older database cannot be backed up that way, so it gets a pre-migration
snapshot (the database file, same bound, with a manifest of its schema version and digest). Restore
it with `tessera deploy-rollback --plan PLAN --receipt RECEIPT` (services must be stopped; it
verifies the digests, keeps the replaced database as `tessera.sqlite3.pre-rollback`, restores the
configs the upgrade rewrote, writes a receipt and starts nothing), then start the release that
wrote the snapshot. `tessera restore` does not take it.

**Memory.** The snapshot's digest, the rollback's copy of it and the config-file copies are streamed
in fixed-size chunks, so they do not hold a whole file in memory. Migrations are not covered: they
still load whole tables, and peak memory is not measured end to end. If a config's saved copy cannot
be written back, the rollback receipt lists it under `configs_not_restored` and still succeeds.

**Who migrated.** The receipt records the stored schema version at each boundary (`schema_trace`: at
preflight, after the services stop, in the backup snapshot, just before the migrate step and after
it). The migrate step says what it did: "migrated schema vN to vM in this step", or "nothing to
migrate" when the schema was current from the start. If the version moved before the step began, the
step is not credited: its detail and `warnings` name the window (while the services stopped, during
the backup, or between the backup and the migrate step), `early_migration` records it, and the backup's
own schema version shows whether the snapshot holds the old schema. A boundary whose stored version
cannot be read is skipped, so the window widens to the nearest readable boundaries ("before the backup was
taken", "before the migrate step began"); when only the snapshot's version is missing between the stop and
the migrate step, the window is named generically ("between two steps").

**On failure** the run stops at that step and the receipt says which. Config keys are dropped only after the
migrate succeeds. A failed backup restarts the stopped services unless a migration is pending (the
new release would fail against the old schema), in which case they stay stopped; after the migration they are left as they are and the receipt names the backup.
Exit status: 0 done (or planned), 1 a step failed, 2 a bad plan.
