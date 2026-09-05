---
name: tessera-review
description: Review a Tessera code change or pull request for correctness, authority boundaries, durable research records, and missing tests. Use for review requests, not implementation or scientific peer review.
metadata:
  project: tessera
  scope: repository-development
---

# Review a Tessera change

Read `AGENTS.md` and the changed files; consult only the affected DESIGN.md
contracts. Review the actual diff and tests, not the author's completion summary.
Do not edit files or post external review comments unless requested.

Trace the affected path from authenticated actor through tenant lookup,
revision/idempotency checks, transaction, event and response. For execution changes,
check fencing, uncertain provider effects, cancellation and cost reconciliation.
For labbook changes, check source support, full in-scope accounting, historical
corrections and permission-filtered retrieval. A plan approval is not an execution
receipt; a second run is not automatically independent replication.

Reproduce a suspected defect with a bounded local check where practical. Report
only actionable findings with severity, file/line, failure scenario, and missing
test. Separate verified defects from design questions. If no defect is found,
say so and identify the validation limits; do not invent findings for coverage.
