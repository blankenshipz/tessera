# ADR 0190: a run's control requests use indexed lookups, not whole-table reads

Status: Implemented (indexes, bounded late-output check, measurement test). Proposed (the
platform follow-ups in "Not done here"). Follows ADR 0178 (lease renewal stall retry) and ADR
0188 (the worker's retry of a stalled output hand-off). Schema 92.

## Context

Long local CPU stages (44 to 56 minutes) were lost at the end-of-run output hand-off: the
worker's call got no answer in time, it reported `quarantine_failed`, and the stage ended
`failed_transient` (exit 137). Each loss matched a 20 to 30 s gap in control's request log. Short
stages with outputs of the same size handed off fine. Three of the six long gaps happened in the
middle of a run, not at the hand-off. The host had 1 to 3 GB of memory available and several GB
of swap in use for the whole period.

One guess was that some end-of-run step costs time in proportion to the run's length. We
measured that before changing anything (`tests/test_handoff_cost_growth.py`):

- The real development worker runs against the real control app. The test seeds a long attempt
  (3,000 observations and 300 renewals, about 50 minutes) into a control database that already
  holds a large history: 200,000 events, 50,000 observations of other leases and 300 open chunk
  uploads.
- It meters every control request of the run: SQLite VM steps (a progress handler), the query
  plan of every statement (`EXPLAIN QUERY PLAN`) and the wall time. Wall time is reported only,
  never asserted.

What we found on a healthy host, before this change (max per request; small history and 8 min
run compared with large history and 50 min run):

| request | small/short | large/long |
|---|---|---|
| running observation | 25k steps, 20 ms | 922k steps, 112 ms |
| terminal (confirmed exit) observation | 46k steps, 27 ms | 1,337k steps, 142 ms |
| run receipt | 41k steps, 29 ms | 254k steps, 43 ms |
| artifact begin / complete (8 MB) | 26k steps, 290 / 372 ms | 29k steps, 225 / 397 ms |
| each 256 KiB chunk (32) | 26k steps, 73 ms | 26k steps, 71 ms |
| manifest, placement, renewal | flat | flat |

- No hand-off step is slow on a healthy host. Each request finished well under 1 s, and the
  run's length moved the cost only a little: the terminal observation went from 46k to 137k
  steps with a small history.
- The cost grows with the installation's history. Every observation post runs
  `_cancellation_payload`, which looks for a `execution.grant_cancel_requested` event of the
  grant. That lookup reads the whole event log (`SCAN events`) inside the write lock, about once
  a second for each running worker, and twice for a terminal observation.
- The run receipt's lease trust fields scan events in the same way (`SCAN events`). So do the
  egress, image build and public fetch follow-ups (`event_type` with a payload field and no
  index).
- The latest-observation, latest-provider and late-output lookups by `lease_id` scan
  `execution_observations`. The output budget's open chunked uploads scan
  `snapshot_chunk_uploads`, which keeps every upload ever begun.
- On a host that is swapping, a whole-table read of a large event log in each request means
  reading many megabytes back from disk while holding the write lock. Every other request waits
  behind it, up to the 10 s busy timeout. That fits the gaps both mid-run and at hand-off. The
  long runs simply spend more requests, and so more chances, in that state.

So the cause is not a run-length-proportional hand-off step. It is history-proportional scans
under the write lock, made into multi-second stalls by host memory pressure.

## Decision

1. Schema 92 adds four indexes, created the same way for a fresh database and by the
   migration (`output_lookup_indexes.py`). The migration is additive and idempotent, changes no
   row, and marks the last backup stale like other schema changes. The indexes:
   - `events_by_type_cursor` on `events(event_type, cursor)`: the cancellation request, lease
     and receipt trust fields, the egress journal, image build and public fetch reports, and a
     confirmed exit's code read only their own event type.
   - `execution_observations_by_lease` on `execution_observations(lease_id)`, in rowid order:
     the latest observation, the latest provider and the late-output check.
   - `execution_observations_confirmed_exit` on `(lease_id, epoch, recorded_at)`, a partial
     index over confirmed exits only: the run receipt's `min(recorded_at)` of the lease's
     confirmed exits.
   - `snapshot_chunk_uploads_by_status` on `(status, expires_at)`: only open uploads count
     toward the output budget.
2. Queries are unchanged, except `ArtifactStore._exited_inside_grant`. It now makes two indexed
   reads: the lease's latest observation, then its confirmed exits. It no longer reads every
   observation of the lease. It decides exactly as before:
   - "latest" means the last observation by rowid for the lease and epoch.
   - `termination_confirmed` can only be 0 or 1 (CHECK).
   - The earliest exit is still the Python minimum of the parsed `recorded_at` values.
   - A test compares the old and new forms on 4,000 seeded observations, with mixed timestamp
     formats and out-of-order recordings.
3. Nothing about the meaning of outputs changes:
   - quarantine, termination confirmation, the late-output rules (ADR 0064, ADR 0086) and the
     tenant checks stay the same;
   - every receipt field stays;
   - a request either commits whole or not at all, inside the same transaction.
   The same measurement after this change (large history, 50 min run): running observation
   28k steps (was 922k), terminal observation 142k (was 1,337k; the remainder is
   `current_operation_reconciliation`, below), run receipt 77k (was 254k). The other requests
   are unchanged.
4. `tests/test_handoff_cost_growth.py` guards the change:
   - No worker request of the run (the mid-run observation and renewal included) has a plan that
     scans `events`, `execution_observations` or `snapshot_chunk_uploads` in full.
   - On the large history and 50 min run, a request's VM steps stay within 4x (+200k) of the
     small and short case.
   - The bounded lookups, called through the product code, each use an index.

## Not done here (proposed)

- **WAL for the control database** (deferred in ADR 0178). Readers would stop waiting for the
  writer, and a commit would no longer wait for readers. It changes backup and restore, and the
  recovery copy that requires `journal_mode=DELETE`, so it needs its own decision.
- **Bound the inline chunk append.** Each 256 KiB chunk rewrites the whole staged blob
  (`staged_content || ?`), so an 8 MB output copies about 130 MB in 32 locked requests. The
  measured cost was flat at about 70 ms per chunk, so this was left alone. Writing large outputs
  through the chunk-stored path (fixed-size chunks) would remove it.
- **`current_operation_reconciliation`** reads every observation of the operation, through its
  index, up to five times per observation post. That grows with the run's length (about 100k
  steps at 50 minutes), but it is not a scan of the history. An incremental projection would
  bound it, but it changes the fail-closed cross-source ordering, so it is left for a separate
  decision.
- **The worker side.** ADR 0188 retries a stalled hand-off call. An acknowledgement of the
  quarantine hand-off that does not wait, or a hand-off timeout tied to the lease's remaining
  time, would make the worker robust to a stall that outlasts one retry.
