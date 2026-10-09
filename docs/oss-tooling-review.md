# OSS, Kubernetes, and data-tooling review

**Review date:** 2026-10-07

**Reviewer:** read-only architecture review, session `ses_ee95c8007ffeYSiPLMliq1mIoO`

**Purpose:** distinguish launch blockers from optional ecosystem choices; recommend the
smallest useful out-of-box workflow. This is an assessment, not permission to install,
deploy, add dependencies, or change the active service.

## Decision summary

1. **Keep native Kubernetes Jobs as the default execution primitive.** Tessera already
   has a bounded native Job path. A small, deterministic CPU/tabular starter workload
   is a better next out-of-box example than adopting a second orchestration system.
2. **Do not make Ray/KubeRay a default or install it automatically.** Evaluate it only
   when measured workloads need distributed task scheduling, shared worker state,
   distributed training, or adaptive sweeps that native Jobs cannot meet efficiently.
   Ray is not a replacement for Tessera authority, provenance, durable claims, or
   artifact custody.
3. **Treat Polars/PyArrow and DuckDB as optional workload-image tools**, not control-
   plane requirements. Add only when the starter's declared data volume and format
   require them. Keep hosted Jupyter, MLflow authority integration, and default S3
   storage deferred.
4. **Resolve platform/release blockers before tool expansion.** Real enrollment and
   trusted runtime adapters, complete installed/source parity, cold native gate
   coverage, required release documentation, supported storage/recovery, and the real
   MCP/A2A/reasoning workflow are not supplied by package choice or source tests.

## Evidence scope and version caveat

The read-only review inspected current README/design/config/license/release material,
the Kubernetes/native Job, dataset-ingestion and artifact-storage implementations,
and the v6 runtime-admission handoff, then compared the architecture with the official
Kubernetes, Ray, Polars and DuckDB references below. No tests, installation, image
build, cluster operation, provider request or live workload was performed for this
review.

The review's v6 runtime-binding gap list is a **dated snapshot**, not a statement that
no subsequent code exists. Revision-7 owner receipts now report a 32-test Python
runtime-binding backend checkpoint, a separate 18-case migration selection, and a
31-case API/authority checkpoint across multiple selections (not one combined run).
The actual Store/API/canonical-IPC paths have local temporary-backend
evidence. That evidence still uses synthetic private-owner/guard/quota/native
resources and is not installed native-runtime, provider, or production-admission
proof. The revision-7 local record ID is `RUNTIME-BINDINGS-V7-PHASE2`; it is retained
outside the public source distribution. Do not repeat the older v6 status as current,
or promote the new local selections into live acceptance.

Source review also found the current native Job profile bounded to at most 1 CPU,
1 GiB memory and a one-hour active deadline, with no automatic retry; the Job path has
existing cancellation, durable journal and artifact handling. These are useful
execution primitives, not aggregate capacity guarantees, distributed scheduling,
trusted production admission or a completed researcher workflow. See
`src/tessera_worker/kubernetes_execution.py` and the associated worker lifecycle/
output collection code.

## Launch blockers and source evidence

| Priority | Gap / observed boundary | Required before claiming the journey or release |
|---|---|---|
| P0 — admission and identity | The reviewed v2 contract is explicit opt-in and preserves strict v1 by default. The current binding/backend has local API/Store/IPC tests, but the trusted runtime-owner/private OAuth guard, same-check fresh quota, real enrolled publisher/principal/session, authenticated operator financial resolver/current revocation, and installed terminal bridge are not evidenced as a complete deployment. | Provide approved owner-held adapters and enrollment; test current scope/issuer/revocation/freshness, final-wire ordering, durable atomic grant/budget reservation, crash/uncertain disposition and reconciliation. Validate actual human session, privileged reauthentication, owner rotation and organization revocation; bearer possession alone is not human identity. Keep provider entitlement/billing unknown only under reviewed policy; unknown required local checks deny. No credential copying or admission lowering. |
| P0 — actual end-to-end use | Real planner/critic generation, human review/approval, experiment and notebook join, installed MCP skill/catalog binding, generated A2A consumption, same-run restart, and running-job cancellation remain distinct from synthetic protocol fixtures. | Exercise the authorized installed workflow with genuine approved authority and actual independent peers. Report human/device/off-host/laptop-off steps separately; this review did not run them. |
| P0 — artifact/source parity | The shipped observer baseline records 27 original/36 target files, while the isolated runtime worktree now reports 33 originals/45 targets. Ignored-worktree changes are not automatically in the package; copied-source provenance and the packaged patch/baseline/license must match exactly. Optional public-runtime support must remain honestly unconfigured without its owner-held secure path; private checkouts and credentials are never packaged. | Reconcile the public file-index delta, patch reconstruction, licenses, documentation inventory and build manifest before freezing a candidate. Rebuild and rerun every artifact-dependent check on the new exact digest. The v5 source/wheel/gate receipts remain historical, not v7 evidence. |
| P0 — native gate completeness | Cold-environment native prerequisites and selected SDK/Effect/Bun/TypeScript tests are not equivalent to complete source coverage. The earlier full-suite skip reasons were not retained; no skipped cases may be called optional without evidence. | Pin and document every native tool/runtime prerequisite; preserve complete private stdout/stderr, exits, source inventory and skip/failure reasons. Reconcile old migration fixtures to the current schema, then run fresh Ruff/format/full pytest on the frozen current source. |
| P0 — release and operations | `docs/release-checklist.md` still leaves vulnerability-reporting, support/privacy/retention/recovery, supported install/upgrade/rollback, schema/backup notes, provenance/signing/update verification and operational/production gates unchecked. Security/identity/storage boundaries and live checks are not replaced by a local package pass. | Reconcile required release docs before freeze. Establish support and recovery ownership, scoped identity, security review, integrity/signing/update process, aggregate capacity, backup/restore/reconciliation, ingress/logging controls and operational monitoring. Keep actual provider billing, native picker, independent host and laptop-off evidence separate. |
| P1 — dataset bytes and artifacts | `src/tessera_control/dataset_ingestion.py` is metadata/proposal provenance: its module contract says it does not fetch source bytes, check licenses or validate dataset contents. The artifact store provides controlled project placement, not an out-of-box scientific ingestion/analysis pipeline. | For a starter, accept only a small, explicitly supplied immutable snapshot; verify its digest/size/schema and declared source/license before admission. Preserve provenance, quarantine, review and export boundaries; do not imply current ingestion checks absent from code. |
| P1 — example/docs parity | The initial review found stale schema-50/current-schema claims and an intent-5 factory note saying the reviewed v2 semantics were unapproved. The documentation reconciliation now distinguishes schema-55 source from the latest schema-52 installed receipt and links Decision 0029/v2. | Before candidate freeze, reconcile any remaining dated package docs against the exact artifact and verify all links. This docs batch does not create or validate a release candidate. |

The P0 list is the shortest path to an honest first governed journey; P1 data tools
are useful only after that journey's admission, artifact and review boundaries work.
Current Job resource caps or a successful local test do not prove aggregate capacity,
independent-host durability, scientific validity, or operational recovery.

## Recommended smallest out-of-box research workload

**Recommendation for a future, separately approved implementation:** one provider-
disabled, deterministic CPU/tabular baseline plus a bounded sweep, executed as ordinary
Tessera-managed native Jobs. Keep the orchestration and authority in Tessera. A
candidate profile for discussion—not a measured or approved limit—is:

- One CPU, 512 MiB memory, 120-second deadline; input at most 4 MiB and output at most
  1 MiB. Fixed seed; at most four sequential, preregistered variants; no dynamic
  resource expansion, automatic retry, network egress, or dataset download.
- Digest-pinned non-root workload image; explicit source/data digest, schema, license
  declaration, split strategy, analysis code and parameter digests. Quarantine every
  output, record terminal outcome including failed/cancelled/inconclusive, then require
  human review before promotion/export.
- Start with CSV and the Python standard library when practical. If measured memory or
  streaming needs justify it, put **Polars** in the isolated workload image. Add
  **PyArrow** only for actual Arrow/Parquet interoperability. Neither belongs in the
  control-plane dependency set by default.
- A fixed majority/constant predictor and a single small model may be a useful
  comparison, but the research owner must preregister the question, split, metrics,
  and interpretation. A successful job is not scientific validation.

These limits are a proposed starter profile, not a new authority grant, runtime config,
product capability, or deployment instruction. The existing dataset API records
claims/proposals rather than ingesting bytes; any first starter must implement and
test verified snapshot delivery rather than treating metadata registration as data
availability.

## Tool adoption and defer criteria

| Tool / option | Decision | Why / trigger to reconsider |
|---|---|---|
| Kubernetes Jobs | **Adopt as current default.** | Existing Tessera native Job contracts already encode bounded, pinned workload execution. Start with ordinary Jobs for independent, short, auditable experiments. For independent homogeneous partitions, evaluate Kubernetes Indexed Jobs before adding a second scheduler; Indexed Jobs are not claimed as current Tessera functionality. |
| Ray / KubeRay | **Defer as default; preserve as an optional future adapter.** | Adds a cluster control plane, head/worker lifecycle, authentication, scheduling, cleanup, cost and failure modes. Reconsider only after a representative benchmark shows a concrete gap: e.g. at least 1,000 short tasks with measured Job overhead above 20% of wall time, persistent shared worker state, a preregistered distributed workload requiring at least two communicating workers, or adaptive early-stopping sweeps that cannot be represented as bounded Jobs. These are proposed decision triggers, not measured Tessera workload facts. A Ray Service is a separate persistent-service requirement, not an automatic way to host research tasks. |
| Ray Data | **Defer.** | Consider for measured high-fan-out ETL/heterogeneous data processing only when bounded per-Job processing is inadequate. Do not introduce it just to read the proposed tiny starter input. |
| Ray Train | **Defer.** | Consider only for an approved multi-worker distributed-training experiment with explicit checkpoints, cancellation, cost and recovery requirements. |
| Ray Tune | **Defer.** | Consider only for adaptive sweeps after a human-approved experiment budget, maximum trial count, early-stop policy, and per-trial provenance exist. Never let an adaptive scheduler expand authority or spend. |
| Polars | **Optional in the workload image.** | Useful if measured table size/streaming justifies it. Its streaming engine may fall back to in-memory execution for unsupported operations; enforce actual memory/deadline caps rather than infer boundedness from the library choice. |
| PyArrow | **Conditional.** | Add only when an accepted Arrow/Parquet interchange or schema contract requires it. It is not needed merely because Polars is available. |
| DuckDB | **Optional isolated analysis tool; not a control-plane dependency.** | Useful for bounded local SQL over explicitly provided files. Disable extension auto-install/load and network paths; use resource and filesystem sandboxing. Do not treat SQL sandboxing alone as tenant isolation. |
| Jupyter | **Defer hosted notebook service.** | A local notebook may consume a reviewed export. A hosted server adds credentials, persistent execution, ingress and session lifecycle. Tessera records remain the control-plane source of authority and provenance. |
| MLflow | **Defer authority integration.** | If later useful, begin with a one-way, redacted export from accepted Tessera records. It must not become the grant, human-review, billing, or audit source of truth. |
| S3/object storage | **Defer as default.** | Revisit for an actual off-host custody requirement after defining scoped identity, integrity verification, conditional writes, retention, backup/restore, egress and cost controls. Do not substitute it for current local artifact guarantees without those checks. |

Ray must never become a second authority/orchestration ledger. If eventually adopted,
keep clusters private and resource-capped, pin images and dependencies, deny unsafe
egress/control APIs, bound head/worker count and cleanup, and isolate each trust domain.
Ray's security guidance is defense-in-depth; it is not a multi-tenant isolation claim.
Human grant, cancellation, settlement, provenance, and artifact disposition stay in
Tessera.

## Official references reviewed

- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
  and [KubeRay getting started](https://docs.ray.io/en/latest/cluster/kubernetes/getting-started.html).
- Ray [security](https://docs.ray.io/en/latest/ray-security/index.html),
  [Data](https://docs.ray.io/en/latest/data/data.html),
  [Train](https://docs.ray.io/en/latest/train/train.html), and
  [Tune](https://docs.ray.io/en/latest/tune/index.html).
- [Polars streaming](https://docs.pola.rs/user-guide/concepts/streaming/) and
  [DuckDB security](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview.html).

## Validation boundary

This document records the architect's read-only assessment and recommendations.
No Ray/KubeRay, Polars, PyArrow, DuckDB, Jupyter, MLflow, or S3 components were
installed or enabled; no test, build, cluster action, provider call, deployment or
live research was performed. Adoption requires a separate scoped implementation
decision and the exact-source release gates above.
