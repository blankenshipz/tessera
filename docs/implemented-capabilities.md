# Capabilities and limits

Tessera is a **single-owner developer alpha**, not a hosted or production platform.
The [quickstart](quickstart.md) is the supported starting point for local evaluation.
The [design](../DESIGN.md) describes the wider target; it is not an inventory of
completed features.

## Current workflow

| Boundary | Implemented | Important limit |
|---|---|---|
| Local control | Authenticated API, tenant-scoped records, revisions, transactional events and idempotency | Bootstrap-owner bearer possession is not federated human login; owner rotation/account recovery is not provided |
| Research ledger | Versioned designs, studies, variants, linked measurements, conclusions/corrections and bounded exports | A design or human-entered result is not an execution receipt or scientific validation |
| Desktop | Connect, project/design/study records, result inspection, quarantine review, separate custody release and export | Execution needs an API/operator handoff; native picker/save acceptance is distinct from renderer simulation |
| Execution | Separately approved grants, target-bound admission, ordinary worker dispatch, durable operation journals and collection | Owned Docker Desktop CPU journeys are laptop-local; they do not prove independent-host or laptop-off support |
| Artifacts | Bounded intake, retained project objects, quarantine, explicit retrieval release and exact-byte access | Release does not classify data, approve publication or accept science; retention is not automatic deletion |
| Recovery | Consistent SQLite/object backups and verified restore to permanently held history | Restore does not fence the original writer/jobs and cannot promote a copy to active failover |
| Agents | Scoped identities, authority requests/grants, MCP stdio, A2A reference exchange, provenance and runtime binding checks | A catalog entry, peer response or signed binding is not proof of integrated planner/critic reasoning |
| Agent Sandbox | Opt-in direct adapter targeting upstream v1.0.6 one-shot network-none Sandboxes | Component-tested; live controller/CRD/isolation acceptance, warm pools and reasoning/input credential delivery are not established |

The [tabular starter](tabular-starter.md) offers a bounded baseline/intervention
workflow. Local CPU and Kubernetes observations used synthetic data/identities;
successful execution did not establish scientific validity. Screenshots in the
[repository overview](../README.md) use a real backend but simulated platform
selection/save interactions. Native OS dialogs remain a separate acceptance gate.

## Design status matrix

| Design area | Current boundary | Remaining acceptance |
|---|---|---|
| Start/adopt work and research records | Local API and desktop records; explicit source/data declarations | Complete representative research pilot and measured researcher effort |
| Approve and execute | Exact revisions, grants, reservation/admission and worker custody | Selected always-on host, real isolation/capacity, restart/cancel and laptop-off journey |
| Discuss and steer | Deterministic provider-disabled paths; proposal and review interfaces | Genuine supervised reasoning and independent critic integration |
| Identity and transport | Local owner/scoped agents, stdio MCP and reference-only A2A | Production owner/private-resource integration, organization sessions/revocation and supported remote deployment |
| Review and recover | Human review, correction lineage, export and held restore | Supported recovery objectives, retention operations and target-specific incident drills |
| Release and operations | Source packaging and reproducible-check tooling | Signed artifact/update verification, independent security review and production service commitments |

## Safety boundaries

- Approval, an execution grant, admission, credential delivery, artifact custody,
  cost settlement and scientific acceptance are separate decisions. An agent may
  propose; it cannot approve its own work or broaden authority after a lost reply.
- Provider activation is disabled by default. Deterministic/provider-disabled
  paths must report that honestly. Internal integer-cent reservations are not a
  provider spending cap; unknown usage/termination stays unknown. No billing-mode
  or API-key fallback follows from a grant.
- Strict runtime admission v1 remains the default. v2 needs explicit human opt-in
  and required local guard/quota/runtime proof. A missing trusted runtime adapter
  denies admission; a generated profile or self-attestation cannot replace it.
- Request/artifact caps do not bound aggregate database, object, journal or backup
  capacity. Operators must monitor private storage; see [local safeguards](launch-readiness.md).
- Source changes are not evidence that an installation was upgraded. Observe the
  installed target from its migration module and state schema from a backup
  manifest; use the matching
  release for backup/restore and explicit migration, never edit schema metadata.

See [compatibility](compatibility-matrix.md), [operator setup](runtime-operator-setup.md),
[recovery](recovery.md) and the [release checklist](release-checklist.md). Local
component tests, installed-package checks, live target runs and native-device
checks are different evidence; none alone closes product acceptance.
