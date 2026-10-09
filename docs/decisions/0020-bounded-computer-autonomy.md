# 0020 — Bounded computer autonomy is not authority

Status: accepted for an API-first, provider-disabled synthetic slice.

## Context and decision

Computer-use agents commonly alternate observation, planning, tool actions and
feedback, with checkpoints for human intervention. We adopt that interaction
pattern, not a claim about any named product's safeguards or capabilities. We do
not copy unrestricted host desktop, terminal, browser, keychain or login access.

An agent may inspect context already authorized by Tessera's project APIs, author
and revise task/environment/data/model acquisition plans, propose capabilities,
define checkpoints and success/stop criteria, and report progress. Untrusted
web/data/package content and agent suggestions are never authority or scientific
truth. A computer sandbox is disposable execution-plane state, not the desktop
client, control service, a host workspace, or a source of human credentials.

### Profiles and escalation

* **observation-only** (default): context and planning; no effect tools.
* **public-prep**: anonymous, hash-pinned public data/model downloads and supported
  hook-free locked package preparation, inside a reviewed capability envelope.
* **experiment-ready**: preparation plus an explicit scientific-execution handoff;
  it does not itself authorize scientific execution.

Default deny applies to tools, destinations/ports, registries, paths, credentials,
image, resource use and effects. New domains or capability expansion require a
new human-reviewed plan/capability envelope. Credentials, private/paid sources,
persistent writes, external side effects, scientific execution and publication or
decision actions always checkpoint for separate human authority. This slice
records those requests but cannot execute them, even after session review.

An authenticated human may explicitly configure `public_auto` in the immutable
session plan. It applies only to the exact already human-approved capability
declaration, anonymous public sources, at most 8 actions/16 MiB aggregate and
900 seconds. It cannot approve a capability policy, issue a grant, add a domain,
or turn on a real backend. Otherwise each preparation action requires a human
checkpoint resume naming that exact declared action. Approval is attributed to
the human policy/configuration, never to the agent's recommendation.

### Frozen binding and limits

The session binds tenant/project/task plan revision and digest, capability policy
and declaration digest (including exact image/environment/roots/registries), an
immutable autonomy-plan digest, disposable workspace identity, tool allowlist,
CPU/memory/PID/scratch/output/inode limits and absolute autonomy duration. Bounds
are declarations, not runtime attestations. Each action binds the existing signed
package digest and operation, worker lease/epoch, and a fresh server nonce. Package
and operation identities cannot be reused in another session. Single-flight only;
no shell, arbitrary argv, browser automation, credentials, host paths or uploads.

Maximum autonomy is 15 minutes, no automatic extension, at most 8 actions and 128
state transitions. Five-second session leases fence broker steps. They are a
cooperative synthetic control fence, **not** an independent kill mechanism. Live
composition must arm the existing independent watchdog/verified sandbox before
release and bind these same limits. Budget is zero authorized paid spend;
billing/usage remain unknown (`null`), not asserted free. Any unknown exposure
blocks paid work. There are no providers/model inference or retries in this slice.

Stop on cancellation, lease/deadline loss, stale/revoked authority, unknown effect,
failed receipt, scope expansion, no progress, exhausted action/resource budget,
achieved criteria, or human direction. Receipt success never evaluates science.

### Durability and lifecycle

Proposed → ready after exact human review → running → ready after a reconciled
synthetic receipt; checkpointed/suspended → ready only with current authority and
explicit human resume. Agent progress/checkpoints are durable. Revisions preserve
old snapshots and invalidate review. Cancel fences immediately; an in-flight
action remains `unknown`, with cancellation requested separately. Crash/lost reply,
watchdog expiry and unknown termination cannot become ready through resume or a
new nonce. Reconcile original journal evidence only. Deadline/lease expiry is
also computed on read, so a dead client cannot leave a green running projection.
Restart reads the same intent; it does not replay. A checkpoint is logical method
state, not a saved process image. Recovery of unknown work is inspection-only in
this slice. Confirmed stop does not erase an unknown execution outcome.

Use the existing transactional event/idempotency journal for bounded session
snapshots; no migration or second authority database. Existing capability policy,
grant/lease, package issuance, claims and receipt custody remain authoritative.
Every session transition is attributed and revision fenced. Worker starts require
an issued synthetic package and fresh operation authority. Existing broker checks
add a session fence for bound operations; unbound legacy operations are unchanged.
Receipt projections derive from existing broker reconciliation, not caller-supplied
success flags. They retain hashes, provenance, package/operation/nonce, inventory,
termination and unknown cost. stdout/stderr hashes are explicitly absent when the
broker did not capture streams; do not fabricate empty-stream observations.

### Evidence, UI and recovery

API/desktop state shows plan, pending review, exact capability declaration,
blocked reasons, checkpoint question, state/revision/deadline, action identities
and receipts. Desktop is a narrow task-card projection, not a GUI redesign or a
privileged terminal. Main-process credentials never enter the renderer/workload.
Held restores are permanently read-only. Control backup retains session events;
worker journals and their bytes must be retained separately. MixFM source/license,
dataset-ingestion, evaluation, result acceptance and publication decisions remain
separate existing workflows. Salad stays deferred.

## Acceptance and live gate

Offline tests must exercise allowed synthetic actions, deny/escalation, exact
identity, stale/revoked authority, checkpoint/resume, unknown/lost reply, restart,
tenant isolation, path/secret/host rejection, receipt/idempotency/audit rollback,
held recovery and unchanged provider-disabled defaults. The opt-in demo uses only
fake broker bytes; no Docker, real downloads/install/model/provider/GPU effects.

Live activation still requires authenticated isolated-package channels, a reviewed
image, independently proven filesystem/PID/network isolation, external watchdog,
exact target/nonce conformance and receipt custody. The local fixture's latest
network witness failure must be investigated without relaxing guards. Kubernetes
collector/witness component tests are not deployed enforcement evidence. This
decision does not authorize any live activation run.
