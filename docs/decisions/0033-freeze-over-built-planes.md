# 0033 — Freeze over-built planes until real experiments pass Outcome 1's gate

## Status

Accepted (2026-11-18, owner direction to act on the Fable concept review §3.3).
Docs only: no code is deleted or disabled by this record.

## Decision

Until Tessera has run real experiments through Outcome 1's gate, the following
planes are **frozen**: bug and security fixes only, no new features and no
extension.

- Salad guardian / custody / IMDS-JWT / peer-credential multi-tenant stack
  (about 5.7k lines). **Exception:** runtime spend-safety fixes from review §4.2
  (off-host guardian, provider-side stop, estimate-only cost labelling) are allowed.
- Capability witness / broker / collector (about 9.4k lines), Kubernetes/minikube
  execution plumbing (about 10.8k) and ordinary-docker (about 3.6k). The
  `docker-local-cpu-v1` CPU path must keep working; Kubernetes remains an optional
  lab profile.
- Astra coordinator (about 4k), OpenCode harness (about 3k), A2A peer (about 1.9k)
  and agent-sandbox/provenance projections (about 4k).

Not frozen: `interaction.py`, which hosts `decision_requests` used by the gate machine.

## Consequences

Work concentrates on the research loop (records, gate evidence, agent-driven CPU
runs) rather than on safety and operations layers that have only been validated
against fakes. Frozen code stays in the tree and its tests stay green.

## Revisit when

- a second GPU vendor is actually needed (do the review §4.1 `adapter_kind`
  registry over `ExecutionAdapter` first), or
- Outcome 1 Gate A is decided.
