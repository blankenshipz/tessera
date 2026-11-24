# Stage watcher

The stage watcher is a small execution-plane agent that watches one execution attempt and reports
what it sees. It only observes and reports. It cannot stop, cancel, retry, resize or re-route
anything, and it makes no model call. It is **on by default** in the worker (opt out with `--no-stage-watcher`, see "Wiring it"), and so is the Salad executor (opt out with
`"stage_watcher": {"enabled": false}`, see "Enabling it on the Salad executor"). It may also file closed decision
*requests* (retry, resize) that control records as advisory events and nobody admits (see
"Decision requests"). Design background is in
[ADR 0087](decisions/0087-execution-plane-agents.md) and
[ADR 0111](decisions/0111-stage-watcher-decision-requests-and-remote-wiring.md); this page
describes the contract digest, the watcher, the Salad wiring and the requests.

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

The shipped probe reads local Docker inspect data. With `--stage-watcher-stats` it also samples
`docker stats` for `mem_bytes` (see "Wiring it"). Another provider (for example Salad) plugs in by
implementing the same interface; the watcher does not change. A Salad probe would run inside the
provider plane, where egress is provider-wide and not restricted per host, so it needs the
egress and kill-switch delivery conditions of ADR 0087 sections 3.2 and 7.4 first, and would be
deterministic observe and report only. No probe runs inside a Salad placement; Salad is watched from the executor through the snapshot file (see "Remote placements").

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
  returning `unknown` or `absent` three ticks in a row (a probe lost). For a remote provider, an
  `unknown` sample that still carries a fresh `progress_marker` does not count toward the three:
  the provider-side driver is alive and polling, for example while a group is allocating. Only a
  missing or stale snapshot, which has no marker, counts.
- For a remote provider, a sample `exited` with no exit code does not produce `failed`. The watch
  ends with outcome `exited_unknown`, and the platform's own exit observation records the outcome.
- A clean exit (code 0, not OOM-killed) ends the watch with outcome `exited_ok` and no report,
  because the closed set has no kind for it; the platform's own exit observation records it.
- The local Docker probe has no cumulative CPU or output counter of its own: `cpu_ms` is always
  absent and `output_bytes` comes only from an optional `output_size` callable the operator
  supplies. With no progress signal at all the watcher never reports `stuck` (it keeps sending
  heartbeats), so a healthy run is not flagged by mistake.
- Docker statuses map as: `running`, `restarting`, `paused` -> running; `exited`, `dead` ->
  exited; "no such container" -> absent; anything else, bad JSON or a label mismatch -> unknown.
  The probe never raises.
- Delivery: each report gets up to 3 attempts per tick with short backoff. Only a permanent
  refusal settles a report as refused (the watcher records it, counts it as `refused_reports`, and
  carries on with the next report): 400, 404, 410, 413 or 422 for `stuck`/`failed`, and the same
  plus 409 for `heartbeat`/`progress`; or, for any kind, a 403/404/409 whose `reason` code (see
  Refusal reasons) is in `PERMANENT_REASONS` and is the code control pairs with exactly that
  status in `REASON_STATUS` (for example 409 `stale_epoch` or `grant_reconciled`). The worker keeps
  only a `reason` that is a short lowercase token (`[a-z][a-z_]{0,40}`), never the detail text.
  Every other answer (a transport error, no status, 5xx, 401 whatever the body, 403 without such a
  code, since the control channel refreshes its credential, 408, 425, 429, 409 for a closing
  report with no reason, `too_early`, an unknown code or a code for another status, any other
  4xx) leaves the report pending under the same Idempotency-Key, and it is retried next tick. So a `stuck` or `failed` is never lost to a refusal that could clear on
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

A `failed` report may also carry the optional closed `exit` object, `{"exit_code": 0..255,
"oom_killed": bool}`. It is present only when the probe read the container as definitively
`exited` with an integer code, never for an unknown or absent probe, a passed deadline or an exit
with no code. Control refuses it on any other kind. A report without it is unchanged. It is the
only watch evidence that can ever close an attempt, and only under the fenced rule of
[ADR 0158](decisions/0158-exited-without-report-closes-attempt.md).

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

### Refusal reasons

Every definite refusal has the body `{"detail": "<text>", "reason": "<code>"}`. The `detail` text
is the same as before, and the codes form a closed set. `stage_watch_limits` exports them as
`PERMANENT_REASONS`, `TRANSIENT_REASONS` and `REASON_STATUS` (code to HTTP status). A permanent
reason means the same body can never succeed. A refusal with no `reason`, or with a code outside
the set, must be treated as transient. Examples are 401 (bad or expired credential), 403 (control
held after restore), 400 (missing Idempotency-Key) and 422 (schema). The worker's own retry rules
still apply to those.

| reason | status | kind | when |
| --- | --- | --- | --- |
| `unknown_operation` | 404 | permanent | the operation is missing or is not this worker's (no existence oracle) |
| `wrong_worker` | 404 | permanent | the worker's credential no longer covers the grant's project |
| `worker_fenced` | 409 | permanent | the operation is fenced pending independent recovery |
| `lease_mismatch` | 409 | permanent | `lease_id` is not the operation's lease |
| `stale_epoch` | 409 | permanent | `expected_epoch` is no longer current |
| `grant_mismatch` | 409 | permanent | the grant does not match the operation, lease or worker |
| `grant_reconciled` | 409 | permanent | the execution grant is already reconciled |
| `before_lease` | 409 | permanent | `observed_at` is earlier than the lease's `issued_at` |
| `out_of_order` | 409 | permanent | `seq` or `observed_at` is not after the last recorded report |
| `routine_budget_exhausted` | 409 | permanent | a routine report once 180 reports are recorded |
| `budget_exhausted` | 409 | permanent | a `stuck` or `failed` once 200 reports are recorded |
| `key_conflict` | 409 | permanent | the Idempotency-Key was already used with a different body |
| `decision_budget_exhausted` | 409 | permanent | a decision request once 16 decisions are recorded (decision route) |
| `decision_not_allowed` | 409 | permanent | a decision `kind` and `reason` that are not an allowed pair (decision route) |
| `too_early` | 425 | transient | `observed_at` is past the control clock plus skew; nothing is recorded |

A replay of an accepted report still returns its stored 201 (or 200 for a coalesced report), even
if a new report would now be refused.

## Campaign receipt: `stage_watch`

The campaign receipt gains an additive section `stage_watch`. It is absent when there were no
reports. Every field in it is an unverified worker claim; it never decides cost, success or custody.
Each attempt entry shows counts by kind, first and last seen, the last `stuck` and `failed`
counters, and `last_counters_seen` (the counters of the latest recorded report), so the worker's
self-reported `refused_reports` and `dropped_reports` are visible on the receipt.

## Reading the reports

`GET /campaigns/{campaign_id}/stage-watch?offset=0&limit=20` (limit 1-100; human, or an agent with
`campaign:logs`; tenant-scoped) and the MCP tool `tessera_campaign_stage_watch` return per attempt
the counts by kind and a page of recorded reports and decision requests, newest first. Closed
columns only (kind, seq, time, integer counters, closed decision codes); no paths or worker text.
All values are unverified worker claims.

## Kill switch

The contract names a marker file. Present means engaged. Absent under a valid parent directory
means clear. Anything else (missing parent, unreadable, error) means engaged. The
switch gates decision requests (engaged: none is filed) and, later, model calls; observing and
reporting continue regardless. A reduce-only stop is not implemented.

## What the watcher cannot do

No stop, cancel, retry, resize or re-route (it can only file a request for a retry or a resize,
which nothing admits). No management credential. No model call. No network
except its injected report sink.

## Wiring it

On by default (ADR 0125). Every `tessera-worker development-run` and `development-poll` starts a
watcher beside each local Docker stage with no flag. The kill-switch marker defaults to
`<workspace-root>/stage-watch.kill` inside the worker's own private state; the worker never creates
it. Absent means watching; present means engaged (the existing marker semantics). If that
directory is not private and usable, the worker still runs your stages with no watcher and prints
one line on stderr. The watcher adds no connection: reports go through the worker's existing
control channel.

Opt out only explicitly: `--no-stage-watcher`, or `"stage_watcher": {"enabled": false}` in the
`development-poll` JSON config. The same optional, closed `stage_watcher` block carries `enabled`,
`kill_switch_path` (absolute), `silent_seconds`, `heartbeat_seconds`, `max_reports`,
`delivery_timeout_seconds`, `stats` and the tick bounds; an unknown key refuses the config. Explicit
values win: `--stage-watcher --stage-watcher-kill-switch /abs/marker` (under the daemon, pass both
after `--`) overrides the config block and the default marker. A watcher flag on its own
(`--stage-watcher`, `--stage-watcher-kill-switch`, `--stage-watcher-stats`) is explicit and enabled; with no
marker path, like a config block that only tunes (`{"silent_seconds": 120}` stays enabled), it takes the default marker. A worker that opted out never loads the watcher library.

`tessera_worker.stage_watch_runner.StageWatchRunner` is set as the CPU supervisor's optional
`watch_runner`. Every supervisor `drive_once`/`reconcile`/`tick`/`cancel` hands it the result:
a running operation gets a watcher (once), an ended one is marked ended, and the runner does one
bounded tick. The contract comes from the operation and lease: provider `local-docker`, stage key
`local-cpu`, `deadline_seconds` = the operation's `max_duration_seconds`, and `silent_seconds` 900,
`heartbeat_seconds` 60, `max_reports` 200 by default (clamped to the deadline). The probe is
`DockerInspectProbe` over the adapter's own Docker `run`, its container name and its ownership
labels. The sink is `ControlSink` over the worker's existing control channel and credential. Each
report gets one attempt per tick with no sleep and a 2 s request cap, so the supervisor never waits
on it; a failed delivery is retried on a later tick. Any watcher exception is caught and counted
(`errors`); it never changes an execution result. The runner has no stop, cancel, retry, resize,
re-route or request method and receives no management credential.

The outbox lives in `<workspace>/stage-watch/` (0700): `<operation_id>.sqlite3` and the
contract it is bound to, `<operation_id>.contract.json` (both 0600). A restarted worker rebuilds a
flush-only watcher for every outbox that still has pending reports, even with no operation
running, and drops it once nothing is pending. A recovered running operation reuses its stored
contract.

The watcher has no run loop. The caller must keep calling `tick()`, or `flush()` (which delivers
pending reports without sampling), after `done` becomes true, until `finished` is true (`done`
and `pending_count == 0`). A caller that stops at `done` can leave a `failed` whose delivery
failed transiently in the outbox until the next start. The runner keeps an ended operation's
watcher until it is `finished`. A one-shot `development-run` that exits first leaves the
report in the outbox, and it is resumed only when a worker next runs over the same workspace.
`development-poll` gives each grant its own workspace, so a finished grant's leftover report is
not resumed. That gap is still open.

`--stage-watcher-stats` (disabled by default, only with `--stage-watcher`) makes the local Docker probe
also sample `docker stats` and fill the `mem_bytes` counter; without it `mem_bytes` stays absent.
It is an unverified worker claim like every counter. `cpu_ms` is not sampled this way because
`docker stats` gives a CPU percentage, not cumulative CPU time.

### Tick budget

Each supervisor tick spends at most a small wall-clock and post budget across all watchers
(defaults 1.0 s and 4 posts; config fields `tick_budget_seconds` and `tick_max_posts`), rotating
so no watcher is starved. The tick shares a thread with lease renewal, so it must stay short.
Reports left over stay pending in the outbox and go out on later ticks. Decision posts share the
same post budget and deadline, so they are bounded too. The per-post timeout is the smaller of
2 s, the control channel's own timeout and the time left in the tick. The watcher's `docker
inspect` and `docker stats` calls use a 2 s bounded runner.

## Remote placements (Salad)

A remote provider placement is watched from the provider-side driver, never from inside the
container. The container stays passive and holds no watcher, no management credential and no
control credential.

- **Where it runs.** The remote watcher runs in the process owner of the provider-side driver. For
  Salad that is next to the executor, under the worker credential. The control fences already
  accept the executor's worker credential, because it owns the external operation, its lease and
  its epoch. The watcher holds no management credential and adds no provider call.
- **The signal snapshot.** The provider-side component drops a non-secret snapshot after each
  successful poll: `tessera.stage-watch.signals.v1`, a closed JSON object
  (`schema`, `observed_at` with an offset, `state` of `running`, `exited`, `absent` or `unknown`,
  and optional integer `exit_code`, `output_bytes`, `cpu_ms`, `mem_bytes`, `restarts`,
  `progress_marker`, optional boolean `oom_killed`). The file is 0600 and written atomically. A
  snapshot older than the staleness bound is read as `unknown`.
- **Mapping for Salad.** `tessera_control.salad_watch_signals.signals_from_poll` is a pure function
  (no I/O, no network, no clock) over values the live-check loop already holds: `running` is
  running; a confirmed stop, or `stopped`, `succeeded` or `failed` after the first successful
  post-start poll, is `exited` with no `exit_code` (Salad gives none here, so the watch ends as `exited_unknown` and files no `failed`); `deleted` is `absent`;
  anything else, including a `stopped` group seen before start, is `unknown`. It accepts no
  identifier, group name, hostname, secret or financial value, so none can reach a snapshot.
- **The runner.** `StageWatchRunner.on_start_remote` is given the operation id, lease id, epoch,
  stage key, provider, deadline and plan digest. A probe-factory registry builds the probe for the
  provider; the generic snapshot-file probe reads `signals_path(operation_id)`. The runner turns
  the snapshots into the same heartbeat, progress, stuck and failed reports as the local watcher,
  through the same worker credential and route. Reports join the campaign receipt `stage_watch`
  section once the attempt row has its operation id; reports sent before that are stored but not
  shown.
- **What stuck means here.** For a remote route the only progress counter is `progress_marker`,
  which is the driver's count of successful observe and evidence passes. `stuck` therefore means
  "the provider-side driver stopped observing", not "the workload stalled". Salad gives no output,
  CPU or memory counter on this path.
- **Descriptive only.** The runner never consumes these reports for settlement, cancel or the
  unknown outcome. Control's own silent-executor window stays authoritative.

## Enabling it on the Salad executor

The executor watcher is on by default (ADR 0125): with no `stage_watcher` block it watches every
route with the default kill-switch marker `stage-watch.kill` in the executor's private `state_dir`
(present means engaged, with the kill-switch semantics above; Tessera never creates it). Opt out only with
`"stage_watcher": {"enabled": false}`. If the watcher cannot start, the executor logs one bounded
line and routes run unwatched. The live-check snapshot key stays opt-in on its own.
[ADR 0111](decisions/0111-stage-watcher-decision-requests-and-remote-wiring.md) makes the limited
ADR 0033 amendment for these two hooks, which edit `salad_live_check.py` and
`campaign_salad_executor.py` and nothing else in the frozen Salad plane. Both are read-only,
non-secret and spend-neutral, never raise into the route, add no credential and no egress.

1. **Snapshot write.** The optional live-check config key `watch_signals_file` names the path the
   poll loop writes after each successful poll: the `tessera.stage-watch.signals.v1` snapshot,
   atomic, mode 0600.
2. **Executor ticker.** The optional `stage_watcher` executor config block starts a ticker thread
   that drives `StageWatchRunner` while the route child process runs, with an in-process channel
   (`stage_watch_channel.py`, which accepts only the watch-reports route) and `SaladRouteWatch`
   carrying the snapshot path and operation facts. Fields: enable flag, the absolute kill-switch marker path
   (optional; defaults to `stage-watch.kill` in `state_dir`), and the optional bounds
   `silent_seconds`, `heartbeat_seconds` and `max_reports`. The block has no envelope fields.
3. **What appears.** The snapshot file at `watch_signals_file`, and in the worker workspace
   `stage-watch/<operation_id>.sqlite3` and `<operation_id>.contract.json` (both 0600). Reports
   reach control through the executor's existing worker credential and route.
4. **Kill switch.** The marker file of the contract. Present, unreadable or with a missing parent
   directory means engaged: no decision request is filed; reports continue.

A failed snapshot write is swallowed (it shows only as a stale snapshot, which the probe reads as
`unknown`); a failed tick or ticker thread is caught and counted. None changes the route, its result
or any spend. The Salad executor passes no envelope and its channel carries only reports, so
**Salad files no decision requests today**; Salad also gives no exit code and no out-of-memory
signal.

## Decision requests

The watcher may file a closed, receipted **request**. It never performs a retry, resize or
re-route.

- **Kinds and reasons.** `retry_request` goes with `transient_exit` or `probe_lost`;
  `resize_request` goes with `oom_killed` or `resource_limit`. The watcher produces only
  `transient_exit` and `oom_killed`. A pair outside this pairing is refused `decision_not_allowed`;
  an unknown kind or reason is a 422 with no reason.
- **Envelope.** Optional fields on the stage contract (covered by the digest: retry attempts
  remaining, transient exit codes, current and maximum memory) state what the approved stage
  allows the watcher to ask for. A contract without them files nothing. No shipped caller passes
  an envelope yet.
- **Record before send.** A request is recorded in the outbox before delivery, and only after the
  `failed` report is recorded. A restart between those two steps files no request.
- **Rate.** At most one request per kind per operation per epoch from the worker; at most 16
  decisions per operation recorded by control.
- **Kill switch.** Engaged means none is filed.
- **Wire.** `POST /worker/operations/{operation_id}/watch-decisions`, worker audience, same lease
  and epoch fences as `watch-reports`, `Idempotency-Key: watch-decision-<operation_id>-<n>`, sent
  from the same durable outbox.
- **Control records it as advisory.** Event `execution.watch_decision_recorded`,
  `observation_proof: trusted_worker_assertion`, `admission: "not_admitted"` always in this slice,
  and `bounds` (`within_envelope`, `blocked_by`) computed by control from the approved spec, not
  from the worker's envelope. It writes no observation, money, custody or attempt state and starts
  no attempt. A request outside the bounds is still recorded.
- **Resize is always outside the envelope for now.** No spec declares a stage memory size, so every
  resize request is recorded with `within_envelope: false` and `blocked_by: "cap_unknown"` until a
  spec declares a memory cap.
- **Refusals.** Two new permanent 409 reasons, `decision_budget_exhausted` (more than 16
  decisions) and `decision_not_allowed` (a kind and reason that are not an allowed pair), listed in
  the table above.
- **Receipt.** Each `stage_watch` attempt entry gains a `decisions` list; each entry has `kind`,
  `reason`, `decision_seq`, `requested_memory_bytes`, `within_envelope`, `admission` and
  `observed_at`. It carries no counters. Unverified worker claims.
- **Not built.** Admission by the runner (it moves spend and places work) needs its own reviewed
  ADR. Nothing reads a decision record to act.

## Limitations and next slices

- No actions of any kind. Salad files no decision requests today. Decision requests are recorded but never admitted (ADR 0087 S3 admission
  is not implemented and needs its own ADR).
- No Salad workload sidecar (S4 proper) and no model relay (S5). Salad is watched from the executor
  process, not from inside the placement.
- Reports are unverified worker assertions; a worker that stops reporting is visible only as a gap.
