# 0034 — Governed sandbox egress and dataset staging into later runs

## Status

Accepted (2026-11-18, owner direction). Phase 1 (local Docker) is implemented with
default tests only; no live network, cluster or provider run establishes it. Phase 2
(Kubernetes Agent Sandbox) and phase 3 (Salad input delivery) are design only.

## Context

Tessera is an agent runtime. Dataset discovery belongs in a sandboxed agent run, and
what the agent fetched must reach a later (GPU) run with provenance. Today every run
is network-none: `ExecutionSpec.network_enabled` is `Literal[False]`, and the
[agent sandbox](0030-direct-agent-sandbox.md) deliberately has "no general network
profile". Nothing turns collected outputs into run inputs, and Salad receives no
input bytes (it only pins `input_snapshot_digest`).

ADR 0033 (freeze over-built planes; on main) freezes the Kubernetes Agent Sandbox,
collector, ordinary-docker and Salad stacks; the `docker-local-cpu-v1` path must
keep working. This record adds the smallest generic capability on unfrozen planes
and specifies, without building, the scoped exceptions the frozen planes need later.

## Decision

### 1. One generic egress profile, frozen in the plan

`ExecutionSpec` gains optional `egress`, an `EgressPolicy`:

- `profile: "allowlist-proxy-v1"` (the only profile);
- `destinations`: 1–32 `Destination(host, port)`, reusing the capability
  `Destination` validator (public FQDN or public IP literal; no `.local`,
  `.internal`, loopback, private, link-local or metadata addresses);
- `max_egress_bytes`: a hard cap on proxied bytes in both directions combined.

Absent `egress` means network-none, the default. The serializer omits a `None`
`egress`, as it already does for `sandbox`, so every existing plan digest is unchanged.
`network_enabled` stays `false`: the workload never gets direct network access.
Egress is part of the plan digest a human approves. An agent may propose it but
cannot approve, issue or widen it, and no new scope or authority route is added.
The runtime package records `network_profile` (`none` or `allowlist-proxy-v1`) and
the frozen policy, so the policy is digest-bound to the operation.

Phase 1 accepts `egress` only with profile `local-cpu`. Every other profile refuses
it: `agent-sandbox` until phase 2, and the frozen `ordinary-docker` and
`kubernetes-opencode` profiles.

### 2. Enforcement outside the workload (phase 1, local Docker)

For an egress package, the CPU adapter:

1. creates a per-operation `docker network create --internal` network;
2. starts a Tessera egress proxy container from an operator-pinned image digest;
   it joins that network and the default bridge and is the internal network's only
   other member;
3. runs the workload on the internal network only, with `HTTPS_PROXY` and
   `HTTP_PROXY` pointing at the proxy and every existing hardening flag unchanged;
4. tears both down, keeping the journal.

The proxy (`tessera_worker.egress_proxy`, stdlib only) accepts only `CONNECT` to an
exact allowlisted `host:port`. It resolves names itself and refuses a destination
if any resolved address is non-public, then connects to the address it checked. It
stops the run's proxying at `max_egress_bytes`. It writes an append-only JSONL
journal: one record per decision, with host, port, allow/deny, reason, bytes up and
down, and times. TLS is not intercepted, so URLs and content are not recorded. A
missing proxy image configuration refuses the package; the adapter never falls back
to bridge networking.

### 3. Recorded per run

The worker posts the journal's SHA-256 and a bounded summary to
`POST /worker/operations/{operation_id}/egress-journal`. That call is fenced by
worker audience, lease and epoch, and is accepted once per operation. The control
plane refuses a summary that names a destination outside the frozen allowlist as
allowed. It records the summary as an append-only `execution.egress_journal_recorded`
event; no schema change is needed. The journal bytes stay in worker custody. The
result summary for agents shows each attempt's `network_profile` and journal digest.
A real run receipt should carry `egress_journal_digest`, verified against
this event. Until then the receipt binds the policy through the
operation's package digest.

### 4. Dataset staging: outputs → snapshot → later run

1. The discovery run writes declared `output_paths`. The existing collector,
   checksummed artifact manifest and private placement apply unchanged. Humans
   record the existing disposition (`retain_quarantined`) and custody release.
2. A human calls `POST /projects/{project_id}/artifact-snapshots` with
   `manifest_id`, `expected_manifest_digest` and optionally a subset of
   `relative_path`s. The control plane requires a human, an active custody release
   of that exact manifest digest, and per-file SHA-256 matches. It builds a
   `CapturedWorkspace` from the placed bytes and adopts it through the existing
   `SnapshotStore.adopt_snapshot`, so the existing 16 MiB and 1,000-file bounds apply.
   It appends a `snapshot.staged_from_artifacts` event naming the snapshot id and
   digest, the manifest id and digest, the producing operation, and every
   `(artifact_id, relative_path, sha256)`.
3. A human releases the snapshot for worker transfer with the existing
   `ReleaseSnapshot` (classification `public`, a human attestation). A later plan
   sets `input_snapshot_digest`, and its grant names the snapshot. Delivery uses the
   unchanged `POST /worker/leases/{lease_id}/inputs` and `materialize_snapshot`.
4. Lineage today: run → grant `snapshot_id` → staging event → manifest → producing
   operation, all immutable and queryable. The agent result summary exposes it as
   an `inputs` section. When typed artifact lineage covers staged inputs, staging should also
   register a `dataset` node with `derived_from` edges to the source artifacts.

### 5. Later phases (design only, need an explicit 0033 exception)

- **Kubernetes Agent Sandbox (phase 2).** Approved as a narrow 0033 exception; see
  the amendment below.
- **Salad (phase 3).** Salad has provider broad egress and receives no input bytes.
  Delivering a staged snapshot requires a new pull path: a short-lived,
  snapshot-scoped, read-only token for that operation, served by the control plane,
  with the guardian verifying the delivered digest. This touches frozen Salad
  custody code and the live session's files. Until then, a Salad plan must not claim
  network-none.

## Amendment (2026-11-18): phase 2, narrow ADR 0033 exception

The owner approved one narrow exception to ADR 0033 (freeze over-built planes; on main):
the same `allowlist-proxy-v1` profile on the Kubernetes Agent Sandbox, because it is
the path to the GPU environment. It is generic and off by default. Nothing else in
the frozen sandbox, collector or witness planes is extended.

- **Plan and package.** `egress` is also accepted with profile `agent-sandbox`.
  Such a package has profile version `agent-sandbox-egress-allowlist-v1`, plus
  `network_profile` and the frozen `egress` policy. Network-none packages keep
  `agent-sandbox-network-none-v1` and are byte-identical. The target must pin a
  digest-pinned `egress_proxy_image`. A target without that pin refuses egress
  plans, so existing targets and witnesses are unaffected.
- **Per-run objects.** Before the Sandbox is created, Tessera creates four
  operation-scoped objects. Each is derived deterministically from the package and
  journaled with the existing intent/claim/UID pattern:
  1. a proxy Pod: pinned image, strong RuntimeClass, non-root, read-only root,
     no token, journal on stdout;
  2. a Service for that Pod;
  3. a NetworkPolicy that lets the sandbox Pod (`tessera.dev/operation`) send
     egress only to its own proxy port and to cluster DNS;
  4. a NetworkPolicy that lets the proxy accept ingress only from that sandbox Pod,
     and send egress only to public addresses (private, link-local, CGNAT and
     metadata ranges excepted) on the allowlisted ports, plus DNS.

  A per-run proxy keeps one run from borrowing another run's allowlist. Shared,
  operator-provisioned proxies were rejected for that reason. A sidecar was
  rejected because NetworkPolicy is per Pod, so the workload would inherit the
  proxy's egress.
- **Sandbox Pod.** It gets `HTTPS_PROXY`/`HTTP_PROXY` set to the Service and
  nothing else. `validate_pod` still compares it exactly.
- **Preflight and release.** Both now require the selecting policies to be the
  namespace default-deny plus exactly this run's sandbox policy, compared by spec.
  Any other additive policy refuses, as before.
- **Witness.** For targets that pin an egress proxy image, the `cni` witness adds
  `egress-proxy-allow` (an allowlisted destination connects through the proxy) and
  `egress-proxy-deny` (direct egress and non-allowlisted destinations are refused).
  The verifier requires these cases only for such targets.
- **Journal.** After the run, a worker-fenced action reads the proxy Pod's stdout
  journal with a bounded log read (2 MiB). It records sha256 and a summary through
  the same `execution.egress_journal_recorded` event and allowlist checks as
  phase 1.
- **Cleanup.** The existing human cleanup also deletes the four objects with UID
  preconditions. Ambiguity retains the journal, as in 0030.

Live activation stays operator-gated. Default tests use the fake cluster; no live
cluster run establishes this.

## Consequences

- Network-none stays the default, and direct workload networking stays off on every
  path.
- Allowlists are hostname- and port-scoped at CONNECT time. Tessera does not inspect
  TLS, so an allowlisted host can serve any content, and the record is
  "destinations and bytes", not "what was downloaded". The staged snapshot's
  checksums are the content record.
- The 16 MiB snapshot bound limits staged datasets until input delivery grows a
  chunked or streamed path.
- No new tables, journals, authority routes or agent scopes. One worker route, one
  human route, two event types.
