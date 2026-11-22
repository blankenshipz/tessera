# Stage watcher

The stage watcher is a small execution-plane agent that watches one execution attempt and reports
what it sees. It only observes and reports. It cannot stop, cancel, retry, resize or re-route
anything, and it makes no model call. It is **off by default** and is a **library**: it is not wired
into the default worker service. Design background is in
[ADR 0087](decisions/0087-execution-plane-agents.md); this page describes the first slice (the
contract digest and the observe-only watcher).

Related: [ADR 0054](decisions/0054-durable-observation-delivery.md) (durable observation delivery)
and [ADR 0034](decisions/0034-governed-sandbox-egress-and-dataset-staging.md) (governed egress).

## The contract

`tessera_worker.stage_watcher.StageContract` is the fixed, per-attempt statement of what the
watcher may do. Fields: operation id, lease id, epoch, stage key, provider, `deadline_seconds`,
`silent_seconds`, `heartbeat_seconds`, `max_reports`, `kill_switch_path`, and a sha256 digest over
the others. It carries no credentials and no URLs. A change to any field changes the digest, and
every report carries that digest.

## The compute probe

`ComputeProbe` is provider-neutral. Its `sample(now)` returns:

- `state`: `running`, `exited`, `absent` or `unknown`
- exit code, an OOM flag
- integer `output_bytes`, `cpu_ms`, `mem_bytes`, `restarts`

The shipped probe reads local Docker inspect data. Another provider (for example Salad) plugs in by
implementing the same interface; the watcher does not change. A Salad probe would run inside the
provider plane, where egress is provider-wide and not restricted per host, so it needs the
egress and kill-switch delivery conditions of ADR 0087 sections 3.2 and 7.4 first, and would be
deterministic observe and report only. No Salad probe exists in this slice.

## Classification rules

Each tick the watcher samples and classifies:

- **heartbeat** on the contract's cadence (`heartbeat_seconds`). Heartbeats continue while the
  stage is stuck, so control can tell "still stuck" from "the watcher died".
- **progress** when `output_bytes` or `cpu_ms` changes in either direction (a container restart can
  reset them) or `restarts` rises, paced to at most one per routine interval (30 s); a faster
  change is folded into the next progress report.
- **stuck**, once per episode, after `silent_seconds` without such a change. After a watcher
  restart, silence is measured from the first sample after the restart, not from the original
  start, so a restart while the stage is making progress does not cause a false `stuck`.
- **failed** on a non-zero exit, an OOM, the deadline passing while still running, or the probe
  returning `unknown` or `absent` three ticks in a row.
- A clean exit (code 0, not OOM-killed) ends the watch with outcome `exited_ok` and no report,
  because the closed set has no kind for it; the platform's own exit observation records it.
- The local Docker probe has no cumulative CPU or output counter of its own: `cpu_ms` is always
  absent and `output_bytes` comes only from an optional `output_size` callable the operator
  supplies. With no progress signal at all the watcher never reports `stuck` (it keeps sending
  heartbeats), so a healthy run is not flagged by mistake.
- Docker statuses map as: `running`, `restarting`, `paused` -> running; `exited`, `dead` ->
  exited; "no such container" -> absent; anything else, bad JSON or a label mismatch -> unknown.
  The probe never raises.
- Delivery: each report gets up to 3 attempts per tick with short backoff. Only a permanent status
  settles a report as refused (the watcher records it, counts it as `refused_reports`, and carries
  on with the next report): 400, 404, 410, 413 or 422 for `stuck`/`failed`, and the same plus 409
  for `heartbeat`/`progress`. Every other answer (a transport error, no status, 5xx, 401/403
  whatever the message, since the control channel refreshes its credential, 408, 425, 429, 409 for
  a closing report, any other 4xx) leaves the report pending under the same Idempotency-Key, and it
  is retried next tick. So a `stuck` or `failed` is never lost to a refusal that could clear on
  retry; it is delivered, or abandoned after 24 h and counted as `dropped_reports`. Closing reports
  are flushed first, and a refused report never blocks later ones.
- `observed_at` is monotonic: each report is dated no earlier than the previous recorded report,
  even if the worker clock steps backwards, so control never refuses it as earlier than the last
  report. Counters such as `elapsed_s` still come from the clock.

## Shared limits

One set of numbers (`tessera_control.stage_watch_limits`) binds both sides, so a watcher never
plans to send more than control accepts:

| Limit | Value | Meaning |
|---|---|---|
| `MAX_REPORTS_PER_OPERATION` | 200 | Reports of all kinds recorded per operation. A contract's `max_reports` is at most this. |
| Routine budget | 180 | `heartbeat` and `progress` are accepted only while fewer than 180 reports are recorded. |
| `TERMINAL_RESERVE` | 20 | Held back for `stuck` and `failed`, which are accepted up to the 200 total. |
| `MIN_ROUTINE_INTERVAL_SECONDS` | 30 | At most one recorded report per routine kind per 30 s of `observed_at`. |

Routine reports therefore cannot use up the budget a later `failed` needs. The watcher keeps the
same reserve for its own `max_reports` (`routine_limit`), and counts it the way control does:
routine reports stop once that many reports of any kind (including `stuck`) are recorded.

## The report body

The body is closed: `lease_id`, `expected_epoch`, `kind` (`heartbeat`, `progress`, `stuck` or
`failed`), `observed_at`, `seq`, `counters` (`elapsed_s`, `silent_s`, `mem_bytes`, `cpu_ms`,
`output_bytes`, `restarts`, `refused_reports`, `dropped_reports`; integers only), `kill_switch_engaged`, and `contract_digest`.

It never contains log lines, prompts, credentials or any free text.

`refused_reports` and `dropped_reports` are the watcher's own count of reports control refused
and reports it dropped (abandoned after 24 h, or dropped at the total report cap). They tell control about gaps it could
not otherwise see; like every counter they are unverified worker claims.

## Durable outbox

A report is recorded in a local durable outbox before it is posted. Delivery is retried with the
same `Idempotency-Key` (`watch-<operation>-<seq>`), so a redelivery cannot duplicate. A report still
undelivered after 24 hours is abandoned, and the abandonment is shown as an explicit gap, not
hidden. A `stuck` or `failed` is not abandoned while a later report is still pending. The outbox
database and its SQLite rollback journal are owner-only (0600).

## Control route

`POST /worker/operations/{operation_id}/watch-reports`

- Worker audience, fenced by lease and epoch in the same way as the egress journal.
- It never writes observations, money, custody or attempt state.
- It records the event `execution.watch_reported` with `observation_proof: trusted_worker_assertion`.
- `observed_at` must be no earlier than the lease's `issued_at` and the last recorded report
  (otherwise 409), and no later than the control clock plus the observation future skew (5 s).
  A report dated later than that is answered 425 `observed_at is ahead of the control clock;
  retry` and records nothing (no event, no idempotency record), so the identical body under the
  same Idempotency-Key is accepted once the control clock catches up. There is no lease-expiry
  bound.
- Routine reports (`heartbeat`, `progress`) are coalesced to one per kind per 30 s: a faster one
  returns 200 `{"recorded": false, "reason": "coalesced"}` and records nothing. `stuck` and
  `failed` are never coalesced.
- Budget (see Shared limits): a routine report once 180 reports are recorded is refused with 409
  `Routine watch report budget exhausted.`; a `stuck` or `failed` once 200 are recorded is refused
  with 409 `Stage watch report budget exhausted.`. At most 200 reports are recorded per operation.
- An out-of-order `seq` or `observed_at` is refused with 409; an exact replay under the same
  Idempotency-Key returns the stored response.
- Lookups read only the operation's project events (index `events_by_project_cursor`), never the
  whole events table.

## Campaign receipt: `stage_watch`

The campaign receipt gains an additive section `stage_watch`. It is absent when there were no
reports. Every field in it is an unverified worker claim; it never decides cost, success or custody.
Each attempt entry shows counts by kind, first and last seen, the last `stuck` and `failed`
counters, and `last_counters_seen` (the counters of the latest recorded report), so the worker's
self-reported `refused_reports` and `dropped_reports` are visible on the receipt.

## Kill switch

The contract names a marker file. Present means engaged. Absent under a valid parent directory
means clear. Anything else (missing parent, unreadable, error) means engaged. In this slice the
switch gates only future requests and model calls; since none exist yet, observing and reporting
continue regardless. A reduce-only stop is not implemented.

## What the watcher cannot do

No stop, cancel, retry, resize or re-route. No management credential. No model call. No network
except its injected report sink.

## Wiring it

Not wired by default. It is a library: an operator-run wrapper constructs a `StageContract`, a
probe and a report sink, and calls the watcher. The default worker service does not do this, and
nothing starts a watcher unless an operator writes that wrapper. The slice that wires it into the
worker is still open.

The watcher has no run loop. The caller must keep calling `tick()`, or `flush()` (which delivers
pending reports without sampling), after `done` becomes true, until `finished` is true (`done`
and `pending_count == 0`). A caller that stops at `done` can leave a `failed` whose delivery
failed transiently in the outbox until the next start.

## Limitations and next slices

- No actions of any kind (ADR 0087 S3: requests admitted by the runner, not implemented).
- No Salad sidecar (S4) and no model relay (S5).
- Reports are unverified worker assertions; a worker that stops reporting is visible only as a gap.
