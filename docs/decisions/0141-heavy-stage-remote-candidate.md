# 0141 — Heavy stages get the operator's remote candidate at submit

## Status

Implemented. No schema change. Amends [ADR 0107](0107-heavy-stage-placement-guard.md) ("it does
not move the stage to Salad") and [ADR 0070](0070-salad-first-placement-preference.md)
("it never adds a destination"): a new operator setting does add one, at submit, never at
placement. Builds on [ADR 0060](0060-capacity-aware-stage-placement.md) (candidates are approved
with the spec), [ADR 0066](0066-preapproval-covers-salad-alternates.md) and
[ADR 0076](0076-salad-cpu-stage-adapter-and-cpu-runtime.md) (the stage adapter).

## Context

The owner rule is that heavy experiment stages run on Salad first and locally only as a recorded
fallback, inside spend caps. Placement only reorders the candidates the approved spec lists, so a
heavy stage whose spec lists only `local-docker` never reached Salad: in `warn` mode it ran
locally with reason `not_listed`. Adding a provider at placement time would run a destination
nobody approved, which ADR 0060 and 0066 forbid.

## Decision

1. **Setting.** `placement_preferences.heavy_remote_candidate: {provider, max_exposure_cents,
   profile?}`, strict: `provider` is `salad` (the only provider whose adapter derives a candidate
   from a local CPU stage), `max_exposure_cents` 1..5000, `profile` optional. Absent: nothing
   changes. It takes effect whenever the operator file names it.
2. **When.** At submit (`POST /projects/{id}/campaigns`, including re-runs), when a person creates
   an approved submission (`kind=submit`), and in the validate dry run, after the adapter and
   receipt references are resolved and before the spec digest is computed. The candidate is
   therefore part of the digest a person, an approved submission or a delegation approves, and it
   passes every submit, approval and placement check of the provider; a Salad review or an
   ADR 0066 pre-approval is still needed to run it.
3. **Which stages.** v2 stages that are heavy by ADR 0107's signals (threshold
   `heavy_memory_bytes`) and list no remote candidate. Light stages, stages that already list a
   remote candidate or the provider, and v1 specs (no alternates) are left byte-identical.
4. **What is added.** The adapter's candidate on the first adapter-enabled CPU profile of the
   provider registered for the campaign's org and project (or the named `profile`), inserted as
   the **first alternate**; heavy-first default ordering (ADR 0088, with the guard's signals) then
   tries it before the stage's own local provider. Its `max_exposure_cents` is the full-duration
   estimate at the profile rate.
5. **Caps.** The estimate must fit the setting, the stage's `max_exposure_cents`, the campaign's
   `budget_cap_cents` and `program_budget.check_admission`. Otherwise no candidate is added and the
   reason is recorded; no cap is raised and the submission is not refused because of this rule.
6. **Record.** Each such stage carries `heavy_remote_candidate` in the stored spec (in the digest)
   and on its receipt stage entry: `added: true` with reason `operator_heavy_remote_candidate`,
   provider, profile, cap and signals, or `added: false` with one reason: `egress_bound`,
   `no_gpu_profile` and `gpu_profile_unfit` (amendment 2026-11-23; formerly `gpu_candidate_unavailable`), `forbidden_by_campaign`, `provider_unregistered`,
   `scratch_requested`, `runtime_not_adaptable`, `input_not_pinned`, `alternates_full`,
   `no_fitting_profile`, `profile_unfit`, `over_operator_cap`, `over_stage_cap`,
   `over_campaign_budget`, `program_budget`, `candidate_refused` (the provider's stage, grant,
   trust narrowing or input-admission check refused the added candidate). Only Tessera writes the
   record: a submitted one is discarded and recomputed; with it goes at most the first alternate, and only
   if it has the shape the adapter derives (the record's provider and profile, adapted), never an
   author-listed one.
   *Amended 2026-11-23c (structured cause):* a `candidate_refused` record also names its cause
   in structured fields, from the refusal's type and attributes (the message is never parsed):
   `cause: "license_label"` with `labels` (the effective licence labels checked, from
   `LicenseRefused.labels`), `cause: "data_class"` with `data_class` (the class the provider is
   not trusted to receive, from `DataClassConflict.data_class`), `cause: "input_not_admitted"`
   (an input refusal that names no class: a mismatched code approval, an unreadable manifest) or
   `cause: "other"` (the provider's stage, grant or narrowing check). These fields appear only on
   a refused record (absent on records stored earlier), are part of the digest like the rest of
   the record, carry no path or secret, and are copied into the ADR 0107 guard `detail` entry
   (`source: "heavy_remote_candidate"`), so the fallback event, placement and receipt show them.
   A placement-time skip of a listed Salad candidate keeps its reason code (`license_label`,
   `untrusted`) and free-text detail unchanged.
7. **Fallback.** A stage placed locally afterwards (skipped candidate, no candidate added, or no
   setting) is the ADR 0107 recorded fallback with its reasons.
   *Amended 2026-11-23 (ADR 0107 amendment 2026-11-23b):* a record with `added: false` explains
   the local fallback (reason `remote_candidate_not_added`, the record's reason in `detail`); with
   no setting there is no record, the fallback is unexplained and, unless the operator set
   `heavy_unexplained_local: warn`, the stage fails with `heavy_local_unexplained`.

## Not done

- ~~A GPU-required (`gpu`-tagged) heavy stage gets no candidate~~: see the 2026-11-23 amendment.
- A stage reading another stage's output cannot be adapted at submit (`input_not_pinned`).
- Standing approvals (`campaign_standing_store`) and approved re-run submissions do not run the
  rule; a re-run submitted through `POST .../rerun` recomputes it under the current setting.

## Tests

`tests/test_heavy_stage_remote_candidate.py`: a heavy local-only stage stored and approved with
Salad first and placed there with an idle local host; validate and submit digests agree; light
and already-listing stages unchanged; no setting refuses the unexplained `not_listed` fallback
(ADR 0107 amendment 2026-11-23b);
operator, stage-cap and program-budget refusals recorded; GPU stage not given a CPU candidate;
no fitting profile and an adapter refusal recorded; a forged record discarded without removing an author-listed alternate; strict setting;
v1 unchanged; an approved submission binding the candidate.

## Amendment (2026-11-23): pinned downloads are heavy-eligible, not egress-bound

A heavy stage whose only network reach is pinned `public_artifact` downloads (ADR 0097) is not
`egress_bound`: `egress_bound` keeps meaning a declared free-form `egress` policy. Such a stage gets the
candidate like any other heavy stage, and the adapter carries its pins as the candidate's only
fetches (ADR 0097 and ADR 0076 amendments of the same date). The guard (ADR 0107) adds the signal
`large_public_inputs` when the pins' summed `max_bytes` reach the heavy threshold, so a large-download
stage is heavy by its downloads alone and is ordered Salad first. Tests:
`tests/test_heavy_download_on_provider.py`.

## Amendment 2026-11-23: GPU candidates for GPU heavy stages

A `gpu`-tagged heavy stage never got a candidate (`gpu_candidate_unavailable`), because the adapter
built CPU candidates only and a GPU profile has no static rate to estimate from.

1. **Operator price.** A GPU profile's `stage_adapter` section may now exist and must then name
   `gpu_hour_usd` (positive USD/hour, the CPU rate shape); a CPU profile's section must not. Its
   presence is the operator's opt-in of that GPU profile to adapted stages. No experiment or spec
   supplies it.
2. **Which profile.** A `gpu`-tagged stage (the ADR 0088 GPU tag set) considers only GPU profiles
   of the campaign's org and project whose `stage_adapter` names `gpu_hour_usd`, in file order, or
   the setting's new optional `gpu_profile`; any other stage only CPU-only adapter profiles (the
   `profile` setting), as before. Neither kind is offered for the other.
3. **What is added.** `derive_candidate_config(..., gpu=True)`: the same derivation with
   `salad.gpu` true (default, so omitted from the JSON). Its `max_exposure_cents` is the full
   duration at `gpu_hour_usd`, rounded up, and must fit the same four bounds; no cap is raised.
   The Salad provider's adapted-stage validation no longer requires `gpu: false`; `check_stage`
   still requires a GPU profile for a GPU candidate. Heavy-first ordering's `gpu` class then
   tries the `remote_gpu` candidate first.
4. **Reasons.** `gpu_candidate_unavailable` is replaced by `no_gpu_profile` (no opened GPU profile,
   including only CPU profiles) and `gpu_profile_unfit` (the adapter refused it); the cap,
   program-budget and `candidate_refused` reasons are shared with CPU candidates.
5. **Cost honesty.** `gpu_hour_usd` is an operator estimate input, not cost proof. Salad
   admission still reads the published GPU rate and refuses the run when it would exceed the
   candidate's cap (`budget_insufficient`): a price set too low fails that Salad attempt, never
   spends past the cap. A GPU candidate skipped at placement (e.g. an expired credential) falls
   back locally as the ADR 0107 recorded fallback with its reason.
6. **Unchanged.** Explicit `{"adapt": {"profile": ...}}` requests still take CPU-only profiles;
   light and CPU heavy stages, and every existing profile file, behave as before. No schema
   change.

Tests: `tests/test_heavy_stage_gpu_candidate.py`.

## Amendment 2026-11-23: chained heavy stages (late-bound inputs)

With [ADR 0140](0140-late-bound-stage-output-inputs.md) the Salad stage adapter accepts a
`stage` input, so the rule no longer refuses it:

- A heavy stage whose inputs are each pinned (`snapshot`, `artifact`, `public_artifact`) or a
  `stage` input naming another stage of the same campaign gets the candidate as in decision 4.
  The DAG edge is part of the spec the owner approves, so the digest the runner binds at stage
  start is covered; no new per-run approval is added. The runner re-checks data class and licence
  against Salad's trust at bind, and the Salad driver pins the bound snapshot digest on the
  reviewed contract (same delivery and verification path as a pre-pinned input).
- Sizing: the candidate is derived with the spec's upstream outputs, so the contract's input cap
  is the upstream outputs' declared `max_bytes` (plus any pinned inputs), held to the profile's
  `disk_mb` and `stage_adapter.max_input_bytes`; an unbounded or oversized input is the adapter's
  refusal, recorded as `profile_unfit` with its detail. No size is invented. The exposure estimate
  stays the full-duration cost and is held to the same operator, stage, campaign and program caps.
- `input_not_pinned` now means an input that is neither pinned nor a `stage` input naming another
  stage of this campaign (defensive: the stored spec cannot otherwise hold one).
- If the upstream has not succeeded and settled at bind time, the runner refuses
  (`late_bind_refused:<code>`, ADR 0140) and nothing reaches Salad.

The "Not done" line on stage outputs is superseded. Tests:
`tests/test_chained_heavy_salad_candidate.py` (submit record and late-bind contract, a declared
bound over the profile input cap, end-to-end bind and Salad placement with the bound digest, an
upstream refusal never reaching Salad, the input classification).
