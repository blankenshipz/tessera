# 0099 — The checked upgrade command

## Status

Implemented. No schema change. Builds on [ADR 0098](0098-deploy-check.md) (the deploy check and its
receipted fixes) and the explicit `tessera migrate`. It adds no authority: it runs the commands
the operator names in a plan file, with no shell, and nothing the operator could not run by hand.

## Context

After the deploy check, an upgrade was still a sequence a person ran in order: stop services, back
up, migrate, restart the control plane and then the executor, wait until each was healthy, run a
smoke, and note what happened. Each step could be done out of order or forgotten.

## Decision

1. **One command, a plan file, dry-run by default.** `tessera deploy-upgrade --plan PLAN` reads a
   closed, owner-only `tessera.deploy-upgrade-plan.v1` file (absolute paths, the state and backup
   and receipt directories, an optional backup bound and source revision, the config files to
   check, up to eight services in start order, and whether to run the CPU smoke). Without
   `--execute` it runs only the preflight and prints what it would do; it writes nothing.
2. **The order.** Preflight (the deploy check; anything that fails and this command does not fix
   blocks the run before a service is touched); stop the services that name a `stop_argv`, last to
   first (a stop that fails aborts before the state is copied); back up; migrate; drop removed
   config keys (only after a successful migrate, with the originals copied beside the backup
   first, so a failed migrate leaves the configs and their old keys untouched); start each service in plan order and wait for it to be healthy; run the smoke set;
   write the receipt.
3. **The backup.** A database already at this release's schema gets the validated, restorable
   backup of `tessera backup` under the plan's bound. An older database cannot (that backup
   refuses any schema but the latest), so it gets a pre-migration snapshot: SQLite's online backup
   of the database file under the same bound, in a new owner-only directory, with a manifest of its
   schema version and digest. It is restored by stopping the services and putting the snapshot back
   with the release that wrote it. The migration runs only after one of them exists.
   A service with no `stop_argv` is never stopped, so the backup and migrate could run against a
   database it still writes. Execute therefore refuses such a service unless the plan declares
   `writes_state: false` (it never writes the control database); the dry run and the receipt
   (`not_stopped`) list every service that is not stopped.
4. **Services and health.** A service is restarted with its own argument list, never a shell, a
   minimal environment and a time limit, then waited on until healthy, by the service-health file
   the units already write (`tessera.service-health.v1`, fresh within the plan's age and written at or after the restart, so the old process's fresh
   file does not count) or a
   loopback HTTP 2xx. The first service that does not restart or become healthy stops the run;
   the later ones are not touched.
5. **The smoke set is fixed and cheap:** a second deploy check must be ready (schema current,
   configs clean, providers valid) and, if the plan asks, the networkless CPU smoke must pass.
6. **The receipt** (`tessera.deploy-receipt.v1`, owner-only, named by timestamp and digest):
   mode, result, failed step, source revision and package version, schema before and after, the
   backup (name, kind, bytes, digest, bound), the fixes applied with their receipt digests, each
   service step, the smoke result, every step's seconds and the total. It records no path and no
   value.
7. **The receipt attributes the migration.** It records the stored schema version at each boundary
   (`schema_trace`). The migrate step reports what it did itself (a migration applied in the step, or
   nothing to migrate); when the version had already moved before it began, the receipt names the
   window and warns instead of crediting the step, because something else (a service started with the
   new release, a health probe, a hook) brought the schema forward.
8. **Failure and rollback.** A failed backup restarts the services this run stopped only when no
   migration is pending; with one pending the installed release would start against the old schema
   and fail, so they stay stopped and the receipt says so (start the previous release). After the
   migration the services stay as they are and the receipt names the backup. A failure step records
   a stable code and the exception type only, never the message (it can carry a path).
   `tessera deploy-rollback --plan PLAN --receipt RECEIPT` puts a pre-migration snapshot back: it
   verifies the receipt digest and the snapshot digest (receipt and manifest), refuses while any
   plan service reports healthy or the database is locked, keeps the replaced database as
   `tessera.sqlite3.pre-rollback`, restores the configs the upgrade rewrote from their copies
   (digest-checked), writes a `tessera.deploy-rollback-receipt.v1`, and starts nothing; start the
   release that wrote the snapshot afterwards. A validated backup is restored with `tessera restore`. Exit status: 0 done (or planned), 1
   a step failed, 2 a bad plan or usage.

## Consequences

- A deploy is `tessera deploy-upgrade --plan PLAN` (look), then the same with `--execute`, and the
  receipt says what happened.
- The plan carries the operator's own restart and stop commands, so the command works with any
  service manager and names none; it never reads a secret and never calls a provider.
- A restored snapshot needs the previous release; `tessera restore` does not accept it.
- Not done: automatic rollback (the helper above is manual), a downgrade path, a staged canary, and any fix that needs a secret.

## Tests

`tests/test_deploy_upgrade.py` (dry run changes nothing; the exact order; stop and restart order;
failure at preflight, stop, backup and restart; the receipt carries no path; both backup kinds and
the bound; the command with real service processes), `test_deploy_upgrade_plan.py` (the closed plan
and its refusals), `test_deploy_services.py` (stop, restart, health probes and the default runner).

`tests/test_deploy_upgrade_followups.py` (config fixes after migrate and restored by rollback;
unstopped services refused/declared; health file must postdate the restart; no restart with a
pending migration; rollback guards; failure details without paths).
