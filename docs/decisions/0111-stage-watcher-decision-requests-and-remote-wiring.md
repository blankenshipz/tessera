# 0111 — Stage watcher decision requests and Salad executor wiring

## Status

Draft for review (2026-11-22). This record makes the limited
[ADR 0033](0033-freeze-over-built-planes.md) amendment that
[ADR 0087](0087-execution-plane-agents.md) proposed for the remote watcher, and records the
second watcher slice: closed decision *requests* filed by the watcher and recorded, advisory
only, by control. It is a follow-up to the first slice (contract digest and observe-only watcher,
described in [the stage watcher](../stage-watcher.md)) and to ADR 0087, whose model-call, escalation and
admission parts stay drafts. Everything the owner did not name below stays frozen. The owner
directed this amendment in the driver brief for the slice; it is limited to the two hooks in
decision 1.

## Context

The first slice proved the observe-only watcher on the local Docker worker path (`--stage-watcher`).
Two gaps remained, both named in ADR 0087.

1. **A Salad run emitted no reports.** The mapping from a live-check poll to a signal snapshot, the
   snapshot format and the runner's remote entry point exist, but two edits to frozen Salad files
   were left out: a snapshot write in the live-check poll loop, and a tick of the runner while the
   route child process runs (the executor blocks in `subprocess.run` during a route). ADR 0087
   recorded both as a proposed amendment, not requested.
2. **The watcher can only say "stuck" or "failed".** The hand steps it is meant to shorten (a retry
   after a transient exit, a resize after an out-of-memory kill) need the watcher to say what it
   would ask for. ADR 0087 section 4 specifies retry, resize and re-route as requests the runner
   admits, never actions the agent performs.

This slice does the first as a minimal, disabled-by-default hook pair, and the second as records only.
Nothing here moves spend, starts an attempt, or changes a decision the runner or control makes today.

## Decision

### 1. A limited amendment of ADR 0033 for two Salad hooks

The Salad plane may carry two additive hooks. Both are off by default and, when off, the
frozen files behave exactly as before.

1. **A per-poll signal snapshot write** in `salad_live_check.py`. After a successful poll, when the
   optional config key `watch_signals_file` is set, the poll loop writes the non-secret signal
   snapshot (`tessera.stage-watch.signals.v1`, built by the pure `signals_from_poll`) to that path,
   atomically, mode 0600.
2. **An optional executor watcher ticker** in `campaign_salad_executor.py`. When the optional
   `stage_watcher` executor config block is present and enabled, the executor starts a ticker
   thread that drives the stage watcher runner while the route child process runs. The block's
   fields are `enabled`, `kill_switch_path` (no marker, no watcher) and the optional bounds
   `silent_seconds`, `heartbeat_seconds` and `max_reports`, checked against the worker's own
   config. An in-process channel (`stage_watch_channel.py`) lets the runner record reports
   through the control store the executor already uses, and `SaladRouteWatch` (begin, ticking,
   end) hands the snapshot path and operation facts between the executor and the ticker thread.
   The in-process channel accepts only the watch-reports route.

The frozen files touched are exactly `salad_live_check.py` and `campaign_salad_executor.py`. No
other Salad, guardian, custody or workload-token file changes.

Guarantees, each tested on the Salad side:

- **Never raises into the route.** A failed snapshot write is swallowed; a failed tick or a
  crashed ticker is caught and counted. The route and its result are unchanged.
- **Never changes control flow or spend.** The hooks read values the loop already holds. They
  add no provider call, no create, start, stop, delete or scale, and no decision.
- **Disabled is identical.** With neither key set the code path, outputs, digests and receipts are
  the same as before this record.
- **No new credential and no new egress.** The ticker uses the worker credential the executor
  already holds, to the control route it already reaches. The snapshot holds no identifier, group
  name, hostname, secret or financial value (the mapping accepts none). The container still holds
  no watcher and no credential.
- **Read-only and non-secret.** The only new write is the local snapshot file and the watcher's own
  outbox.

ADR 0087's "proposed amendment" for S4 is now made by this record. Everything else in
ADR 0033, and the rest of ADR 0087's S4 (a workload sidecar inside the placement, the
kill-switch delivery to a remote placement, any model call on Salad), stays frozen or open.

### 2. Decision requests: the agent files requests, never actions

The watcher may file a **decision request**, a closed, receipted statement of what it would ask
the platform to consider. It still never performs a retry, resize or re-route, and it has no
method that does.

- **Kinds and reasons.** Control's closed pairing: `retry_request` goes with `transient_exit` or
  `probe_lost`, and `resize_request` goes with `oom_killed` or `resource_limit`. The watcher
  produces only `transient_exit` (an exit code the envelope lists, with retry attempts left) and
  `oom_killed` (an OOM-killed exit, with room to double memory up to the envelope cap). The other
  two reasons are accepted by control but the watcher never files them. A pair outside the
  pairing is refused `decision_not_allowed`; an unknown kind or reason is not a pair at all and is
  a schema refusal (422, no reason).
- **Envelope.** The stage contract gains optional envelope fields (`retry_attempts_remaining`,
  `transient_exit_codes`, `memory_bytes_current`, `memory_bytes_max`) describing what the
  approved stage allows the watcher to ask for; `decide()` files nothing outside them. A contract
  without the fields cannot file requests. The digest covers them, so a request names the
  contract it was filed under. The runner takes the envelope as an optional argument when it starts
  a watch. No shipped caller passes one yet (neither the local Docker wiring nor the Salad
  executor), so no request is filed in practice until a caller supplies it.
- **Rate.** At most one request per kind per operation per epoch from the worker side, and at most 16
  decisions recorded per operation by control.
- **Record before send.** A request is written to the watcher's durable outbox before any delivery
  attempt. It is filed only when the watcher records a `failed` report for an exited sample, so a
  restart between recording the `failed` report and recording the request files no request.
  That limitation is accepted; the `failed` report still goes out.
- **Kill switch.** The kill switch gates requests. When it is engaged (or unreadable, which counts
  as engaged) no request is filed. Observe and report continue, as ADR 0087 section 7 requires.
- **Wire.** A request is posted to `POST /worker/operations/{operation_id}/watch-decisions`, under
  the same worker audience, lease and epoch fences as the report route, with
  `Idempotency-Key: watch-decision-<operation_id>-<n>` (`n` is the `decision_seq`), through the
  watcher's durable outbox, on its own queue.
- **Control records it as advisory.** Control records the event `execution.watch_decision_recorded`
  with `observation_proof: trusted_worker_assertion`, a `bounds` object (`within_envelope`,
  `blocked_by`) computed by control from the *approved spec* (never from the worker's own
  envelope), and `admission: "not_admitted"`. In this slice admission is always `not_admitted`. The
  route writes no observation, money, custody or attempt state and starts no attempt. A request
  outside the bounds is still recorded, with `within_envelope: false`.
- **Retry bounds.** A retry is within the envelope only while the approved stage still has attempt
  numbers and retry budget, declares `transient_exit_codes`, and the operation belongs to the
  latest attempt of a running campaign; `probe_lost` is never within (an effect whose outcome is
  unknown is not retried). Otherwise `blocked_by` names the reason.
- **Resize bounds.** No spec version declares a stage memory size today, so control cannot
  determine a memory cap. Every resize request is recorded with `within_envelope: false` and
  `blocked_by: "cap_unknown"` until a spec declares a cap.
- **Refusals.** Two new closed, permanent 409 reasons, in the same `{"detail", "reason"}` shape as
  the report route: `decision_budget_exhausted` (the 17th decision for an operation) and
  `decision_not_allowed` (a kind and reason that are not an allowed pair).

### 3. Runner admission is not built

A request moves nothing. Admitting one (deciding that a new attempt, a larger resource class or a
different provider is used) moves spend, places work and competes with the runner's own
decisions. That is a different authority from observing and recording, so it is **explicitly not
built here and needs its own reviewed ADR**, which must also settle ordering, rate limits and
races with a runner decision (ADR 0087 open question 8). Until then the runner never reads a
decision record, and a person or the existing control paths act as they do today.

### 4. Resource events

On the local Docker path, an optional counter may be sampled with `docker stats` when the
operator passes `--stage-watcher-stats` (default off; `mem_bytes` only). The sample is an
integer in the existing closed report counters, an unverified worker claim like every counter.
`cpu_ms` is not added: `docker stats` gives a CPU percentage, not cumulative CPU time. Salad has no
resource counters on this path, no exit code and no out-of-memory signal, so Salad snapshots carry
none of them and a Salad run can never produce an `oom_killed` or `transient_exit` request.

### 5. Wiring

The local garage worker path was wired in the previous slice (`--stage-watcher`). The Salad
executor is wired by this slice, behind the `stage_watcher` config block of decision 1 and the same
kill-switch rule: no marker path, no watcher. The Salad executor passes no envelope, and its
in-process channel carries only watch reports, so the Salad executor files no decision requests
today. Posting requests from Salad needs both an envelope and a channel that accepts the
watch-decisions route, which a later slice would add.

### 6. Receipts

Every recorded decision appears on the campaign receipt. The `stage_watch` attempt entry gains a
`decisions` list. Each entry has `kind`, `reason`, `decision_seq`, `requested_memory_bytes`,
`within_envelope`, `admission` and `observed_at`; it carries no counters. All of it is an
unverified worker claim; `within_envelope` is control's computation but of a claimed request, and
`admission` is always `not_admitted`. The receipt never reads a decision as a cause or as
authority for what happened next.

### 7. How a remote probe sample is read

For a remote provider the platform's own exit observation records how the stage ended; the watch
only describes. Two rules follow.

- **An exit with no code is not a failure.** A probe sample `exited` with no exit code does not
  produce a `failed` report. The watch simply ends, with outcome `exited_unknown`, and the
  platform's own observation records the outcome. Salad is this case: a stopped or finished group
  gives no exit code.
- **A live driver is not a lost probe.** The three-in-a-row `unknown` or `absent` rule that ends
  in a `failed` report (probe lost) does not count an `unknown` sample that still carries a fresh
  `progress_marker`. A fresh marker means the provider-side driver is alive and polling, for
  example while a group is allocating. Only a missing or stale snapshot, which reads as `unknown`
  with no marker, counts toward the three.

## Consequences

- A Salad run can now emit descriptive reports, with the tick running on the executor's own
  process and credential. Advisory requests exist in the worker library and on the control route,
  but the Salad executor files none today (no envelope, a reports-only channel).
- A Salad group that stops without an exit code ends the watch as `exited_unknown` and leaves the
  outcome to the platform, rather than being reported as `failed`; a group still allocating is not
  reported as a lost probe while the driver keeps polling.
- The cost is a frozen-file change: two small edits plus one thread. The amendment is limited to
  those hooks and the guarantees above, and the Salad tests prove the disabled path is unchanged.
- Requests are advisory. Nothing acts on them, so a request on its own shortens no recovery.
  The value in this slice is the evidence trail and a measured basis for the later admission ADR.
- A thread in the executor is the main new risk: a stuck or slow tick must never delay a route.
  The ticker is bounded per tick, daemonised, and its failures are counted and swallowed.
- Decisions are worker claims. A worker that fabricates requests can only fill its own 16 slots and
  the receipt, never move money or work.
- Every resize is recorded as outside the envelope until a spec declares a memory cap, so a
  resize record is evidence only. A restart between recording `failed` and recording a request
  loses that request.
- Descriptive limits remain: on a remote route `stuck` means the provider-side driver stopped
  observing, not that the workload stalled.

## Alternatives considered

- **Let the runner admit requests now.** Rejected: it moves spend and places work, and it needs
  ordering and race rules that have not been reviewed.
- **Have the watcher act directly (retry, resize) under a kill switch.** Rejected by ADR 0087:
  the agent keeps no authority over spend or placement.
- **A watcher process separate from the executor.** Avoids a thread but needs a second
  credential holder or an IPC channel and still needs the snapshot write; rejected as a larger
  surface.
- **Write the snapshot from outside the poll loop (re-poll in the watcher).** Rejected: it would
  add provider calls, which this amendment forbids.
- **Record decisions as `observed` reports.** Rejected: the closed report kinds should stay
  descriptive; a request is a different statement with its own budget and receipt shape.

## Open questions

1. How and by whom a request is admitted (retry, resize, re-route): a separate reviewed ADR. Until
   then every record is `not_admitted`.
2. Whether and how the runner consumes a `stuck` or `failed` report. Today it never does; control's
   silent-executor window stays authoritative.
3. A snapshot write failure is swallowed and is visible only as a stale snapshot, which the probe
   reads as `unknown`; it is not visible to control or on the receipt. Whether to surface it.
4. Whether a resize request can carry a measured basis on Salad, which exposes no resource
   counters, and which spec field should declare a stage memory cap so a resize can be within the
   envelope.
5. Whether a request outside the approved envelope should be refused at control rather than
   recorded with `within_envelope: false`.
