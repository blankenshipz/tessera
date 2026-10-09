# 0022 — Transferable execution-plane monitoring, not a control-host privilege

Status: accepted boundary; implementation is opt-in, closed Docker monitoring and
offline protocol conformance. Production activation remains blocked.

## Decision

Move the *placement* of independent observation/termination authority to the
execution plane. A control service may run on a laptop, remote server or another
cluster without `/proc`, Docker socket, host PID/network namespaces or execution
storage access. A monitor and its guardian run beside the assigned target, outside
the agent and broker lifetime/cgroup. This extends, rather than replaces, decisions
0019–0021 and the provider-neutral execution contracts.

One immutable signed assignment binds authenticated tenant/project, plan/package,
operation, nonce, monitor/worker/authority identities, adapter, exact target/workload
generation, privilege profile, resource/network/output limits, required evidence
and absolute expiry. It grants observation and exact-owned stop/cleanup only:
never create, submit, start, restart, renew authority, arbitrary exec or host access
chosen by a workload. Signed assignment delivery is not approval or global claim.

Use separate assignment/channel/evidence keys with domain-separated HMAC-SHA256,
canonical bounded JSON and nonce-bound challenge/response. Remote transport also
requires TLS 1.3 mutual certificate authentication, CA/hostname validation and a
pinned server certificate. No ambient contexts, owner token, dynamic adapter
imports, proxy fallback or credential discovery. HMAC is authentication among
trusted custodians, **not** asymmetric non-repudiation or hardware attestation.

The monitor's independent monotonic deadline cannot be extended by heartbeats.
Control/broker loss, stale/replayed responses, clock regression, mismatched identity
or missing enforcement evidence fences work and consumes at most one durable stop
intent. A guardian watches its owned child handle, not an arbitrary PID. Only after
confirmed child death and exclusive journal custody may it perform the first
historical exact-owned stop. Uncertain kill/remove replies are never retried.

Retain canonical signed, hash-chained local receipts and allowlisted raw numeric/
identity observations. Reconcile them to a separate control receiver ledger using
authenticated receipt-digest acknowledgements. Historical uploads do not renew
authority; duplicate identical uploads are idempotent, contradictory uploads fail.
Artifact linkage requires independently validated quarantined byte custody, not an
operator-provided success flag. No database migration or fabricated broker receipt.

## Alternatives rejected

* Giving the control plane Docker/host namespace access: couples placement to the
  execution host and leaks broad host authority into the research service.
* An agent-owned watchdog thread: dies/hangs with the agent and cannot establish
  independent liveness, enforcement or termination.
* A default privileged DaemonSet/universal Docker socket agent: excessively broad;
  privilege requirements must be explicit per adapter and measured at the target.
* Inferring isolation from signed configuration or Docker/Kubernetes readback:
  signatures authenticate who observed a value, not whether a limit was enforced.

## Scope and consequences

The Docker adapter is a stop-only attachment to the exact closed `wait` fixture.
It reuses the namespace proof without weakening it, and distinguishes native
procfs/cgroup/filesystem observations from daemon declarations. Kernel/filesystem
exhaustion and nonempty artifact capture are not implemented by this adapter;
those gates remain unknown and stop the fixture. A Docker socket is host-equivalent
authority despite the adapter's narrow API; use an isolated execution host and
explicit opt-in, never mount it into the control plane or workload.

Kubernetes and Salad have typed identity contracts and rejecting implementations,
not enabled providers. A Kubernetes sidecar alone usually cannot attest node CNI,
CRI/cgroup/filesystem behavior. Salad status replies alone cannot prove native
termination, artifact custody or billing. Deployment enrollment, TLS server/service
integration, native sensors, real package issuance/global claims and full enforcing
adapter conformance remain production gates. See the [runbook](../execution-monitor.md).
