# 0067 — Resource-limit stops are infrastructure retries

## Status

Implemented. No schema change. Extends the infrastructure retry of
[ADR 0058](0058-infrastructure-retry-of-stages.md) and uses the placement of
[ADR 0060](0060-capacity-aware-stage-placement.md).

## Context

A stage can end because it reached a resource limit rather than because its task failed: it
stopped on its own time budget and said so in a metric, it was killed at the stage's time limit,
or the container ran out of memory. Each ended the campaign and needed a person, although the
cause is the capacity of the place it ran, not the work.

## Decision

### 1. What counts

- **A stop metric.** A spec may list stage metrics in `retry.resource_limit_metrics` (up to four,
  each a declared metric of the stage). An attempt that otherwise succeeded, whose recorded value
  for a listed metric is a number above zero, is not a success: it is classified
  `failed_transient` / `infra_resource_limit`, with the metric and value in its reason.
- **A kill.** A confirmed exit with a code in `retry.infra_exit_codes` (default 137: the kernel's
  out-of-memory killer, or a kill at the stage time limit) stays `infra_container_killed`.

Only a platform-measured value counts: a missing, non-numeric, boolean or zero measurement is
not a stop. Both classes use ADR 0058's bound, `retry.infra_retries` (off unless the spec enables
it, counted in the approved digest and the worst-case exposure), never a scientific retry. With
the bound off, or spent, a stop is left exactly as it was before this change: a kill stays the
permanent failure it was, and an attempt that succeeded with a stop metric above zero keeps its
recorded success (a spent bound never turns a result into a failure); the spent bound is recorded
once, naming the metric, so the stop is on the timeline and receipt. A metric listed in
`resource_limit_metrics` may not also be a `stop_when` metric: the spec is refused at validation,
so one metric never means both an infrastructure stop and a scientific stop.

### 2. Where the retry goes

- **Placement candidates (ADR 0060).** The candidate whose attempt just ended on a resource limit
  is skipped for the next placement with the recorded reason `resource_limit_last_attempt`; the
  stage moves to the next eligible approved candidate, through the same checks and records.
- **Reroute providers (ADR 0058).** The retry moves along the approved list as before; the stage's
  own provider is the last fallback.
- **The same provider, after a stop on the stage's time budget,** is never chosen again: free
  memory says nothing about time, and the approved spec has no raised time budget to retry with
  (a spec's `max_duration_seconds` is digest-pinned). The next candidate takes it, else the stage
  ends as below. A raised-budget retry would be a spec change and its own decision.
- **The same provider, after a kill,** depends on whether the stage has anywhere else to go. A
  stage with no placement alternates and no reroute providers keeps the in-place retry ADR 0058
  gave it, bounded by `infra_retries`. A kill is exit 137 whether the kernel's memory killer or
  the stage time limit ended the container, so a kill at the time limit repeats the same limit
  in place until `infra_retries` is spent, exactly as ADR 0058 already allowed; only a stop the
  stage reports on its own time budget is never repeated in place (above). (This ADR restores it; an earlier draft gated it on memory,
  which would have ended such stages on every worker that reports none). Where other approved
  places exist (placement alternates and reroute providers are one candidate list, ADR 0060
  section 5a), a candidate untried by the infrastructure comes first, and one whose own attempt
  was killed is chosen again only when its free memory is shown to exceed the limit that attempt
  ran with. The gate covers every code in `retry.infra_exit_codes`, not only 137: control cannot
  tell an out-of-memory kill from another kill, so a stage that lists a non-memory code (143, for
  example) and has other places also needs that evidence to return to a killed provider. The limit is the container memory recorded on the attempt's
  run receipt (`memory_bytes`) when the last attempt ran on that provider, else the container
  limit that provider's worker reports (workers may be configured differently). Free memory is the
  figure the worker reported in a fresh heartbeat. Either figure unknown, or free memory not above
  the limit, means not allowed.
- **No place left.** The stage ends with `resource_limit_no_provider` (also when every placement
  candidate was skipped, the last attempt's with `resource_limit_last_attempt`) and a
  `campaign.stage_resource_limit_no_provider` event records what was known, instead of
  repeating the stop where it happened. The stop reason is on the receipt. When a candidate was
  also skipped for a budget or policy cause (`over_run_cap`, `plan_mismatch`, `untrusted`,
  `not_enrolled`; `over_campaign_budget` takes its own earlier path), that cause is the stage
  failure and the resource limit stays only in the recorded skip list.

### 3. What the worker reports

A heartbeat may carry `memory_available_bytes` (the host's `MemAvailable`) and
`container_memory_bytes` (the memory limit its stage containers run with), recorded on the
`worker.heartbeat` event and read only for this decision. They are optional (an older worker
sends none), come from figures the host could read (never guessed), and a pending beat replays
the body it first sent, so a restart cannot change what an idempotency key covered.

Both figures are the worker's own unverified claim: control records what the heartbeat said and
cannot check it against the host. The free-memory figure is self-reported by the worker principal and is trusted only as far as
that principal already is: it can only allow a bounded retry (`infra_retries`) on a provider the
approval already names, never add a destination, raise a cap or change a hold, so a false figure
can waste at most the retries the approver counted.

**Upgrade order: control plane first.** An older control plane's heartbeat model forbids unknown
fields. A worker that is rejected (HTTP 400/422) for its memory fields sends the plain heartbeat
under its own key and stops reporting memory until it restarts, so liveness never depends on the
new fields; the resource-limit decision then sees no report (unknown, as for an older worker). The run
receipt records the container's `memory_bytes` beside `scratch_bytes`.

### 4. Unchanged

The campaign budget cap, every hold, the project limits and ADR 0047 apply to each retry exactly
as to any attempt: placement and reroute choose only among approved, registered, enrolled, trusted
providers and no destination is added. Each attempt keeps its own receipt entry
(`infrastructure_retry` carries the reason) and the retry is recorded as `campaign.stage_infra_retry`.

## Consequences

- A stage that ran out of time or memory is retried by the runner on another approved provider
  within a bound the approver saw, instead of waiting for a person.
- ADR 0058 behaviour is unchanged for a single-provider stage killed with 137: it retries in
  place within `infra_retries`. A single-provider stage that stopped on its own time budget now
  ends as `resource_limit_no_provider` rather than repeat the stop; the operator raises the
  limit or adds a candidate.
- A stop metric is the stage's own claim: it is read only for stages whose spec names it, and a
  retry it causes is bounded and recorded.

## Tests

`tests/test_resource_limit_retry.py`: a stop retried with the reason on the receipt, zero and
unlisted metrics ignored, off by default, the bound and its exhaustion, a kill on the same path,
the metric reader, the digest and validation, a time-budget stop never repeated in place
whatever the free memory, a single-provider 137 retried in place within the bound, the next
placement candidate and a reroute provider taking the retry, every placement candidate skipped
(event, stop reason and receipt record `resource_limit_no_provider`), the heartbeat body,
journal replay and memory reader, `memory_report` and its heartbeat freshness read directly,
the return to the first provider with room and its refusal for too little, stale or silent
memory, the limit taken from the attempt that ran there, a retry refused when the budget cap is
spent, an exit 137 SIGKILL retried in place, no lineage registered for a reclassified stop, the `stop_when` overlap refusal, and the fallback heartbeat
for an older control plane.
