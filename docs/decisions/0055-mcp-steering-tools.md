# ADR 0055: steering tools in `tessera_mcp`

Status: accepted. Builds on ADR 0038 (campaign runner), 0040 (providers and data-class routing),
0047 (data destinations) and 0053 (resolving a confirmed exit).

## Context

`tessera_mcp` could submit, read and cancel campaigns, but steering an experiment still sent
people to the control API by hand: a spec could be checked only by submitting it, a failed
stage could only be redone by writing a new spec, a cap could not be lowered and a release a
finished campaign left behind could only be revoked by a human route.
[docs/mcp-steering-gaps.md](../mcp-steering-gaps.md) lists every need and its outcome.

## Decision

Add five tools and the control routes behind them, with no new enrolled action: each is gated by
an existing scope, the control API re-checks it, and the submitter-only rule of cancel applies to
every write.

1. `tessera_validate_campaign_spec` (`campaign:submit`): `POST /projects/{p}/campaign-specs/validate`
   runs the same parse and reference checks as submit (provider registry, grant lifetime, trust
   narrowing, snapshot digests and data classes, pinned artifact inputs) and returns the digest, the
   stage order and each stage's provider and data classes. It writes nothing. A pass is not approval.
2. `tessera_campaign_grants` (`campaign:logs`): read-only, explicit columns, per attempt grant and
   release status.
3. `tessera_reduce_campaign_budget` (`campaign:cancel`): lowers an approved, unfinished campaign's
   cap. A raise, an unapproved or finished campaign and a cap below held plus settled are refused.
   Revision-guarded, idempotent, recorded as `campaign.budget_reduced`.
4. `tessera_revoke_campaign_release` (`campaign:cancel`): revokes an active release one of the
   campaign's attempts used, only after the campaign finished or while it is cancelling. Same
   transition and event as the human revoke.
5. `tessera_prepare_stage_retry` (registered only with `campaign:read` and `artifact:read`): offline. From a finished
   campaign's spec, stage states and recorded outputs it builds a new v2 spec for the stages that did
   not succeed and everything that consumed them. Succeeded stages are not run again; their outputs
   are pinned by digest as `kind: artifact` inputs, which the platform resolves only from a
   succeeded attempt in a terminal receipt whose output manifest is retained and custody-released
   (the runner does this when the stage succeeds; recorded outputs now carry `custody_released`, and
   the builder refuses an output that is not released, since only a human restores custody). The cap
   never exceeds the original. Nothing is
   submitted: the agent validates and submits it like any spec and a human approves its digest.

The revoke tool is annotated destructive: an agent cannot undo it. If it races the runner's own
release cleanup while the campaign cancels, the runner re-reads active releases every tick; a stale
revoke it already sent is refused without stalling the cancel (tests/test_steering_cancel_race.py).

Not exposed, on purpose: approval, a cap or concurrency increase, grant reconciliation, release
creation, snapshot staging and gate decisions. They are human authority (concurrency is the
operator's project limit).

## Data destinations

The tools add no destination. The adapter still talks only to the configured control endpoint;
validation and the retry spec go through the unchanged reference checks, so ADR 0047's invariant
holds: a retry reuses the stage's providers and data classes and cannot widen them, and a submit of
it is checked like any other.

## Tests

`tests/test_campaign_steering.py` (control routes: scopes, submitter-only, dry run writes nothing,
reduce-only, release rules, grant view), `tests/test_mcp_steering.py` (paths, scope-gated
registration, destructive annotation, no approve or raise tool, the retry builder and its refusals),
`tests/test_stage_retry_end_to_end.py` (a real failed chain through the builder, validate and submit,
and the custody dependency) and `tests/test_steering_cancel_race.py`.

## Addendum: the stage status read

`tessera_campaign_stage_status` is a read-only steering input, not a steering action: one bounded
record per stage from rows the platform already keeps (placement decision and skip codes, waiting
events, latest attempt with its grant, executor lease and heartbeat age, failure and rejection
counts). It returns codes, ages and counts only; a free-text waiting reason is withheld unless it is a
short code, and no principal, credential, worker name or address is returned. It needs the campaign
log scope, answers another organization with a plain 404, and changes nothing. A lease reads as held
only while it is unexpired and its grant is still `active` or `cancel_requested`.
