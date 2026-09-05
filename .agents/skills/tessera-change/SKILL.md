---
name: tessera-change
description: Implement and test changes to the Tessera repository. Use for control-plane, desktop, worker, schema, or developer-workflow implementation; not for running a research investigation.
metadata:
  project: tessera
  scope: repository-development
---

# Implement a Tessera change

Read `AGENTS.md`, `CONTRIBUTING.md`, and the relevant DESIGN.md contract from the
repository root. Check the branch and existing edits. Agree on one observable
behavior and its failure cases; do not treat the design roadmap as authorization
to build unrelated infrastructure.

Follow the affected boundary: API admits commands; storage owns atomic state,
event and idempotency writes; the future supervisor controls external execution.
Tenant identity comes from authenticated credentials, never a request's claimed
organization. Retries cannot create new authority or erase real costs.

Implement with tests using temporary state and fake provider effects. Distinguish
implemented guarantees from planned behavior in docs. Run the contributing checks,
inspect the diff for state/secrets and accidental scope expansion, then report
changed behavior, test evidence, and limitations. Commit/push/open a PR only when
the user's request includes that workflow; never infer permission to merge/deploy.
