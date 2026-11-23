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

`max_open_billable_frames` is optional and must be `1` (the default); the service refuses to
start with any other value, because concurrent billable frames need a record that covers
total-cap races, partial stops and accrual interleaving (ADR 0041 resolved question 4b).

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
30 s soft lease is re-checked on every pull. A minted ticket the guardian never received
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

**Open billable frames.** A `reserve` is refused (wire code `single_operation_orphan_exposure`)
while the number of other frames that may still bill is at `max_open_billable_frames`. A frame
counts until its status is `stopped` or `reconciled` (a create proven absent reconciles); an
ambiguous create with no provider id still counts. A stopped, reconciled or absent frame frees the
slot at once, with no wait. Each refusal writes a `fence_refusals` row in the service database (the
reservation and operation refused, the open count and the limit; no other frame's ids), apart from `events`. The
total-cap refusal is checked first and is not recorded as a fence refusal. Admin `status` adds
`max_open_billable_frames`.

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
exact observation at most 5 s old, and when every attempt fails the rate is unknown exactly
as before: no grace and no reuse of an earlier rate
([ADR 0031](decisions/0031-salad-single-container-gpu.md) unchanged). The next rate poll is
paced from the end of the sequence. Retries are added to the reservation's cumulative
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
local work; the 45 s unseen-run gap that backdates accrual is sized with it. The retry
windows add up to 4.5 s of stop latency on a pass whose reads keep failing; that is
accepted, as at pilot rates it costs a negligible amount. Group and instance reads share
one failure kind (`provider_read_failed`) and so one window: the module pins the set of
kinds to two (`rate_read_failed`, `provider_read_failed`), and a third would put the worst
case past the gap. The allowance covers the polled reservation; first attempts for any
further reservation are bounded by the transport deadline only, not by the windows. One window
of each kind serves all reservations in the pass, so an early reservation's retries can
leave none for a later one, which then fails closed. When every attempt fails the poll stops
exactly as before, and the closed reason the read gave up with (`timeout`,
`provider_unknown`, `rate_stale`, `rate_observation_invalid`, ...) is kept, at most once
per reason per minute, as `last_rate_read_failure` / `last_provider_read_failure` in status
(the most recent ever, not cleared by later successful reads; `*_at` is the time of the
latest recorded event, which is the first occurrence within its one-minute deduplication)
and `guardian_last_rate_read_failure` / `guardian_last_provider_read_failure` on the live
receipt. The first cause of a guardian's own stop decision
(`provider_unavailable`, `provider_unknown`, `missed_poll`, `control_disconnect`,
`deadline`, ...) is kept as `stop_cause` in status, even after the operation later turns
`stopped`, and as `guardian_stop_cause` on the live receipt. A refused workload call
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
`last_provider_read_failure`,
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
