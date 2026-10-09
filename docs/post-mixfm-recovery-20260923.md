# Desktop verification and post-MixFM held recovery — 2026-09-23

This is local operational evidence, not a scientific result, writable failover,
deployment upgrade, or full integration acceptance. No commit, push, install,
download, external-service call, provider run, or Kubernetes Job was performed.
Unrelated working-tree edits were preserved.

## A. Desktop IPC acceptance

At initial inspection, the dirty working tree **already contained** the requested
fix and `desktop/test/main-dispatch.test.mjs`. No desktop source or test was
rewritten during this task:

- `programContext: id => connection.programContext(id)`;
- `createDecision: (body, key) => connection.createDecision(body, key)`;
- the real registration loop validates the sender and 128 KiB request bound,
  removes exactly one connection ID, checks `assertActive`, then invokes the
  operation handler with its remaining arguments.

The dispatcher tests execute the actual `main.mjs` registration loop and actual
`preload.cjs` in VM contexts, replacing Electron/native boundaries, and use the
real `ControlConnection` with a fake transport. They verify program-context URL,
decision body and idempotency key, missing key rejection, wrong/missing/stale/
disconnected IDs, same-ID old-generation response fencing, lost-response and
invalid-JSON unknown outcomes, no automatic retry, explicit retry body/key
preservation, sender rejection and request-size fencing. This is real dispatcher
flow with mocked native/HTTP effects, **not a live Electron GUI or provider test**.

Exact checks from `desktop/`:

```sh
npm test
for file in *.mjs *.cjs test/*.mjs test/*.cjs; do node --check "$file" || exit; done
npm pack --dry-run --offline --ignore-scripts
```

- Tests: **141 passed, 0 failed, 0 skipped**, including all seven dispatcher tests
  and the actual packed, self-contained sandbox-preload package test.
- Syntax: all selected modules passed, exit 0.
- Package dry run: exit 0, 12 files, 70.1 kB packed / 319.3 kB unpacked; both
  `preload.cjs` and `ipc-contract.cjs` included.
- `git diff --check`: passed. Full Python integration suite and native graphical
  smoke were not run for this bounded task.

## B. Explicitly selected local deployment

Runbook: [safe backup and held recovery](recovery.md), including its post-MixFM
gate, and [single-active lab operations](single-active-lab-operations.md).

| Item | Observed value |
|---|---|
| Kubeconfig / context | Operator-supplied `$KUBECONFIG` / `docker-desktop` |
| API server | `https://127.0.0.1:59675`; `/readyz` returned `ok` |
| Namespace / Deployment | `tessera-local-control-20260921` / `tessera-control` |
| Deployment UID / generation | `8eda97bd-dc30-4022-9833-6aca0d622d8f` / `4` |
| Writer configuration | One replica, one ready replica, `Recreate` |
| Pod | `tessera-control-6d78bddc74-vdfgd`, Running/Ready, zero restarts |
| Pod UID | `2c30c3b2-f685-42f3-b5ba-4407cf958cdc` |
| Configured image digest | `sha256:facbb6eda289f9e663a52f79abe1d0002ebab93bfb98046b00461206f140556c` |
| Runtime-reported image ID | `docker.io/library/import-2026-09-21@sha256:8726abacaf4e39d2904ff43a9f623b60ec57f933b6cebdb7d087a0fecb605eb7` |
| PVC | `tessera-control-state`, Bound, 1 GiB, RWO, `standard` |
| PVC UID | `c3843e82-f2d1-4b7e-831e-1a26df43e9c5` (original retained claim) |
| State path | `/state/control` |
| Schema / backend identity | `48` / `734ca007871e5f06c65b65df` |

Both `/health/live` and `/health/ready` returned HTTP 200 before and after the
drill through Pod loopback. The latter reported `ready`, profile `remote-lab-v1`,
and the same backend identity. No Deployment/PVC specification was modified,
production process restarted, second PVC-mounted Pod created, or claim deleted.
The backup command did update the normal source backup-health record, as the
documented workflow specifies; no research/history or credential mutation was
made on the source. The selected namespace's final Job list was empty.

Initial operator warnings: `worker_heartbeat_stale`, `backup_stale`,
`resource_budget_exhausted`. After publication, backup health became `healthy` /
`fresh`; the other two warnings remain. Two historical active grants have past
expiry timestamps and remain unresolved in the restored projection. Nothing in
this drill proves provider termination or reconciles those grants. Older fixture
namespaces also exist; one old control fixture showed `Init:ErrImageNeverPull`.
That is not the selected live control deployment and was left untouched.

## Backup and exact cutoff

The same deployed schema-48 CLI was used for backup, restore and held serving;
the current checkout was not substituted into the running deployment. No migration
was performed. The one-use helpers are retained at
`/tmp/opencode/tessera-post-mixfm-recovery.py` and
`/tmp/opencode/run-post-mixfm-recovery.py` (outside Git).

Executed host command:

```sh
.venv/bin/python /tmp/opencode/run-post-mixfm-recovery.py
```

The helper sends the fixed drill through `kubectl --kubeconfig
 "$KUBECONFIG" --context docker-desktop -n
tessera-local-control-20260921 --request-timeout=120s exec -i
pod/tessera-control-6d78bddc74-vdfgd -c control -- python -`. Its effectful
CLI calls inside that Pod were:

```sh
tessera backup --state-dir /state/control --output /tmp/post-mixfm-recovery-20260923/backup
tessera restore --backup /tmp/post-mixfm-recovery-20260923/backup --state-dir /tmp/post-mixfm-recovery-20260923/held
# Twice, with a distinct available ephemeral loopback port:
tessera serve --state-dir /tmp/post-mixfm-recovery-20260923/held --port PORT
```

The drill directory is private and outside the PVC on the Pod's `/tmp` emptyDir.
Only the held service was stopped/restarted. Both disposable service processes
exited after verification; held state remains private for inspection and is not
durable across Pod removal. No held credential was copied off the Pod or printed.

| Backup fact | Observed value |
|---|---|
| Backup ID | `dfd18a161e6e45d08b8d28bb29df5e1a` |
| Created UTC | `2026-09-23T03:58:47.969630Z` |
| Format / schema | `1` (SQLite-only) / `48` |
| Event cutoff and watermark | `66` |
| Snapshot bytes | `1,654,784` |
| Snapshot SHA-256 | `2282e3b75eb495c965378d9c8019e8aa5fccc9a6dcbdfbccdf42e629ed324a7b` |
| Restore CLI duration | `1.719` seconds (not an RTO claim) |

Baseline reads and source history rows were bracketed by the same event cursor,
66. Backup rows matched that baseline; source cursor and compared history rows
still matched after the drill. The live source was not fenced or quiesced, so
this is an observed cutoff, not a promise about later activity.

Private off-PVC Mac evidence (ignored by Git):

```text
.tessera/post-mixfm-recovery-20260923/
  backup/manifest.json
  backup/snapshot.sqlite3
  baseline.json
  report.json
  drill.stdout
  drill.stderr
```

Backup directories were verified mode `0700`, manifest/snapshot `0600`.
The copied snapshot's SHA-256 was independently recomputed on the Mac and matched
the manifest. This establishes off-PVC custody on this Mac, not off-machine disaster
recovery. The historical pre-v48 backup and smoke evidence were preserved.

## Held verification results

MixFM studies compared:

- Outcome 0: `study_ba00da5f49994ee18bd566a832552f9f`.
- Outcome 1: `study_f54445f7d24a4be2b3083f1fc1dfaa96`.

For each, the complete study export, program-context projection, dataset snapshot
page (limit 200), and explicitly empty next page matched the baseline exactly,
including record IDs, revisions, attribution, history and declared digests. All
eight projection hashes are retained in private `report.json`. These are metadata
records, not verified molecular bytes or measured model performance.

All compared history rows survived unchanged:

| Table | Rows |
|---|---:|
| research_studies | 3 |
| research_sources | 3 |
| research_variants | 8 |
| research_findings | 2 |
| decision_requests | 12 |
| dataset_snapshots | 3 |
| dataset_snapshot_sources | 3 |
| dataset_ingestion_proposals | 4 |
| events | 66 |
| idempotency | 69 |

Checks passed on the held target both initially and after its process restart:

- SQLite integrity `ok`; zero foreign-key violations.
- All **3 original credential rows revoked**, exactly one fresh active credential.
- Fresh owner read all eight projections; original owner returned **401** on all.
- `/recovery`: `held_restore`, `recovery_hold: true`, `promotion.supported: false`,
  exact manifest digest/watermark, original branch unfenced, later activity unknown.
- `/health/ready`: **503** because the copy is held, not writable production.
- Valid new project metadata write: **403**.
- Historical project-create body and idempotency key: **403**. The reconstructed
  normalized body digest was checked against the saved original request digest.
- Valid dataset-ingestion review: **403**.
- Held database bytes unchanged across reads, denials and restart:
  SHA-256 `cf334285f41644cf2d423b9cd08caa0a288f7e8993693f88c4f3b2d398b27a03`.

**Credential coverage limit:** only the old owner bearer was available for actual
HTTP probes. Other original credential rows were checked revoked in the restored
database; their unavailable bearer values were not recovered, minted, or claimed
to have been HTTP-tested. This is not complete old-human/agent/worker bearer-type
HTTP coverage. No offline fallback was necessary because live held recovery passed.

The two temporary helpers passed `py_compile` and Ruff `--select F`. No passing
assignment was rerun. For research readiness, see the separate
[bounded Outcome 0 inventory](outcome0-local-inventory-20260923.md).
