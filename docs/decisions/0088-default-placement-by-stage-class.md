# 0088 — Default placement by stage class

## Status

Implemented. No schema change beyond two optional v2 stage fields (`memory_bytes`, absent from the
serialized spec unless set, and `expected_duration_seconds`, likewise, so every existing digest is
unchanged). Extends
[ADR 0070](0070-salad-first-placement-preference.md) and the candidate placement of
[ADR 0060](0060-capacity-aware-stage-placement.md); composes with
[ADR 0066](0066-preapproval-covers-salad-alternates.md) Salad pre-approval,
[ADR 0067](0067-resource-limit-stops-are-infrastructure-retries.md) resource-limit retries and
[ADR 0076](0076-salad-cpu-stage-adapter-and-cpu-runtime.md) CPU stage adaptation. ADR 0047
(the approved spec is the contract) and ADR 0058 (reroute targets stay last) hold unchanged.

## Context

ADR 0070 let an operator write rules so heavy stages try a Salad candidate first. Without a rule,
placement still followed the spec's order, so heavy work stayed on the local provider unless every
operator and spec author arranged otherwise. The platform should send heavy work where it fits and
is cheapest by default, keep work that fetches from the internet off rented capacity, leave
every spec without such a signal exactly as its author wrote it, and record why. ADR 0070 also
had no memory criterion because a stage declared no memory; that gap is closed by declared
fields (memory and expected runtime).

## Decision

1. **Declared estimates.** A v2 stage may set `memory_bytes` (1 MiB to 2 TiB), a working-set
   estimate, and `expected_duration_seconds` (1 to 604800, and not above its
   `max_duration_seconds`), a runtime estimate. Both are read only by placement. Neither is a
   request or a limit; the provider's own limits apply, and `max_duration_seconds` stays a cap
   that is never a placement signal. Both are part of the approved spec and absent from the
   serialized spec unless set.
2. **Stage class.** When no operator rule matches, a stage that lists `alternate_providers` is
   classed from what its approved spec declares, first match wins: `egress_bound` (declares
   `egress`: it fetches from the internet), `gpu` (placement tag `gpu`), `heavy` (declared
   `memory_bytes` of at least 1.5 GiB (signal `declared_memory`), `expected_duration_seconds` of
   at least 1800 (signal `long_expected_runtime`), or a `heavy` or `training` tag), else `light`
   (no signal).
3. **Order.** Placement names no vendor. Each provider may report, per stage candidate, a
   `PlacementCapability(tier: local|remote, hardware: cpu|gpu)` through an optional
   `placement_capability(stage)` method: a worker-grant provider is local CPU by its descriptor,
   the Salad provider reports remote and CPU or GPU from the stage's own Salad block, and a
   provider with no report is `other` and stays where the spec put it. Candidate kinds on the
   receipt are `remote_cpu`, `remote_gpu`, `local`, `other` and `unknown`. `heavy` tries a remote
   CPU candidate first; `gpu` a remote GPU candidate first; `egress_bound` moves remote candidates
   behind the others (such a stage cannot list a rented-capacity alternate at submit anyway).
   `light` is not reordered: it keeps the order its spec wrote (put the local provider first in
   the spec to keep light stages local). The rest keeps the spec's order, reroute-only targets stay
   last (ADR 0058), a stage without alternates is never reordered, and steering (ADR 0060
   section 4) still filters or pins the result.
4. **Checks and fallback.** ADR 0060's checks run on every candidate in that order. A remote
   candidate that is unregistered, not enrolled, untrusted, over the per-run cap or over the budget
   is skipped with its reason and the next candidate (the local provider) is tried. That is the only
   fallback.
5. **Operator rules win; operator switch.** A matching ADR 0070 rule applies instead, and its
   record is unchanged. `placement_preferences` may now be just `{"default_policy": false}` (policy
   off, placement as before), and may set `heavy_memory_bytes` and `long_runtime_seconds` (default
   1800) to change the thresholds. `rules` is optional.
6. **Audit.** The `campaign.stage_placed` event and the receipt attempt's `placement.preference`
   carry `source: default_policy`, `class`, `signals`, `kinds`, `spec_order`, `order`, `reordered`,
   `chosen` and `fell_back` (each skipped candidate with its reason), plus a note when no matching
   candidate is listed. As in ADR 0070, `order` precedes the ADR 0060 and 0067 retry rules; the
   order actually tried is the event's `candidates`.
7. **Inspection.** `GET /campaigns/{id}` (`tessera_get_campaign`) `placement_preference` gains a
   `default_policy` block: enabled, the thresholds, and per stage the class, signals, order and
   `applies`.

## What it does not do

- It never adds a destination, raises a cap, skips a check, changes a hold or touches the stored
  spec or its digest. It only reorders destinations the human already approved by digest.
- A rented-capacity candidate (Salad) still needs its own review or an ADR 0066 pre-approval;
  being tried first approves nothing.
- It does not reorder a stage with no heavy, GPU or egress signal, nor a stage without alternates.
- It cannot route to a destination the spec does not list. The spec author (or the control-plane
  agent building specs) must list the Salad candidate as an alternate; ADR 0076's `adapt` form
  makes that cheap for a local-docker CPU stage.
- It does not measure memory or runtime; it reads declarations. A wrong declaration changes the
  order, not the guarantees, and the receipt shows the signals used. A stage that declares no
  estimate is not heavy by runtime: `max_duration_seconds` is a cap, not a signal.
- Known limit: the heavy and GPU tag lists (`heavy`, `training`, `gpu`) are fixed generic labels,
  not operator-configurable in this ADR; only the two thresholds are.
- A stage with egress cannot list a Salad alternate (refused at submit); the regression test
  `test_a_stage_that_declares_egress_and_lists_a_salad_alternate_is_refused_at_submit` pins that.
- It does not fake or relabel anything: a skipped candidate is recorded with its reason, and a
  provider-disabled path stays provider-disabled.

## Consequences

- Heavy and GPU stages that list a remote alternate go there first with no operator rule, and fall
  back to the local provider with the reason on the receipt. Egress-bound stages keep remote
  candidates behind the others. Light stages (no signal) keep the spec author's order.
- The default policy can change which candidate a stage with a heavy, GPU or egress signal tries
  first versus the order its spec wrote; the receipt records it (`reordered`). Specs without any
  such signal, and their digests, are untouched. Operators who want the previous behaviour set
  `default_policy` to false.
- A new compute vendor needs only to implement `placement_capability(stage)` in its registration;
  placement does not change. This is the extension point for pluggable compute.
- ADR 0070's "Why no memory criterion" is superseded for the default policy by `memory_bytes` and
  `expected_duration_seconds`; operator rules still have no memory criterion.
- The fallback evidence is partly synthetic. Of the four fallback tests in
  `tests/test_campaign_placement_default.py`, only the skipped-candidate test reaches its fallback
  without patching. The over-run-cap, no-longer-trusted and over-campaign-budget tests force the
  condition by monkeypatching (`stage_on_candidate`, `provider_admits`), so they show the
  fallback and its receipt reason, not that the real cap, trust or budget check fires.

A candidate the provider registry no longer knows cannot be ranked (kind `unknown`): the heavy
policy does not move it first, the stage runs on the next listed candidate, and the receipt's
`preference.unavailable` names it, so the reason a registered-then-dropped destination was not
tried is on the record.
