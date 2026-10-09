# Intent-9 local release-candidate evidence (2026-10-08)

This note summarizes one measured `0.1.0rc2` candidate. It is evidence for that
exact candidate, not release approval or proof that subsequent source/documentation
changes are covered. Production, provider, independent-host, device and full research
acceptance remain separate.

## Artifact and source checks

The candidate was built twice with byte-identical outputs:

| Artifact | SHA-256 |
|---|---|
| Source archive | `c19508014775fa23b5bf814cec561b82907b4204f035671ad41de18ed6cc2df0` |
| Python wheel | `7237772c8b7fd1024fb29b40ea9555e4a2c1ed9c41b6fd75b61aefc69aaf876b` |
| Frozen root-source namespace | `95f121751817bee8a1eb7515fef2ffba1b0381af6e84dcbf25c10fdacc0dfc82` |

The bounded package audit checked source/wheel member and license-byte parity and
excluded private paths from the measured inventory. It is not a universal secret
scan, SBOM, signature, verified upstream-provenance record, or legal review.

On this frozen source, Ruff lint/format and diff checks passed. The whole collection
and four complete, disjoint shards reported **4,925 passed, 47 skipped, 67 warnings**,
with no failures or errors. The 47 opt-in cases were not counted as passes by that
gate: 25 require the native-binding inventory/bridge and 22 require the terminal
inventory. Separate receipts provide composite local coverage for those two sets;
they are not a single 47-case invocation and do not establish production owner or
provider facts.

A fresh offline install of this wheel initialized schema 55 and passed four
migration/repeat/rollback/backup/held-restore checks using isolated synthetic state.
Installed origin/resource checks also passed. These fixtures do not establish
production identity or enrollment.

## Installed protocol and local native slice

The exact installed wheel passed one compatibility case across eight MCP/A2A stages
with independently running protocol peers. Generated text, identities and admission
resources were synthetic; this is protocol compatibility, not generated model
reasoning, a production admission, or a full research workflow.

An opt-in local Kubernetes run using the installed candidate completed three native
Jobs: baseline, same-Job supervisor restart, and cancellation. The run recorded 22
accepted lease-renewal events and checked UID-bound inputs, measurements, quarantine,
human-role review/export, and cancellation/absence evidence. The dataset, principals
and result are synthetic; the result is not scientific validation or a real provider
call. The restart occurred during dataset-verification initialization, before the
workload process started; it does not prove restart during training or a crash. The
cancellation was observed while the workload was running and termination/absence
were confirmed, but settlement remains held/unresolved. The tested local cluster is
not independent-host, laptop-off, or physical-device evidence.

## Current boundary

This candidate does not demonstrate genuine planner/critic reasoning, production
owner/provider binding, scientific validity, full human UI review, independent-host
execution, laptop-off operation, or device acceptance. Later source or documentation
changes require a new complete inventory and the applicable candidate-bound gates;
do not reuse these results as if they tested a later candidate. See the
[release checklist](release-checklist.md), [implementation journal](remote-agent-implementation-journal.md),
and [remote-agent install/runbook](remote-agent-platform-install.md) for current
scope and remaining checks.
