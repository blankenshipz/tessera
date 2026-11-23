# 0128 — A conclusion's evidence shows each cited run's status and result, and a failed run cannot carry a stance

## Status

Implemented (no schema change: read-time projection and a write-time check). Extends
[ADR 0104](0104-conclusion-decision-and-evidence-stance.md) and the attempt result assessment
(a succeeded admitted attempt may be assessed `positive`, `negative` or `inconclusive`, separately from its
operational outcome). `scientific_validity` stays `not_assessed`.

## Context

The workflow says to keep a failed run distinct from a valid negative or inconclusive result. The
ledger already separates an attempt's operational outcome (`succeeded`, `failed`, `cancelled`,
`inconclusive`, and `unknown` for a resolved admission) from its result assessment, and only a succeeded
attempt can be assessed. Conclusions did not carry the distinction: an evidence link named only an attempt,
artifact, measurement and stance, so a reader of a conclusion could not tell whether a cited run
failed, was a valid negative result, or was inconclusive, and a failed run could be cited as
`opposes` evidence, which reads as a negative result.

## Decision

1. **Read.** Every evidence item in a conclusion response and in the experiment export carries
   `attempt_kind` (`legacy` or `admitted`), `attempt_outcome` (the operational outcome; null for an
   unresolved admitted attempt), `result_assessment` (`positive`, `negative` or `inconclusive` for an admitted
   attempt that has one, otherwise null) and `run_failed` (true when the operational outcome is
   `failed`, `cancelled` or `unknown`). A valid negative result has `run_failed` false and
   `result_assessment` `negative`.
2. **Write.** A citation with a stance (`supports` or `opposes`) is refused (409) when the cited
   attempt's operational outcome is `failed`, `cancelled`, `unknown`, `inconclusive` or unresolved, or
   when its result assessment is `inconclusive`. Such a run can still be cited with no stance, as
   context. A succeeded attempt, assessed `positive` or `negative` or not yet assessed, may carry a stance.
3. **Nothing relabelled.** Stored rows are unchanged; existing conclusions keep their links and only gain the
   projected fields. The rule applies when a conclusion is created or updated.
4. **Unchanged links on update.** An update re-checks every citation, so a draft written before this rule that
   carries a stance on a failed or inconclusive run would otherwise be refused (409) on any edit, even one that
   only changes the statement. A citation is therefore kept as it is when every one of its links (attempt,
   artifact or measurement, and stance) is already stored on that conclusion with the same stance. A new link,
   a link whose stance changed, and a stance added to a link stored without one still get the full stance
   check, so the rule is never weaker for new evidence. All other evidence checks (the attempt, artifact and
   measurement must belong to the experiment and the cited attempt) still run for every citation.

## Consequences

- A failed run can never be presented as evidence for or against an idea; a null result is recorded as a
  negative assessment of a succeeded run.
- Not covered: campaign receipts, which record stage status and measurements and are not experiment
  attempts; a campaign result assessed into an experiment goes through an attempt.
