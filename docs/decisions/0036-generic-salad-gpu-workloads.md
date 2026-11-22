# 0036 — Generic GPU workloads on Salad (proposed)

## Status

Proposed (2026-11-19). Prerequisite for running a GPU pilot as
Tessera runs. Phase 1 (workload contract v1) is implemented. Phase 2 (guardian
input delivery) is implemented end to end and tested against the fake provider and
synthetic guardian only: a workload contract may declare `input` (bound to
`manifests.data`); the live driver delivers the ticket to the guardian and
confirms it before start (a failure never starts the group), refreshes it from
the keepalive and revokes it on every exit; gpu_runtime_v1 pulls and re-verifies
the snapshot after begin-once and may carry its fetch manifest inside it
(up to 128 fetches); setup stages `--input-dir` as the declared chunked snapshot.
Input replies are 1 MiB ranges sized to the guardian's rate-derived request budget;
disk and staged-input checks (including the input-carried manifest) run when a route
is proposed, approved and admitted, not when a stored proposal is parsed. Phase 3 (declared multi-file chunked output custody,
up to 16 files and 16 MiB in total, per-file sha256 verified again by control
on import) is implemented and tested against the fake provider and synthetic
guardian only; see docs/salad-proxy.md. Uploading 16 MiB inside the 120 s
upload grace is untested live: at about 5 Mbit/s it should take under a minute,
but below about 2 Mbit/s it will not fit (see the runtime README). Phase 5's slim runtime recipe
(`workloads/gpu_runtime_v1/`) is implemented and its image is built and
published, but no live cold start has run yet; the published image predates
phase 3 and still uploads only a single `output.json` through the original
endpoint, so multi-file contracts need a rebuilt image. A manifest item may also be served from
the verified input (`{"kind":"entry","source":"input","path":...}`), so a workload's
own entry needs no public URL; it is bound by the snapshot digest, not by egress, and
admission refuses a snapshot that lacks it. A generic cold-start smoke
(`workloads/gpu_runtime_v1/smoke/`, stdlib only, no fetches) uses it; it is offline-
tested only and needs the rebuilt image: input-sourced items require an image
built at or after the commit that added them (an older image refuses them, and with
an input-carried manifest only after begin-once has spent the claim). Phase 4 is not
implemented.

Reviewed fetch redirects (2026-11-19, implemented and offline-tested only). A pinned
artifact URL may answer with a redirect to a CDN whose signed URL expires and so
cannot be pinned. A gpu_runtime_v1 contract may declare `egress.redirect_hosts` (at
most 16 exact lowercase DNS names; refused for any other runtime); they join the capability network on port 443, an argv
`redirect-hosts:` token binds the manifest's copy of the list, and gpu_runtime_v1
follows at most 3 HTTPS redirects to exactly those hosts, with the per-fetch sha256
and `max_bytes` still applied to the final bytes. An empty list is not serialized,
so existing contract and proposal digests are unchanged. An image older than this
change refuses the token at argv parsing, before any fetch or begin-once. An older
tessera_control also fails closed: its egress model forbids unknown fields, so it
rejects any contract or proposal carrying `redirect_hosts` (such a route then
needs this version to parse, stop or reconcile). No live
run has followed a redirect yet; it needs a rebuilt image.

Review follow-ups on the input path (2026-11-19):

- **M1, resolved.** [ADR 0039](0039-data-class-guard.md) records each staged
  snapshot's data class. Salad is marked `third_party_compute`, so it admits only a
  snapshot recorded `public`, with license and source evidence from a human. The check
  runs at admission and again at provider ticket issue.
- **L1, fixed.** Input install guards `execution is None`.
- **L3, fixed.** The control fetch now runs under a wall-clock alarm, not only a
  per-recv socket timeout.
- **L4, verified.** `SafetyHandler._reply` and admission use the same compact encoding;
  a test pins it.
- **L7, fixed.** The `MAX_FETCHES` comment no longer cites a pilot shape.
- **L5 and L6, accepted.** See Consequences.

## Context

The live Salad path is proven end to end for one workload: the reviewed CUDA
smoke (run 29 passed with a verified receipt, confirmed stop and reconnect).
It is deliberately narrow, and a real experiment needs more than it allows:

1. **Smoke-only workload contract.** `argv_binding="smoke_operation_v1"` fixes
   the argv shape; the workload runtime argument is capped at 300 s; custody
   accepts exactly one `output.json` of at most 64 KiB.
2. **No input delivery to Salad.** ADR 0034 phase 3 is design only; staged
   snapshots cannot reach a Salad job.
3. **Egress.** Salad has provider-wide egress. A job that fetches wheels or a
   base model must declare that, never claim network-none, and must pin every
   fetched artifact by sha256 and report the digests.
4. **No GPU `ExecutionSpec.profile`.** Plans reach Salad only through an
   `ExecutionSelection` route, not a named profile.
5. **Cold start.** A ~3.5 GB image repeatedly failed to start inside 15 min;
   a slim runtime image with run-time, hash-pinned fetches is required.

## Decision (proposed phases)

1. **Workload contract v1.** Replace the smoke-only binding with a reviewed
   generic contract: digest-pinned image, fixed entrypoint, argv template with
   operation/epoch/custody slots only, declared non-secret env allowlist,
   `workload_seconds` up to 3600, route window up to 7200 s (up to 45 min unbilled provisioning plus up to 60 min billed workload plus upload grace), declared output
   files (count and per-file byte caps), and an explicit `provider_broad_egress`
   field with a declared list of fetched artifacts and their sha256.
2. **Input delivery (ADR 0034 phase 3).** The guardian serves a staged
   snapshot read-only at `/v1/workload/input` to the bound workload (same IMDS
   identity check as begin-once), bounded by the snapshot's digest and size.
3. **Output custody for artifacts.** Multi-file, chunked upload with per-file
   sha256 and caps, sealed on confirmed stop as today. Implemented: custody
   total is 16 MiB (the existing `max_output_bytes` platform bound), 1 MiB
   chunks, and stop on retained output only once every declared file is complete.
4. **CPU stages as Tessera runs.** Teacher cache and variant building run as
   `agent-sandbox` jobs that fetch data under the ADR 0034 allowlist and
   register their outputs as snapshots feeding phase 2.
5. **Slim image recipe.** CUDA runtime base plus python; the entrypoint
   fetches hash-pinned wheels and the model revision at run time.

Safety controls carry over unchanged: stop on retained output, run-wide
deadline, heartbeat stop, orphan fence with operator reconcile, per-run
`max_cost_cents` and the experiment budget ceiling. A never-bound reservation
(failed create) leaves the fence only as `reconciled` (`lost_create_absent`)
after its deadline plus a 900 s margin and a definite provider 404 for the
reserved name (docs/salad-spend-safety.md, "Lost create").

## Consequences

The platform stays experiment-agnostic: the contract is generic and the
Program-specific code stays in the gitignored `experiments/` workspace. Estimated
pilot cost after these phases: one LoRA run of about an hour on a 24 GB
consumer GPU at $0.2–0.35/h, i.e. well under $1 per run and far below the
$15 pilot ceiling.

Accepted input-path trade-offs:

- **L5.** For a manifest carried in the input, wheels and files are fetched after the
  single begin-once claim, whereas the inline form fetches them before. A failed
  download is then terminal and billed. Carrying the manifest in the input buys up to
  128 fetches at the cost of moving that class of cheap failures after billing starts.
- **L6.** A reply cut off on the wire is refunded from the served-byte budget at most
  16 times per ticket. A client that reads a reply and then resets the connection can
  therefore read about 16 extra 1 MiB ranges (about 16 MiB) per ticket beyond the
  budget. This is bounded and accepted.
- **L2 (not changed).** A cache miss fetches a whole 4 MiB chunk from control while
  only the served range is charged. Control egress stays bounded by the request cap at
  roughly 8× the snapshot size, to our own control plane.
