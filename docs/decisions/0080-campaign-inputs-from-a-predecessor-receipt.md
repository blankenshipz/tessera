# 0080 — A campaign input can name a value on a predecessor's receipt

## Status

Implemented (no schema change). Builds on the `artifact` input and custody checks of the
campaign runner ([campaigns](../campaigns.md)); the provider and data-class rules of
[ADR 0047](0047-agent-routed-data-destinations.md) are unchanged.

## Context

A later campaign often needs a value an earlier one produced and recorded on its terminal
receipt, such as a frozen hyperparameter file's hash or an archive digest. An `artifact`
input can already pin that output, but the author had to copy the source campaign id and
sha256 by hand, which invites a stale or mistyped pin and leaves no record of where the value
came from.

## Decision

1. **A reference kind.** A v2 stage input of `kind: receipt_output` has `name`, `stage`,
   `output` and exactly one of `campaign_id` or `predecessor`. The submit body gains optional
   `predecessors: {name: campaign_id}` (at most 16) so a spec can say `predecessor: tuning`
   instead of an id.
2. **Resolved at submit, in the transaction.** For each reference Tessera requires the source
   campaign to be in the same organization and project and `succeeded`, with a terminal
   receipt that lists a succeeded attempt output of that stage and name (the last succeeded
   attempt wins). It replaces the reference with a pinned `artifact` input: campaign id, stage,
   output, sha256 and `resolved_from {predecessor?, source_receipt_sha256}`.
3. **Resolution precedes the digest.** This happens before the spec is digested and stored, so
   approval binds the exact bytes and lineage a person reviews. The stored and approved spec
   never contains `receipt_output`; normalization refuses an unresolved one. A spec without
   references keeps its digest.
4. **Existing verification still applies.** The resolved `artifact` input goes through the
   usual artifact checks at submit and again at stage start: custody released,
   `retain_quarantined`, same project, and data class never relabelled.
5. **Refusals are 409 Conflict** naming the stage, the input and the reason: unknown
   predecessor name, source campaign not succeeded or not in this project, no terminal
   receipt, output not listed, or `predecessors` given but unused.
6. **The consumer's receipt records it.** The terminal receipt of the consuming campaign gets a
   `chained_inputs` list (`stage_key`, `input`, `campaign_id`, `stage`, `output`, `sha256`,
   `predecessor`, `source_receipt_sha256`), absent when there are none.
7. **Lineage is server-derived only.** `resolved_from` is set by submit alone. A submitted
   `resolved_from` on any `artifact` input is refused with a 409 ("resolved_from is set by submit
   only") on submit, validate and standing-approval create. As defence in depth,
   `resolve_artifact` also refuses an input whose `source_receipt_sha256` differs from the
   source campaign's recorded receipt digest.
8. **Validate resolves references.** The dry-run validate resolves `receipt_output` references
   read-only with the same resolver (it accepts `predecessors`) and returns the pinned spec's
   digest, or the refusal. A standing approval refuses `receipt_output` explicitly (pin an
   `artifact` input instead).
9. **Idempotency.** `predecessors` is omitted from a request dump when empty, so request
   digests from before this change still match on retry.
10. **MCP.** `tessera_submit_campaign(spec, idempotency_key, predecessors=None)` and
   `tessera_validate_campaign_spec(spec, predecessors=None)` pass the mapping through.

## Consequences

- A session chains campaigns without copying ids or hashes, and a reviewer sees the pinned
  bytes and the receipt digest they came from. A later change to the source cannot alter an
  approved spec.
- No new credential or scope: submitting still needs `campaign:submit`, approval stays human,
  and ADR 0047's rule that data never goes to the public internet is untouched.
- Limits: a predecessor must already have `succeeded` at submit. Resolving a reference at
  stage start against a still-running predecessor is not implemented, so such a chain still
  needs two submits.
