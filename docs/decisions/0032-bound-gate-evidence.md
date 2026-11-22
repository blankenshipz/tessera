# 0032 — Gate verdicts bound to stored, split-locked, multi-seed evidence

## Status

Accepted implementation direction. The integrator may renumber this note and
migration v58 when merging.

## Decision

Add an opt-in, version-2 evaluation protocol whose gates are satisfied only by
platform-computed replicate aggregates. The chain is:

- server-digested split locks with recorded within-lock and cross-set leakage
  audits;
- pre-dispatch binding of admitted attempts to a lock;
- typed per-seed metric records whose value, source and sample count the server
  reads from a stored measurement;
- replicate groups whose members the server selects and whose mean, SD and 95%
  CI it computes.

Measurement provenance becomes a server-set `source` column. Outcome ids and
condition accounting are declared by each plan (the platform holds no
research-program catalog), and a research-program entity reports read-only spend
from existing budget ledger records.

The human "go" and every existing fence are unchanged. A failed or unrecorded
seed is listed, never imputed. Missing or leaky evidence evaluates inconclusive,
which blocks go.

New plans must declare a version-2 protocol: proposing a caller-reported
(version 1) or protocol-less plan returns 409. Plans recorded before that rule
stay readable and keep working, with no rows rewritten. They are labelled
`unbound_caller_reported_legacy` and `none_prose_only_legacy` in program context.

## Acceptance and limitations

The new isolated tests cover:

- measurement source and reserved-key rejection;
- split digest, overlap and audit-masking rules;
- binding;
- metric typing;
- aggregation and statistics;
- plan-declared outcomes and the spend roll-up;
- bound-gate evaluation and the go fence.

They do not show that any experiment ran or that any statistic is scientifically
adequate. The bootstrap interval is anti-conservative for few seeds. The cross-set
audit compares exact keys and declared group ids only, not chemical similarity.
See [gate evidence](../gate-evidence.md).
