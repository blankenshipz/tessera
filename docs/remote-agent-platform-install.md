# Governed agent setup

This operator guide configures ordinary Tessera entrypoints. It does not establish
production identity, integrated planner/critic reasoning or laptop-independent
execution. See [capabilities](implemented-capabilities.md) and the
[design contract](../DESIGN.md#53-governed-agent-identity-reasoning-and-peer-transports).

## Install and inspect

From a trusted checkout:

```sh
uv sync --locked --extra mcp --extra a2a
.venv/bin/tessera --help
.venv/bin/tessera-mcp --help
.venv/bin/tessera-reasoning --help
.venv/bin/tessera-a2a-peer --help
```

For an installed environment, build/install the reviewed Python artifact with
`[mcp,a2a]` extras as described in [installation](installation.md). Desktop is a
separate Node/Electron package. Linux service templates and the `LoadCredential`
mapper are in [deployment](../deploy/remote-agent-platform/README.md); templates
do not enroll identities, start OpenCode or authorize inference.

## Enroll and connect

1. Select one authoritative control service and private storage home. Remote
   access requires verified HTTPS and independent supervision. Docker Desktop
   on a laptop is not an always-on host.
2. A human operator enrolls a distinct agent identity for the exact project and
   destination through the authenticated control workflow. Store its credential
   in a current-user-owned regular file with no group/other permissions. Never
   give an agent an owner header, worker token, provider secret or another agent's
   identity. No token values belong in arguments, environment variables, prompts,
   configuration, logs or the repository.
3. Configure the MCP-capable client to launch installed `tessera-mcp` as a local
   **stdio child** using that client's documented configuration format. Supply
   references only:

   | Environment key | Value |
   |---|---|
   | `TESSERA_CONTROL_URL` | Control origin; remote origins use HTTPS |
   | `TESSERA_AGENT_TOKEN_FILE` | Private file path for this agent credential |
   | `TESSERA_PROJECT_ID` | Credential-scoped project |
   | `TESSERA_AGENT_DESTINATION` | Enrolled destination |
   | `TESSERA_CONTROL_CA_FILE` | Optional public CA bundle file |

   The adapter reopens the credential file for each request and rejects symlinks
   and unsafe ownership/permissions. Changing an identifier grants no authority.
   MCP is session-based protocol 2025-11-25 with SDK 1.26.0, not an HTTP listener.
4. Begin with identity and disclosed-context reads. Verify scope, destination,
   expiry and backend availability; a catalog entry is not deployment proof.
   Follow the typed [MCP workflow](mcp-full-workflow.md) and
   [authority contract](agent-authority-interface.md).
5. Agents propose work/request bounds; a separately authenticated human issues
   the exact grant. Consume only its bound operations. Preserve request bodies
   and idempotency keys across reconnect; uncertainty requires reconciliation,
   not replacement dispatch or expanded authority.

## A2A and reasoning

An operator-configured reference peer uses:

```sh
tessera-a2a-peer --config /private/peer.json --port 8771
```

Keep its database/config private and credential-to-identity map owner-bound.
Use only an enrolled agent control credential. The [A2A peer contract](a2a-peer.md)
defines the closed profile, TLS and protocol 1.0.0 JSON-RPC subset. Reference
exchange is not a reasoning critic, canonical notebook writer or grant.

The [reasoning runtime](remote-reasoning-runtime.md), [generated outputs](a2a-generated-output.md)
and [provenance interface](agent-provenance-interface.md) define further boundaries.
Real planner/critic inference and integrated research acceptance remain unverified.
Do not reuse a coding-agent session as a research runtime or infer provider
entitlement, billing or cancellation from an agent reply.

## Runtime admission and operations

Use [runtime setup](runtime-setup.md) for exact policy/profile/enrollment,
financial-reference, doctor/current-resolution and revoke/reconcile commands.
Strict v1 is default; v2 requires explicit human opt-in and genuine owner-bound
guard/quota/private-resource and installed linkage proof. Binding generation is
not admission. Missing trusted runtime integration denies; flags, copied
credentials, fixture identities and self-attestation cannot repair that denial.
No OAuth/keychain discovery or credential copying is part of setup.

Revocation and expiry fence future use. Readback-only reconciliation does not
revive authority. Preserve original journals and reconcile original operations
before replacement work; worker renewal and lost-owner limits are in
[installation](installation.md). [Recovery](recovery.md) creates permanently held
history, never active failover.

For opt-in integration testing use the [bounded journey harness](remote-agent-platform-journey.md).
Its reference exchange/local execution assertions are distinct from genuine
reasoning, native dialogs, off-host operation and laptop-off acceptance. Review
exports with their completeness disclosure; denied/unknown provenance must remain
visible. Successful execution is not scientific validation.
