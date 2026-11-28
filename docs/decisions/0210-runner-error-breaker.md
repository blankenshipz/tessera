# 0210 — One stage's error never wedges its campaign, and a repeating error blocks it

## Status

Accepted. Amends [ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) (the runner's
per-campaign error handling and delegated-step classification). No schema change and no
migration: the breaker's count is in memory and its outcome is recorded with existing statuses
and events.

## Context

The runner swallowed every per-campaign exception in `tick` and returned normally, so the
background loop never backed off and the campaign showed nothing but a log line. A
deterministic error repeated every interval forever (observed live: a re-run looped on one
`ValidationError` every few seconds with a hold held). Three paths fed that loop:

1. The stage loop of a running pass had no per-stage isolation: an exception from one stage
   ended the pass, so later stages were never observed or settled and the campaign never
   completed.
2. A refused provider preparation step (for example a Salad study scaffold) is a recorded
   failed intent that replays the same `StepFailed` on every tick, before anything is reserved.
3. `StepExecutor.run` turned any non-store exception from a store call into `StepTransient`, so a
   deterministic error stayed pending and was retried forever, logged as a deferral.

## Decision

1. **Per-stage isolation.** Each stage of a running pass is driven in its own guard. Control
   flow (lease lost, delegation lapsed, waiting, `StepTransient`) and every
   `sqlite3.OperationalError` end the pass exactly as before. A `StepFailed` that escapes a
   stage's non-attempt paths fails that stage closed (`step_failed:<step>:<class>`) only when the
   stage has no attempt that is active or `unknown`; otherwise it is recorded and the pass goes
   on, so a started attempt is never failed by it. This includes the success path: a step
   refused while a stage whose attempt already succeeded is being recorded (for example a
   measurement or lineage step) now fails the stage instead of replaying the refusal on every
   tick. Any other exception is logged by class,
   recorded once per (campaign, stage, class) as `campaign.stage_runner_error` (class name, and
   for a refused step its step and class; no exception text), and the pass continues with the
   next stage and the completion check.
2. **Prepare refusal fails the stage.** A `StepFailed` from a provider's `prepare` fails the
   stage closed as `prepare_refused:<step>:<class>`. Preparation runs before the attempt's
   reservation, grant or hold, so nothing is held for it.
3. **Breaker.** The runner counts, per campaign and error class, consecutive drives that raised
   it (a tick-level exception or an isolated stage error). A clean drive clears the count. A
   pass that ends deferred or waiting clears nothing: contention and `Waiting` count nothing,
   a deterministic reduce-only replay (section 4) counts its error class, and errors that
   earlier stages of the same pass recorded count either way. After
   `RUNNER_ERROR_THRESHOLD` (10) consecutive drives, a `running` or `stopping` campaign is
   blocked through the ordinary block path with `stop_reason=runner_error:<Class>` (only when no
   stop reason is set), one `campaign.runner_error` event and a `campaign.blocked` event. The
   block itself is reduce-only: no stage is failed and no attempt, lease or leased grant is
   touched, and the block settles nothing. A later pass of the blocked campaign gives back each
   never-leased Salad grant, settled at zero (ADR 0208 addendum), as for any blocked campaign.
   Unlike an unknown-attempt block it is not lifted by the runner on its own or by a
   restart: it stands until a person re-approves the campaign (a `campaign.approved` event after
   the breaker's event); the existing unblock then resumes it and clears the breaker's own stop
   reason, recorded as `cleared_stop_reason` on `campaign.unblocked`. Cancel works as for any
   blocked campaign. A `cancel_requested` campaign is never blocked by the breaker.
4. **Step classification.** A store call's exception stays `StepTransient` (intent pending,
   replayed) only when it is contention or I/O (a busy or locked database, a timeout, an OS or
   connection error, including SQLite's "disk I/O error"), or when the step is reduce-only.
   Reduce-only steps (revoke, reconcile, release, cancel) stay replayable by design: they create
   no authority, and a sticky failure would strand a hold that only a later, fixed replay can
   give back. When a reduce-only step's error is not contention or I/O, its `StepTransient` is
   marked deterministic with the error's class, and the breaker counts it (section 3): a
   `running` or `stopping` campaign that keeps raising it is blocked with every hold kept. A
   `cancel_requested` campaign is not blocked and keeps replaying (logged with the step and
   class); once it reaches the threshold, one `campaign.cancel_runner_error` event per
   (campaign, class) makes the loop visible. Any other exception of a forward step re-reads the intent
   (an effect that committed before the error is returned as done) and otherwise fails the intent
   with the exception's class and raises `StepFailed`. A deferral's log line names the step and
   underlying class.

## Consequences

A deterministic error costs at most ten passes before a person sees it on the campaign, and it
never stops sibling stages from settling. No cancel, claim, spend, trust or approval check
changes. The count resets when the process restarts, but the block itself is persisted. A
cancelling campaign whose reduce-only step keeps failing still replays every tick; it is shown
once per class by `campaign.cancel_runner_error`, never blocked.

## Addendum: every runner path, and one table for store and filesystem errors

### Context

Three gaps remained. The breaker only acted on a `running` or `stopping` campaign, so an error on
the path of a blocked or paused campaign repeated every pass with a log line and nothing else.
A provider's pre-stage hooks (`after_tick`, `evidence_pass`) ran outside the per-stage guard, so
one hook's error stopped every stage of the pass and then blocked the campaign. And each `except`
decided on its own what an error meant: any `OSError` was replayed forever with no bound (a
blob that retention removed, a full disk), while a SQLite "disk I/O error" outside a store call
counted toward every campaign's breaker, so a short run of them on a nearly full disk blocked all
of them, each then needing re-approval.

### Decision

1. **The breaker covers every status.** Every drive's error counts the same way whatever the
   campaign's status: the tick preamble (stuck-grant reclaim, paused release, exit resolution,
   unreported exits), the pre-stage hooks, the evidence pass, the blocked drive, the paused drive
   and the cancel path. At the threshold a `running` or `stopping` campaign is blocked as before.
   A campaign the breaker cannot block does not change status: a `cancel_requested` one records
   `campaign.cancel_runner_error` once per class (as before), and a `blocked` or
   `paused_delegation_lapsed` one records `campaign.stuck_runner_error` once per (campaign,
   status, class), with no second event when the campaign is already blocked by the breaker for
   the same class. Only an already `blocked` campaign then backs off: the runner skips its
   drive for 30 s, doubling to 300 s on each further trip. Any change to the campaign row
   (status or revision, for example a person's re-approval or cancel) or a clean drive lifts
   the back-off. The back-off is in memory, like the count. A `cancel_requested` or paused
   campaign is never backed off, so a cancel still runs every pass: it keeps settling exited
   attempts and releasing grants of its other stages.
2. **Pre-stage hooks are isolated like stages.** Each provider's `after_tick` and `evidence_pass`
   runs under the per-unit guard: control flow, `StepTransient`, database errors and host faults
   end the pass as before; a brief I/O error feeds the host streak and counts nothing; any other
   error (including any other filesystem error) is recorded once per (campaign, hook, provider, class)
   as `campaign.hook_runner_error` (class name only, no exception text), counted toward the
   breaker, and the stages of the pass still run. An evidence hook's escaped `StepFailed` is
   still recorded as `campaign.evidence_pass_failed` and not counted.
3. **One error table** (`runner_faults.classify`), used by delegated steps, the per-campaign
   handler, the tick itself and the stage and hook guards:
   - *contention* (a locked or busy database): replayed, counts nothing (unchanged).
   - *transient*, from a short explicit list only (`EAGAIN`, `EINTR`, `EBUSY`, `ETIMEDOUT`, a
     timeout, a dropped or refused connection). Outside a step (a stage, a hook, the drive's
     preamble) there is no replay bound, so it counts toward the campaign's breaker like a
     deterministic error. Inside a step the intent stays pending; its replay count is persisted on the pending intent as
     `error_class = transient:<n>:<Class>` (no schema change; a failed intent overwrites it).
     From the third replay the step backs off (2 s doubling to 60 s, in memory). Past 20
     replays it is deterministic.
   - *brief I/O* (SQLite "disk I/O error", `EIO`): replayed as a transient and never counted
     toward a campaign's breaker. Five ticks in a row that each saw one are a host fault; a step
     whose I/O error outlasts the 20-replay bound is a host fault too.
   - *environment* (disk full or quota, read-only filesystem or database, out of file
     descriptors or memory, a database that cannot be opened, or a missing or unreadable
     state directory: an `ENOENT`, `ENOTDIR`, `EACCES`, `EPERM` or `ENODEV` that names the state
     root, an ancestor of it or one of its top-level entries, or any of those while the root is
     not a readable, writable directory, as after a deploy with the wrong owner or an
     unmounted volume): one host-level alarm per
     spell (logged once and reported as `runner_host_fault`, critical, on
     `GET /operator/health`), and the runner pauses all work for 30 s doubling to 300 s. No
     campaign is counted, blocked or changed and no intent is failed, so nothing needs
     re-approval: the first clean tick after the pause clears the alarm and work resumes. The
     alarm is in memory because a full disk cannot record it.
   - *deterministic*: everything else, including an `OSError` whose errno is not listed above
     or that has none (`EEXIST`, `EXDEV`, `EINVAL`, a `URLError`) and a missing or unreadable
     file inside a usable state root. A forward step's intent fails (`StepFailed`, as before); a
     reduce-only step's replay is counted toward the breaker (as before). Outside a step it is
     recorded once as a stage or hook runner error and counted toward the breaker; the stage is
     not failed. The one exception is a missing blob for a recorded sha
     (`ENOENT` whose final path component contains a sha-256 digest, matched by name only, so a
     tenant or project file or directory so named counts too; `runner_faults.MissingBlob` is also
     accepted but nothing raises it today): a stage that
     raises it with no active or `unknown` attempt fails closed as `os_error:ENOENT`, and one with
     such an attempt is recorded as a stage runner error and counted, never failing a started
     attempt.

### Consequences

A deterministic error, or a transient one outside a step, raised on every pass at a stage, a
pre-stage hook, the evidence pass or the drive's preamble costs at most ten passes before it is on
the campaign record, with one breaker event however long it repeats; sibling stages keep advancing
in each of those passes. A transient error inside a step is bounded by its 20 persisted replays
instead. After the block a blocked campaign is retried at most every five minutes instead of every
pass; a cancelling one is retried every pass. Not yet covered: the tick-level setup (scheduling,
expired-grant and idle-release sweeps) and provider `sweep`, where a repeating error still stalls
the tick with log lines only. A failing
hook no longer holds every stage back. A host that runs out of disk pauses instead of blocking
every campaign; when the disk is freed, campaigns resume without a person. A transient error that
never clears now ends a forward step after a bounded number of replays. No cancel, claim, spend,
trust or approval check changes.
