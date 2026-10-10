# Tessera documentation

Tessera is a developer alpha for local evaluation. Start with the current guides
below; protocol specifications and opt-in fixture runbooks are references, not
claims of a supported production deployment.

## Use Tessera

- [Quickstart](quickstart.md): install, connect, record a design, review and export.
- [Capabilities and limits](implemented-capabilities.md): what exists and what
  remains unverified.
- [Local API workflow](local-api-workflow.md): separate approval, admission,
  execution, custody release and scientific review.
- [Tabular starter](tabular-starter.md): bounded public-data baseline/intervention.
- [Research records](research-human-records.md), [dataset snapshots](dataset-snapshots.md)
  and [workspace adoption](workspace-adoption.md): record provenance without
  mistaking metadata for transferred or validated data.

## Operate a service

- [Installation](installation.md): Python distribution, desktop and separate services.
- [Operator setup](runtime-operator-setup.md): private state, upgrades and enrollment.
- [Storage](project-artifact-storage.md), [quarantine](artifact-quarantine.md) and
  [backup/recovery](recovery.md): custody, retention and permanently held restores.
- [Runtime setup](runtime-setup.md) and [governed agents](remote-agent-platform-install.md):
  explicit identity, policy, financial references and revocation.
- [Agent Sandbox](agent-sandbox.md): direct one-shot adapter and live prerequisites.
- [Kubernetes deployment](../deploy/kubernetes/README.md),
  [Astra operator setup](astra-operator-setup.md) and [connection tunnel](connection-tunnel.md):
  operator-owned targets; no automatic infrastructure authorization.
- [Privacy](privacy.md), [support](support.md) and [security reporting](../SECURITY.md).

## Develop and verify

- [Contributing](../CONTRIBUTING.md): setup, default checks and opt-in fixtures.
- [Design](../DESIGN.md): authoritative product, authority and safety contracts.
- [Compatibility](compatibility-matrix.md): runtime requirements and protocol pins.
- [Records API](records-api.md), [agent authority](agent-authority-interface.md),
  [provenance](agent-provenance-interface.md), [MCP workflow](mcp-full-workflow.md),
  [A2A peer](a2a-peer.md) and [runtime bindings](runtime-bindings-interface.md).
- [Admission](execution-admission.md), [capability policy](agent-capability-policy.md),
  [runtime admission v2](runtime-admission-v2.md) and [guard evidence](guard-phase-evidence.md).
- [Release checklist](release-checklist.md), [packaging](release-candidate.md),
  [source gates](release-candidate-gates.md) and [protocol checks](protocol-compatibility.md).
- [Architecture decisions](decisions/0001-control-plane-foundation.md): numbered
  decisions in `decisions/` preserve implementation rationale.

Other pages in this directory are focused interface or fixture references. Dated
implementation journals, candidate reports and obsolete reviews are retained in
Git history rather than duplicated in the current guide. Private receipts and
runtime state are not public documentation.
