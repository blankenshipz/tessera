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
  "admin_token_file": "/PRIVATE/salad-safety/admin-token"
}
```

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

`status.retained_output` is read-only metadata before stop: exact custody binding,
`name:"output.json"`, `retained`, `sha256`, `bytes`, and `quarantine:true`. Missing
output has `retained:false` and null hash/size. A true result requires completed
durable upload plus current byte rehash/cap verification; it contains no raw output
and does not close writers, seal inventory or pretend execution finished. The
normal driver waits for this metadata, then cancels, verifies provider stop and
sealed custody, then imports bytes. It must not call `import` to poll for upload.

Before new work/arming/begin, the service independently reads the selected GPU's
fresh official high-priority published hourly rate through `gpu_class`. Its
durable Decimal exposure ledger conservatively charges all reserved startup and
unknown seconds, not just observed running time. Planned remaining exposure must
fit the approved `max_cost_cents`; stale/unknown/changed rate fences new work and
initiates stop-only policy. Active provider/rate polls are paced at five seconds.
Pending 202/outage/orphan exposure continues accruing past the deadline and may
exceed the estimate cap; `escalation_pending` then remains durable. Single-operation
capacity fences a new reservation while a prior operation remains unresolved.

`estimated_exposure_cents`, `published_hourly_rate_usd`, `rate_unknown`,
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
