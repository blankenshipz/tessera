# 0066 — A review pre-approval covers a placed Salad alternate

## Status

Implemented (no schema change). Closes the follow-up of
[ADR 0060](0060-capacity-aware-stage-placement.md) and extends
[ADR 0044](0044-salad-review-preapproval.md); the provider rules of
[ADR 0047](0047-agent-routed-data-destinations.md) are unchanged.

## Context

A stage may prefer a CPU host and name a Salad provider as an `alternate_providers` entry; the
runner then places it on Salad when the host is busy (ADR 0060). A Salad attempt stops for a
person at its source assessment and at each plan and source-use review unless the owner opted
in with a review pre-approval (ADR 0044). That opt-in only accepted a stage whose *own*
provider is Salad, so an alternate that was placed still waited for a session to approve each
stage, which defeats a loop that spreads work without one.

## Decision

1. **An entry names its candidate.** A `preapprovals` entry in the approve body (never in the
   spec) may carry `provider`: the stage's own provider (the default, so existing bodies keep
   their digest) or one of the stage's `alternate_providers`. Approval refuses it unless that
   provider is a candidate of the approved stage, is registered, and is a Salad route;
   `max_attempt_cents` must cover that candidate's own `max_exposure_cents` and
   `max_total_cents` stays within the campaign's approved budget cap. One entry per stage
   (unchanged), so a stage whose own and alternate providers are both Salad covers the one it
   names.
2. **The digests bind the alternate.** The three bound digests (source assessment, plan,
   source use) are computed for that candidate's runtime, configuration and profile
   (`tessera campaign preapproval-digests` lists each Salad candidate with its `provider`). The
   runner resolves a review as `campaign_delegated` only when the content recomputed from the
   stored row equals the bound digest, so a stage placed on a different candidate, or a changed
   spec, source or profile, declines and waits for a person. Nothing about the decline path
   changes.
3. **An entry covers only the candidate it names.** The approve event records each entry's
   `provider_id`; resolving a plan or source-use review compares it with the provider the
   attempt was placed on (an entry recorded without one covers the stage's own provider) and
   declines any other (`wrong_provider`). The source assessment is bound by its spec-fixed
   content digest alone, as before.
4. **Caps follow the placed provider.** The plan review's per-attempt cents are the hold of the
   candidate the attempt was actually placed on, checked against the entry's per-attempt and
   total ceilings. For every provider, the stage's own included, one the approved spec does not
   name for the stage, one no longer registered, or one that is not a Salad route yields no
   cents and so no authorization.
5. **Idle hosts cost nothing.** A pre-approval that is never used because the stage ran on its
   CPU host records no use, scaffolds no Salad stage and holds nothing.

## Consequences

- An owner who approves a campaign with an alternate and a bound entry lets the agent loop place
  that Salad alternate without a session approving per stage, never beyond the approved cap, a
  digest they bound, or an unregistered provider. Reviews of a Salad candidate with no entry
  still wait for a person.
- Gate go, conclusions, exports, publication, data classification and approving a campaign stay
  human; a standing approval still never carries a pre-approval.
- Limits: one covered candidate per stage. The provider is recorded on the approve event, not in
  a new column, so there is no schema change.
