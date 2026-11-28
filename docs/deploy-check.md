# Deploy check

`tessera deploy-check` turns the steps a deploy used to need a person for into one checked
command. It is read-only unless you name a fix with `--apply`, it reads no secret, it never
contacts a provider and it spends nothing. Design and limits: [ADR 0098](decisions/0098-deploy-check.md).

```
tessera deploy-check \
  --state-dir STATE_DIR \
  --executor-config EXECUTOR_CONFIG [--live-config LIVE_CONFIG] \
  --providers-file PROVIDERS_FILE \
  [--max-backup-bytes N] [--smoke] \
  [--apply drop_removed_keys --apply migrate --backup-confirmed --receipt-dir RECEIPTS] \
  [--format json] [--report-file REPORT]
```

Every path is absolute and belongs to you; each option is optional, and an area you do not name is
not checked. The exit status is 0 when nothing failed, 1 when something did, 2 for a usage error.

| Check | What it reads | Fix |
| --- | --- | --- |
| `state.schema` | the control database's schema version, read-only | `--apply migrate` (with `--backup-confirmed`), or `tessera migrate` |
| `backup.cap` | the estimated backup size against the bound `tessera backup` would use | pass `--max-backup-bytes N` to `tessera backup` and `restore`; the bound has no stored setting |
| `config.N.keys` | a config's keys: ones a release removed, ones no release defines, and its own validation | `--apply drop_removed_keys` removes only the removed keys; an unknown key is yours to edit |
| `config.N.source_pin` | an executor config's `source_revision` is the pin the live check accepts for the installed source | `--apply refresh_source_pin --source-commit <40hex>` re-pins it in place (receipted); `deploy-upgrade` does this with the plan's `source_revision`. Only the digest is measured from the install; the commit is operator-asserted. Run from a repository checkout (`tests/` and `src/` in the working directory), the command refuses a commit that is not the checkout's head, before any fix; with no checkout it cannot compare and accepts the commit as given. If a later upgrade step (start, smoke) fails after the re-pin, the config copies from before the fixes are in the upgrade's `<name>-configs` directory beside the backup (made before the first config fix, after a successful migrate) |
| `providers.file`, `providers.audit` | the providers file and its audit chain (ADR 0081) | register through `tessera providers` |
| `providers.frame_verdict_read` | when the providers file configures `salad_profiles`: that `salad_guardian_read` is set and a valid guardian read config, and, with `--guardian-status-file FILE` (a saved copy of the guardian's `status` reply; the check never contacts the guardian), that the reply carries `frame_record` | upgrade the guardian first so its `status` returns `frame_record`, then set `salad_guardian_read` (or `tessera serve --salad-guardian-read FILE`) |
| `providers.executor_profiles` | each profile an executor config names, in its profiles file | register the profile through `tessera providers` |
| `smoke.cpu` (`--smoke`) | a synthetic container: no network, read-only root, no capabilities, a pinned local image that is never pulled | `docker pull` the pinned image yourself |

`providers.frame_verdict_read` fails (exit 1) with `detail.reason` `config_missing`, `config_invalid`,
`guardian_version_missing` or `guardian_status_unreadable`. Without it, Salad frames silently stop
auto-settling and every guardian-bounded hold waits for a human charge (ADR 0041). Deploy order:
upgrade the guardian, confirm its status carries `frame_record`, then give control the config. Without
`--guardian-status-file` the guardian version is not checked and the finding says so.

## Startup checks

```
tessera deploy-check \
  --serve-args "--state-dir /var/lib/tessera/state --port 8765 --campaign-providers /etc/tessera/providers.json" \
  --executor-args "--config /etc/tessera/salad-executor.json" \
  --guardian-args "--run --config /etc/tessera/salad-guardian.json"
```

Each option is the argument string the service's unit passes (shell-quoted, without
`--check-config`; `--executor-args` and `--guardian-args` repeat). The command runs the service's
own `--check-config` with them in a fresh process under the interpreter running `deploy-check`,
so run it from the release you are about to install, before stopping anything ([ADR
0214](decisions/0214-deploy-gate-startup-equal-check.md)). `--check-config` is the startup path
itself: the same parsing and validation a start runs, then an exit without binding a socket,
writing state, taking a lock, contacting anything or reading a secret value. So `startup.*` fails
when that service's start would refuse its configuration (exit 2), and also when the check could
not run (any other exit), with these exceptions, where a real start can still refuse what the
check passes: the executor's check does not read or authenticate the worker credential (it checks
only that the file is private), `serve` does not load the runtime owner (its loader runs adapter
code) or exercise the agent sandbox witness (both are printed as notes in the check's line), the
guardian's certificate is not loaded together with its key (that reads the key), and `serve` does
not open the control database (`state.schema` covers it). A stale executor `source_revision` is a note,
not a failure, because `deploy-upgrade` re-pins it; the start itself still refuses a stale pin.

| Check | What it reads | Fix |
| --- | --- | --- |
| `startup.serve.N`, `startup.salad-executor.N`, `startup.salad-guardian.N` | the service's `--check-config` exit code and its one `tessera.config-check.v1` line | edit the file the message names |
| `startup.route_width` | the widths those checks parsed: control's `salad_max_concurrent_routes` ≤ the executors' `max_concurrent_paid_routes` (summed) ≤ the guardians' `max_open_billable_frames` (summed); every level that is set is compared (an executor wider than its guardians fails even with control unset), a width that is not an integer fails, then an unset control width is a warning | make the three agree |
| `state.stored_specs`, `state.stored_attempts` (with `--state-dir`) | every non-terminal campaign's stored spec, re-parsed and re-digested with this release (its Salad workload contracts are part of the spec), and the stored guardian finals and live receipts of its unsettled attempts, read-only; skipped until the schema is current, so for a release with a migration it first runs in the post-migrate smoke, after the restart, not before the stop | a person reviews the named campaign; at runtime such a campaign is blocked, not failed |

Exit codes of the services themselves: 2 only when the operator's configuration is refused
(the units set `RestartPreventExitStatus=2`), 1 for every other fatal error, so
`Restart=on-failure` restarts a transient fault (a key fetch at boot, a busy database). The one
exception is `tessera serve` failing to load its runtime owner configuration at a real start,
which exits 78 (the check, which does not load the owner, never returns it).

Readiness: under `tessera serve`, `/health/ready` is unready (`runner_not_ticked`) until the
in-process runner completes its first tick. If `tick()` raises on every attempt since the process
started, readiness stays unready indefinitely, and under Kubernetes the pod then leaves its
Service; read the runner's error log rather than waiting.

An applied fix writes an owner-only receipt (`tessera.deploy-fix-receipt.v1`) to `--receipt-dir`.
It records the digests before and after and the key names or schema versions that changed, never a
path or a value. Applying again changes nothing and writes no second receipt.

An agent or a person can read the control-side part of the same report (schema and the providers
file) without file access: `GET /admin/deploy-check`, or the MCP tool `tessera_deploy_report` when
the operator enables the provider tools. Config files, the backup bound and the smoke are local to
the operator's host and stay with the command.
