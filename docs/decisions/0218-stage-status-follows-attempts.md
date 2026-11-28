# 0218 — A stage's status follows its attempts

## Status

Accepted. Completes the hold of [ADR 0169](0169-salad-profile-fallback-policy.md) and the resume
of [ADR 0212](0212-reconciled-uncertain-no-output-retry.md). No schema change and no migration.
Container-exit classification ([ADR 0196](0196-salad-start-failure-and-events.md)) is unchanged.

## Context

Two live stages of one campaign ended with attempt 1 `failed_permanent` /
`salad_container_exited_nonzero`, settled at the guardian's bound, with no retry and with the
stage still `blocked_unknown`. Their guardian frame was closed by the provider-confirmed
reconcile after the provider changed the allocation under the frame.

The stuck stage is a stage-status defect. After a terminal attempt of a stage that lists
alternate profiles and ended unstable, ADR 0169 held the stage `blocked_unknown`.
`blocked_unknown` is final for the stage loop, and the blocked drive only looks at `unknown`
attempts, so once the campaign unblocked nothing ever moved the stage again. It stayed non-final
with no live attempt, its dependants waited on a dead stage, and the stage's work was lost.

## Decision

1. **Classification is unchanged.** A recorded process exit (non-zero, not 137 or 143) stays a
   permanent workload failure (`salad_container_exited_nonzero`), whatever stop cause the
   guardian recorded, and is never retried as infrastructure. An earlier design that retried
   such an exit after an `allocation_changed` stop was rejected: the exit record carries no
   timestamps, so nothing shows the exit came after the provider's move, and a real crash that
   the provider then moved (a long run, an exhausted exit-watch read budget, a race between the
   poll and the watch) would have been retried at further paid cost and blamed on the provider.
2. **A stage follows its attempts.** At the start of each drive of a running, stopping, blocked
   or cancel-requested campaign, a stage that is `blocked_unknown` while none of its attempts is
   `unknown` or live (and whose latest ended attempt is settled) is repaired in one transaction
   with one `campaign.stage_status_repaired` event, by its latest attempt:
   - `failed_permanent`: the stage becomes `failed`, the stage-failure stop reason is set when
     none is, and its dependants are skipped by the ordinary loop. The ADR 0169 hold keeps its
     `campaign.salad_profile_fallback_stopped` event and never moves the profile.
   - `refused_budget`: the stage becomes `refused_budget`; `cancelled`: it becomes `skipped`
     (both final for the stage loop).
   - any other ended attempt: the stage returns to `preparing` and `_advance_stage` retries or
     finishes it by its own rules.
   A stage with an `unknown` or live attempt, no attempt, or an unsettled latest attempt is never
   touched. The repair only moves stages toward final states, so it also runs for a
   cancel-requested campaign (the cancel cannot finish while a stage is `blocked_unknown`). It is
   idempotent and aborts when the runner has lost its lease.
3. **Existing rows.** The repair reads only current rows, so deploying it repairs stages already
   stuck on the next runner pass, with no operator action and no direct database edit. A stage
   whose attempt already failed permanently ends `failed`. Its work is recovered by the official
   rerun of failed stages (`POST /campaigns/{id}/rerun`, mode `failed_stages`, or the MCP rerun
   tool) once the campaign is terminal. The old attempt keeps its recorded class and settlement
   (it is not reclassified after the fact).

## Consequences

- No stage stays non-final with every attempt terminal (tests/test_stage_status_follows_attempts.py).
- The ADR 0169 hold lasts one runner pass, then the stage is `failed`; the retry decision it
  described is the `failed_stages` rerun. The stuck-campaigns view stops reporting a held,
  terminal stage after one pass.
- A crash that the provider then moved is a permanent failure, as before.
- Verified with fakes only; nothing here has run against live Salad.
