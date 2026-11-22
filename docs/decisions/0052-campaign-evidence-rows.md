# 0052 — Typed evidence rows ingested by the platform

## Status

Implemented (schema v70, additive; if another change took v70 first, this step moves to the
next free version at merge: one constant, `EVIDENCE_ROWS_SCHEMA_VERSION`). Builds on the paired-delta groups and unlock
requirements of [ADR 0045](0045-campaign-gates-program-budget.md).

## Context

A paired-delta gate compares two arms seed by seed. A stage that trains and scores both arms
knows every per-arm number, but the platform's evidence records are one per admitted attempt,
entered through the API, so a person had to transcribe the numbers and the platform could
not check that the gate's inputs matched what the stage produced.

## Decision

A v2 stage output may be `kind: evidence_rows` and must then carry an `evidence` block:
`split_lock_id`, `variants` (experiment variant ids), `seeds`, `metrics` (name to unit) and
`partitions`. It is part of the spec digest, so the human approving the campaign approves the
pre-registered grid. The grid is capped at 5000 cells. Without the block, and for every other
output, spec digests are unchanged.

The output file is `{"rows": [{variant, seed, metric, value, partition, sample_count}]}`.
After the stage's attempt resolves `succeeded`, the runner takes a new delegated step,
`ingest_evidence_rows`, under the campaign's approval. In one transaction the platform:

1. reads the retained, custody-checked output (the same read as `derive_measurement`) and
   checks that it is the stage's declared output path;
2. compares the rows with the approved grid, never with the request: every row is inside the
   grid and the lock, `sample_count` equals the lock partition's member count, no cell
   repeats, and the grid is filled **exactly** (a gap, an extra cell or a duplicate refuses the
   whole file);
3. appends the rows to `gate_ingested_rows` (append-only, unique per attempt, variant, lock,
   metric, partition and seed, so a repeat run in another campaign, or new seeds, ingest
   cleanly and an exact replay of one attempt is refused rather than duplicated) and records `gate.evidence_rows_ingested`.

There is no human route: the step is refused for any actor that is not a campaign's
delegated stage, and it is not delegated to Salad stages. Spec submission and approval refuse
an `evidence_rows` output on a stage whose provider does not list the ingest step (Salad and
any plugin that omits it), so no one approves a stage whose rows could never reach a gate.

`compute_paired_delta` (so a bound-outcome unlock) reads the named campaign's ingested rows
beside ordinary metric records as `derived` sources; another campaign's rows are never mixed
in. A seed present in both is refused. The declared variants need only exist in the project:
the human who approves the digest chose them, and the unlock separately requires the paired
attempts to belong to the named campaign. A
stage whose rows were refused leaves the series empty, so the unlock stays closed (fail
closed); the receipt's measurement entry `evidence_rows.<output>` carries the row count or the
error class.

## Consequences

- A paired gate over a stage that trains both arms needs no transcription, and the
  cross-check is the platform's own.
- Ingested rows are measured numbers, `not_assessed`; the lock, the arms and the grid are
  still chosen by a human before the run.
- Ingested rows support unlock evaluation. A stored paired group (`POST
  /projects/{id}/paired-replicate-groups`) refuses a series that contains them
  (`ingested_rows_not_storable`), because its members reference per-attempt metric records.
- A retry ingests only if the earlier attempt ingested nothing, because only a succeeded
  attempt ingests and two stages of one campaign over the same cells are refused as duplicate
  seeds.
