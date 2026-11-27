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
   block is reduce-only: no attempt, grant or hold is touched, nothing is settled and no stage is
   failed. Unlike an unknown-attempt block it is not lifted by the runner on its own or by a
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
