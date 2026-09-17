---
name: tessera-change
description: Implement and test changes to the Tessera repository. Use for control-plane, desktop, worker, schema, or developer-workflow implementation; not for running a research investigation.
metadata:
  project: tessera
  scope: repository-development
---

# Implement a Tessera change

Read `AGENTS.md`, CONTRIBUTING.md's applicable workflow/verification sections, and
the affected DESIGN.md contract. Use AGENTS.md's targeted-read and worker handback
rules. Check the branch and existing edits; do not treat the design roadmap as
authorization to build unrelated infrastructure.

Derive user-visible acceptance, failure cases and targeted checks from the request;
ask only about material ambiguity. A worker's acceptance covers its assignment;
the primary retains responsibility for the full authorized task.
Distinguish process liveness, application readiness, visible GUI and completed
workflow; report only what was observed. For launch tasks, specify whether the
process must remain running and how any authorized cleanup is bounded.

Follow the affected boundary: API admits commands; storage owns atomic state,
event and idempotency writes; the supervisor boundary controls external execution.
Tenant identity comes from authenticated credentials, never a request's claimed
organization. Retries cannot create new authority or erase real costs.

Implement runtime changes with default tests using temporary state and fake provider
effects. Also implement explicitly requested live/provider/paid test harnesses outside
default pytest/CI, and execute them when requested with bounded usage and authorized
credential references. Preserve that authorization in handoffs. Missing live targets
or credentials block the run, not independent harness implementation; follow the
opt-in fixture guidance in CONTRIBUTING.md and never substitute fake evidence.
For docs/instruction-only changes, use CONTRIBUTING.md's relevant checks,
not unrelated runtime traces. Distinguish implemented guarantees from planned
behavior in docs. Run the assigned verification gate only with permitted tools;
report unavailable checks rather than claiming success or bypassing restrictions.

Inspect the diff for state/secrets and accidental scope expansion. Stop on accepted
passing evidence; report changed behavior/files, exact checks/results and remaining
limits, distinguishing task acceptance from integration readiness. Follow AGENTS.md
for evidence-backed failure handback. Commit and push each require explicit user
authorization, subject to higher-priority policy; permission for one does not
authorize the other. Open a PR only when requested; never infer permission to
merge/deploy.
