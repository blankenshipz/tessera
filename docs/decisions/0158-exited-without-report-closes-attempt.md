# 0158 — Closing an attempt whose container exited but whose executor never reported

## Status

Proposed. No schema change. Amends [ADR 0087](0087-execution-plane-agents.md) (a stage watch
report never touches attempt state) for one narrow, fenced case, and adds a fallback beside
[ADR 0053](0053-resolve-attempts-on-confirmed-exit.md) (resolving attempts on the executor's
confirmed exit). It does not change ADR 0053, ADR 0048 or the human reconcile route.

## Context

A local stage container was killed (exit 137, not out of memory). The stage watcher saw it and
reported `failed`, but the worker executor's confirmed exit observation and run receipt never
reached control. ADR 0053 needs that observation, and ADR 0087 forbids the watcher's report from
touching attempt state. Nothing on the control side ended the `running` attempt. Its grant held the
project's only free slot until it expired about an hour later, with its lease long expired, and
other campaigns queued behind it.

The primary fix belongs in the worker: the executor must deliver a definitive exit even after its
lease lapses, and then ADR 0053 handles the rest. This ADR is the fallback for when the executor
path is dead.

Today's `failed` watch report cannot serve as that fallback. It does not distinguish "the
container exited" from "the probe lost the container" or "the deadline passed".

## Decision

### 1. The report says when the exit was definitive

The watch report gains one optional closed field, `exit = {exit_code: int 0..255, oom_killed:
bool}`. The watcher sets it only on a `failed` report whose probe sample was `exited` with an
integer exit code. That means `docker inspect` showed the container exited or dead, not running,
with labels matching the operation and an integer `ExitCode`. The field is never set for:

- an `unknown` or `absent` probe (three ticks);
- a passed deadline;
- an exit with no code (`exited_unknown`).

The worker body check and the control model accept the field only on `failed`. A report without it
is valid and keeps its exact body and idempotency digest. Control copies the field onto the
`execution.watch_reported` event. Recording the report still writes nothing else.

### 2. A fenced runner sweep may close the attempt

`campaign_exit_without_report.assess` reads control-plane records only. The runner applies it in one
transaction (`CampaignRunner._close_unreported_exit`) on every tick, after ADR 0053's sweep. It
closes an attempt only if **all** of these hold:

- The stage's provider is a non-billable worker-grant provider.
- The attempt is `admitted` or `running` and unsettled.
- Its grant is `active` or `cancel_requested`, with exactly one external operation, and that
  operation is the attempt's.
- The operation's **latest** watch event is a `failed` report carrying a well-formed `exit` with a
  non-zero code or `oom_killed`. Its grant, lease and epoch equal the operation's, and the lease
  row's epoch equals the operation's.
- No confirmed exit is on record from any source. That case belongs to ADR 0053, which always runs
  first and wins.
- No run receipt or output manifest exists.
- Nothing is later than the watch exit (`observed_at` at or after it): no worker, provider-receipt or
  recovery observation, no worker observation recorded after the watch event, and no lease of the
  grant issued afterwards. A later `running` contradicts the exit.
- Every lease of the grant has expired, so the executor stopped renewing.
- No output upload is still arriving (ADR 0064's `uploads_arriving`, with its hold ceiling).
- At least `exit_report_grace_seconds` of control's clock have passed since control recorded the
  watch event (`events.created_at`, never the worker's clock). The default is 300 s, the bounds are
  120..900 s, and the value is a module constant with a validated runner attribute, the same way
  ADR 0053's grace is a constant.

Every close is `failed_transient` with failure class `exited_without_report`, never
`failed_permanent`. The exit class records what the watcher saw, for the receipt only:

| Exit class | When |
| --- | --- |
| `oom` | `oom_killed` is true |
| `killed` | exit 137 or 143, not OOM |
| `error` | any other non-zero code (declared transient or not; the evidence records which) |

The stage's retry policy decides what follows: another attempt while `retry.max_attempts` allows
it, then the stage fails with `exited_without_report_exhausted`. This deliberately differs from
ADR 0053, where an undeclared code on the executor's confirmed exit is a permanent failure: the
watcher's word alone never makes a failure permanent.

With ADR 0058 infrastructure retries on, a `killed` or `oom` exit whose code is in the spec's infra
codes goes through `_infra_failure` and becomes `failed_transient` / `infra_container_killed`, so it
spends the infra bound and not a scientific retry. Once that bound is spent the close stays
`failed_transient` / `exited_without_report`, and `retry.max_attempts` ends it.

The cost is zero (`local_zero`) and never above the hold.

ADR 0053's `release_grant` reconciles the grant, so the slot frees in the same transaction. In that
same transaction:

- `_settle` records `exit_resolved` evidence: the reason, exit class and code, the watch event
  cursor, seq and times, the lease and epoch, the grace, and the evidence digest.
- The existing events `execution.exited_grant_reconciled` (`observation_cursor: null`,
  `watch_event_cursor`) and `campaign.attempt_exit_resolved` are written, now carrying
  `closed_reason`, `exit_class`, `exit_code` and `watch_event_cursor`.
- The campaign receipt's `exit_resolved` entry shows the closed reason, exit class, exit code and
  watch event. Older entries keep their shape.

The watcher's exit code is never written as the attempt's `exit_code` column. The stage's retry
policy decides what follows.

### 3. Races and late reports

The assessment is repeated inside the runner transaction, under `_hold_lease`. The grant release is
guarded on the grant's revision and status (`reconcile_grant_row`). Any second closer raises
`Conflict`, and everything here rolls back. Second closers include ADR 0053, ADR 0048's expired-grant
release, a human reconcile and a second sweep. A runner that lost its lease writes nothing.

After the close, a late exit observation (`execution.py`), run receipt (`execution_run_receipts.py`)
or watch report (`grant_reconciled`) is refused because the grant is reconciled. Nothing is settled
twice, and a late receipt never turns the attempt into a pass.

## What is trusted and what is not

- **Trusted.** The watch report is a worker assertion (`trusted_worker_assertion`). It comes from the
  same worker trust domain as the executor's own observation, which ADR 0053 already acts on. It is
  trusted only as "this worker's probe read this container as exited with this code". The code
  classifies the close for the receipt (`killed`, `oom`, `error`) and nothing else: it never makes
  a failure permanent, never becomes the attempt's `exit_code`, and never decides a retry beyond
  what any transient failure gets.
- **Not trusted.** On its own the report closes nothing. Control's own records must agree that
  nothing is still running: an expired lease, no later observation, no live upload, the grace
  passed. The outcome is never a pass and never a measurement (`is_evidence: false`).
- **Never closes anything.** A report without `exit`, an unknown or absent probe, a passed deadline,
  `exited_unknown`, a zero code without OOM, a mismatched lease or epoch, a live lease, a later
  observation, a confirmed exit, a receipt or manifest, or a billable or route-based provider.
- **The exit evidence assumes the container cannot restart.** Every local Docker path creates
  containers with restart policy `no`:
  - `local_docker_engine.py`: `--restart no` at create, and `RestartPolicy {"Name": "no"}` is
    verified on inspect;
  - `cpu.py`: `--restart no` on both create paths;
  - `ordinary_docker_contract.py`: `RestartPolicy {"Name": "no"}`.

  Kubernetes paths use `restartPolicy: Never`. A restarted container would also show up as a later
  observation or lease, which blocks the close.

## Consequences

- An attempt like the one in the incident closes about 300 s after the watcher's report, not about an
  hour. Its slot is freed, and its retry follows the stage policy.
- No schema change. The new field lives in the event's `payload_json`, the failure class is text,
  and the body keys are code constants.
- A worker that does not send `exit` behaves exactly as before.
- A run that truly failed with an undeclared code is retried up to `retry.max_attempts` instead of
  failing at once, as it would under ADR 0053. That costs at most the stage's own retry bound (a
  free worker grant, settled at zero) and is the price of not trusting the watcher's code further.

## Coordination

- **ADR 0053** runs first and takes every case with a confirmed exit. This path never acts while a
  confirmed exit is on record.
- **Blocked-campaign slot reclaim** covers stuck campaigns whose attempts have no lease and no
  operation (`t1_grant_expired_unleased`). This path needs an operation and a lease, so the two do
  not overlap.
- **The worker-side fix** (the executor reports a definitive exit after lease expiry) is primary.
  Once it lands, this path should rarely fire.
