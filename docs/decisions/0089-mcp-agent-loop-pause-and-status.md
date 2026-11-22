# 0089 — MCP tools to see and pause the standing agent loop

## Status

Proposed. Adds two opt-in MCP tools; the default tool catalog is unchanged.

## Context

ADR 0085 ([standing agent loop](0085-standing-agent-loop-and-auto-created-research-jobs.md))
gives the on-platform loop a kill switch: a marker file the loop treats as engaged when it exists,
cannot be read, or has no parent directory. Its call ledger records halted state, consecutive
failures, backoff and calls whose outcome is uncertain. A client working through MCP has no way to
see that state or to stop the loop short of shell access.

## Decision

With `--agent-loop-marker PATH` (and optionally `--agent-loop-ledger PATH`, which requires the
marker flag) the MCP server registers two tools. Without the marker flag nothing is registered.

- `tessera_agent_loop_status` (read-only) reports whether the kill switch is engaged, using
  the loop's own `KillSwitch` so the fail-closed rule is not duplicated. It adds `halted`,
  `consecutive_failures`, `backoff_until` and up to 50 uncertain item ids from the ledger, opened
  read-only (`mode=ro`: never created, written or migrated). With no ledger, or an unreadable one,
  those fields are null. The text says it is the operator's local marker and ledger: not provider
  billing and not proof that a call ran.
- `tessera_pause_agent_loop(reason)` creates the marker exclusively (`O_CREAT|O_EXCL|O_NOFOLLOW`,
  mode 0600) with a small JSON body. If the marker exists it answers `already` and changes
  nothing. It refuses when the parent directory is missing, a symlink, owned by another user or
  group- or world-writable.

There is deliberately no resume tool. A pause is easy to grant and hard to undo by mistake: a
person lifts a pause by deleting the marker file, after looking at the ledger.

## Authority boundaries

The tools touch only the marker path and read the ledger path the operator named. They never
remove or rewrite a marker. Pausing engages the kill switch and nothing else: it cannot cancel
in-flight calls, campaigns, workers or routes, and it grants no approval. The tools do not call
the control API or a provider.

## Consequences

An agent that can reach this server can stop the loop but never restart it. Status is a local
view and says nothing about provider billing or whether a call actually ran. Marker and ledger
paths are operator configuration, not tool arguments.

## Amendment: the pause is also recorded on the platform

The marker stays the fast path and the only thing the loop reads. A pause may now also be recorded
by the control plane: `POST /projects/{project_id}/agent-loop/pause` (body `{"reason"}`, required
`Idempotency-Key`) writes one `agent_loop.paused` event in the same transaction as the idempotency
record. The payload carries the project, the principal and credential ids, the credential kind, the
reason and its sha256, and `scope: "local_kill_switch_request"`. The caller needs a human session or
an agent credential enrolled with the new action `loop:pause` (`--allow-loop-pause` on
`scripts/enroll_local_mcp.py`); workers, agents without the action and other projects or tenants
are refused. `GET /projects/{project_id}/agent-loop/pauses` lists recorded pauses, newest first,
under the same rule. Replaying a key with the same body returns the first record and writes no
second event; the same key with another body is a 409.

The record is a request record. It does not prove the operator's loop saw the marker, and it is not
a model-call receipt. The route changes no loop state, campaign, job or gate, and adds no schema.
