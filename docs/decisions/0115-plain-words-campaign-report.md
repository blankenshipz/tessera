# 0115 — A plain-words report of a campaign, from its receipts

## Status

Implemented. No schema change. Reads the campaign receipt of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md), the egress journal summary of
[ADR 0034](0034-governed-sandbox-egress-and-dataset-staging.md) and the model-spend ledger of
[ADR 0090](0090-model-spend-ledger.md). It records nothing and grants nothing.

## Context

The campaign receipt is the evidence of a run, and it is written for a verifier: ids, digests,
hold and settlement figures, journal hashes. After a run the owner wants the other reading: what
ran, where, what it cost, how long it took, which hosts it reached, what data moved and how it
ended, in sentences, without having to decode ids.

## Decision

1. **A report computed from recorded facts.** `tessera campaign report`, `GET
   /campaigns/{id}/report`, `GET /projects/{id}/campaign-report?since=&until=` and the MCP tool
   `tessera_campaign_report` (all `campaign:read`, read-only) return one document
   (`tessera.campaign-report.v1`) and the same content as plain `text`. For each stage: what ran
   (the runtime in words), where (the provider, and that it moved if placement moved it), the
   number of attempts and why a retry happened, the outcome, the time (the sum of its attempts'
   recorded start-to-last-update spans, said to be approximate), the cost settled, the data that
   went in and out, and the hosts it reached with the bytes and any denied hosts. A window
   report covers the project's campaigns created in `[since, until)`, newest first, with totals.
2. **No internal ids unless asked.** Without `detail` the report holds no campaign, attempt,
   grant, operation, snapshot or principal id, no digest and no absolute path; stage names, provider
   names and data names are the operator's own. `detail=true` adds an `ids` block. A failure
   detail line is omitted from the plain report because it can carry an id or a path.
3. **Nothing is invented.** A missing figure is said so and is `null` in the data: a hold not yet
   settled ("cost not yet settled"), a worker that has not reported its network use, a stage that
   never ran. The report derives wording from recorded status and failure classes only; it judges
   no result, and a failure is never called a negative result.
4. **Model-call spend is the project's, for the period.** The spend ledger is keyed by project
   and run, not by campaign, so the report sums settled calls of the campaign's own project over
   the campaign's time span and says plainly that it is project-wide for that period, not
   attributed to this campaign. Open reservations and released calls are counted apart and are not
   spend. Another project's or organization's rows are never read.
5. **Access is the receipt's.** The caller must be able to read the campaign (or the project, for
   a window); the same tenant checks apply. The report is computed on request from the stored
   terminal receipt, or from a freshly built one for an unfinished campaign.

## Consequences

- The owner reads one page after a run instead of a receipt, and an agent can read the same facts
  through one tool.
- The time per stage is an approximation from attempt timestamps, not a measured run time.
- Not done: a per-campaign attribution of model-call spend, notifications, and any score or
  validity judgement.

## Tests

`tests/test_campaign_report.py` (wording for each outcome, cost arithmetic, egress, model-spend
scoping, window selection, no ids or digests or paths without `detail`) and
`tests/test_campaign_report_wiring.py` (the routes, the access rules, the CLI and the MCP tool).
