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
the service-health file the units write or a loopback HTTP 2xx.

**Backup.** A database already at the new schema gets the validated `tessera backup` under
`max_backup_bytes`. An older database cannot be backed up that way, so it gets a pre-migration
snapshot (the database file, same bound, with a manifest of its schema version and digest). Restore
it by stopping the services and putting `snapshot.sqlite3` back as the database with the release
that wrote it; `tessera restore` does not take it.

**On failure** the run stops at that step and the receipt says which. Before the migration the
stopped services are restarted; after it they are left as they are and the receipt names the backup.
Exit status: 0 done (or planned), 1 a step failed, 2 a bad plan.
