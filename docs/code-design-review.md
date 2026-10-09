# Code versus design: bounded review checkpoint

This is a static, targeted review of the accumulated implementation against
[DESIGN.md](../DESIGN.md), not an exhaustive audit of every changed file or a
claim that the full product/pilot is complete. Local tests, real protocol traffic,
local workload execution, simulated platform controls and native-device journeys
are different evidence. Historical release-candidate evidence remains historical.

## Concrete defects and repairs

- **P1 — unsafe observation ordering:** `current_operation_reconciliation` in
  `src/tessera_control/external_operations.py` selected worker/recovery history
  with SQLite `MAX(observed_at)` over text. A whole-second `...00Z` sorts after
  a chronologically later `...00.500000Z`, hiding adverse evidence and allowing
  terminal settlement to release held exposure. The repair parses existing
  historical timestamps with exact datetime precision, selects each source's
  latest instant and retains all equal-time conflicts. It does not rewrite
  historical evidence or infer provider termination. Historical confirmed exits
  remain available when classifying a later adverse observation.
  `tests/test_operation_observation_ordering.py` covers both observation sources,
  fractional/microsecond timestamps, offset representations and equal-time
  contradictions. `tests/test_goal_to_decision.py` additionally checks running
  and unknown projections, denied reconciliation and retained exposure through
  the actual isolated stores.
- **P2 — stale backup freshness:** `migrate_v54_to_v55` in
  `src/tessera_control/migrations.py` retained a pre-upgrade success timestamp and
  backup ID although the new release cannot restore a schema-54 backup. It now
  follows previous migrations: clear only active-state freshness; preserve
  failure history and held forensic copies. `tests/test_runtime_backup_migration.py`
  checks active/held state and an idempotent current-schema repeat.

These repairs require focused independent confirmation before commits. They are
not retroactive changes to a tested immutable candidate. Reconciliation now reads
historical samples to compare exact instants; future indexed optimization must
preserve mixed historical encodings, microsecond ordering and equal-time conflicts.

## Contract-to-implementation matrix

| Design contract | Source and representative checks | Implemented boundary / remaining gap |
|---|---|---|
| Authority, authentication, owner lifecycle (§3–5) | `src/tessera_control/{store,agent_authority,runtime_bindings,runtime_composition,admin_secrets}.py`; `tests/test_{agent_authority,runtime_bindings,remote_agent_services,secret_native_boundary}.py` | Bounded tenant/role/grant checks, runtime-binding admission and explicit owner lifecycle are implemented. Synthetic owner/configuration lifecycle tests do not establish genuine production owner identity, credentials or financial authority. |
| Cancellation, costs and recovery (§6) | `src/tessera_control/{execution,external_operations,recovery}.py`; `src/tessera_worker/kubernetes_execution.py`; observation and recovery regressions above | Partial implementation: explicit uncertainty, grants/leases, held exposure, reconciliation and recovery fencing. The ordering defect is repaired; genuine cancellation settlement, crash during training and all provider accounting boundaries remain unverified. |
| Notebook/labbook and research records (§4, §7) | `src/tessera_control/{research,experiments,programs,dataset_snapshots,agent_research}.py`; `tests/test_{goal_to_decision,program_evaluations,research_journey}.py` | Durable bounded metadata, attempts, variants, findings and measurements exist. One real local baseline produced joined output/measurement records. Metadata and operational execution do not constitute a scientifically valid investigation or complete notebook workflow. |
| Corrections, custody and portable export (§2.7, §7) | `src/tessera_control/{research,artifact_custody,generated_outputs,project_artifacts}.py`; `tests/test_{research,artifact_custody,generated_outputs}.py` | Partial correction/evidence/custody/export records and authorized byte retrieval. Bundle-record creation is not a full portable filesystem bundle. Full experiment-chain JSON remains API-first; complete downstream correction-impact and replication journeys are not established. |
| MCP/A2A and provenance (§5.3) | `src/tessera_mcp/{server,mcp_workflow,runtime_admission}.py`; `src/tessera_control/a2a_peer.py`; `src/tessera_agent/research_peer.py`; `tests/test_{mcp_adapter,a2a_peer,guard_phase_provenance}.py` | Real local protocol/client/service mechanisms have bounded tests and historical protocol proof. Deterministic/synthetic reasoning events do not prove genuine planner/critic reasoning or a complete independent-peer research journey. |
| Supervised reasoning (§5.3) | `src/tessera_agent/{remote_reasoning_runtime,remote_reasoning_store,research_service}.py`; `tests/test_{remote_reasoning_runtime,research_service}.py` | Durable supervision/service interfaces and fail-closed setup exist. Authorized live model reasoning, production enrollment, off-host/laptop-off operation and device selection remain unverified; no provider inference was run for this review. |
| Desktop isolation and alpha workflow (§2, §3) | `desktop/{connection,main,renderer,artifact-save}.mjs`, `desktop/{preload,ipc-contract}.cjs`; `desktop/test/` | Supported-Node desktop gate: 228 passing tests. Current-renderer project/design/results/export/error/reconnect exercised against the real local backend with simulated platform selectors. Actual native credential picker/save dialogs and personal UI review remain unverified; native automation denial is preserved. |
| Existing-work adoption and skill workflow (§2.1–2.2) | `src/tessera_control/{snapshots,skills}.py`; existing adoption/snapshot/skill APIs and tests | Scoped records/interfaces are implemented in part. Complete repository/notebook adoption, desktop skill authoring/test/release and investigation-to-reproduction journeys remain implementation/acceptance gaps, not permanently prohibited capabilities. |
| Packaging and reproducibility (§8–9) | `dev/{build_release_candidate,public_runtime_bundle}.py`, `release/opencode-observer/`; `tests/test_{release_candidate,public_runtime_packaging,observer_deliverable}.py` | File-pinned licensed sparse source bundles and historical package/install gates exist. Hash/license integrity is not independent upstream provenance, a universal secret/license audit or release approval. Unchanged architecture PDF/PS redistribution rights remain unresolved. |
| Full pilot and post-pilot operations (§8–9) | DESIGN milestone table; [release checklist](release-checklist.md), [launch readiness](launch-readiness.md) | Deferred/uncompleted: measured full research pilot, quality/human-effort comparison, complete adoption/correction/export journey and production operational/security gates. Post-pilot scale/team operations are separate roadmap work, not implicitly included in these repairs. |

## Verification scope

The historical source candidate reported 5,008 passes and 47 opt-in skips, not
5,055 live acceptances. Its package/install and separate protocol receipts must
be matched by exact relevant bytes before reuse. The supported-Node desktop
receipt and renderer journey provide distinct local evidence; neither proves
native dialogs or full-design completeness. Current targeted repair checks are
recorded separately outside shipped inputs. No new Job, provider inference,
service restart, native automation retry, push or release occurred here.
