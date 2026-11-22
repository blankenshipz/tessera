# 0100 — A person records one immutable assessment of a succeeded attempt's result

## Status

Implemented (schema 82: one additive table). Builds on the admitted-attempt records and
semantic state history described in [records-api.md](../records-api.md).

## Context

README step 3 has the researcher run the experiment and then judge what the result means
before concluding. Today the only outcome Tessera stores on an admitted attempt is how it
ended operationally: `succeeded`, `failed`, `cancelled` or `unknown`. That says whether the
process finished, not whether the result supports the idea. Reading `succeeded` as "positive"
or `failed` as "negative" would turn an operational outcome into a scientific one. A person
had no place to record their own reading of a result against the attempt that produced it,
other than free text in a later conclusion.

## Decision

1. **Route.** `POST /experiment-admitted-attempts/{id}/result-assessment` with
   `{assessment, rationale, expected_revision}`. `assessment` is one of `positive`,
   `negative` or `inconclusive`. `rationale` is required free text from the person.
   `expected_revision` is the attempt's current revision; a stale value is a 409.
2. **Human only.** Only a person may assess. An agent or worker actor gets 403. Nothing is
   inferred, defaulted or computed: not from the resolve outcome, not from metrics, not from
   a threshold.
3. **Only a succeeded attempt.** The attempt must be resolved `succeeded`. A `failed`,
   `cancelled`, `unknown` or unresolved attempt is refused with 409, because an operational
   failure is not a negative result. A person who believes a failed attempt says something
   about the idea records that in a conclusion, not as an attempt assessment.
4. **One time, immutable.** A second assessment of the same attempt is a 409. The assessment
   is stored in `research_attempt_result_assessments`, with database triggers that refuse any
   update and any delete. There is no edit, retract or replace path; a changed view is a
   conclusion amendment.
5. **Atomic.** The row, the `experiment.attempt_result_assessed` event and the idempotency
   record are written in one transaction, with the tenant check at data access. A replay with
   the same key returns the stored answer; a reused key for a different request is a 409.
6. **Reads.** The admitted-attempt reads and `GET /experiments/{id}/export` carry
   `result_assessment`, which is `null` when none was recorded. The assessment is part of the
   export's existing bounded collections.
7. **Not validity.** `scientific_validity` stays `not_assessed`. An assessment is a
   researcher's record of their reading, never an approval, a gate decision or an authority
   for any later action. Recorded historical outcomes are never relabelled.
8. **Schema 82** (the version the number allocator reserved; versions 80 and 81 belong to
   other work and must be registered before this ships, since a database cannot skip a version). The migration is additive: it creates the table and its triggers and
   changes no existing row. Like every schema bump it clears the backup-health success markers
   (`last_success_at`, `last_backup_id`) so the next backup is taken on the new schema; that is the
   convention of every migration, not a change to a record. The assessment event is written at the
   attempt's current revision and does not bump it: a duplicate is refused by the table's primary
   key and the "already has an assessment" check, not by revision.

## Consequences

- A result's operational outcome and a person's reading of it are separate facts, each
  attributed to its source. A reader of the export can tell "ran to completion" from "a
  person called it positive, and why".
- The record is final once made. A mistaken or revised reading cannot be edited; it has to
  be stated in a later conclusion or amendment, which keeps the earlier record visible.
- Because failed and cancelled attempts cannot be assessed, a campaign of mostly failed
  attempts shows no assessments rather than a pile of "negative" ones.

## Not done

- No assessment of legacy attempts; only admitted attempts can be assessed.
- No desktop UI or MCP tool yet; the route is the only entry point.
- No automatic assessment by agents or from metric thresholds.
- No requirement that an attempt be assessed before a conclusion cites it.

## Tests

`tests/test_attempt_result_assessment.py` (routes, authorization, refusals, atomicity, reads,
export) and `tests/test_attempt_result_assessment_migration.py` (schema 82 migration and the
no-update/no-delete triggers).
