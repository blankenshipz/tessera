# 0098 — The deploy check

## Status

Implemented. No schema change. Builds on the backup bound of the recovery commands, the
explicit `tessera migrate`, the closed executor and live-check configs of
[ADR 0075](0075-executor-worker-credential-cancels-own-route.md) and the validated providers file
of [ADR 0081](0081-validated-provider-registration.md). It adds no authority: every fix it can
make is one an operator already makes by hand.

## Context

A release that changes configuration or schema needed a person for a string of separate steps:
choose a backup size bound, run migrations, delete a config key the release removed (the closed
configs refuse a stale key, so the service does not start), register a provider profile through
the validated path, start the executor and run a CPU smoke. Each step was documented, none was
checked, and a missed one showed up as a refused start or, worse, a late failure.

## Decision

1. **One command, one report.** `tessera deploy-check` reports every area it is given as a
   `tessera.deploy-check.v1` document (`--format json`, `--report-file`): a list of checks, each
   `ok`, `warn`, `fail` or `skipped`, a one-line summary, structured detail and at most one fix.
   `ready` is true when nothing failed. The exit status is 0 when ready, 1 when not, 2 for a
   usage error. Nothing is changed unless `--apply` names a fix.
2. **What it checks.**
   - *Schema* (`--state-dir`): the control database's schema version against this release, read
     read-only; pending migrations are a failure, a database newer than the code is a failure.
   - *Backup bound* (`--state-dir`, `--max-backup-bytes`): the estimated backup size against the
     bound that `tessera backup` and `tessera restore` would use (default 1 GiB, at most 64 GiB).
     The bound is a per-command flag with no stored setting, and this check does not invent one:
     it reports the value to pass.
   - *Config keys* (`--executor-config`, `--live-config`): keys a release removed (a small table,
     currently `cancel_credential_file` from ADR 0075), keys no release defines, and the file's
     own validation. Key names and field locations are reported, never values.
   - *Providers* (`--providers-file`, and the profiles an executor config names): the providers
     file validates and its audit chain verifies; every profile the executor names exists in its
     profiles file and validates.
   - *Smoke* (`--smoke`): a synthetic CPU container with no network, a read-only root, no
     capabilities and a pinned local image that is never pulled; it passes only if the container
     prints a fresh random value. It spends nothing and reads no secret.
3. **Fixes.** Two are applied by the command: `drop_removed_keys` (remove only the keys a release
   removed from an owner-only config, atomically, keeping mode 0600; an unknown key is reported
   and never removed) and `migrate` (run the existing explicit migration; it needs `--state-dir`
   and `--backup-confirmed`, an operator statement that a backup was taken). Every applied fix
   writes a receipt (`tessera.deploy-fix-receipt.v1`, owner-only, named by its digest) to the
   `--receipt-dir`, recording the digests before and after and the key names or schema versions
   changed, never a path or a value. The receipt directory (and any `--report-file`) is proven writable before any fix
   changes anything, so an applied fix is never left without its receipt. Re-running applies nothing new and writes no receipt. The
   backup bound, a missing profile, a missing image and an unknown key are reported with the
   exact command or edit; they are not applied for the operator.
4. **Read from the control plane.** `GET /admin/deploy-check` and the MCP tool
   `tessera_deploy_report` return the control-side part of the same report (schema and provider
   file) for a human or a `provider_trust:read` agent, so an agent can ask whether a deployment
   is ready without file access. It is read-only and applies nothing.

## Consequences

- A deploy is one command whose report an agent or a person can read, and whose fixes leave a
  receipt. A refused start from a stale config key is caught before the restart.
- It checks structure, not truth: a valid profile can still name an account that has no quota,
  and a passing smoke does not show a provider is reachable.
- It never starts or stops a service, never calls a provider and never spends; starting the
  executor stays with the service manager.
- Not done: a persisted backup bound, applying a provider registration, and any fix that would
  need a secret.

## Tests

`tests/test_deploy_check_cli.py` (report, receipts, exit codes, the command), `test_deploy_config.py`
(removed and unknown keys, the atomic receipted fix, a fixed config loads), `test_deploy_state.py`
(schema, migrate, backup bound, providers, executor profiles), `test_deploy_smoke.py` (the container
argv, cleanup, failure modes) and `test_deploy_check_api.py` (the control-side route and tool).
