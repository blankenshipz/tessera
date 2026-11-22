# ADR 0065: an operator-enabled MCP approval tool

Status: accepted. Addendum to ADR 0055 (MCP steering tools) and ADR 0038 (campaign runner), which
left approval to the CLI and said no `tessera_mcp` tool approves.

## Context

A client with a person's own session could approve a submitted campaign only through the CLI, so a
steering session had to leave MCP at the one step that starts the work.

## Decision

`tessera-mcp --approval-tool` registers `tessera_approve_campaign`. By itself the flag makes an
approve-only server (no submit, read or cancel tools); `--campaign-tools` adds those.

1. The tool is a thin client of the existing `POST /campaigns/{id}/approve`. The control API still
   decides: only a person's session credential is accepted (an agent credential gets HTTP 403 and
   nothing is written), and the revision and spec digest, the program cap, the campaign budget,
   trust and reference checks and the one-key-per-approval rule run exactly as for the CLI.
2. The caller names the exact spec digest. The tool reads the campaign's current revision, refuses
   without sending anything if the stored digest differs, and uses a fresh idempotency key per call
   unless one is supplied. To replay a lost answer the caller sends the same key and the revision it
   read before. It returns the approval record.
3. It is off by default and no enrollable scope grants it, so an agent credential never registers
   it. It belongs on a separate operator server with its own credential file. The OpenCode harness
   catalog is unchanged.
4. Unlock conditions and pre-approvals (ADR 0044, 0045) are not exposed; they stay on the CLI.

## Consequences

- The approval guarantee is unchanged: a person's credential still approves a digest the person
  named; MCP is only the transport. Anyone who can run the approve-only server with that credential
  can approve, exactly as with the CLI, so the credential file's protection is the control.
- ADR 0055's statement that no tool approves holds for every agent credential and every default
  server.
