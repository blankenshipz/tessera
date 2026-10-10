# Runtime and protocol compatibility

This is a current requirement/reference matrix, not a historical test ledger.
Pins describe a selected contract or fixture; they do not enable dispatch or prove
an installed service is compatible. Check the exact installed artifact and target.

| Component | Requirement or selected protocol | Boundary |
|---|---|---|
| Python control | Python >=3.11; committed `uv.lock` for checkout dependencies | Native setup additionally requires compatible SQLite |
| SQLite | Built-in deterministic and innocuous `json_extract`, with defensive `trusted_schema=OFF` | Incompatible builds fail before state writes; no universal OS/architecture support claim |
| Desktop | Node >=22.12.0, locked Electron/npm dependencies | Separate from the Python wheel; native installation/dialogs require their own checks |
| MCP | Python SDK 1.26.0; session-based protocol 2025-11-25 over stdio | Not remote HTTP MCP or the stateless 2026-07-28 protocol |
| A2A | `a2a-sdk[http-server]==1.2.1`; protocol 1.0.0 JSON-RPC | Reference peer exchange is not generated reasoning or execution authority |
| OpenCode context-only adapter | v1.18.30, source commit `3104c1428ec91f809e5ab86631300de41eb6952e`; `opencode-v1.18.30-context-only` | Runtime `/doc` digest and real-server checks remain separate; see [harness](opencode-harness.md) |
| Legacy OpenCode fixture | `legacy-synthetic-v1`, OpenCode-shaped 1.2.3; canonical fixture `/doc` SHA-256 `b88fd046a0438e416207539e04ea3fd8b9b4e4f17f48c7580cf507fa835b9053` | Synthetic numeric SSE replay, not an installed upstream build |
| Public observer bundles | Closed v2 file-pinned and runtime-v8 source bundles | Preserve source/license/provenance pins; [packaging contract](release-candidate.md) |
| Kubernetes worker | Explicit `kubernetes-job-v1` package/target, namespace/PVC/image/CA and fresh network witness | No ambient kube context, automatic provisioning or general cluster support claim |
| Direct Agent Sandbox | Upstream v1.0.6 one-shot, network-none contract | [Adapter prerequisites and limits](agent-sandbox.md); no live acceptance claimed |
| Development CPU worker | `development-local-cpu-v1`; pinned Bookworm fixture image in [contributing](../CONTRIBUTING.md) | Opt-in local development, not production provider accounting |

## State compatibility

Inspect the installed artifact's migration target and a verified backup manifest's
`schema_version` for the state being protected. The source migration target is
defined by `LATEST_SCHEMA_VERSION` in
`src/tessera_control/migrations.py`, not by a dated documentation receipt. Startup
refuses older state and rejects future schemas. Upgrade only after a verified
private backup; run `tessera migrate` explicitly. Never downgrade the binary
against newer state or change a manifest/schema version to bypass validation.

Restore requires a backup matching the running schema. For an older backup,
use the matching older release to restore, then explicitly migrate the permanently
held copy. See [recovery](recovery.md) for schema allowlisting, credential revocation
and the manual fenced-rebuild boundary.

## Live activation

Real OpenCode/provider/remote profiles need exact build/configuration and schema
pins, verified TLS, identity enrollment, isolation, cost and cancellation evidence
for the selected host. Unit doubles, a NetworkPolicy declaration, an Agent Card,
an abort acknowledgement and `/health/ready` do not satisfy those gates. Follow
[provider activation](provider-activation-and-collaboration.md),
[runtime setup](runtime-setup.md) and [release checks](release-checklist.md).
