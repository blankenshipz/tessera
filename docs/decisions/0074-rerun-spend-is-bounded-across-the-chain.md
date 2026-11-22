# 0074 — A re-run's cap is bounded by what its whole re-run tree has committed

## Status

Implemented (no schema change). Narrows [ADR 0069](0069-rerun-a-finished-campaign.md) §4; every
other rule of that ADR is unchanged.

## Context

ADR 0069 bounded each re-run by its source's own cap, so a chain of explicit-cap or `identical`
re-runs could, link by link, be approved to spend more in total than the first campaign's cap.
Each link needed a person's approval, but a reader could reasonably expect "never above the
original" to cover the chain. This ADR adds a check over the whole tree of re-runs, siblings and unsettled links included.

## Decision

1. **The tree's budget.** The tree is the first campaign and every campaign re-run from it, at
   any depth, read through the recorded `campaign.rerun_submitted` links. A finished campaign
   (failed, stopped, cancelled, succeeded) commits what it settled; an unfinished one (submitted,
   approved or running) commits its whole cap, or what it already settled if that is more. What
   is left is the first campaign's cap less everything the tree has committed. A submitted
   re-run that is never approved keeps its cap committed until it is cancelled. A tree too long
   to read (200 members) reports itself truncated and refuses further re-runs.
2. **Enforced at submission, twice.** The plan gives the message; the bound itself is re-read
   inside the submit transaction, under the write lock, so two re-runs planned against the same
   state cannot both fit: the second is refused ("no longer covers") and writes nothing. The plan refuses a cap above what is left, whether the cap is the
   default, an explicit `budget_cap_cents`, or the cap an `identical` re-run inherits from its
   spec. The default is the source's cap less its settled spend, and never more than what is left.
   A stage exposure that does not fit in what is left is refused with the amount left in the
   message. Nothing is clamped silently and nothing is written on a refusal.
3. **Inspection.** `GET /campaigns/{id}` (so `tessera_get_campaign`) shows `rerun_tree` on a
   campaign in a tree: root and its cap, committed and remaining cents, and each member's
   status, cap, settled and committed cents. It is read-only and adds no scope.
4. **Identical re-runs.** An `identical` re-run yields the same spec digest, so its cap cannot be
   lowered; when that cap is above what is left it is refused, and the caller re-runs the failed
   stages with a smaller cap instead.
5. **Freeing budget.** A refusal names the unfinished re-runs that hold budget. A person can
   cancel one that is not wanted: a re-run that was never approved is cancelled at once and its cap
   is freed (an agent may cancel only what it submitted, a person any campaign of the project).
   Reducing an approved re-run's cap (ADR 0055) also frees the difference.
6. **What the tree does not cover.** Only campaigns linked through `POST /campaigns/{id}/rerun`
   are in the tree. A spec built with `tessera_prepare_stage_retry` and submitted by hand is an
   ordinary campaign with no link: it is bounded by its own cap and settled spend and by the same
   approval, program budget and project limits, but it does not draw on, or count against, the
   tree.
7. **Unchanged.** The new campaign is still an ordinary submission that a person approves by
   digest; the program budget and project limits apply at that approval as before.

## Consequences

- The caps approved across a re-run tree can no longer add up to more than the first campaign's
  cap, counting unfinished re-runs at their cap. Two siblings share one remainder.
- Not a new authority: each re-run is still a separate approval by a person, counted by the
  program budget and project limits. The tree bound is checked at submission, so a cap reduced
  later (ADR 0055) only frees budget for further re-runs.
- A source that settled spend before its first re-run can no longer be re-run with an explicit
  cap equal to its full cap, nor `identical` when its cap exceeds what is left. A re-run waiting
  for approval blocks its siblings' budget until it is approved and finishes or is cancelled.
- No schema change; the cap and status come from the campaign rows already stored.
