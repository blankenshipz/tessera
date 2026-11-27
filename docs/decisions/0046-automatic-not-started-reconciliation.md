# 0046 — Automatic reconciliation of attempts whose launch was refused

## Status

Implemented. No schema change. Narrows "every unknown effect needs manual reconciliation"
([ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) section 4, DESIGN.md retry
classes) for one case the worker itself reports: a launch refused before any container
existed. Every other unknown attempt keeps the human path. Uses the frames of
[ADR 0041](0041-execution-frames.md) and its zero settlement bases.

## Context

A launch that fails before a container exists (for example the engine cannot create the
container network because its address pools are exhausted) leaves a campaign attempt that
nobody ran. Its grant lapses, the attempt becomes `unknown`, the stage `blocked_unknown`
and the campaign `blocked`, and it waits for a person although there is nothing to
reconcile. The platform's stated aim is that campaigns run, retry, reconcile and record
themselves.

Control cannot see a container. It only hears what the worker reports, so silence is not
evidence of absence. A worker can create and start a container and lose contact before its
first report; the worker may be on another host, so control's own boot time says nothing
about it. A host reboot is therefore **not** evidence that nothing started and is not a
trigger (an earlier draft used it; review showed it would label a container that really
ran as `not_started` and run it again).

Facts this design relies on:

- An attempt becomes `unknown` only after its grant expired.
- A zero-hold local attempt has no money to protect; the risk is only wrongly claiming that
  a container never ran.
- `unclaimed_zero` is a final zero basis that releases the experiment hold (ADR 0041 Q3).
- A worker's observation is a trusted-worker assertion (`observation_proof`); control
  records it and never infers a provider outcome from silence.

## Decision

### 1. The rule (fail closed)

`campaign_autoreconcile.assess` reads control-plane records only. An unknown attempt is
reconciled automatically only if **every** guard holds:

- it is unsettled, holds `0` cents, and has no exit code, run receipt, manifest or Salad
  evidence;
- its grant is unresolved, expired, has `max_exposure_cents = 0`, and every lease on it is
  past its hard expiry;
- it has at most one external operation and that operation has no provider identity;
- no worker observation for the grant ever carried a provider identity, a `running` or
  `exited` state or a termination confirmation;
- there is no operation receipt, run receipt, recovery record, Kubernetes dispatch, output
  manifest, workspace handoff or Salad attempt record;

**and** the worker reported a launch error for the grant: `launch_error` on an unknown
observation with no provider identity, a closed class such as
`network_address_pool_exhausted` (`ReportExecutionObservation.launch_error`, recorded in the
`execution.observation_reported` event and read scoped by project). Control refuses the
report if container evidence already exists for the grant.

An attempt without a reported launch error, in particular one that is merely unreported
after a lease (a host reboot, a lost control link, a remote worker), has an unknown effect
and keeps the existing human or guarded path untouched, as do attempts that held money or
show any container evidence.

### 2. Worker contract

A reported launch error is a trusted worker assertion, so the worker must never start the
operation afterwards. The development worker raises it only on a definite Docker refusal
with a fresh read showing no container (never on a lost reply or timeout) and its
supervisor journal then holds the operation `uncertain`, where a later drive only
reconciles and never prepares or starts. Tested in `tests/test_worker_launch_error.py`.
Control backs this with a fence: once a launch error is recorded for an operation, control
refuses to renew its lease (409), so a worker that ignored the contract cannot keep the
operation alive past the reported refusal. Tested in `tests/test_campaign_autoreconcile.py`.

### 3. What it records and does

In one runner transaction (re-assessed inside it, under the runner lease):

- the grant is reconciled at zero (`settlement_cents = 0`; its exposure is zero, so no
  limit hold changes) and an `execution.not_started_grant_reconciled` event records it with
  `provider_termination_inferred: false`;
- the attempt becomes `failed_transient` with `failure_class = not_started` and a reason
  naming the launch error; it is settled zero on the `unclaimed_zero` basis through the
  single settlement path, so the frame closes (as `lost`, claiming no outcome) and the
  experiment hold is released;
- a `campaign.attempt_not_started` event is recorded on the campaign (and the grant's task)
  with the attempt, grant, reason, evidence digest, `consumed_scientific_retry: false` and
  `is_evidence: false`; the frame settlement receipt carries the reason and evidence
  digest, and the campaign receipt marks the attempt `not_started` with `is_evidence: false`;
- a stage that was `blocked_unknown` returns to `preparing`, and a `blocked` campaign with no
  other unknown attempt resumes.

`not_started` is **no evidence**: the attempt carries no exit code, receipt, manifest or
measurement, so no gate, stop rule or metric can read it as a pass or a fail.

### 4. Retry accounting and bound

A not-started attempt uses an attempt number but not a scientific retry: the
`retry.max_attempts` check counts `attempts_used` minus not-started attempts. Attempt
numbers stay capped at five.

Repeats of the **same reason** are bounded: after `MAX_AUTO_REQUEUES` (2) consecutive
not-started attempts of one stage with the same launch-error class, the next identical one
is **not** reconciled: it stays `unknown`, the stage `blocked_unknown` and the campaign
`blocked`, with the attempt's detail, the `campaign.blocked` event and a
`campaign.autoreconcile_stopped` event naming the repeat. A person then reconciles it as
before. Alternating classes are bounded only by the five attempt numbers; the same applies
when attempt numbers are exhausted.

### 5. Where it runs

The campaign runner applies it on every tick, so it runs at service startup (the first tick
after a restart) and in the background loop: on the stage-advance path for a running
campaign, on the blocked path for a campaign already blocked (including when the worker's
report arrives late), and on the cancel path so a cancelled campaign is not held open by an
orphan.

### 6. Worker reporting

The development CPU adapter raises `CpuLaunchError` only on a definite Docker refusal with
nothing launched: a refused egress network create (before any container exists), or a
refused container create followed by a fresh read showing no container. Only the closed
class leaves the worker (never Docker's text, paths or host names). The supervisor carries
it on `DriverResult.launch_error`, the observation report posts it, and the worker's report
journal stores it in a `launch_error` column (added in place to an older journal) so a
pending report replays byte for byte and is never merged with a plain unknown report.

## Consequences

- Launch refusals that provably started nothing no longer wait for a person.
- A reboot, or any unreported attempt, still blocks until a person reconciles it; this is
  the cost of never labelling a container that ran as "not started".
- The trust placed in a worker's launch-error report is the same as in its other
  observations; control adds only the container-evidence guards above.

## Tests

`tests/test_campaign_autoreconcile.py`: refused launch reconciled and re-queued, a started
but unreported attempt NOT reconciled, late report on a blocked campaign, cancel path,
retry not consumed, money-held and container-evidence guards, bounded repeat and the
gate-evidence columns left empty. `tests/test_worker_launch_error.py`: adapter
classification, no claim on a lost reply, no start after a launch error, the supervisor
result, the journal column and a refused launch reported by the worker and recovered by the
runner.

## Amendment: worker authority that lapses before a Salad route starts

A Salad attempt holds its execution lease from packaging through its plan and source-use
review. The lease's hard expiry is fixed when the lease is issued, and a soft lease that lapsed
is renewed only inside a running live check. So a review wait longer than that bound leaves the
attempt with authority that can no longer route, and control then refuses the route (lease
fenced, or the worker shown as disconnected). Nothing was sent to the provider before
`start_route`.

Such an attempt is now unwound through the existing unclaimed path (cancel any proposed route,
revoke the grant, reconcile at 0 on the store's own no-claim proof). It closes as
`failed_transient` / `not_started`, so it uses an attempt number but no scientific retry, and the
stage gets a fresh grant and lease. Two reasons apply: `lease_lapsed_before_route`, read from the
lease row before the reviewed, capability or route phases run, and
`worker_unavailable_before_route`, used when a pre-route step was refused and the registry shows
the worker not ready or not seen within its heartbeat window. Both are read from the store and
never parsed from a refusal message. While the review is still pending the attempt is left alone,
so a person who is away does not get a new review stop for every lapse.

The identical-repeat bound above still holds. Once a reason has repeated `MAX_AUTO_REQUEUES`
times, the attempt keeps `t1_salad_unclaimed` and the stage's own retry bound decides. A
completed `start_route`, a claimed provider effect, and every refusal that is not about worker
authority (review, policy, capability or configuration) stay exactly as before.

The Salad executor also heartbeats its worker registration on every pass, whether it is idle,
holding only fenced attempts, or unable to list its work. Worker liveness no longer depends on
lease renewals. Tests: `tests/test_salad_review_wait_lease.py`.
