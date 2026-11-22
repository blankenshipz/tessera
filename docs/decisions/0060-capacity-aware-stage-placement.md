# ADR 0060: capacity-aware placement of campaign stages on provider candidates

Status: accepted. No schema change. Builds on ADR 0038 (campaign runner), 0040 (providers and
data-class routing), 0045 (program budget), 0047 (agent-routed data destinations), 0048 (grant
lifetime and slot release), 0053 (resolving a confirmed exit), 0055 (steering tools) and 0058
(infrastructure retries, whose reroute list it absorbs, section 5a).

## Context

Each v2 stage names one provider. A project host typically has one local CPU slot, so
independent CPU stages queue behind each other while other registered providers (a self-hosted
server, a Salad CPU profile) sit idle, and an operator session had to rewrite specs to spread the
work. Tessera itself should spread a campaign's work across the providers the operator
registered, without ever reaching the public internet or an unregistered destination
(ADR 0047), and without any new way around admission, budget or trust.

## Decision

### 1. Candidates in the spec

A v2 stage may add `alternate_providers`: up to four `{provider, runtime, provider_config,
max_exposure_cents?}` entries. The stage's own `provider`/`runtime`/`provider_config` is the first
candidate; the alternates follow in the order written. The field is omitted from the stored spec
when absent, so every existing spec keeps its digest and a stage without alternates runs exactly
as before (no placement event, no receipt change).

A candidate's `max_exposure_cents` is its cost estimate and the hold taken while it runs; absent,
the stage's own value is used. It may not exceed the stage's `max_exposure_cents`, which stays the
per-run cap the approver saw. Candidates must be distinct and none may be in the campaign's
`forbid_providers`.

At submit, approval and standing starts every candidate passes the checks the stage's own provider
already passed: it is registered with that runtime and a valid `provider_config` and output limits,
its worker is enrolled for the project (`check_stage`), its grant lifetime fits, its trust
narrowing is valid, and provider trust admits its worker for every stage input. The approver
therefore approves every destination by digest; the delegation's worker set includes each
candidate's worker and nothing else.

### 2. Placement

Whenever a stage with alternates needs a new attempt (it became ready, or a transient failure
allows a retry), the runner evaluates the candidates in order and chooses the first that:

1. is not excluded or un-pinned by steering (section 4);
2. is still registered with the runtime and passes the provider's own stage validation;
3. has a worker enrolled for the campaign's project (`check_stage`);
4. is trusted by provider trust for every input the stage reads (`check_stage_inputs`, under the
   intersection of the approval-time setting, the current setting and `send_classes`). Outputs are
   produced on the provider from those inputs and always come back `private_derived` (ADR 0039),
   so they expose nothing to it beyond what the input check already admitted;
5. fits the per-run cap (its estimate is at most the stage's `max_exposure_cents`);
6. fits the campaign's remaining budget (held plus settled plus its estimate within the cap);
7. for a positive estimate: no covering program budget is exhausted or unavailable (ADR 0045) and
   no provider exposure is unresolved;
8. fits the project's concurrency and monetary limits;
9. has a free slot: the provider's free slots (`free_slots`, else `slot_free`, ADR 0048) minus
   attempts already placed there that do not hold a grant yet. A provider without a slot hook is
   bounded only by the checks above.

These are the existing checks, called from one place; the attempt's reservation, release, grant
issue and settlement then run the unchanged per-provider path and repeat their own checks, so a
race between placement and reservation fails closed exactly as a single-provider stage does.

The evaluation is deterministic: the same recorded state yields the same choice. Because every
attempt placed but not yet granted counts against its provider's slots, independent stages in one
tick go to different providers and run concurrently.

The chosen provider's `prepare` (scaffolding such as Salad's, which may take several ticks or wait
for a person) runs before every reservation on this path and must return `ready` before anything
is reserved there. No earlier attempt is taken as proof that a provider is still prepared, so a
stage that moves A -> B -> A asks A again. This relies on the provider contract that `prepare` is
idempotent: once a provider's preparation of a stage stands it returns `ready` without effects
(the worker provider always does; Salad's scaffold is kept per stage, never torn down, and only
re-reads the source verdict). The recorded placement says where, never whether that work finished.
One exception: the unavoidable budget refusal of section 3 records its `refused_budget` attempt
without calling `prepare`, because that attempt takes no hold, release or grant and runs nothing.

The choice is recorded in one transaction as the stage's current `provider_id` and a
`campaign.stage_placed` event carrying the candidates, the chosen provider, its hold, every
skipped candidate with its reason and detail, the candidates not evaluated, and the steering in
force. It binds that attempt number: a placed attempt does not move.

### 3. When nothing fits

Reasons are transient (`no_free_slot`, `project_limits`, `exposure_open`, `program_budget`,
`budget_in_flight`, meaning a hold now in flight may settle below its estimate) or permanent for
this attempt (`excluded_by_steering`, `not_pinned`, `unregistered`, `provider_invalid`,
`not_enrolled`, `untrusted`, `over_run_cap`, `over_campaign_budget`, `plan_mismatch`).

- If any skipped candidate has a transient reason, the stage waits: no attempt, no hold, no grant.
  A `campaign.stage_waiting_placement` event records the reasons once per distinct set.
- Otherwise the stage ends exactly as a single-provider stage would, never on another destination:
  if a candidate was refused only by the campaign budget, the first such candidate gets the
  ordinary recorded budget refusal (`refused_budget`, stop reason `budget_refused:<stage>`, the
  placement marked `budget_refusal`); if every remaining candidate was untrusted the stage fails
  `input_not_admitted`; if every one was unregistered or invalid it fails `provider_unavailable`;
  otherwise `no_eligible_provider`.

Placement never degrades silently: it never picks an unapproved provider, never lowers a hold
below a candidate's approved estimate, and never skips a check to make a stage run.

### 4. Steering

`tessera_steer_campaign_placement` (`campaign:cancel`, submitter-only like the other ADR 0055
writes; a human may use it too) calls `POST /campaigns/{id}/placement-steering` with a stage, an
optional `pin` and an `exclude` list. Only the stage's approved candidates may be named, at least
one must remain, and a stage with no alternates is refused. It is revision-guarded, idempotent and
recorded as `campaign.placement_steered`; a later call replaces the previous steering (no pin and
an empty exclude clears it, back to the approved list). It applies to the next placement; an
attempt already placed keeps its provider. Steering is not reduce-only over time: because the
latest call replaces the previous one, an agent can re-allow a candidate it excluded earlier. It
is always a filter over the approved list, so it can never reach a provider the approved spec does
not name and never widens authority beyond that approval. The spec-level counterpart is the
existing `forbid_providers` plus the order of the candidates.

### 5. Retries and slot release

A transient failure (ADR 0038 retry policy) re-runs placement for the next attempt, so a retry may
move to another eligible candidate. Once the stage's task plan binds an execution spec, a later
attempt may only move to a candidate whose execution spec is identical (`plan_mismatch` skips the
rest), so the planned and the executed run never differ. The task's budget uses the stage's own
`max_exposure_cents`, the cap every candidate fits under. A placed attempt waiting for its grant
holds its slot claim; ADR 0048/0053 slot release frees a provider's slot as before, and the next
placement sees it.

### 5a. One candidate list for placement and infrastructure reroute

ADR 0058's `retry.reroute_providers` and `alternate_providers` feed one ordered candidate list,
and every attempt, including an infrastructure retry, is placed by `decide` with the same checks
and records (`campaign.stage_placed`, the receipt's `placement`, the `placement` status field).
The candidate list is the stage's own provider, then `alternate_providers`, then
`reroute_providers`; the order an attempt tries them in is:

- a first attempt: the list in order;
- a retry after a task (scientific) failure: where the last attempt ran first (ADR 0058: it
  stays there), then the rest in order;
- a retry after an infrastructure failure: candidates the infrastructure has not failed yet
  first, in order, then those it has. With no alternates this is ADR 0058's order exactly: the
  first untried reroute target, returning to the stage's own provider when none is left.

ADR 0058's limits keep applying to a candidate named only in `reroute_providers`:

- it is used only for an infrastructure retry, or to stay where one moved the stage; for any
  other attempt it is skipped as `infra_retry_only` (never for a first attempt);
- each placement re-runs ADR 0058's `check_reroute` on it (registered, an unbilled worker-grant
  provider of the stage's runtime, the provider's stage check, trust narrowing and input
  admission) and skips it as `reroute_refused` when that fails, so a billable destination is
  reachable only as an `alternate_providers` entry, approved with its estimate;
- it runs with its own worker principal, without the stage's `provider_config`, at the stage's
  own hold, and infrastructure retries stay bounded by `retry.infra_retries`;
- steering may exclude it but never pin it, and must leave the stage's own provider or an
  alternate.

A stage with reroute targets but no `alternate_providers` is placed only when a reroute can
apply: its last attempt was an infrastructure failure, or ran on a reroute target. Its first
attempt and its other retries keep the single-provider path exactly as ADR 0058 had it (reserved
on the stage's own provider, waiting there for a slot at grant time, no placement record). When
a reroute does apply, the target is also checked for a free slot, the budgets and the project
limits before it is chosen; when it cannot run now the next candidate in the order is tried, and
the stage waits only if none can.

### 6. Visibility

`tessera_get_campaign` (`GET /campaigns/{id}`) gains a read-only `placement` list: per stage with
alternates, its candidates, current steering, every recorded placement with its skip reasons and,
while it waits, the latest reasons. Each attempt in the campaign receipt carries a `placement`
section with the same fields, and the attempt's and stage's `runtime` are the chosen candidate's.

## Consequences

- Independent CPU stages spread across registered, enrolled, trusted providers with free slots,
  inside the approved spec, with each choice explained on the receipt.
- A candidate Salad (or other billable) provider is approved like any stage provider and holds its
  own estimate; a Salad review pre-approval covers a Salad candidate that its approve-body entry
  names ([ADR 0066](0066-preapproval-covers-salad-alternates.md)); an alternate with no bound
  entry waits for a person (fail closed).
- A placed attempt does not move if its provider's slot is taken by another campaign before its
  grant; it waits there as a single-provider stage does.
- Trust narrowed after approval intersects with the approval-time setting; if that setting is not
  stored, only public data is admitted anywhere (unchanged).

## Tests

`tests/test_campaign_placement.py`: unchanged digest and no placement record for single-provider
specs, submit-time candidate validation, first-candidate order, two independent stages granted at
once on two providers, waiting when every slot is busy, an untrusted candidate skipped, all
candidates untrusted failing as `input_not_admitted`, an unregistered own provider skipped, a
budget-in-flight skip to a cheaper candidate, an unavoidable budget refusal, steering (pin,
exclude, refusals, submitter-only), the HTTP route and status field, and the MCP tool and status
description.

`tests/test_campaign_placement_salad.py` (fakes only; the Salad executor, guardian and human are
test doubles, no Salad call): a busy CPU host sends the stage to its Salad alternate, which is
scaffolded, reviewed, routed, charged and settled, with the placement on the receipt and in the
status field; an idle CPU host keeps the stage off Salad (never scaffolded or held); and Salad
excluded by steering makes the stage wait for the CPU host instead of degrading.

`tests/test_campaign_placement_prepare.py`: a transient failure moves the retry to a candidate
whose `prepare` reports progress, then waits for a person; nothing is held, reserved or granted
there until it is ready, and the retry then runs there.

`tests/test_campaign_placement_reroute.py`: an infrastructure retry placed on the reroute target
and recorded; a first attempt of a stage with only reroute targets keeping the single-provider
path (reserved on its own provider and waiting there for a slot, nothing placed); the candidate
order and `infra_retry_only` rule for a stage with both alternates and reroute targets; a reroute
target that stopped qualifying skipped as `reroute_refused`, returning to the stage's own
provider; steering refusing to pin a reroute target; and a stage moving A -> B -> A asking A's
`prepare` again (nothing is reserved while it waits). ADR 0058's
`tests/test_campaign_infra_retry.py` passes unchanged except its overlay test, which now calls the
unified `stage_on_candidate`.
