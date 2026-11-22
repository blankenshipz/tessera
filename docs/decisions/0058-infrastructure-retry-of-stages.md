# 0058 — Infrastructure retries of campaign stages

## Status

Implemented. No schema change. Extends the retry classes of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) section 4 and builds on the
provider model of [ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) and the data
destination rules of [ADR 0047](0047-agent-routed-data-destinations.md).

## Context

A stage attempt can end because the infrastructure under it failed, not because the task
did: the container engine or the kernel's out-of-memory killer ends the container (exit
137), or a worker cancels a run it could not report. Before this change only two cases
were transient (a grant that expired unleased, and an exit code the spec declared
transient), so a session had to relaunch such a stage by hand.

## Decision

### 1. What counts, and what never does

A confirmed exit whose recorded code is one the spec names as infrastructure
(`retry.infra_exit_codes`, default `[137]`) on an attempt that would otherwise be a
permanent undeclared-exit failure is reclassified `failed_transient` with failure class
`infra_container_killed` and a reason that names the exit code and the retry number. This
holds on both ways an exit reaches the runner: the normal observation path, and the
confirmed-exit resolution of [ADR 0053](0053-resolve-attempts-on-confirmed-exit.md) (an
exit control recorded when the worker's report never arrived, ADR 0054), which uses the same
classification and bound and notes that it resolved on the worker's confirmed exit.

Nothing else is retried by this rule: a task failure (any other exit code), a declared
transient code (which keeps its own class and its scientific retry), a refused step, a
policy or data-class denial, a budget refusal, and any attempt whose effect is unknown
(those stay `unknown` for a person, or for the reconciliation of
[ADR 0046](0046-automatic-not-started-reconciliation.md) when it provably never started).
An infrastructure failure that nobody can confirm is not guessed at.

### 2. The bound, off unless the spec enables it

`retry.infra_retries` (0 to 3, **default 0**) is the number of extra attempts infrastructure
failures may use, beyond `max_attempts`. It is off unless a spec says otherwise, so an
approval never covers spend its approver did not see: a spec written before this change, or
without the field, gets no infrastructure retry and keeps its digest exactly. When it is
enabled it is in the digest a person approves, and the stage's worst-case exposure counts
it: `max_exposure_cents × (max_attempts + infra_retries)` must fit the experiment budget
ceiling at admission, for every provider including billable ones, and the stage's
experiment budget ceiling is that same sum (capped at the platform ceiling, which the
admission check makes unreachable for an enabled spec). The campaign cap and project limits
still bind every attempt as before, and so does the operator's own billable ceiling: a Salad
stage's `max_exposure_cents × (max_attempts + infra_retries)` must fit its profile's
per-task ceiling (`max_task_cents`), so enabled retries can never take a billable stage
past what the operator allows.

What an approver agrees to is stated, not inferred from omitted defaults: the submit
response and every campaign read carry `infra_retry_policy`, per stage, with the
retry bound (0 when off), the exit codes that count, the reroute providers and the
worst-case attempt count the holds are counted over.

An infrastructure attempt never uses a scientific retry, and a scientific retry never uses an
infrastructure one. When the bound is spent the failure stays permanent
(`undeclared_exit_code`) and a `campaign.stage_infra_retries_exhausted` event records why
(never for a spec that left the loop off). Attempt numbers stay capped at five and `0` is
off. The fields are omitted from the stored spec at their defaults.

### 3. Each retry is a real attempt

A retry is a new attempt under the same approval: it reserves its own hold against the
campaign budget cap and the project limits (a refusal is a recorded budget refusal, never
retried), takes its own grant, run receipt, settlement and receipt entry, and the
campaign receipt marks the failed attempt with the recorded reason
(`infrastructure_retry`). The stage's experiment budget ceiling covers `max_attempts` plus
`infra_retries` holds. A `campaign.stage_infra_retry` event records the reason, the retries
used and left, and the provider the retry runs on. A cancelled or stopping campaign never
retries.

### 4. Optional reroute

`retry.reroute_providers` (up to four provider ids) lets an infrastructure retry move to
another provider. It is part of the approved spec, so a person approves the destinations.
Admission refuses a reroute provider that is not registered, is not an unbilled
worker-grant provider of the stage's runtime, is forbidden by the campaign, repeats the
stage's own provider, or whose worker principal the provider trust setting does not admit
for the stage's inputs. Those checks (`CampaignStore.check_reroute`: registered, unbilled
worker-grant provider of the runtime, the provider's own stage check, trust narrowing and
input admission) run at submit and approval and are **repeated when the retry is reserved**,
so a provider that stopped qualifying is skipped, with a warning, rather than used. The runner moves only along that list, in
order, to a provider not yet tried and still registered, and returns to the stage's own
provider when none is left. A rerouted attempt runs the stage on the target as a worker-grant stage of the same runtime:
it takes the target's own worker principal and **drops the stage's `provider_config`** and
any stage-level worker principal (they belong to the provider the stage was approved on), so
a stage that depends on provider-specific configuration should not name a reroute target.
Nothing outside the approved list is ever chosen, so no data reaches an unregistered
destination.

### 5. Seeing and stopping it

`tessera_campaign_retries` (`GET /campaigns/{id}/retries`, `campaign:read`) returns, per
stage, the bound, the retries used and remaining, the infrastructure failures with their
recorded reasons and providers, the exit codes and the reroute list. It changes nothing.
Stopping the retries is stopping the campaign with `tessera_cancel_campaign`
(`campaign:cancel`): a stopping campaign retries nothing. `tessera_get_campaign` and the
`logs` view already show every attempt and its failure class.

## Non-goals and follow-ups

- **Lease loss and a worker report failure with no confirmed exit** are not retried: the
  run may still be going, and a second dispatch would be a duplicate run of an unknown
  effect. They stay `unknown` for a person (or ADR 0046 when nothing provably started).
- **A provider that is unavailable** (unregistered after approval, or refusing the stage's
  inputs) is not retried either: nothing ran, so there is no infrastructure failure to
  classify. The stage fails as it did (`provider_unavailable`, `input_not_admitted`). Using
  `reroute_providers` for an unavailable *first* attempt is a follow-up.
- A provider other than a worker-grant provider (for example Salad) is never a reroute
  target; infrastructure failures of its own are the Salad guardian's and settlement's.

## Consequences

- A killed container no longer needs a session to relaunch the stage, within a bound the
  approved spec sets and each retry's cost counted.
- A deterministic out-of-memory kill repeats up to the bound, so a spec that enables
  retries with a tight limit should raise the limit or leave `infra_retries` at 0.
- Lease loss, an unreported start and a worker report failure with no confirmed exit stay
  unknown and need a person: Tessera does not retry what may still be running.

## Tests

`tests/test_campaign_infra_retry.py`: off by default, the digest and the worst-case exposure
counting an enabled bound (and the billable Salad per-task ceiling),
the approval-time `infra_retry_policy` view, the experiment ceiling covering every attempt, the confirmed-exit
path retrying and respecting the bound, reservation-time reroute checks, a rerouted attempt
dropping provider settings, kill then success with its own hold, grant, receipt
and recorded reason, the bound and its exhaustion event, a task failure never retried,
`infra_retries: 0`, no use of a scientific retry, a declared transient code winning, a
cancelled campaign not retrying, the budget cap, an unchanged digest and the policy
bounds, a reroute to a registered named provider, a reroute refused at submit, and the
retry-state view. The MCP tests cover the new tool and route.
