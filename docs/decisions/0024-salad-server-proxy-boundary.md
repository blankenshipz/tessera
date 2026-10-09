# 0024 — Salad management authority stays in a trusted server proxy

Status: accepted for **offline implementation only**. Live activation is blocked.

## Context

Decision 0023 separates desktop control from remote execution but intentionally
provides only a disabled Salad seam. A Salad management key may have broad account
authority. A secret reference, provider 202 acknowledgement, credit balance or
signed monitor assignment cannot prove isolation, termination or a hard spend cap.

## Decision

Keep the management key in a trusted proxy process outside desktop, agent and
workload trust domains. Server configuration contains an opaque reference and an
exact version, never an API key. A resolver interface separates metadata freshness,
version, purpose, expiry and revocation from transient owned secret buffers. The
only resolving implementation shipped is a fixed synthetic marker resolver. AI
Gateway credentials are a different purpose and are rejected by this boundary.

There is no generic HTTP proxy. Internal operations form a closed vocabulary;
requests contain route/effect IDs, not account keys, endpoints, methods, paths,
headers, commands or provider identity overrides. Only the fixed management origin
is valid. Redirects, ambient proxy settings, retries and live activation cannot be
enabled by configuration. `SaladProxy.request` returns a typed blocked result
before parsing input, resolving secrets or touching a transport.

An explicitly fake server composition tests admission against retained control
authority. It reuses the route ledger rather than creating an independent grant:
current human plan/source-use/execution reviews, exact tenant/project/target,
approved capabilities, package/attempt/lease/epoch, executable/image/platform and
limits, fresh preflight and unique operation-bound group. Each effect consumes a
durable nonce after committed intent and before resolving the fake marker. It
cannot be replayed following timeout, restart or credential rotation. Stop-only
authority survives revoked execution authority and quota loss, but not loss of
the authenticated dispatcher or exact group identity.

Provider observations remain immutable synthetic receipts. Stop/delete 202 means
pending; timeout/outage/malformed response means unknown. Allocation changes fence
continuation. Logs/events are bounded hash references, never trusted instructions
or retained raw text. Custody requires the existing same-operation artifact
manifest/byte workflow. Cost and usage remain unknown, not zero. Kubernetes target
routing, capability broker, collector and monitor activation guards are unchanged.

## Consequences and non-goals

This is **not a provider isolation witness and does not make Salad a trusted
autonomous sandbox**. No live SDK, network transport, deployed secret backend,
Salad project creation, provisioning, gateway, queue, scaling, SSH/exec or
interactive session is implemented. Fake dependency injection is not activation.

The current route package is still the provider-disabled Kubernetes-derived
contract. The additional Salad declaration does not turn its network-none claim
into Salad enforcement. A separately reviewed native Salad runtime package, wire
encoder, deployment and independent monitor/termination/cost/artifact conformance
are required before any live activation implementation can be considered.

No schema migration is needed: claims and receipts append to existing tenant-scoped
events. Never discard those events or create replacement groups to repair an
uncertain create. See the [deployment and incident runbook](../salad-proxy.md).
