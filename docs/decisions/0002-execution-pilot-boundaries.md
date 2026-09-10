# 0002: Execution pilot boundaries

Status: implementation decisions; acceptance remains in progress.

Priority redirect: the next milestone is a thin, laptop-independent vertical
slice—goal → bounded experiment design → remote execution → linked evidence →
human decision—using a public/simple CPU baseline and one intervention. Local CPU
remains the primary development target while one remote target is validated in
parallel. Succeeded, failed, cancelled, and inconclusive outcomes must remain
visible, and a proposed 25% total-effort reduction is a hypothesis to measure,
not evidence. Freeze broad provisioning/cloud, skill, and desktop expansion until
this slice has accepted evidence.

MLflow, Databricks, Spark, and similar products are optional integrations, not
the authority for Tessera's experiments, attempts, operations, evidence, or human
conclusions. Sessions, experiments, attempts, external operations,
artifacts/measurements, and conclusions have independent lifetimes; no shorter
lifetime may imply completion or validity of a longer one.

Keep Python/FastAPI and SQLite as the authoritative local control plane. Use
explicit relational migrations and transactional operation journals rather than
introducing a workflow engine. Native service lifecycle is independent of the
desktop; Compose remains a developer convenience. Network operation requires
explicit TLS trust and authenticated identities, not a public bind toggle.

The first agent harness is Codex App Server over local stdio, pinned and tested
against installed CLI 0.153.4. Use its stable initialize/thread/turn/interrupt and
approval protocol. Disable experimental APIs, uncontrolled child delegation,
external process-control hosts, and unapproved connectors. Never enable bypass
flags. Subscription auth stays in owner-bound harness custody; no copied human
credential enters experiments. Unknown usage remains unknown, not zero.

Codex is not the authority for Tessera grants or cost caps. The supervisor owns
audience, expiry, epoch, capability checks, operation intent, disconnect deadline,
and cancellation reconciliation. A turn interruption does not prove experiment
termination. The CPU adapter must separately confirm stopped process/container
ownership and preserve unresolved effects when confirmation is unavailable.

Local CPU fixtures use immutable inputs and bounded isolated output, no network,
no credentials, explicit resource/time limits and no shell interpolation. Real
research dispatch stays unavailable until sandbox, grant, cancellation and cost
tests pass. A normal subprocess is not a sandbox for untrusted research code.

Use local opaque credential references initially; external secret backends remain
explicit integrations. Personal enrollment precedes scoped human sessions and
worker identities; organization federation remains a separate adapter. No
production secrets vendor or public hosting service is implied by the pilot.

Alternative: treat `codex exec` as an opaque job. Rejected for interactive work
because its exit status cannot represent live steering, tool mediation or
provider termination. App Server still needs Tessera-side enforcement. Rollback
disables new dispatch while retaining journals and approved research history.

Sources: [App Server](https://learn.chatgpt.com/docs/app-server),
[authentication](https://learn.chatgpt.com/docs/auth),
[sandbox boundaries](https://learn.chatgpt.com/docs/agent-approvals-security).
Local feasibility evidence: `codex --version`, `codex app-server --help`, and
`codex exec --help`; no model job or subscription call was made for the probe.

CPU adapter implementation note: Docker creation uses a preinstalled digest-pinned
allowlisted image, one CPU, bounded RAM/scratch/process count, read-only root,
non-root UID, no network, no added capabilities, no host mounts, no automatic
restart or image pull, and bounded local log retention. Durable start intent must
precede `docker start`: Docker can restart an exited container, so inspection
alone is insufficient for concurrent/recovered start delivery. Container log
retention is not yet validated artifact collection; truncation must be disclosed.
See [Docker run options](https://docs.docker.com/reference/cli/docker/container/run/)
and [local logging bounds](https://docs.docker.com/engine/logging/drivers/local/).
