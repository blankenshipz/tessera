# Working on Tessera

Tessera separates the desktop client, control plane, and execution plane. Read
the relevant contract in DESIGN.md before changing behavior; README.md describes
what is implemented. CONTRIBUTING.md owns setup and the pull-request workflow.

## Repository map

- `src/tessera_control/`: local Python control service, persistence, and CLI.
- `tests/`: isolated API, storage, and bootstrap tests; no paid/provider jobs.
- `.agents/skills/`: instructions for agents developing this repository, **not**
  researcher-authored runtime skills or execution permissions.
- `docs/decisions/`: decisions that narrow or implement the design.

## Workflow

Use a feature branch and open a PR. Do not push implementation directly to main,
merge a PR, change repository visibility, or deploy unless the user asks.
Preserve unrelated work. Do not commit runtime state, credentials, or local auth
files. Follow `.agents/skills/tessera-change/SKILL.md` for implementation and
`.agents/skills/tessera-review/SKILL.md` for a requested review.

Keep the first slice honest: approvals record human decisions; they do not yet
authorize or launch real experiment jobs. Never fake provider status or execution
receipts to make a demo look complete. Keep tenant checks at data access and
state/event/idempotency writes in one transaction.

Run `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, and
`.venv/bin/pytest`. Report what was tested and what remains unimplemented. Use
bounded delegation only when independent work benefits the task; workers own
disjoint files and must not recursively delegate without an explicit assignment.
