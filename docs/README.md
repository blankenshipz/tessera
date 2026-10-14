# Tessera documentation

Tessera helps computational researchers keep a durable account of a question,
bounded experiment, evidence and human decision. It is a developer alpha for local
evaluation, not yet an integrated agent-assisted research workflow. Start with the
reader paths below; protocol specifications and opt-in fixture runbooks are
references, not claims of a supported production deployment.

## Plan, run, monitor, review and revisit research

- **Start and plan:** [Quickstart](quickstart.md) for local setup and versioned
  designs; [tabular starter](tabular-starter.md) for a concrete synthetic-data
  question, baseline, intervention and comparison criteria.
- **Run a bounded attempt:** [Local API workflow](local-api-workflow.md) for the
  separate operator handoff, approval, grant, admission and execution. Recording
  a plan in the desktop does not start a job.
- **Monitor and stop:** [Execution monitor](execution-monitor.md) and
  [desktop execution procedure](../desktop/EXECUTION.md) for attempt/output
  inspection and stop boundaries; requested cancellation is not confirmed termination.
- **Review and decide:** [Research records](research-human-records.md) for linked
  measurements, conclusions and corrections; the quickstart separates quarantine
  retention, retrieval release and export from scientific acceptance.
- **Revisit the investigation:** reconnect to durable records without replaying
  commands; use [dataset snapshots](dataset-snapshots.md) and
  [workspace adoption](workspace-adoption.md) to retain provenance without
  mistaking metadata for transferred or validated data.
- **Check the boundary:** [Capabilities and limits](implemented-capabilities.md)
  separates implemented components, demonstrated journeys and planned capabilities.

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
