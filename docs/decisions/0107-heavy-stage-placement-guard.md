# 0107 — Heavy-stage placement guard

## Status

Implemented. The only schema change is the stuck findings code check (Decision 8, schema 79). Otherwise: one
optional key in `placement_preferences` (`heavy_local_fallback`, absent means `warn`), one new field on the placement record
(`placement.heavy_guard`), one new event and two new stuck-detector codes. The stored spec and every
digest are unchanged. Extends
[ADR 0088](0088-default-placement-by-stage-class.md) (stage class and its signals),
[ADR 0070](0070-salad-first-placement-preference.md) (the operator's placement preference) and
[ADR 0091](0091-campaign-stuck-detector.md) (the stuck detector). ADR 0060 (capacity-aware
placement) and ADR 0058 (reroute targets stay last) hold unchanged.

## Context

ADR 0088 sends heavy work to a remote candidate first, and falls back to the local or self-hosted
provider when the remote one is skipped. That fallback is correct and was already on the receipt,
but it was quiet: a heavy stage could land on a small local machine because a credential lapsed or
no profile fit, and nobody was told until the stage ran slowly or failed. When a Salad profile has
been registered for the project, the operator has said that heavy work belongs there, so running it
locally is a deviation worth surfacing, and some operators would rather the stage wait than run
locally at all. The platform should say so on the record, show it in status and the stuck detector,
and let the operator choose, without changing anything for stages or projects that the guard does
not concern.

## Decision

1. **Heavy.** A stage is heavy for this guard when it has any of these signals:
   - ADR 0088's `declared_memory`, `long_expected_runtime`, `tag:heavy` and `tag:training`,
     computed by the same stage-class code and operator thresholds;
   - `tag:pretraining` (placement tag `pretraining`);
   - `large_inputs`: the sum of the byte sizes of the stage's pinned inputs that the store knows for
     the campaign's organization and project (snapshot manifest sizes and artifact `size_bytes`)
     is at least the threshold. Inputs of unknown size are ignored and stage-to-stage inputs are
     not counted, so a stage is never heavy by guesswork;
   - `large_outputs`: the stage's summed declared output `max_bytes` limits are at least the
     threshold. This is a cap the stage may not exceed, not an estimate of what it will write.

   The threshold is the operator-overridable `heavy_memory_bytes` (default 1.5 GiB). A GPU tag
   alone is not heavy, and a stage that declares egress (egress-bound) is never guarded. The guard
   only adds signals: ADR 0088's class names and ordering are unchanged.
2. **When it applies.** Only when placement chose a candidate of kind `local` for a heavy stage,
   after that candidate passed every other check, and the `salad` provider is registered with at
   least one Salad campaign profile usable by that organization and project (a metadata lookup; no
   provider call). A stage that is not heavy, a project with no usable Salad profile, or a stage
   placed on a remote candidate is not touched, and nothing is recorded.
3. **Reasons.** The guard names why the stage is local, listed in this order (several may hold):
   `credential_unavailable` (a remote candidate was skipped for a credential or availability
   reason, for example `credential_expired`, `credential_unusable`, `safety_service_down`),
   `no_fitting_profile` (the Salad candidate was skipped `not_enrolled` or `provider_invalid`),
   `remote_skipped` (a remote candidate was skipped for any other reason; `detail` lists each
   `{provider_id, reason}`), `operator_override` (an ADR 0070 operator rule put local first, the
   default policy is off, or a steering pin or exclusion removed the remote candidate),
   `not_listed` (the spec lists no remote candidate although a profile is registered) and
   `remote_not_tried` (a remote candidate is listed after the chosen local one and no operator
   action explains it).
4. **Warning and recording.** In the default `warn` mode placement is exactly what it was, plus a
   record. Once per attempt, in the same transaction as the placement, the runner writes the event
   `campaign.heavy_stage_local_fallback` with payload `stage_key`, `attempt`, `provider_id`, `mode`,
   `signals`, `reasons` and `detail` (`[{provider_id, reason}]`; ids, codes and digests only), and
   `placement.heavy_guard` (`{warning: true, signals, reasons, detail, mode}`) on the
   `campaign.stage_placed` payload and on the receipt attempt. A warning therefore cannot exist
   without the placement or the reverse. A stage without alternates is also guarded before
   reservation (reason `not_listed`); its receipt `placement` is `{heavy_guard}` rebuilt from the
   event.
5. **Operator setting.** `placement_preferences.heavy_local_fallback` is `"warn"` (default, also
   when absent) or `"refuse"`, validated strictly with the rest of `placement_preferences`. In
   `refuse` mode the local candidate for a stage the guard applies to is skipped with the transient
   reason `heavy_local_refused` (detail: the reason codes). With no other candidate the stage waits
   (`campaign.stage_waiting_placement`, whose payload carries
   `heavy_guard {warning: false, refused: true, ...}`) and is placed again when a remote candidate
   qualifies; nothing is cancelled and nothing fails. A single-provider stage in `refuse` mode
   waits until the operator switches to `warn`. The setting is read only by the guard, so it cannot
   admit a candidate the spec does not list or skip a check.
6. **Status.** `GET /campaigns/{id}` (`tessera_get_campaign`) shows per stage a `heavy_guard`
   readout, `{mode, reasons, signals, provider_id?, waiting, recorded_at}`, from the latest warning
   or refusal event. It is read-only and present only when such an event exists.
7. **Stuck detector.** Two codes are added to ADR 0091, with suggested actions only (the detector
   still never acts):
   - `heavy_stage_on_local` (suggested `check_salad_credential_or_profile`): a live attempt
     (reserved, granted, admitted or running) with a warn event for the same attempt. Evidence:
     `attempt_id`, `attempt`, `attempt_status`, `provider_id`, `reasons`, `signals`, `age_seconds`.
   - `heavy_stage_refused_local` (suggested `restore_remote_capacity_or_allow_local`): a stage
     waiting on a refusal for longer than the project's `lease_grace_seconds` (default 300).
     Evidence: `stage_key`, `attempt`, `reasons`, `waiting_seconds`, `grace_seconds`. It clears when
     the stage is placed.
8. **Schema.** The stuck findings table is rebuilt by a schema migration that widens the findings
   code check to accept the two new codes (schema 79, after the conclusion decision migration).

## What it does not do

- It never adds a destination, raises a cap, skips a check, approves anything or changes a hold. In
  `warn` mode placement is identical to before; `refuse` only removes a local candidate for a stage
  the guard applies to, and the stage waits.
- It does not measure memory, size or runtime; it reads declarations and the byte sizes the store
  already holds. A wrong declaration changes the warning, not the guarantees.
- It does not move the stage to Salad. A remote candidate still needs to be listed by the approved
  spec, enrolled, within caps and budget, and reviewed or pre-approved (ADR 0066).
- It does not apply to light stages, to stages with no heavy signal, or to projects without a
  usable Salad profile.
- It does not fake or relabel anything: the receipt records what ran where, and the warning does
  not change that.

## Consequences

- A heavy stage that ends up local while Salad is registered is visible on its receipt, in campaign
  status and in the stuck detector, with a reason, instead of silently.
- An operator who prefers waiting to running heavy work locally sets `heavy_local_fallback` to
  `refuse`. The cost is that a stage can wait indefinitely if remote capacity never returns; the
  stuck detector then reports it with a suggested next step.
- Default behaviour changes only by the added record and event for heavy local placements in
  projects with a Salad profile. Everything else, and every digest, is untouched.

## Tests

Focused tests only, no live calls.

- `tests/test_heavy_stage_guard.py`: the heavy definition and signals
  (`test_a_light_stage_is_not_heavy`, `test_heavy_keeps_the_adr_0088_signals_and_adds_pretraining`,
  `test_a_gpu_tag_alone_is_not_a_heavy_signal`, `test_large_outputs_from_the_declared_output_limits`,
  `test_large_inputs_sum_the_sizes_the_store_records_for_this_project`), strict setting validation
  (`test_the_operator_setting_is_validated_and_defaults_to_warn`), the reasons
  (`test_salad_not_enrolled_falls_back_to_local_with_a_warning`, `test_a_credential_skip_is_recognised`,
  `test_an_operator_rule_keeping_heavy_work_local_is_an_override`,
  `test_a_heavy_stage_without_a_remote_candidate_is_not_listed`), refuse mode
  (`test_refuse_mode_waits_then_resumes_on_salad`, `test_refuse_mode_waits_for_a_one_provider_heavy_stage`)
  and unchanged placement (`test_a_light_stage_placement_is_unchanged`,
  `test_without_a_salad_profile_a_heavy_fallback_records_nothing_new`).
- `tests/test_heavy_guard_surfaces.py`: the status readout
  (`test_status_shows_the_latest_warning`,
  `test_status_shows_a_refusal_while_waiting_and_not_once_placed`,
  `test_status_has_nothing_for_a_stage_without_events`) and both stuck codes
  (`test_a_running_heavy_attempt_with_a_warning_is_stuck_on_local`,
  `test_a_refused_waiting_stage_is_stuck_only_after_the_grace`,
  `test_a_refused_stage_that_was_placed_since_is_not_stuck`).

## Amendment (2026-11-23)

Owner rule: heavy stages are placed on Salad first; local is only a fallback, recorded on the
receipt with an accurate reason. This amends Decisions 1-3 and 5; nothing else changes.

- **One definition of heavy.** Placement ordering (ADR 0088's default policy) now receives the
  guard's signals (`campaign_heavy_guard.heavy`, computed once in `campaign_placement.decide`), so a
  `[local, salad]` stage that is heavy only by `tag:pretraining`, `large_inputs` or `large_outputs`
  is ordered Salad first and its preference record is `class: heavy` with those signals. Egress-bound
  and gpu classes are unchanged, and light stages (no signal) keep the spec's order. The
  read-only inspection shows the spec-derived signals only (`large_inputs` needs the store).
- **A gpu requirement is a guard signal** (Decision 1 no longer says a GPU tag alone is not heavy for
  the guard): `tag:gpu*` is a signal, so a gpu-tagged stage placed locally while a Salad profile
  exists records the warning. ADR 0088's class names are unchanged.
- **`credential_unavailable`** (the skip placement emits when the provider's own `check_stage`
  reports a credential state) is now a credential reason, not `remote_skipped`.
- **`remote_failed_infra`** (new, after `no_fitting_profile`): a remote candidate's earlier attempt
  failed on infrastructure, so ADR 0060 moved it last and the retry went local. `detail` carries
  `{provider_id, reason: <failure class>}`; the reason no longer reads `remote_not_tried` or
  `operator_override`.
- **`no_remote_profile`** (new, listed first and alone): a heavy stage placed local while the project
  has no usable Salad profile. Recorded with warn semantics in both modes (placement unchanged, once
  per attempt, same event and receipt block, `mode: warn`). In `refuse` mode the guard downgrades to
  warn without a profile: it records but never waits, since capacity that cannot exist cannot arrive. The stuck detector does not report
  `heavy_stage_on_local` for this reason alone.
- **Not changed:** the single-provider warning event is still written in its own transaction before
  reservation: that path has no `campaign.stage_placed` transaction, and the attempt row is created
  in `_reserve_hold`. The event is keyed by `(stage_key, attempt)` and is written once, so a crash
  between the two repeats nothing; Decision 4's "same transaction" holds for placed stages only.

Tests: `tests/test_heavy_stage_placement_reasons.py`. `tests/test_heavy_stage_guard.py` changed two
tests for the contract (gpu is a guard signal; no profile records `no_remote_profile`).
