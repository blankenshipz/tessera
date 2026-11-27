# 0069 — Re-run a finished campaign's failed stages from the platform

## Status

Implemented (no schema change). Builds on [ADR 0055](0055-mcp-steering-tools.md) (the offline
retry builder) and [ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md); every
authority rule of [ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) and
[ADR 0047](0047-agent-routed-data-destinations.md) is unchanged.

## Context

After a platform fix is deployed, a session still had to rebuild a failed campaign by hand: read
it, run `tessera_prepare_stage_retry`, validate, submit under a fresh key, and later explain to a
reader how the new campaign related to the old one. The builder lived in the MCP package, so the
control plane could not offer it, the new campaign carried no record of the old one, and a v1 spec
or a campaign that is simply to be run again had no path at all.

## Decision

1. **A route and a tool.** `POST /campaigns/{id}/rerun` (body `mode`, optional `stage_keys`,
   `budget_cap_cents`; an `Idempotency-Key`) and `tessera_rerun_campaign` (registered with
   `campaign:submit`, `campaign:read` and `artifact:read`). The caller needs the first two for
   any re-run and `artifact:read` for `failed_stages`, which hands back the reused outputs'
   digests (in the pins and on the receipt), as the offline retry tool already required;
   `identical` returns none. The tool is registered only when all three scopes are enrolled, so a
   credential without `artifact:read` can request an `identical` re-run over HTTP but not through
   the tool. Nothing new is enrolled. The source must be failed, stopped or
   cancelled.
2. **It is an ordinary submission.** The route plans a spec and calls the same submit path as
   `POST /projects/{p}/campaigns`: the same reference, provider, trust, data-class and
   grant-lifetime checks run, the result is a new `submitted` campaign, nothing is approved or
   granted, and a person approves the new spec digest before anything runs (the program budget,
   unlock and every admission check apply at that approval). Digest binding is untouched: an
   approval of the old digest approves nothing here.
3. **Modes.** `failed_stages` (default) re-runs the stages that did not succeed and anything that
   consumed them, and reuses each succeeded stage's output through the existing custody and
   lineage mechanism: the consumer gets a `kind: artifact` input pinning the old campaign, stage,
   output and sha256, which resolves only from a succeeded attempt in the terminal receipt whose
   manifest is retained and custody-released. The builder moved into the control plane
   (`campaign_rerun.py`, shared with the offline MCP tool) and keeps its refusals: v1 spec,
   output with no single digest, output not custody-released, several outputs of one stage read
   by one input (lifted by [ADR 0209](0209-rerun-multi-output-stage-pin.md) when the explicit
   folder pin is in the reviewed spec; see the amendment below). `identical` resubmits the same spec (any version), for a campaign to run again
   unchanged; it takes no stage keys or cap and yields the same spec digest.
4. **Spend.** The new cap defaults to the source's cap less what the source settled (never
   below the re-run stages' exposure). The per-submission limit this ADR first set (an explicit
   cap or `identical` mode could ask for the source's full cap again) is superseded by
   [ADR 0074](0074-rerun-spend-is-bounded-across-the-chain.md), which bounds the cap over the whole
   re-run tree by what it has committed. Each link is
   still a separate approval by a person, counted by the program budget and project limits,
   which are enforced where they always are.
5. **The link, atomically.** Submission records a `campaign.rerun_submitted` event in the same
   transaction as the campaign (source campaign, mode, source spec digest, re-run stages, reused
   outputs by digest, cap); no schema change. The new campaign's receipt gains a `rerun` section
   from it, `GET /campaigns/{id}` shows `rerun` on the new campaign and `reruns` on the source.
   The source's own receipt is immutable and unchanged; its view lists its re-runs.
6. **Idempotency.** The key binds the request: the same key and request is answered from the
   stored record before anything is planned, so a custody or output change since then cannot turn
   a replay into a refusal; the same key for a different request is a 409, as for approval keys.
   A new request is planned afresh, and the submit transaction re-resolves every pin, so custody
   revoked between planning and submit is refused there, not at approval.
7. **Reading the link.** The link is read through the project's event range; a lineage column is
   the next step if one project's events grow very large.

## Consequences

- A session no longer rebuilds a failed campaign by hand; one call yields the submission, and the
  link between old and new is on the receipts.
- It does not shorten the human step: a person still approves the new digest. A custody
  revocation, a changed trust setting or a refused provider fails the re-run loudly at submit or
  approval, not silently.

## Amendment (ADR 0209): several outputs of one stage read by one input

The refusal of "several outputs of one stage read by one input" in section 3 is lifted only for one
case. The re-run spec must then hold an explicit `kind: artifact_folder` pin under the same input
name, listing each file's output, declared path and sha256, so the person who approves the new
digest sees the mount layout (`<name>/<declared output path>`). Each file passes every check of
section 3. One file that has no single digest, is not custody-released and retained, comes from a
stage that did not succeed, or comes from a stage that is itself re-run refuses the whole re-run.
Every other refusal of this ADR stands, and approval of the source digest still approves nothing.
