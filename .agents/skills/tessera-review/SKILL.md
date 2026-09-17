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
Use AGENTS.md's targeted-read and worker handback rules. Do not edit files or post
external review comments unless requested and permitted by the role.

For runtime/API/storage changes, trace the affected path from authenticated actor
through tenant lookup, revision/idempotency checks, transaction, event and response
where applicable. For execution changes, check fencing, uncertain provider effects,
cancellation and cost reconciliation.
For labbook changes, check source support, full in-scope accounting, historical
corrections and permission-filtered retrieval. A plan approval is not an execution
receipt; a second run is not automatically independent replication.

For docs/instruction-only changes, check consistency, scope, authority, tool
feasibility, model/evidence attribution and relevant verification requirements.
Check skill frontmatter/discovery if affected; mark unrelated runtime traces not
applicable rather than inventing them.

Reproduce a suspected defect with a bounded local check only when the assignment,
role and available tools permit it. Otherwise report the unperformed check and
limitation. Report actionable findings with severity, file/line, failure scenario
and missing test or validation. Separate observed defects from prospective risks
and design questions. If no defect is found, say so and identify validation limits;
do not invent findings for coverage.
