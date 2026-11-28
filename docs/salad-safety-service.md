# Independent Salad guardian and quarantine custody

This is a separately launchable service, not a signed readiness assertion or a
workload sidecar. Default invocation is disabled and performs no file/network
effects. Local tests exercise real TLS, ES256 and retained SQLite bytes, with
synthetic provider observations. No deployed service or live Salad stop is proven.

## One-time administrator setup

Provision a distinct trusted host/service identity and supervisor independently of
control, proxy and provider workload. Do not expose its files or management
credential to those clients. Install the `salad` optional dependency from the
project lock; missing `cryptography` fails closed. No dependency is installed by
the service. Provision owner-private mode-0700 state and credential directories,
mode-0600 configuration/admin credential/TLS private-key files, a public TLS
certificate with the approved hostname, and the separately authorized management
credential through `PrivateFileSecretResolver`. No credential discovery occurs.

The private configuration has these keys (paths and IDs are explicit references,
not embedded credentials):

```json
{
  "state_directory": "/PRIVATE/salad-safety/state",
  "credential_directory": "/PRIVATE/salad-safety/credentials",
  "credential_reference": "credential-ref:salad-guardian",
  "credential_version": 1,
  "organization_ids": {"APPROVED_ORGANIZATION_NAME": "EXACT_ORGANIZATION_UUID"},
  "destination": "custody.example.invalid",
  "listen_host": "127.0.0.1",
  "listen_port": 9443,
  "tls_certificate": "/PRIVATE/salad-safety/certificate.pem",
  "tls_key": "/PRIVATE/salad-safety/key.pem",
  "admin_token_file": "/PRIVATE/salad-safety/admin-token",
  "total_cap_cents": 10000,
  "control_origin": "http://127.0.0.1:8000"
}
```

`control_origin` is optional: where the guardian pulls input snapshots from. It
follows the provider-input client rules: HTTPS, or plain HTTP only to loopback
(the co-hosted control-gateway deployment). Without it the input endpoint and the
admin `input_ticket` are refused.

`total_cap_cents` is optional (integer 1..1,000,000). When set, the service
refuses any reservation whose per-run cap, added to every other reservation's
committed estimate, would exceed it (see below). Omitted means no total cap.

`max_open_billable_frames` is optional: an integer from 1 (the default) to 4. The service
refuses to start with anything else (0, 5, a bool, a string, a float or null). With more than
one frame allowed, every per-frame rule below holds for each frame and the total cap holds in
aggregate (see **Open billable frames**); the unseen-run gap each admission charges grows with
the setting (ADR 0041 section 7.2, resolved question 4b).

The configuration is a closed set. Required: `state_directory`, `credential_directory`,
`credential_reference`, `credential_version`, `organization_ids`, `destination`, `listen_host`,
`listen_port`, `tls_certificate`, `tls_key`, `admin_token_file`. Optional: `total_cap_cents`,
`allocation_deadline_seconds`, `provisioning_deadline_seconds`, `control_origin`,
`max_open_billable_frames`, `max_prestart_rebinds`, `prestart_read_deadline_seconds`,
`unix_socket` and `proxy_uid` (given together). Any other key (a misspelt `total_cap_cent`, say),
a missing required key or a value of the wrong type (a JSON boolean is not an integer) refuses the
whole file before anything is fetched or opened; the message names the key, never its value
(an unknown key's name is cut to 64 characters). A real start and `--check-config` call the same
parser (`validate_guardian_config`, ADR 0214 and ADR 0215).

Exit codes: `0` for a clean stop or `--check-config` success, `2` only when the operator's own
configuration is refused (the file, a key, or a file it names being unsafe), `1` for every other
fatal error (the 2-second JWKS fetch failing at boot, a bind or TLS error, an exception in the
serve loop). The stdout line says which: `{"state":"unavailable","reason":"config_refused"}` or
`..."reason":"service_prerequisite_or_runtime_failure"`. The systemd unit restarts exit 1 and
not exit 2.

`--check-config --config FILE` runs the same read and validation plus the owner/mode checks on the
TLS key, the admin token, the state and credential directories and the unix-socket directory, then
prints one `tessera.config-check.v1` JSON line (`widths`: `max_open_billable_frames`) and exits 0
or 2. It fetches no JWKS, reads no key or token bytes, binds nothing and opens or writes no state.

`--health-file PATH` writes a `tessera.service-health.v1` file (`name` `salad-guardian`) before the
serve loop, from the loop at most every 5 seconds, and `stopped` on exit. A failed health write is
reported on stderr and never stops the guardian.

The admin token is an independently provisioned random bearer credential of at
least 32 bytes, never the Salad management credential. Trusted clients pin the
server certificate digest and verify hostname/CA with TLS 1.3. Workloads use only
short-lived native IMDS JWTs, never this admin credential. A public workload TLS
hostname must route directly to the service's bounded HTTPS listener, with any
fronting infrastructure explicitly reviewed; no forwarded identity is trusted.

```sh
# No effects:
.venv/bin/python -m tessera_control.salad_safety_service
# Explicit independently supervised process; may read/stop admitted exact groups:
.venv/bin/python -m tessera_control.salad_safety_service --run --config /PRIVATE/service.json
```

An optional Linux-only `unix_socket` plus `proxy_uid` accepts the existing
`SaladSafetyClient` newline JSON protocol and authenticates `SO_PEERCRED`. Its
parent directory is service-owned 0700 and socket mode 0600. This same-host mode
requires deliberately configured access; use HTTPS for separate hosts/UIDs.
Existing sockets are not automatically removed on restart. An operator must
verify the old service is dead and remove only that exact stale socket, preserving
all databases. HTTPS has no stale-socket prerequisite.

## Normal operation and interfaces

Reservation admission and every bind/ready/heartbeat recheck require the pinned
stop credential to remain valid **strictly beyond the execution deadline plus a
60-second recovery margin**. Insufficient coverage fails before provider reads or
provisioning. This does not renew credentials or prevent later revocation/outages;
those remain unresolved stop/exposure liabilities. Still-valid stop-only use does
not require the full admission margin. Stop verifies the exact organization,
project, reserved group name and bound UUID, but does not let mutable workload
configuration drift block shutdown; create/start remain strictly fenced. A stop
acknowledgement remains pending, never physical termination/no-writers proof.

`SafetyHTTPSClient(host, port, token_file=..., ca_file=...,
certificate_sha256=...).exchange(envelope)` posts to `/v1/admin`.

1. `reserve`: the existing envelope contains `reservation`, `route_id`, `org_id`,
   `project_id`, approved `proposal`, full `routing_package`, retained immutable
   `runtime_package`, `route_digest` and
   numeric `deadline`. The digest binds the complete approved package including
   tenant/project/task/proposal; admission/proxy must
   authenticate current human/task authority **before** sending it. The service
   retains the exact approved argv, reserves one operation-wide byte/file budget,
   and retains the supplied lease-bounded startup-plus-run deadline before provider
   creation. It must be unexpired and within the workload's runtime bound; duplicate
   reservations never extend the first deadline.
   The runtime package's canonical digest must match `proposal.package_digest`;
   operation/lease/epoch/project/task/plan/image/data/hard-expiry bindings and the
   approved spec/caps are independently checked. Only reviewed
   `argv_binding=smoke_operation_v1` can substitute server-known operation/epoch
   into the two fixed literal slots; default `exact` remains exact. Retained
   source package/spec bytes are never rewritten.
2. `ready`: fresh actual service-loop heartbeat and open durable custody are
   required. It is not a signature/configuration boolean. Recheck before create.
3. `bind`: after stopped creation, pass the reserved provider ID. The service
   independently reads the approved immutable group through the real transport;
   supplied allocation IDs are not evidence. It arms the exact group before
   start, accepts the first independently observed allocation, and never adopts
   replacements. Recheck `ready` before start. A pre-start stopped group is not
   prematurely treated as completed work.
4. `heartbeat`: send at least every 30 seconds while current control authority
   remains valid; it cannot undo stop-only state or extend the fixed deadline.
5. Workload `POST /v1/workload/begin-once` contains `binding=asdict(Request)` and
   `Authorization: Bearer <IMDS JWT>`. Exact provider-derived group/machine identity
   and exact retained argv are checked. Operation-wide consumption commits before
   granting computation; a lost response cannot regrant computation.
6. Workload `POST /v1/workload/output` contains `binding`, `sha256`, and `output`
   JSON. Canonical bytes/checksum/caps are verified and atomically retained under
   private quarantine. No overwrite, additional filename or unbounded upload is
   available to this fixed one-output smoke recipe. General bounded checkpoint
   primitives remain available through `SaladCustody`, not a workload-selected
   destination on this smoke endpoint.
7. `cancel` closes all operation-epoch writers and invokes stop-only enforcement.
   `status` reports terminal/unknown state, orphan/allocation uncertainty,
   quarantine and unknown final charges. `import` returns sealed retained output
   and manifest for independently authorized control artifact import/forensics;
   it never publishes or scientifically accepts output.

**Loop freshness and admin refusal codes.** `ready`, `bind`, `heartbeat`,
`reserve` and `input_ticket` require a guardian loop pass at most 2 s before the
request was *accepted* (the unix connection accept or the HTTPS accept, before the
frame/body read), not before the reply. The loop and request handling share one
thread, so the bounded provider reads a request makes itself (a heartbeat's rate
refresh, up to two 2 s reads) delay the next loop pass but can no longer make the
guardian refuse its own heartbeat as `guardian_loop_unready`. A missing, non-finite,
future or pre-loop acceptance stamp is refused (the HTTPS listener passes NaN when it
has no stamp, so it never gets the direct-call default of "now"), and a request that completes more
than frame read + admin alarm (2 + 10 s) after acceptance is refused as
`admin_deadline_exceeded` (normally the alarm fires first). Expiry, stop deadlines
and budgets are still judged at request time.

On the trusted unix admin socket only, a refusal is
`{"error":"request_rejected","code":<code>}`. The code is the guardian's own
`ValueError` identifier when it matches `^[a-z][a-z0-9_]{0,63}$` (for example
`guardian_loop_unready`, `exposure_fenced`, `stop_only`); every other error,
including library errors, alarms and anything that could carry text, paths or
values, is `internal`. The HTTPS listener (workload and admin paths) still answers
a bare `request_rejected`: it is the network-exposed surface, and its admin client
reports every non-200 as unavailable. `SaladSafetyClient` raises
`GuardianRefused` (a `ValueError`, message `independent service refused: <code>`,
`.code`; a reply without a valid code, or with the reserved `unavailable`, is
`internal`) and keeps
`GuardianUnavailable` (`independent service unavailable`) for a missing/unsafe
socket, connect/timeout failure or cut-off reply. The trusted server records a
refused heartbeat as `input_log` event `heartbeat_refused` with the code (and adds
the code to `refresh_failed`/`drop_failed` records); the live-check receipt carries
`guardian_refusal: {code, cause}` when a refusal ended the run and a
`guardian_transient_refusals` count.

The live driver's keepalive classifies refusals. Transient, and only when the
guardian itself returned them from the heartbeat call: `guardian_loop_unready` and
`workload_verifier_unready`; it skips that beat and
retries at the next poll interval, at most 3 times in a row and only while the next
beat (about one interval away) still lands within 30 - 5 s of the last successful
beat's start; otherwise it cancels with cause `silence_bound`. Terminal, cancelling
at once: `unavailable` (no answer; a down guardian enforces nothing), revoked
control authority (even when the follow-up guardian cancel fails), and every other
code, including `stop_only`, `exposure_fenced`,
`budget_binding_expired`, `stop_credential_window_insufficient`, `custody_unready`,
`guardian_unready`, `budget_*`, `service_deadline` (a request alarm or provider
deadline expired while the guardian handled the request), `internal` and unknown codes.
For `internal` and `service_deadline` the guardian writes one journal line with the
exception class only (never its message). `rate_unknown` is
terminal because on the heartbeat path the guardian raises it only after fencing the
reservation. A *running* guardian stops a run on 30 s of heartbeat silence by itself;
a guardian that is down does not, which is why `unavailable` is terminal. The client
reserves `unavailable` for "no answer": a guardian reply carrying it, or any code
outside the closed pattern, is reported as `internal`.

**Input delivery (ADR 0036 phase 2).** The trusted server (which holds the worker
identity) mints a provider input ticket and passes it to the guardian only, with the
authenticated admin envelope `{operation:"input_ticket", reservation,
input_ticket:{token, expires_at, snapshot_id, snapshot_digest}}` (`input_ticket:null`
drops it). The ticket never enters the Salad profile, env, argv, status or any
durable file; the guardian holds it in memory and drops it on cancel, recovery,
deadline or retained-output stop. The guardian refuses a ticket whose
`snapshot_digest` is not the route's approved `manifests.data`; for a `workload_v1`
route it also refuses (`input_not_declared`) unless the reviewed contract declares
`input`, and caps the served total at `min(disk_mb, input.max_bytes)`. The bound workload
reads input with `POST /v1/workload/input` and `Authorization: Bearer <IMDS JWT>`,
under exactly the begin-once identity checks (JWT bound to the group and machine,
allocation not uncertain, not in recovery, within deadline and heartbeat), and only
after the operation's begin-once:

- `{"binding": <request>, "request": "manifest"}` returns
  `{schema:"tessera.input-manifest.v1", snapshot_id, digest, manifest, delivery:"chunked", chunks:[{seq,relative_path,offset,size,sha256}]}`.
- `{"binding": <request>, "request": "chunk", "seq": N, "offset": O, "length": L}`
  returns one **canonical** range: `O` a multiple of 1 MiB inside the stored chunk and
  `L = min(1 MiB, size - O)`; anything else is refused. It returns
  `{schema:"tessera.input-range.v1", snapshot_id, seq, offset, length, content_base64}`.
  The workload reassembles each stored 4 MiB chunk from its ranges and verifies it
  whole (size and sha256 against the index) before writing it.
There is no whole-chunk request.

Every input reply, including the manifest with its chunk index, is at most
`INPUT_REPLY_BYTES` (1,402,197 B; a larger manifest is refused, and setup and route
admission refuse such a snapshot first). Input requests run under the 20 s chunk
alarm: headers 3 s + small body 1.70 s + auth and one provider read 3 s + one
control fetch on a cache miss 2.5 s (a wall-clock alarm around the whole fetch, not
only a per-recv socket timeout) + the reply at the 1.5 Mbit/s floor 8.48 s =
18.68 s, asserted at import. The guardian keeps one verified stored chunk per
operation, so the ranges of a chunk cost one control fetch. A reply that is cut off
on the wire (reset, timeout or the alarm) is refunded from the served-byte budget,
at most 16 times per ticket, so retries do not exhaust it. A client that reads a
reply and then resets the connection can therefore read up to 16 extra ranges
(about 16 MiB) per ticket beyond the budget; this is bounded and accepted. The served, refund and
failure counters carry across a ticket drop and re-install for the same snapshot, so
a ticket expiry or revoke never resets them.

Every input request whose JWT is for the bound group and machine (after begin-once,
binding matched) is charged to a per-operation counter **before** any provider read,
refused requests included: at most `2 × (1 + canonical ranges of the verified
snapshot) + 32` (before the manifest is verified only the manifest read is planned).
An exhausted budget refuses without touching the shared Salad key.

The guardian fetches from control `/provider-inputs/*` with the ticket and verifies
before relaying: manifest digest equals `manifests.data` and is re-derived from the
manifest, the chunk index matches the manifest, each chunk's sha256 and size match
the index, total bytes are at most min(2 GiB, route `disk_mb`), and re-reads are
bounded at twice the snapshot size. One chunk is in memory at a time (the
one-chunk cache) and nothing is written to disk. The workload re-verifies end to end with
`materialize_chunked_snapshot`. The trusted server reissues (revoking the old ticket)
when the remaining life is under min(600 s, max(60 s, a quarter of the issued lifetime)), from its heartbeat, and passes the new ticket by
`input_ticket`; it revokes at control, best effort and recorded in
`TrustedSaladServer.input_log`, when it stops the route. A ticket's lifetime follows the lease's hard expiry, the release and one hour; the
150 s soft lease (ADR 0189) is re-checked on every pull. A minted ticket the guardian never received
is discarded once it is near expiry and a new one is minted. The guardian refuses input
for a budget-fenced or rate-unknown reservation, bounds failed control fetches (3 per
manifest or chunk, 12 per ticket) and installs an admin ticket only after the
readiness checks pass. Each input call costs one provider group read (the same
identity read as begin-once) and no rate read. The guardian handles one
request at a time, so a pull blocks its loop for the duration of the control fetch.

`status.retained_output` is read-only metadata before stop: exact custody binding,
`name:"output.json"`, `retained`, `sha256`, `bytes`, and `quarantine:true`. Missing
output has `retained:false` and null hash/size. A true result requires completed
durable upload plus current byte rehash/cap verification; it contains no raw output
and does not close writers, seal inventory or pretend execution finished. The
normal driver waits for this metadata, then cancels, verifies provider stop and
sealed custody, then imports bytes. It must not call `import` to poll for upload.

**Two windows.** The `deadline` is the control route window, the outer bound
and the provisioning deadline (image pull and allocation are unbilled and may
use most of it). Separately the reservation stores `running_seconds`, taken
from the approved argv's `MAX_SECONDS` (never longer than the route window),
and `first_running_at`, set when the service first observes the bound instance
running. The guardian stops the run at `min(deadline, first_running_at +
running_seconds)`, or at `deadline` if the instance is never seen running.
Pre-create admission requires `rate * running_seconds <= max_cost_cents`.

**Total cap.** On `reserve` with `total_cap_cents` set, `committed` is the sum
over all other reservations of their accrued estimate when stopped/reconciled,
else `max(accrued, per-run cap)`, plus this reservation's `max_cost_cents`;
`committed > total_cap_cents` fails with `total_budget_exhausted`. Admin
`status` adds `total_cap_cents`, `total_committed_cents` and
`total_remaining_cents` (null without a cap), plus `first_running_at`.

**Per-frame isolation and the lost-create hold (ADR 0191).** Each open frame runs in its own guarded step of the loop pass. If it
raises, the service records an event and a frame note of kind `loop_frame_failed` (the exception type name only), fails that frame closed
(recovery, budget fence, stop) and carries on with the other frames; the aggregate total-cap check still runs at the end of every pass. A
frame closed as a *lost create* (resolved `automatic` or `operator`) keeps its hold counted against `total_cap_cents` for the 24 h
late-resource watch, so a late group that reopens it cannot push the total past the cap; its open-frame slot is freed at once. A create
proven absent after Salad's own clean rejection frees its hold immediately.

**Open billable frames.** A `reserve` is refused (wire code `single_operation_orphan_exposure`)
while the number of other frames that may still bill is at `max_open_billable_frames`. A frame
counts until its status is `stopped` or `reconciled` (a create proven absent reconciles); an
ambiguous create with no provider id still counts. A stopped, reconciled or absent frame frees the
slot at once, with no wait. Each refusal writes a `fence_refusals` row in the service database (the
reservation and operation refused, the open count and the limit; no other frame's ids), apart from `events`. The
total-cap refusal is checked first and is not recorded as a fence refusal. Admin `status` adds
`max_open_billable_frames` and `unseen_run_gap_seconds`.

With `max_open_billable_frames` above 1 the guardian keeps these rules:

- *Aggregate total cap.* The total-cap sum, the open-frame count and the insert run in one
  `BEGIN IMMEDIATE` transaction, so a reserve sees every other frame's hold. Each frame is
  accounted on its own rate, start time and ledger row; one frame's accrual never offsets
  another frame's hold. After every loop pass the guardian recomputes the total. If an
  overshoot or a cap lowered across a restart puts it over `total_cap_cents`, every frame
  that may still bill is fenced and stopped (event `total_cap_breached`, cause `total_cap`), not
  only the one that overshot. If the total cannot be read at all, every such frame is fenced the
  same way but under its own cause `accounting_unavailable` (event of that kind, the exception
  type name only): the cap was never shown breached, so control may retry the stage within
  `retry.infra_retries` (ADR 0215), while a real `total_cap` stop is never retried.
- *Per-frame stops.* A frame's own cap breach, rate change, deadline, heartbeat loss or
  reallocation fences and stops that frame only. Stopping or settling one frame frees one
  slot. An ambiguous create still counts.
- *Distinct provider groups.* A reserve naming the same organization, project and group as
  another open frame is refused (`provider_group_in_use`). The guardian also refuses to bind
  a provider group that another open frame already holds (`provider_identity_in_use`).
- *Longer unseen gap.* One loop pass reads every open frame. Beyond the first frame, each
  can add up to 8 provider requests (rate, group, instance, observe, stop and close check),
  each bounded by the 2 s transport deadline. So the unseen-run gap is
  `60 + (N - 1) * 16` seconds: 60, 76, 92 or 108. Admission, accrual backdating and
  begin-once all charge it. A longer pass can also make the guardian stop a healthy frame
  as `missed_poll`, or deny it a read retry because the pass's shared retry window is
  spent. Both stop the frame, so they cost a run, never money.

Before new work/arming/begin, the service independently reads the selected GPU's
fresh official high-priority published hourly rate through `gpu_class`. Its
durable Decimal exposure ledger conservatively charges every second from the
first observed running instant until stop is confirmed (unbilled provisioning
before it is not charged), and unknown seconds after it. Planned remaining exposure must
fit the approved `max_cost_cents`; stale/unknown/changed rate fences new work and
initiates stop-only policy. Active provider/rate polls are paced at five seconds. A fast
transient rate-read failure (`provider_unknown`, such as a Salad 5xx or reset, or a
timeout) is retried at most twice (0.2 s, then 0.4 s backoff) and only while another full
read (the transport's request timeout, 2 s) fits a 4.5 s budget inside the loop pass,
so a read that failed by timing out still gets one retry. A stale or mismatched observation, a
credential refusal or any other error is never retried. Each attempt must still return an
exact observation at most 5 s old. When every attempt fails, the rate is unknown and the
frame is fenced and stopped, with one bounded exception
([ADR 0204](decisions/0204-guardian-transient-read-tolerance.md), amending
[ADR 0031](decisions/0031-salad-single-container-gpu.md)): on a running frame (bound, billing
begun, rate known and not fenced), a read that gave up on a transient failure (timeout or
`provider_unknown`) keeps the last known rate while that rate's observation is under
`RATE_STALE_WINDOW_SECONDS` (60 s) old. Only the guardian loop and the driver heartbeat take
this tolerance; reserve, bind, ready and begin-once stay strict. Accrual and the per-run cap
check continue at the last rate, and the frame is fenced when the window ends, a fresh read
shows a changed price, or required exposure at the last rate exceeds the cap. The route
deadline, heartbeat loss and the total cap are unchanged. A frame that is already stopping
(stop-only recovery set, or its guardian assignment no longer `armed`, for example after the
output was retained) is neither re-read nor fenced; accrual at the last rate continues until
the stop is confirmed. The next rate poll is paced from the end of the sequence. Retries are added to the reservation's cumulative
`rate_read_retries` (status) whether or not a later attempt succeeds, and appear on the
live receipt as `guardian_rate_read_retries`. The guardian's group/instance reads follow
the same rule (cumulative `provider_read_retries`, receipt
`guardian_provider_read_retries`); each read kind (the rate read; the group/instance reads)
has its own 4.5 s window per loop pass, measured from its first read in the pass, so a slow
rate read cannot starve a group-read retry. A retry starts only while that window still
has room for its backoff and a full read, so retries never run past the window's end;
first attempts are unchanged and may still take the pass past it. The loop-pass
allowance (`LOOP_PASS_ALLOWANCE_SECONDS`, 12 s) covers that worst case: both windows, one
first-attempt read started at the end of a window (2 s transport deadline) and 1 s of
local work; the 60 s unseen-run gap that backdates accrual is sized with it (it needs only
44 s for the pass; ADR 0204 widened it to equal the guardian's post-start read window). The retry
windows add up to 4.5 s of stop latency on a pass whose reads keep failing; that is
accepted, as at pilot rates it costs a negligible amount. Group and instance reads share
one failure kind (`provider_read_failed`) and so one window: the module pins the set of
kinds to two (`rate_read_failed`, `provider_read_failed`), and a third would put the worst
case past the gap. The allowance covers the polled reservation; first attempts for any
further reservation are bounded by the transport deadline only, not by the windows. One window
of each kind serves all reservations in the pass, so an early reservation's retries can
leave none for a later one, which then fails closed. When every attempt fails, the rate read
follows the rule above and a failed group/instance read is the guardian's: before start it
is retried within the pre-start read deadline, and after the run began it is retried at
each poll for at most `POSTSTART_READ_WINDOW_SECONDS` (60 s, equal to the one-frame
unseen-run gap admission charges) and `POSTSTART_READ_RETRY_LIMIT` (13) consecutive
failures, then stops as `provider_unavailable` (ADR 0181, widened by ADR 0204). The closed
reason the read gave up with (`timeout`,
`provider_unknown`, `rate_stale`, `rate_observation_invalid`, ...) is kept, at most once
per reason per minute, as `last_rate_read_failure` / `last_provider_read_failure` in status
(the most recent ever, not cleared by later successful reads; `*_at` is the time of the
latest recorded event, which is the first occurrence within its one-minute deduplication)
and `guardian_last_rate_read_failure` / `guardian_last_provider_read_failure` on the live
receipt. The first cause of a guardian's own stop decision
(`provider_unavailable`, `provider_unknown`, `missed_poll`, `control_disconnect`,
`deadline`, ...) is kept as `stop_cause` in status, even after the operation later turns
`stopped`, and as `guardian_stop_cause` on the live receipt. A stop the service itself
starts records its first closed cause the same way (`heartbeat_silence`, `deadline`,
`clock_uncertain`, `rate_unknown`, `cost_cap`, `total_cap`, `accounting_unavailable`, `loop_frame_failed`,
`guardian_restart`, `control_cancel`, or `stop_only_recovery` when a recovery names no other
cause), also kept on the reservation as `stop_cause` (ADR 0075). A refused workload call
(begin-once, input, output) records its closed refusal code (never the token or body) as a
`workload_refused` event, at most once per code per reservation per minute; the latest is
`last_workload_refusal` in status and `guardian_last_workload_refusal` on the receipt, so a
container that failed at start (runtime status `custody`) leaves its reason behind. This
field is **advisory**: it is recorded before the call's token binds, from the operation id
in the request body, so a caller that knows an operation id can influence the recorded code.
It is a diagnostic hint, not evidence of container behavior, and no stop, budget or custody
decision reads it. A recorded cause that is not a closed token is reported as `invalid_code`
on the receipt rather than dropped.
Pending 202/outage/orphan exposure continues accruing past the deadline and may
exceed the estimate cap; `escalation_pending` then remains durable. Single-operation
capacity fences a new reservation while a prior operation remains unresolved.

`estimated_exposure_cents`, `published_hourly_rate_usd`, `rate_unknown`, `rate_read_retries`, `provider_read_retries`, `stop_cause`, `last_workload_refusal`, `last_rate_read_failure`,
`last_provider_read_failure`, `last_loop_frame_failure` and `last_loop_frame_failure_at` (a frame whose loop step raised: the exception type name and time only),
`budget_fenced`, `escalation_pending` and `exposure_pending` are accounting
projections, not invoices. `actual_billed_cost_cents` remains null,
`exposure_unknown:true` and `financial_hard_cap:false`: provider outage, changing
prices, fees and external billing cannot be bounded by a software stop request.
No billing endpoint, credit top-up or provider credit hard cap is invented.

All envelopes include `operation` and `reservation`. Except `status`/`import`, a
successful response is `{reservation,state:"ready",observed_at}`. Rejected requests
return only a closed error code. Request bodies are capped at 128 KiB; handlers
have bounded socket timeouts and emit no request/header/claim/exception logs.

## JWT and provider trust limits

The fixed JWKS URL is
`https://matrix-rest-api.salad.com/.well-known/workload-jwks.json`. Its HTTPS fetch
is capped, redirect-free and rate-limited; keys expire locally after one hour and
refresh periodically. ES256/P-256, exact key schema, issuer
`https://matrix-api.salad.io` from the documented decoded example, organization
UUID audience, group alias/name, machine subject and at most five-minute
iat/nbf/exp scope are mandatory. Header `jku`/`x5u` and unknown algorithms are
rejected; unknown kids never trigger arbitrary fetches. The documented JWT has no
project claim: exact project authorization comes from the authenticated provider
API's approved organization/project/group mapping, not client claims.

References: [Salad JWT tutorial](https://docs.salad.com/container-engine/tutorials/security/jwt-authentication)
and [IMDS token API](https://docs.salad.com/reference/imds/get-token).

## Stop, exit and reconnect

Control/proxy loss, deadline, missed polls, allocation changes and provider unknown
state initiate stop-only policy. Stop intents/retry deadlines are persisted before
dispatch; retries may stop only the exact retained group, never create/start.
202 is pending. Outage, absent group or changed allocation cannot prove termination.
An exact provider stopped readback with zero instances/pending changes plus sealed
external custody closes the bounded provider/custody stop boundary. Node-local
writer death and final billing remain unknown. Unknown output/stop is quarantined.

Stopping this service is **not** stopping Salad workloads. Preserve its supervisor
and credentials through reconciliation. On service restart every retained operation
is stop-only, including consumed begin-once and lost-create reservations. It
reconciles the same immutable reserved name; neither reconnect nor new epoch can
reset the consumed operation. For normal return/reconnect, keep this independent
service alive and reconnect control/proxy to the same reservation. After service
failure, use stop-only recovery and fresh human admission for genuinely new work.

The service cannot kill through an unavailable Salad API, guarantee node-local
quiescence, enforce provider billing as a hard financial cap, or survive simultaneous
host/storage/supervisor loss. Multiple concurrent Salad operations are not this
milestone's acceptance; deploy and capacity-test the single-operation journey.

## Allocation deadline

A container group that never gets an instance must not burn the whole route window. The guardian
counts from the first poll at which the group is no longer stopped (the start was dispatched), and
only while no instance has **ever** appeared in the group's instance list. An instance that exists
but is still allocating, downloading its image or creating is an allocation: the first sighting
disables the deadline for good (recorded in the guardian's own database, so a restart cannot revive
it). If the clock runs out first, the guardian stops the group through the ordinary stop path with
`stop_cause` `allocation_timeout` and its status then carries the elapsed seconds.

The setting is `allocation_deadline_seconds` in the service configuration (default 600, bounds
60 to 3600; an invalid value refuses at startup). It never raises or replaces the route deadline,
the run cap or the total cap, and an adapter that cannot say how many instances the group has
never triggers it. The live check exits as soon as it sees the cause and its receipt says
`allocation_timeout` with the elapsed seconds; spend so far is the attempt's `guardian_estimate`.
The deadline is on by default for every Salad operation; it is a per-service setting, not part of a
campaign spec, so spec and profile digests are unchanged.

Control records the attempt as `failed_transient` with failure class `infra_allocation_timeout`
(a placement failure, not a stage logic failure). The stage's `retry.infra_retries` bounds the
retries exactly as for other infrastructure failures, and `retry.reroute_providers` may place the
next attempt on another allowed provider; each retry is a new attempt under the campaign and
project spend caps, and nothing escalates spend. The timed-out attempt still settles through the
guardian-bounded hold (or a human charge when the published rate was never readable), and a retry
waits for that settlement because open Salad exposure blocks a new positive hold.

### Amendment: an instance means a placed machine

A started group with no capacity reports the group status `running` with an instance only
`allocating` (waiting for a node) or none, and no allocation id. That is not an allocation and not
a running machine. The service's observation therefore counts an instance only once Salad has
placed it (`creating_count`, `running_count`, `stopping_count`, or a listed instance that is
past `allocating` or already names its `machine_id`; `allocating_count` alone, or a listed
allocating instance with no machine, is zero), and reports `running` (and starts accrual,
`first_running_at`) only for a listed running instance or a running group whose complete counts do
not say nothing is placed. Both the deadline and accrual use the same test. Anything the read does
not say completely stays as before (a running group with missing counts still counts as running; an
adapter that cannot count never triggers the deadline). No stop or cap is loosened.

An allocating instance that already has a `machine_id` counts as placed. This matches `_provider`
and the workload binding (a container may call in while the API still says `allocating`) and is the
safe choice for billing and exposure. Salad's billing page (updated 2 Nov 2026) says billing is for
"the time container group instances are running on one of our machines", that "No charges are
incurred while your container group is allocating or downloading", and that you "will never pay for
Cold Boot time". Treating `machine_id` as placed is therefore conservative: our exposure estimate
and deadline latch may start earlier than Salad bills, never later.

An allocating instance with no `machine_id` is never placed: it neither latches the deadline nor
counts as an instance, however many polls saw it, and it may disappear again. A placed one (a
`machine_id`, or a state past `allocating`) latches it for good, as before. The clock still starts
only on the first poll that saw the group started, never at arming.

Reads that time out or fail do not hold the deadline off. When a poll fails after the clock has run
past `allocation_deadline_seconds` with no instance ever latched, no allocation bound, nothing
seen running and the adapter reporting that no custody has begun (an adapter that cannot say, or
raises, never qualifies), the guardian stops with `allocation_timeout` (and keeps the evidence) instead of
`provider_unavailable`. A failed read before the deadline behaves as before (retry window, then
`provider_unavailable`); the latch is only ever set by an exact, fresh read, so a group that showed a
placed instance is never stopped as a timeout this way.

When the route limit stops a started group that never showed an instance, the status carries the
same `allocation_timeout` evidence (elapsed seconds; the stop cause stays `deadline`), and control's
guardian-final projection keeps it for a `deadline` as well as an `allocation_timeout` stop, so the
attempt is recorded as the retryable `infra_allocation_timeout` rather than a permanent
`salad_not_passed`. In that case `deadline_seconds` repeats the elapsed time (the route limit, not
the allocation setting).

### Amendment: a provisioning bound that needs no start read

The allocation deadline's clock starts only on a read that says the group started (not stopped
and no `pending_change`) with a count of zero. A provider that keeps a started group's change
pending, or omits its counts, never starts it, and a never-placed group then ran to the route
deadline and was recorded as the workload's failure. The guardian therefore also keeps a
provisioning bound, counted from arming (the exact group is armed before its start):
`provisioning_deadline_seconds` in the service configuration (default the route window's
provisioning allowance, 1500 s, never below `allocation_deadline_seconds`; bounds 120 to 7200; an
invalid value refuses at startup). When it passes with no placed machine ever latched, no
allocation bound, nothing seen running and the adapter saying no custody of any kind has begun
(an adapter that cannot say, or raises, never qualifies), the guardian stops the group with
`allocation_timeout` (`deadline_seconds` is the bound). It reads only the guardian's own
database, so neither a failed read nor a read that does not say can hold it off. A machine that
was assigned (a slow image pull) latches the evidence and is never cut by it. An assignment armed
before this setting existed has no arming instant and never triggers it.

The same never-placed state also gives a `deadline` stop its `allocation_timeout` evidence
(elapsed seconds since the group was seen started, else since arming) when no fresh zero-count
read was available, so the route limit of a group that never ran is the retryable
`infra_allocation_timeout`, not `salad_not_passed`. The live check ends on that evidence like
on its own timeout, without importing outputs nothing could have written.

Such an attempt settles at the guardian's own accrued estimate, rounded up (zero when nothing
was ever seen running; a backdated poll gap rounds up to a cent), instead of the whole hold. Salad
bills only while an instance runs on a machine. The basis stays the provisional
`guardian_bounded_hold`, so a recorded charge still raises or confirms it, and only the
guardian's recorded evidence (never a receipt reason) selects it.

When the route limit stops a run after an instance was seen (the guardian's `deadline` stop with
no allocation-timeout evidence), the status carries `route_deadline.running_seconds` (elapsed
running seconds) and `route_deadline.limit`: `route_deadline` when the route window itself ran
out, `running_cap` when the run overran its own declared-run cap. Only the former is recorded as
`failed_permanent` with failure class `route_deadline_reached`; a `running_cap` stop stays
`salad_not_passed` (the workload overran). It is not retried here. A trainer that stops
itself and exits 0 remains `succeeded` at platform level; the receipt carries
`completeness_guard: "experiment"` because output completeness is the experiment's own guard.

When the adapter positively says no custody work began, the record also carries
`route_deadline.custody_work_begun: false` (absent when it began, is unknown, or the adapter
raises). A `route_deadline` stop with that word and at most 60 s running is a placed machine
that never got going (a slow image pull or start), not the workload: control records the
transient `infra_salad_provisioning_overrun`, retried within `retry.infra_retries` (then the
class/profile fallback decides), and settles it at the guardian's accrued estimate like a
never-placed group ([ADR 0206](decisions/0206-salad-provisioning-not-run-time.md)). The lease of
a Salad campaign attempt also covers the provisioning allowance (lease issue plus the task's
`max_duration_seconds` plus 1500 s, still bounded by grant and credential expiry), so
provisioning no longer spends the stage's run time; the run itself stays bounded from first
running by the running cap. That cap, and the guardian's first-running latch, start only on an
instance-level `running` (or a positive `running_count` when no instance list was read): a group
that reads `running` over an instance still creating or downloading its image starts accrual
(conservatively, at the first such read) but is reported to the guardian as pending, so an image
pull never spends the declared run.

## Start-failure loop before custody

A container that dies at once is rarely seen `running` at a 15 second poll, and each failed start
moves to another machine (Salad's backoff grows toward 20 minutes), so an assigned machine alone
(see the allocation deadline) would hold the frame to the route deadline. While the adapter says
no custody has begun, the guardian keeps the distinct machines it has seen assigned (a truncated
digest of each id, never the id). At three distinct machines it stops the group with `stop_cause`
`start_failure_suspected`; status carries `start_failure.machines`. One machine that stays
`downloading` or `creating` is a slow pull and counts once. The live check stops polling at
once and its receipt reason is `start_failure_suspected`. Control records `failed_permanent` with
that failure class only when the events read below recorded an `instance_exited` (Salad also moves
healthy instances between nodes); otherwise it is a transient
`provider_reallocated_before_custody` that a fresh attempt may retry. The permanent failure is not
retried or rerouted, and the stage is not moved to the next class profile. A run seen `running` on
one poll is still counted (only custody work proper ends the count); a restart on one machine is
not. `start_failure_machines` is a guardian setting (2 to 10, 0 off). ADR 0196.

After any stop whose cause is not `output_retained`, the service reads the group's
`instance_controller` events once (`read_events`, one organization-scoped `query_log_entries` call
with a bounded page and window), off the loop thread, so the stop never waits on it. The attempt
is claimed durably first; a failed read is `unavailable` and a 429 is `log_quota_exhausted` (the
daily log query limit lasts until 00:00 UTC); neither is retried. A query that returns zero
entries is `empty`, not a successful `read`, so a wrong label name is visible. The result is saved
on a later loop pass and may miss an immediate receipt, which only makes the failure transient. Status and the receipt keep
closed tokens only (`start_events`: outcome, event names, exit codes, a distinct-machine count),
never text, ids or hostnames.

Group status `pending` (image pull) and `deploying` (started, no instance running) are reported
as observations in the readback and the stage status; a failed read stays `unknown`. They never
confirm a stop or start accrual.

## Allocation change before anything ran

Salad can reschedule a container group while its image is still being pulled. An allocation change
seen before any instance was observed running and before any custody began (no input fetched, no
output written, not in recovery) is therefore a re-bind, not a stop: the guardian adopts the new
allocation, records `prestart_reallocation` (old and new allocation, time) on its status, and keeps
waiting. The deadline, the running window, the heartbeat and billing clocks are never reset, and the
number of re-binds is bounded by `max_prestart_rebinds` (default 3; 0 turns the behaviour off; values
above 10 are refused at startup). Once the bound is spent, once any instance ran, or once any custody
began, a change of allocation stops the run with `allocation_changed` exactly as before, and a guardian
that cannot tell whether custody began stops too.

A stop on `allocation_changed` after an instance ran but before any custody work (the workload
called no begin-once, no input was served, no output was requested or written) is reported on the
live receipt as `provider_reallocated_before_custody` when the provider confirmed the exact group
stopped, and the status says `custody_work_begun: false`. The campaign runner may then place a
fresh attempt (never a re-bind of the old instance), bounded by `retry.max_reallocation_retries`
and the budget cap ([ADR 0179](decisions/0179-precustody-reallocation-retry.md)). After custody
work began, or when the guardian cannot say, the stop keeps `provider_stopped_custody_unsealed`.

A route the provider itself moved (the route's reallocation flag, the provider's read of the exact
group stopped and a confirmed exited worker) whose receipt carries neither guardian custody word
(another stop cause, or custody unknown) is the transient `infra_salad_reallocated`: never the
workload's failure, its outputs never accepted, retried within `retry.infra_retries` (then the
class/profile fallback decides) ([ADR 0206](decisions/0206-salad-provisioning-not-run-time.md)).
Since [ADR 0207](decisions/0207-salad-runtime-retry-and-lease-window.md) it settles at the
guardian's accrued estimate (rounded up, at least one cent, within the hold, provisional) when the
recorded final is `stopped` or the safety service's provider-confirmed reconcile
(`provider_confirmed_exact_group_stopped`, only `allocation_uncertain` set) and control's own read
of the frame record agrees, so its retry needs no person's charge. An operator-attested reconcile
or any other flag still waits for a charge. A run whose first stop was retaining its output and
whose allocation then changed while it was stopping (custody unsealed) carries
`retained_before_move` on its final and is the same retryable move.

Since the ADR 0207 amendment of 2026-11-27, any recorded final the safety service closed by its own
provider-confirmed reconcile (`reconciled`, `provider_confirmed_exact_group_stopped`) that is not a
safety-plane stop settles at the guardian's estimate on the same conditions, with or without
`retained_before_move` or a move class (an attempt recorded by an older release included). The
attempt keeps its outcome class: only its settlement changes (a `salad_not_passed` attempt stays
`failed_permanent`, is not retried, and none of its outputs are accepted).

## Safety-plane stops are infrastructure

The guardian's first stop cause is carried on the executor's recorded final as
`safety_stop_cause` only when it is one the safety plane made for its own health:
`heartbeat_silence`, `control_disconnect`, `missed_poll`, `clock_uncertain`, `guardian_restart`,
`loop_frame_failed`, `rate_unknown`, `provider_unknown`, `provider_unavailable` or
`accounting_unavailable` (ADR 0215). Control then
records the transient `infra_salad_safety_stop` (retried within `retry.infra_retries`, then the
class/profile fallback) instead of `salad_not_passed`; a cause seen only on the live receipt never
selects it. Settlement is unchanged.

## Open-frame capacity before launch

The admin request `{"operation": "open_frames", "reservation": <route reservation digest>}` is a
read: it returns `open_billable_frames` (counted as the reserve counts them) and
`max_open_billable_frames`, and writes nothing. The executor asks it before `mark_executing` and
defers the launch while the limit is full or the answer is unreadable
([ADR 0207](decisions/0207-salad-runtime-retry-and-lease-window.md)).

## Container exited non-zero

A one-shot container whose process exits with a non-zero code is restarted by Salad and later
moved to another machine, which used to surface only as an `allocation_changed` stop and a
`salad_interrupted` (or, since ADR 0206, `infra_salad_reallocated`) retry. While a frame is armed, seen running and has no retained output, the
service reads the group's system events. The reads back off: 30 s apart at first, the gap
doubling every 4 reads (60, 120 s) and again after a failed read (at most 240 s), at most 12 reads
per frame. Every frame's watch reads together are bounded per UTC day, and a rate-limited answer
(Salad's daily log query limit, which lasts until 00:00 UTC) ends every watch read until then, so
the one post-stop read of ADR 0196 still finds quota. Exit codes are kept in first-seen order, and
the earliest non-zero exit decides: when it is the workload's own (not 137 or 143, a kill or
platform stop), so `[1, 137]` (a crash loop the guardian stopped first) included, the service
records the start events, sets the stop cause `container_exited_nonzero`, closes the custody
writers and stops the exact group through the ordinary stop-only path; the provider's own read
still confirms the stop. A kill that came first (`[137, 1]`) keeps today's handling. Once the
watch recorded the events, the post-stop events read is not sent again. Control classifies the
attempt `failed_permanent` / `salad_container_exited_nonzero` with the exit code in its detail,
whatever stop cause the guardian recorded, so it never spends `retry.infra_retries`. The one exception is an exit on a frame the provider moved whose runtime status line names a custody or input refusal (or, with no status line, custody never began): that is `failed_transient` / `infra_salad_reallocated` and spends one (ADR 0224). A group
whose guardian already stopped (a reported workload failure included) is not watched.

After a recorded non-zero exit (from the watch or the post-stop read) the service sends one
`read_logs` query for the exact group's container logs (durably claimed once per operation, off
the loop thread, skipped after a rate limit that day). The result is reduced on the transport
side to a bounded, sanitized tail (`salad_container_log`): the last 20 lines, at most 1024 bytes,
200 characters a line, printable ASCII only, with secret-shaped values, long tokens, URLs with
credentials, e-mail and IP addresses, UUIDs and home-directory paths redacted, marked
`"untrusted": true` with the digest of the kept lines. A secret-shaped value is redacted after an
auth scheme (`Basic`, `Bearer`, `Token`: the scheme word is kept) and after a connecting word
(`password is ...`), and a well-known credential prefix (`sk-`, `ghp_` and the other GitHub
token prefixes, `xox?-`, `AKIA`/`ASIA`) is redacted at any length; the rules fail closed, so an
ordinary word after `Basic` or `Bearer` is redacted too. The receipt's `guardian_container_exit`
carries the exit codes and that tail as `stderr_tail`, or `{"outcome": "unavailable", "reason":
...}` with a closed reason (`log_quota_exhausted`, `read_failed`, or `not_read` when no tail was
recorded by the time the receipt was built, or `data_class_sealed` for a sealed run;
`no_trusted_log_source` stays in the closed set for older receipts). A sealed run (control's
reserve says it runs a final holdout stage or reads a sealed partition, ADR 0068) is never sent a
`read_logs` query and no tail of it is stored: the claimed read records `data_class_sealed` at
once. Treat the tail as the workload's own untrusted words: it explains a failure, it
is never evidence of anything.

`read_events` and `read_logs` are guardian-only reads. A worker's proxy request under an observe
effect may send only the exact group and instance reads; the trusted server refuses either stream
read as `admission_denied` before any claim, so no effect nonce is spent and no provider call is
made.

One workload exit on one machine is enough (the earliest non-zero exit, when it is not 137 or
143): unlike the start-failure loop (three distinct machines, ADR 0196), there is no retry for a node-local fault that exits non-zero
([ADR 0193](decisions/0193-salad-allocation-deadline-machine-assignment.md) and
[ADR 0197](decisions/0197-salad-post-start-interruption-retry.md) amendments).

A frame the watch did not catch still depends on the post-stop events read. That read runs on a
short-lived thread after the stop and is recorded on a later loop pass, so the executor can record
its final before the exit is on the guardian's record; control then classifies without it. The
watch closes this for a group it sees exit while armed; for any other frame the order is not
guaranteed. This race is older than the watch.

## Allocation change after custody began

When the guardian stops on `allocation_changed` after custody work began (`custody_work_begun` is
true) and the provider's own read confirms the stop, control records the attempt as
`failed_transient` with failure class `salad_interrupted`
([ADR 0197](decisions/0197-salad-post-start-interruption-retry.md)). It spends one
`retry.infra_retries`, settles at the guardian's accrued estimate (provisional, like the bounded
hold), and its other outputs are never accepted. The estimate settles (rounded up, at least one
cent, within the hold) when the guardian's final is `stopped` or, for this case only,
`reconciled` (the guardian refused the live check's cancel `stop_only` and closed the frame by
reconciliation), and control's own read of the guardian's frame record says the same. A missing
estimate, any other flag (orphan, escalation, budget fence, unknown rate, a billed cost) or an
estimate above the hold still waits for a person's charge. A checkpoint that custody already holds as the
reserved `checkpoint.bin` output reaches control through a partial manifest (the executor
imports the sealed, incomplete custody and lists only the hash-verified files that arrived; the
diagnostic is not required, and such a manifest never verifies as a pass). It is recorded by digest and size and listed in the campaign receipt.
The retry's input snapshot is composed with it (under the declared output's name), only for the
attempt right after the one that produced it, and the retry's recorded inputs list it by digest;
the snapshot is private-derived data, so the provider must be trusted to receive that class. The
retry is a new operation with its own claim and hold. Without the provider's confirmation the attempt stays unknown and held.
