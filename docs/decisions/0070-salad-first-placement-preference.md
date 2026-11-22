# 0070 — Salad-first placement: operator preference by resource class

## Status

Implemented. No schema change. Numbered 0070 because 0069 is the campaign rerun decision.
Extends the candidate placement of
[ADR 0060](0060-capacity-aware-stage-placement.md); composes with
[ADR 0058](0058-infrastructure-retry-of-stages.md) reroute targets,
[ADR 0066](0066-preapproval-covers-salad-alternates.md) Salad pre-approval and
[ADR 0067](0067-resource-limit-stops-are-infrastructure-retries.md) resource-limit retries.

## Context

ADR 0060 places a stage on the first eligible candidate in the order its spec writes. Sending
heavy stages to one provider (a Salad CPU or GPU profile) and keeping the project host for the
rest therefore needed every spec author to order each heavy stage's candidates by hand. The
operator, who knows which providers suit long or large work, had no way to say so once.

## Decision

1. **Operator rules.** The `--campaign-providers` file may carry `placement_preferences`:
   `{"rules": [{"name", "when", "prefer"}]}`, at most 8 rules. `when` holds at least one of
   `min_duration_seconds` (1 to 86400; the stage's `max_duration_seconds` is at least it),
   `min_scratch_bytes` (1 MiB to 64 GiB; the stage's `scratch_bytes` is at least it) and
   `any_tags` (1 to 8 labels; the stage declares one of them). A stage matches when **any**
   stated criterion holds. `prefer` is 1 to 5 distinct provider ids, each registered when the
   file is loaded. The models are closed and strict; a malformed section refuses to start the
   service before any provider is registered. Absent, placement is exactly as before.
2. **Stage tags.** A v2 stage may declare `placement_tags` (1 to 8 distinct lowercase labels such
   as `heavy`). They are part of the approved spec (absent from the serialized spec unless set,
   so every existing digest is unchanged), never name a destination and change no check.
3. **Reorder only.** For a stage with `alternate_providers`, the first matching rule moves the
   preferred providers that the stage lists (its own provider and alternates) to the front, in
   the rule's order; the other candidates keep the spec's order; `retry.reroute_providers`-only
   targets stay last under ADR 0058's limits. A preferred provider the stage does not list is
   ignored. Placement then runs ADR 0060 unchanged on that order: every candidate still passes
   registration, enrollment, trust, the per-run cap, the campaign and program budgets, open
   exposure, project limits and the slot count, and the first that passes is chosen, so a
   preferred provider that fails a check falls back to the next in the preferred order. ADR
   0060's retry order (stay after a task failure, untried first after an infrastructure failure)
   and ADR 0067's `resource_limit_last_attempt` skip apply on top of the preferred order. A stage
   without alternates is never routed through placement by a rule.
4. **Audit.** When a rule matched, the `campaign.stage_placed` event and the receipt attempt's
   `placement` carry `preference`: the rule's name, the criteria matched, its `prefer` list, the
   spec order, the order used and whether it differed. The approver approved the set of
   destinations by digest; this record shows why an attempt tried them in another order. A
   placement without a matching rule records no `preference`, as before. The recorded `order` is the
   preference's order before the ADR 0060 and 0067 retry rules; the order an attempt actually
   tried is the event's `candidates`.

5. **Inspection.** `GET /campaigns/{id}` (`tessera_get_campaign`) gains a read-only
   `placement_preference`: whether rules are configured, the rules, and for each stage a rule
   matches the rule, the criteria met, the preferred providers the stage lists, and `applies`
   (true only when the rule changes the spec's order: false for a stage without
   `alternate_providers`, one that lists no preferred provider, or one whose preferred
   provider is already first).
6. **One Salad executor serves several profiles.** A Salad CPU candidate (ADR 0062) and a GPU
   one use different operator profiles, but the executor config named exactly one `profile` and
   refused work for any other (`refused_profile`), so with the executor configured for the GPU
   profile a stage placed on the CPU profile was scaffolded, held and granted and then never
   leased, until its grant expired. The config now takes `profile` or `profiles` (1 to 8
   distinct names, exactly one of the two). Each attempt is served with the profile its stage
   names, never another: lease, the budget bound, the re-derived selection, the live config and
   custody import all use that profile, and work naming an unlisted profile is still refused.
   Every listed profile must belong to the executor's worker principal and the config's custody
   host. A one-profile config behaves as before.

## Why no memory criterion

(For the built-in default policy this is superseded by the declared `memory_bytes` stage field, [ADR 0088](0088-default-placement-by-stage-class.md).)

A stage has no per-stage memory request: a local-docker container's memory is the poller's
operator `memory_bytes`, and a Salad candidate's is its profile's `memory_mb`. Both are properties
of the candidate, not the stage, so a rule on "the stage needs 1.5 GiB" has nothing to read. The
rule instead uses what a stage does declare: its digest-pinned time budget, its scratch request
and an explicit tag the spec author sets for memory-heavy work (`heavy`). A per-stage memory
request is its own decision if it is needed.

## Consequences

- An operator states once that long, large or `heavy`-tagged stages go first to a preferred
  provider (for example a Salad CPU profile listed as an alternate) and fall back to the project
  host; light stages keep the spec's order.
- It never adds a destination, raises a cap, changes a hold, skips a check, or touches a stored
  spec or its digest. A Salad candidate still needs its own review or an ADR 0066 pre-approval
  naming it; a preference that puts it first does not approve it.
- The rules are read at start, like the rest of the provider file. A changed file changes the
  order of later placements only; an attempt already placed keeps its provider.
- Steering (ADR 0060 section 4) still filters the resulting order; a pin wins.

7. **Salad refuses `scratch_bytes`.** Salad grants no scratch request (ADR 0059): its disk is
   the profile's. A v2 stage or candidate on Salad that sets `scratch_bytes` was accepted and
   the request silently not honoured; it is now refused at submit and approval, as
   `docs/campaigns.md` already stated. A `min_scratch_bytes` rule therefore never sends a stage
   to Salad; use a duration or tag rule for heavy stages there.

## Not changed here

Running the executor is deployment, not code: a control host with no executor process (or one
started for a different profile) leaves Salad attempts at `awaiting_lease` until their grant
expires. The executor reads the control store directly, so it runs on the host that holds the
control state, with the worker credential, guardian and custody settings of
`deploy/systemd`. A Salad candidate still needs its review or an ADR 0066 pre-approval, and
spend stays bounded by the profile's cent ceilings, the per-run cap and the campaign budget.

## Tests

`tests/test_campaign_placement_preference.py`: no rule keeps the spec order and records no
preference; tags omitted from the spec unless set and digests kept; a stage heavy by duration or
by tag placed on the preferred candidate with the rule on the event and receipt and the spec
digest unchanged; a light stage keeping the spec order; the scratch criterion and first-matching
rule; fallback in preference order when the preferred provider has no free slot, is untrusted,
unregistered, not enrolled, or over the remaining budget; a preferred provider the stage does not
list never used; a stage without alternates not placed; reroute-only targets kept last; a
resource-limit stop on the preferred provider retried on the next; and strict config refusals
(through `configure_providers` too, before any provider is registered).

`tests/test_campaign_placement_preference.py` also covers a memory kill (137) on the fallback
host retried on the preferred provider rather than on the host, and the inspection view.
`tests/test_campaign_salad_executor.py`: one executor runs a CPU-profile attempt beside a GPU
profile with that profile's policy and custody import, a GPU selection under the CPU stage's
profile refused, an unlisted profile refused, and the `profile`/`profiles` config rules and
loading.
