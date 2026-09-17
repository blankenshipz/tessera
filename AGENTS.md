# Working on Tessera

Tessera separates the desktop client, control plane, and execution plane. Read
the relevant contract in DESIGN.md before changing behavior; README.md describes
what is implemented. CONTRIBUTING.md owns setup and the development workflow.

## Repository map

- `src/tessera_control/`: local Python control service, persistence, and CLI.
- `tests/`: default isolated API, storage, and bootstrap tests; live/provider jobs belong in separate opt-in harnesses.
- `.agents/skills/`: instructions for agents developing this repository, **not**
  researcher-authored runtime skills or execution permissions.
- `docs/decisions/`: decisions that narrow or implement the design.

## Workflow

When commits are explicitly requested, use logical, tested commits on `main`.
Commit and push each require explicit user authorization; permission for one does
not authorize the other. PRs remain paused unless requested. Never infer permission
to merge, change repository visibility, or deploy; higher-priority policy still applies.
Preserve unrelated work. Do not commit runtime state, credentials, or local auth
files. Load `tessera-change` for implementation and `tessera-review` for a requested
review through the skill tool; if unavailable, read the matching
`.agents/skills/<name>/SKILL.md`. Do not load unrelated skills.

Keep evidence honest: a recorded approval is not proof that an experiment ran.
Existing provider-disabled paths must remain truthful. Unsupported capabilities
are implementation gaps, not permanent prohibitions: implement requested features
through explicit contract changes and tests while preserving authorization checks.
Never fake provider status or execution receipts to make a demo look complete. Keep tenant checks at data access and
state/event/idempotency writes in one transaction.

Derive user-visible acceptance and targeted checks from the request and contracts;
ask only about material ambiguity. Follow CONTRIBUTING.md's verification gates;
task acceptance is not full integration acceptance. Workers stop at their assigned
acceptance; the primary continues remaining authorized scope after checkpoints or
handbacks, without requiring another "continue". Stop when the requested scope is
complete or a real blocker needs user input, without automatic extra review.
Report exact checks/results, files changed (or none), and remaining limitations.

Requested live/provider/paid tests are permitted development work. Write the harness
when requested; execute a bounded live run when execution is requested. Keep it out
of default pytest/CI, minimize calls/tokens/retries, and use existing authorized
credentials through runtime references without exposing secrets. Carry the user's
authorization into delegations; do not impose a blanket no-paid-tests rule or ask
again for approved effects. Coding-agent subscription limits govern agent inference,
not the application's provider integrations. Missing live prerequisites block the
run, not independent implementation. Preserve synthetic/provider-disabled paths;
add explicit live behavior rather than relabeling fake evidence or bypassing guards.

## Routing and worker boundaries

Honor named-model requests through actual configured agent/model selection, not
an "act as" prompt. Attribute decisions to the recorded agent/model; disclose
unverified provenance or an unavailable requested model instead of claiming it ran.
Repository instructions never override runtime permissions or configure models.

Delegate only when permitted and independent work benefits the task. Give each
worker a bounded objective, owned files, non-goals, acceptance checks and stop
conditions; parallel workers own disjoint files. Match required tools to the
selected role's available capabilities. Read-only roles inspect and report, not
implement; checks require both assignment permission and available tools. Missing
shell capability is not a missing executable and does not justify installation.

Workers never delegate. Repair an in-scope objective failure, then hand back after
repeated objective failure or a concrete high-risk issue. An unavailable required
capability is an immediate blocker, not a reason to retry denied operations. Return
the blocker, exact evidence, attempted repair and needed capability or decision to
the parent (or user when working directly); do not escalate vague uncertainty.

Search for relevant sections, then read targeted ranges within advertised tool
limits. Never bypass denied broad reads with shell or exhaustive slices. If needed
context remains inaccessible, hand back the file, concrete question and tool error.
