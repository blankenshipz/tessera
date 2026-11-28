# 0214 — The deploy gate runs the real startup validation; exit code 2 is config refusal only

## Status

Accepted. Amends [ADR 0098](0098-deploy-check.md) (what `deploy-check` checks) and
[ADR 0099](0099-deploy-upgrade.md) (the upgrade's preflight and stop). No schema change and no
migration.

## Context

Four gaps let a deploy, or the code a deploy installs, take a running program down:

1. **The preflight checked less than startup.** `deploy_state.check_providers` read the providers
   document and listed its entries; it never ran the closed provider models that
   `configure_providers` applies at `tessera serve` start. A providers file the new release would
   refuse (an operator key the model did not know, a boolean route width) passed the preflight and
   then stopped control. The trust file, Salad profiles, program budget, review destinations and
   the guardian config were not checked against the new code at all. The guardian config was an
   open dictionary, so a misspelt key (`total_cap_cent`) was silently ignored: no aggregate cap.
2. **Exit code 2 covered runtime faults.** The guardian and executor units set
   `RestartPreventExitStatus=2` so a refused config does not restart in a loop, but the guardian
   returned 2 for a two-second key fetch failing at boot and for any exception escaping its loop,
   and the executor returned 2 for any setup error. Those were never restarted, and the guardian
   wrote no health file, so nothing noticed.
3. **The stop lapsed leases.** A lease lasts 150 s and a lapsed lease cannot be renewed. The
   upgrade guard counted only `executing` attempts, once, before the focused tests ran, so a stop
   longer than a held lease's remaining time requeued every queued stage (and spent a requeue).
4. **New code failed old campaigns.** A stored spec that no longer validated under the new code,
   or no longer re-digested to the approved digest, failed its campaign
   (`spec_digest_mismatch`) with live attempts not unwound.

## Decision

1. **`--check-config` is the startup path.** `tessera serve`, the Salad executor and the Salad
   guardian accept `--check-config` with their real arguments. Each runs the same validation
   function its real start runs, then exits without binding a socket, writing state, taking a
   lock, contacting anything or reading a secret value. It exits 0 and prints one
   `tessera.config-check.v1` line (program, the route width it parsed, short notes), 2 when the
   start would refuse the configuration, 1 when the check could not run. Exclusions are named in
   its notes: the serve runtime owner (its loader runs adapter code) and the agent sandbox witness
   are not exercised; the guardian does not load its certificate together with its key; a
   well-formed but stale executor `source_revision` is a note, since `deploy-upgrade` re-pins it
   after the stop (the start itself still refuses it).
2. **`deploy-check` runs them under the new release.** `--serve-args`, `--executor-args` and
   `--guardian-args` (and a plan service's `config_check`) give the units' argument strings; each
   check runs in a fresh process under the interpreter running `deploy-check`, before anything is
   stopped. The route widths those checks parsed must agree: control's
   `salad_max_concurrent_routes` ≤ the executors' `max_concurrent_paid_routes` (summed) ≤ the
   guardians' `max_open_billable_frames` (summed). Every level that is set is compared, so an
   executor wider than its guardians fails even when control's width is unset; a reported width
   that is not an integer fails. Only then does an unset control width warn.
   The guardian config is closed: every key is named, unknown and misspelt keys, missing required
   keys and wrong types (a boolean where an integer is expected) are refused at start.
3. **Stored state is re-read with the new code.** With a state directory, `deploy-check` opens the
   database read-only and verifies every non-terminal campaign's spec with the runner's own
   function (parse, re-digest, normalize), and the stored guardian finals and live receipts of
   their attempts with the runtime parsers (`state.stored_specs`, `state.stored_attempts`). The
   check runs only once the stored schema is current: for a release that carries a migration it
   is skipped before the stop and first runs in the post-migrate smoke, after the restart (it is
   not re-implemented against pre-migration rows). A release without a migration is checked
   before any stop. At
   runtime a spec that does not verify blocks the campaign (`campaign.blocked` with
   `stored_spec_unreadable` or `spec_digest_mismatch`, once) instead of failing it; nothing about
   its stages, attempts, grants or holds changes. When the spec verifies again (a fix or a
   rollback) the campaign resumes and the reason is cleared; the approval's digest check still
   applies.
4. **Exit code 2 is config refusal only** (`tessera_control.exit_codes`). Every other fatal error
   exits 1, so `Restart=on-failure` restarts it. The guardian writes the service-health file the
   executor writes (`tessera.service-health.v1`, `--health-file`, at most every 5 s), and its unit
   gets the same report-only health-check timer.
5. **The stop guard is lease-aware.** The upgrade reads every Salad attempt from `leased` through
   `executing` whose grant is unresolved and whose lease is live, and refuses when any held lease
   expires within the plan's `expected_downtime_seconds` (default 120). It reads again immediately
   before the first stop, after the focused tests. A database that exists but cannot be read
   blocks. `--allow-lease-lapse` turns the refusal into a warning.
6. **Readiness waits for the runner and the providers.** Under `tessera serve`, `/health/ready` is
   ready only after the in-process runner completed a tick since the process started (when it
   runs) and the providers file was registered. The request gate is unchanged.

## Consequences

- For a release with a migration, a stored spec the new code cannot read is found only after the
  migrate and restart (post-migrate smoke); the campaign is then blocked, not failed, and a
  rollback unblocks it. Run `deploy-check --state-dir` against a restored copy migrated by the new
  release when that window matters.
- A deploy that would stop a service on a refused file now fails before any stop, with the
  service's own message. The check is only as wide as the arguments given: a unit whose arguments
  are not passed to `deploy-check` is not checked.
- A guardian config that carried an unknown key, or `unix_socket` without `proxy_uid`, or empty
  `organization_ids`, is now refused at start; `deploy-check --guardian-args` reports it first.
- With leases renewed to about 150 s, any held lease refuses a stop of 150 s or more; the upgrade
  in practice waits until no Salad stage holds a lease, the rule operators already followed.
- A campaign whose spec cannot be read stays blocked, and a cancel of it cannot finish, until the
  spec reads again.
- Not done here: an executor drain marker, a guardian status probe in readiness, and automatic
  rollback.
