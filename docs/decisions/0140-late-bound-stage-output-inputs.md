# ADR 0140: late-bound stage-output inputs (bind an upstream output's digest when the stage starts)

## Status

Accepted and implemented in the runner. No schema change. Builds on
[ADR 0039](0039-data-class-guard.md) (stage outputs are `private_derived`),
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) (provider trust and placement of data),
[ADR 0047](0047-agent-routed-data-destinations.md) (registered destinations only) and
[ADR 0113](0113-license-placement-guard.md) (licence labels).

## Context

A stage input of kind `stage` names declared outputs of an earlier stage of the same campaign. Its
content digest does not exist when the spec is submitted and approved, so a provider that needs input
digests at submit (a remote provider that fetches a pinned snapshot) cannot accept such a stage. The
runner already composes stage outputs into a snapshot for a local worker, but nothing generic pinned the
exact upstream bytes, recorded which upstream output a dependent stage read, or re-checked routing
against the resolved artifact rather than the spec's claim.

## Decision

1. **Bind at stage start.** When a stage with `stage` inputs starts its input preparation, the runner
   resolves every referenced output (producing stage plus declared output name) in one transaction,
   reading only rows of the campaign's own org and project:
   - the producing stage must be `succeeded`; its last succeeded attempt must be settled
     (`settled_cents` and `settlement_basis` recorded) and must have collected a custody manifest;
   - the output must be in that attempt's output index (`campaign_stage_io`) and in the manifest with
     the same sha256 and size, and the attempt's recorded manifest digest must match the manifest;
   - a stage that already bound (a later tick or a retry) must resolve to the same digest.
2. **Closed refusal codes.** Anything else refuses with exactly one of `campaign_not_found`,
   `upstream_missing`, `upstream_not_succeeded`, `upstream_not_settled`, `output_missing`,
   `digest_mismatch`, `data_class_refused` or `license_refused` (`LateBindRefused.code`). The stage fails
   with stop reason `stage_failed:<stage>:late_bind_refused:<code>`, any unstarted reserved attempt is
   cancelled (its hold is released), and the event `campaign.stage_input_bind_refused` records the code,
   input, provider and worker principal. Nothing is released to a worker.
3. **Routing re-checked after resolution.** The stage's provider instance must admit each resolved
   output's recorded data class and the licence labels it inherits through lineage (the upstream stage's
   input labels), under the same provider trust setting and approval digest that the reservation check
   uses. A provider that the operator does not trust with that class or label is refused with
   `data_class_refused` or `license_refused`. This is how the operator's rule that certain
   non-commercial or restricted rows never go to a rented provider holds for derived data too. No
   dataset or vendor is named in code: the rule is the operator's trust file.
   Detail of the refusal records: `data_class_refused` names the first offending input (the
   event's `input`); `license_refused` is decided on the union of the labels of all resolved
   inputs, so it names no single input (`input` is null) and its reason lists every label the
   provider may not receive. The Salad adapter's own late-bound codes
   (`late_bound_input_*`, `late_bound_resolver_unavailable`, event
   `campaign.salad_late_bind_refused`) are not `LateBindRefused` codes and carry only the
   `code`: no `input` and no source stage.
4. **Pinned and recorded.** The resolved tuple (`ResolvedStageInput`: input name, source stage, output
   name, digest, bytes, data class, licence labels, path, manifest id) is recorded with the event
   `campaign.stage_inputs_bound` under the key `late_bound_inputs`, in the same transaction as the
   resolution. Snapshot composition uses exactly those digests and refuses if the manifest listing
   differs. The campaign receipt adds `late_bound_inputs` to the dependent stage's `input` section (the
   lineage edge); stages without stage-output inputs keep their receipt bytes.
5. **One provider interface.** Providers read the binding through `RunnerServices.bound_inputs(ctx,
   key)` (or `late_bound_inputs.bound_inputs` on a connection); they receive resolved digests only,
   never a reference to resolve. The runner core has no vendor branch. A remote provider fetches the
   bytes only from registered destinations (ADR 0047) and verifies each digest; enabling that for a
   given provider adapter is that adapter's change.

## Consequences

- A remote provider adapter can drop its submit-time refusal of stage-output inputs and size the input
  from the bound tuple when the stage starts.
- A tightened trust file or a label added after approval refuses at bind time instead of releasing.
- Not done here: provider adapters' own acceptance and staging of late-bound inputs, and a bound input
  pinned across campaigns (use an `artifact` or `receipt_output` input for that).

## Amendment (2026-11-23): receipt fields

Each `late_bound_inputs` entry on the dependent stage's receipt also carries `bound_at`, the
timestamp of the `campaign.stage_inputs_bound` event (written in the binding transaction; no extra
clock read). The launch inputs a provider receives and the event payload are unchanged. When
binding is refused, the receipt's `late_bound_inputs` holds one entry `{"status":
"input_not_pinned", "code": <closed refusal code>, "input": <name>, "source_stage": <stage>}` taken
from `campaign.stage_input_bind_refused` (which now also records `source_stage`); the free-text
reason stays off the receipt. A stage with both a `campaign.stage_inputs_bound` and a
`campaign.stage_input_bind_refused` event shows both: the bound entries first, then the
`input_not_pinned` entry, so a consumer can tell the final failure came from a refused bind.
(Retries on the same provider reuse the pinned binding.) The Salad driver's own refusal also
records the runner's `campaign.stage_input_bind_refused` (same payload shape: closed code, input,
source stage, provider, worker; no free-text reason) in the same transaction as its
`campaign.salad_late_bind_refused` event, so the receipt code stays provider-agnostic, one event is
the source of truth, and a refused Salad alternate shows `input_not_pinned` like any other. Additive:
a new event row only, no schema change. A `license_refused` refusal names the first resolved input carrying a
label the provider may not receive. Stages with no stage-output input keep their receipt bytes,
and a stage that bound without a refusal keeps its entries unchanged.

## Amendment (2026-11-23): the heavy-stage Salad candidate covers chained stages

[ADR 0141](0141-heavy-stage-remote-candidate.md)'s submit rule now adds the operator's Salad
candidate to a heavy stage whose non-pinned inputs are all `stage` inputs of the same campaign
(previously `input_not_pinned`). The candidate is derived exactly as an explicit adapter request
is: the contract declares the late-bind key with a cap from the upstream outputs' declared
`max_bytes`, and decisions 1 to 4 above bind, re-check and pin the digest at stage start. Nothing
about binding or refusal changes; a refused bind fails the stage before anything reaches Salad.
