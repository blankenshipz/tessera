# Deploy upgrade

`tessera deploy-upgrade` runs an upgrade as one checked command: preflight, backup, migrate,
restart the services in order, wait for health, a fixed cheap smoke and a receipt. It is a dry run
until you pass `--execute`. Design and limits: [ADR 0099](decisions/0099-deploy-upgrade.md); the
preflight is the [deploy check](deploy-check.md).

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
and any service stop, so a failure costs no downtime. A failing run, a selection that cannot map
the changed files (the upgrade never falls back to the full suite), a range git cannot resolve, or
a checkout that is not at `source_revision` blocks the upgrade. With no repository checkout at
`repo_root` (no `tests/` and `src/`, as on a packaged install) the step is skipped and the finding
says so. The receipt gets a `focused_tests` key (range, jobs, selected files, smoke set, unmapped
files, result, duration) only when the step is enabled.

**Backup.** A database already at the new schema gets the validated `tessera backup` under
`max_backup_bytes`. An older database cannot be backed up that way, so it gets a pre-migration
snapshot (the database file, same bound, with a manifest of its schema version and digest). Restore
it with `tessera deploy-rollback --plan PLAN --receipt RECEIPT` (services must be stopped; it
verifies the digests, keeps the replaced database as `tessera.sqlite3.pre-rollback`, restores the
configs the upgrade rewrote, writes a receipt and starts nothing), then start the release that
wrote the snapshot. `tessera restore` does not take it.

**On failure** the run stops at that step and the receipt says which. Config keys are dropped only after the
migrate succeeds. A failed backup restarts the stopped services unless a migration is pending (the
new release would fail against the old schema), in which case they stay stopped; after the migration they are left as they are and the receipt names the backup.
Exit status: 0 done (or planned), 1 a step failed, 2 a bad plan.
