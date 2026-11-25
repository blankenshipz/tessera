# 0151 — A Salad stage may list alternate execution profiles, tried in order

## Status

Implemented, as narrowed by [ADR 0169](0169-salad-profile-fallback-policy.md): a stage moves to its
next listed profile only before any possible start (an attempt proven never started and settled
at zero, the ADR 0060 class fallback addendum), never after a run that may have started or been
billed. The execution-plane section below states the rule as implemented.

## Context

Allocation instability (ADR 0148) lets placement prefer another approved candidate when the
GPU class of a stage's first candidate keeps losing its allocation, and provider-confirmed
termination (ADR 0149) records when the provider itself ended a node. Both found the same gap:
for a stage whose only candidate is Salad, the record is always
`allocation_unstable_no_alternate`, because an approved spec named exactly one operator profile
(one GPU class) per Salad candidate. The only way to try another class was a new spec and a new
human approval, so an unstable class stopped the campaign even when the operator had a second,
equally capped profile ready. ADR 0066 already lets a review pre-approval cover a stage's
alternate *providers*; nothing let one approved stage name several *profiles* of one provider.

## Decision

### Spec and approval

1. **Spec.** `SaladStageSpec` gains `alternate_profiles`: an ordered list of one to four operator
   profile names, distinct, none equal to `profile`. Absent (never `[]` or null in the stored
   form) is the default, so every existing v1 and v2 spec and its digest are byte-identical. The
   stage adapter request (ADR 0076) takes the same optional field, `adapt: {profile,
   alternate_profiles?}`, and its expansion carries it into the explicit block.
2. **Submit and approval checks.** `SaladRouteProvider.check_stage` checks each listed profile
   exactly as the stage's own, with the stage's unchanged `max_exposure_cents`: registered, of
   this organization and project, the stage's own worker (alternates do not change the worker),
   the same GPU or CPU kind as `salad.gpu`, an unblocked credential, the image digest in its
   allowlist, the hold within its `max_run_cents` (and the CPU worst case of the whole duration
   within the hold), and the worst-case hold of every attempt within its `max_task_cents`. A
   refusal (409) names the alternate. The data-class, trust and public-artifact guards (ADR 0039,
   0040, 0047, 0097) are provider-level (the Salad provider's trust setting, the stage's inputs
   and worker): they do not read the profile, so they hold for every listed profile alike. The
   adapter refuses an alternate that would derive a different workload (worker, custody host,
   watchdog, program or grant lifetime), and its estimate covers the costliest listed profile.
   Together these make the profile name the only profile-dependent field of an approved stage,
   so `salad_alternate_profiles.stage_on_profile` is an exact substitution that refuses a profile
   the stage does not list. The run-time selection (GPU class, resources, target digest; see
   `derived_digests`) is derived from that profile at use, as for the stage's own.
3. **What a human approves up front.** The spec digest binds the list, so approving the spec
   approves running the stage on any listed profile, in order, under the same per-run cap; a
   pre-start move needs no other human step, and a profile not in the approved list is
   unreachable. A stage that lists alternates must set `retry.max_class_fallbacks` to at least 1
   (the bound of pre-start moves, ADR 0060 addendum), else submit refuses it: nothing could reach
   the alternates.
4. **Review pre-approval (ADR 0044, 0066).** The plan review depends on the profile (its compute
   window is the profile's `route_max_seconds`); the source-assessment and source-use reviews do
   not. One pre-approval entry per stage (the one-row-per-stage store is unchanged, no migration)
   therefore names every profile of the candidate it covers: `plan_digest` stays the candidate's
   own profile's, and `alternate_profiles: [{profile, plan_digest}]` gives each alternate's.
   Approval refuses, naming the profile, an entry that omits a listed alternate, names a profile
   the spec does not list for that stage (so it can never be bound), or repeats the own profile.
   The covered profiles and their digests are recorded on the `campaign.approved` event (as ADR
   0066 records the provider; `campaign_preapproval.recorded_profiles`). At resolution the plan
   digest compared is the one bound for the profile the attempt runs on
   (`salad_alternate_profiles.attempt_profile`: an attempt-level record, else the stage row); an
   attempt on a profile the entry does not cover is a typed decline (`wrong_profile`) and waits
   for a person. A candidate without alternates keeps the pre-ADR-0151 entry and rule exactly; the
   operator's `preapprove_reviews: false` still only narrows. The read-only digest preview
   (`tessera campaign preapproval-digests`) adds each alternate's plan digest.

### Execution-plane fallback (strict rule, ADR 0169)

1. **Only before any possible start.** The stage moves to the next listed profile only after an
   attempt its provider proves never started (no `start` effect on the route, never reallocated,
   a live receipt naming the pre-start step or a create rejection) and that settled at exactly
   zero, recorded `prestart_no_spend` (ADR 0060 addendum), at most `retry.max_class_fallbacks`
   times. The next profile is `salad_alternate_profiles.next_profile(approved stage, tried)`;
   `tried` is the stage's own profile plus every recorded switch target on that candidate, so a
   profile is never chosen twice and the order always comes from the approved stage.
2. **Never after a possible start.** An attempt that ended unstable on its own route (its Salad
   verdict is `failed` and its route shows the allocation change: within ADR 0148's window,
   `allocation_unstable`, or a reallocated route whose stop the provider confirmed by ADR 0149,
   `termination_confirmed_unstable`) started, or may have, and may have been billed. The stage
   does not switch: it keeps the attempt's outcome and settlement, is held (`blocked_unknown`,
   campaign `blocked` with reason `post_start_switch_refused`) and records exactly one
   `campaign.salad_profile_fallback_stopped` event (`outcome: post_start_switch_refused`, the
   trigger as `reason`, `started: true`, `spent` from the settled amount, `evidence`). The stuck
   view's finding for the stage carries `salad_profile_decision` in its evidence; a person or an
   approved delegation decides. A run
   that failed on a stable route, an unconfirmed termination (held, ADR 0149) and an attempt in
   flight change nothing. A held, unconfirmed attempt never switches, including when the
   confirmation arrives after the termination bound: a person still decides. The stop event and
   the stage's change to held commit together. A switch records the profile for the first attempt
   that can use it on that candidate; placement may still put that attempt on another provider
   (ADR 0148), so a receipt or stage status labels an attempt with a Salad profile only when it
   ran on the candidate that lists the profiles, and the receipt keeps the switch history even
   when the stage ends on another candidate.
3. **Derived, not remembered.** Attempt N runs on the `to_profile` of the latest
   `campaign.salad_profile_switched` event on its candidate whose `to_attempt` is at most N, else
   on the stage's own profile. The same runner transaction that records the attempt's
   `prestart_no_spend` transition appends that event and sets `campaign_salad_stages.profile`
   (the executor's work listing and the pre-approval's `attempt_profile` read it); it is a
   mirror, not the source. No schema change.
4. **The stage on its profile.** The runner's current stage is `stage_on_profile` of the approved
   stage, so every step re-derives from the profile: selection and route, plan scope and its
   digest, capability declaration, executor profile, and placement re-runs every check for it.
   The stage's capability is bound to its task plan: a stored capability whose declaration
   differs from the current profile's is refused, never reused. This also applies to a stage with
   one profile: if the operator edited that profile after its capability was approved, the step
   is refused (`step_refused`) instead of reusing the approved capability.
5. **Checked before a move, fail closed.** A next profile that is not covered records one stop
   event (`outcome: profile_not_approved`, `started: false`) with `detail` `profile_unavailable`,
   `over_run_cap` (above the stage's own cap or the profile's `max_run_cents` as the operator
   file says now), `capability_mismatch` or `preapproval_not_covering` (the stage resolves reviews
   under a pre-approval whose recorded entry does not bind this profile's plan digest), and the
   stage is held; nothing moves and no later profile is skipped to. No profile left on the
   candidate moves the stage to its next `alternate_providers` candidate or fails it
   `class_fallback_exhausted` (ADR 0060 addendum).
6. **Receipt and event.** `campaign.salad_profile_switched` carries `stage_key`, `provider_id`,
   `attempt_id`, `from_attempt`, `to_attempt`, `from_profile`, `to_profile`, `reason`
   (`prestart_no_spend`), `prestart_reason`, `started: false`, `spent: false`,
   `settled_cents: 0`, `order` and `recorded_at`, once per ended attempt. The campaign receipt's
   stage carries `salad_profiles` (`order`, `switches`, `stopped`) and each attempt on that
   candidate its `salad_profile`, only for a stage that lists alternates; every other receipt
   keeps its shape.

### Views

A control-plane session steers from the MCP read, not from journals. `tessera_campaign_stage_status`
(and `tessera_campaign_logs`, per stage) adds `salad_profiles` to every Salad stage, built from rows
the platform already keeps: the approved spec's ordered list (`profile_order`), the switch events
(`campaign.salad_profile_switched`) and the stop event (`campaign.salad_profile_fallback_stopped`).

- `profiles`: the profile keys in the order they are tried, primary first (one element for a
  single-profile stage).
- `latest_attempt_profile`: the `to_profile` of the latest switch, else the stage's own profile.
- `switches`: each switch so far (`from_profile`, `to_profile`, `reason` code, `from_attempt`,
  `to_attempt`, `started`, `spent`, `at`); empty for a single-profile stage.
- `outcome`: null, or the latest stop (`outcome` of `profile_not_approved` or
  `post_start_switch_refused`, a `detail` code, the `attempt`, the `next_profile` key and
  `started`).

The view is additive (no existing key changes) and secret-safe: it returns operator profile keys,
closed codes and counts only. Every key must match the short `[a-z0-9_-]` shape the read uses for
profile keys and every reason or detail must be a short code; anything else is returned as
`detail_withheld`, never as free text, a host or an identifier of a worker or credential.

## Consequences

- A Salad-only stage can name a second (third, fourth) GPU class or CPU profile the operator
  already capped, and an approved campaign moves to it, before any possible start, without a new
  spec or approval. After a possible start it is held for a person instead. Old specs, their
  digests, their pre-approval bodies and their approve events are unchanged.
- The per-run cap is not widened: every profile is checked against the same stage
  `max_exposure_cents`, and each profile's own operator ceilings still bound it.
- A pre-approval of a stage with alternates is all or nothing: a person who does not want to
  pre-approve the plan on some listed profile approves without pre-approving that stage (each
  attempt's plan then stops for a person), or drops the profile from the spec.
- A profile's per-task ceiling counts every attempt's hold, pre-start moves included
  (`max_attempts + infra_retries + max_class_fallbacks`). Submit checks every profile with
  `max_exposure_cents x (max_attempts + infra_retries) <= max_task_cents`, so the static bound
  covers every attempt on any listed profile. What is not covered is the runtime tally of what
  is already held or spent per profile against that ceiling.
- Not covered: a profile edited by the operator after approval is re-checked only
  where the existing checks re-run, as for the stage's own; alternates share the stage's worker
  (an alternate with another worker is refused rather than substituted); and the pre-approval's
  profile check relies on the execution plane recording the attempt's profile before proposing
  that attempt's reviews (otherwise a stage-row profile outside the entry fails closed to a
  person). Verified with fakes only; nothing here has run against live Salad.
