# 0004: Optional pinned OpenCode harness boundary

Status: accepted design decision; adapter integration and synthetic POC pending.

This supersedes the first-harness selection in decision 0002; it does not change
that decision's control-plane, credential, sandbox, or cancellation boundaries.

## Decision

Treat OpenCode as an optional, version-pinned harness implementation behind the
Tessera harness contract. Tessera remains authoritative for plans, approvals,
budgets, leases/epochs, credentials/access, artifacts, provider operations,
result acceptance, and the labbook. OpenCode sessions and jobs remain
independently managed. Node setup/enrollment is an infrastructure operation
separate from research control.

The adapter translates bounded Tessera commands into OpenCode's supported
interface and attributes every upstream event to the tenant, task, operation,
epoch, and OpenCode session/job. It must handle reconnect by cursor, duplicate
events, and cancellation without treating an abort acknowledgement as provider
termination. Unknown attribution, incompatible versions, expired authority, and
unconfirmed cancellation fail closed or remain visibly unresolved.

OpenCode permission prompts, tools, MCP, skills, session events, provider auth,
and UI are integration capabilities, not Tessera authority boundaries. Current
upstream capabilities and links are listed in DESIGN.md and remain subject to
compatibility testing.

## Alternatives

- **Build a Tessera-native harness:** rejected for the first slice because it
  duplicates a capable interactive runtime and delays testing the contract.
- **Treat OpenCode as an opaque job or authority:** rejected because it loses
  interactive/event semantics and would incorrectly delegate authorization,
  cost, credentials, or result ownership.
- **Require OpenCode:** rejected; the contract must preserve harness portability
  and allow Tessera control without coupling the product to one upstream.

The next evidence is a synthetic, no-provider-call POC covering start, event
translation, attribution, reconnect, expiry, and cancellation failure cases.
Passing it does not complete the pilot.
