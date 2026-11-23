# 0132 — A stuck-loop runner offers findings to a standing loop

## Status

Proposed.

## Context

The control plane records stuck-campaign findings, and `StuckWorkSource` can offer the open ones
to the standing agent loop as candidate work. Nothing runs that loop against the source: the
README says the work source is available and tested but not yet wired into a running loop. Until
something does, findings are visible to a person and to MCP tools, but a loop that is already
running never sees them.

## Decision

1. **A thin shell around `StandingLoop`.** `tessera-stuck-loop` builds a `StandingLoop` with
   `StuckWorkSource` and an offer handler, and adds only process concerns: argument parsing, a
   policy file, a journal and a ledger. It makes no model call and refuses to start when
   `ANTHROPIC_API_KEY` is in the environment (exit 2). It exits 3 when the loop halts.
2. **Disabled by default, three gates.** The strict owner-only JSON policy must set
   `enabled` to true (otherwise the program prints `disabled` and exits 0); the example systemd
   unit starts only when an admission marker file exists and has no `[Install]` section; and a
   kill-switch marker pauses the runner while it exists. A missing or unreadable marker state is
   treated as engaged, so an absent directory pauses rather than runs.
3. **Offers only.** Each tick offers at most one new finding: the handler appends a `stuck_offered` receipt to a 0600 JSONL
   journal. With `ack_on_offer` set (default false) it also acknowledges the finding, using the
   `campaign:cancel` scope the credential already needs for that endpoint. Reading uses
   `campaign:read`. The runner never resumes, cancels, approves, submits or changes settings.
4. **Increases stay human.** Caps (`max_calls_per_hour`, `max_consecutive_failures`, the backoff
   bounds, `source_limit`) and the acknowledgement option live in the policy file a person edits;
   the runner has no path that raises them.
5. **Durable, once per occurrence.** A SQLite ledger and the journal survive restarts. One
   occurrence of a finding is offered once; a recurrence carries a new occurrence count, so it is
   new work.
6. **Fail closed.** When control is down or answers badly, no items are produced and nothing is
   offered.

## Consequences

- The loop surfaces findings in a journal a person can read, but a person still decides and acts;
  with acknowledgement enabled the only change to control is marking a finding seen.
- The handler is the one hook: a later change can swap it for one that decides what a person
  delegated, without changing the gates, the ledger or the source.
- A credential for this runner needs `campaign:read`, plus `campaign:cancel` only when
  acknowledgement is on, so a read-only deployment has no write scope at all.

## Alternatives

- **A model-driven handler.** Rejected for now: it would add spend and a new decision surface
  before the offer path itself has run anywhere.
- **Acknowledge by default.** Rejected: acknowledging hides a finding from other readers, so it is
  an explicit opt-in.
- **Polling inside `serve`.** Rejected: it would tie an agent identity and its credential to the
  control process, and the gates could not be separated from it.

## Open questions

- Whether a receipt per finding is enough noise control for a very large backlog, or the journal
  needs rotation.
- How a person should be told when the runner halts (exit 3) beyond the unit failing.
