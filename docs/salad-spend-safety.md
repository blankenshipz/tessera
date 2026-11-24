# Salad runtime spend safety

Status of the review §4.2 / Risk 4 spend controls for the live Salad GPU path,
verified on 2026-11-18 against the code at this commit, Salad's documentation and
the first live runs. Cost figures are
estimates; Salad exposes no usage or billing API.

## What Salad itself does and does not provide

From docs.salad.com (billing, deployment lifecycle, container groups, IMDS):

- Billing is per second and **only while an instance is running**. Allocation
  and image download are not billed. GPU-class prices include vCPU and RAM.
  Storage billing is not documented and is not modelled.
- There is **no provider-side auto-stop**, maximum runtime, TTL or budget cap
  on a container group, project or organization. The only automatic shutdown
  is account credit reaching $0.
- `restart_policy: "never"` behaviour after a clean exit is not documented.
  Observed live (run 16): with `replicas: 1`, Salad schedules a **replacement
  instance** after a one-shot container exits.
- A container **cannot stop its own group**: IMDS offers only status,
  token, restart, recreate and reallocate.
- Scheduled scaling (`scaling-actions` with `replicas: 0`) accepts only
  recurring cron schedules, and the docs do not say whether it stops running
  instances. It is a possible provider-side backstop that needs a cheap live
  check before being relied on. Not implemented.

So the stop path is entirely Tessera's guardian plus the operator.

## Controls in place

| Control | Where | Status |
| --- | --- | --- |
| Guardian off the desktop | `tessera-salad-safety` systemd unit on the always-on host; the live driver also runs there | Done. Desktop sleep or loss does not affect a run. |
| Guardian restart stops active work | `SaladGuardian.__init__` marks active assignments `guardian_restart` | Existing |
| Route (provisioning) deadline | guardian outer deadline = approved route window (<= 7200 s, and at most the plan's `max_duration_seconds` plus the Salad provisioning allowance: unbilled provisioning plus billed runtime) | Done |
| Billed-time deadline | guardian also stops at `first_running_at + running_seconds` (see "Two windows") | Done |
| Total budget cap | optional `total_cap_cents` held by the safety service; a reservation that would push committed spend past it is refused (`total_budget_exhausted`) and needs the owner | Done |
| Heartbeat-silence stop | guardian stops after 30 s without driver heartbeat | Existing; driver now heartbeats every poll |
| Stop as soon as output is retained | `SaladGuardian.complete`, triggered by the custody upload | Done (prevents the post-exit replacement instance) |
| In-container runtime limit | smoke workload argv `MAX_SECONDS` <= 300; `workload_v1` `workload_seconds` <= 3600 (ADR 0036) | Existing |
| Pre-create ceiling | published high rate x billed `running_seconds` <= `max_cost_cents` (no longer the full route window, because provisioning is unbilled) | Done |
| Single-operation orphan fence | no new reservation while another is not stopped/reconciled | Existing; an uncertain orphan closes automatically on an exact provider read of the group stopped (`provider_confirmed_exact_group_stopped`), or by operator `reconcile` |
| Lost-create resolution | a never-bound reservation (create failed, no `provider_id`) becomes `reconciled` (`lost_create_absent`) only after route deadline + 900 s, a definite ProblemDetails 404 on the exact reserved name and a complete project group list without it; operator `reconcile` needs the same margin and 404 plus a list read that reached the project; a 24 h watch voids it on any non-absent read | Done; see "Lost create" |
| Create-rejected release | a create answered with a fully read HTTP 400/422 ProblemDetails body is classified `provider_rejected_request` (a create that provably never left the process, `request_not_sent` with status 0, takes the same release) (not `provider_unavailable`) with a bounded summary (status, `title`/`detail` <= 200 chars, `errors` field names) in the dispatch receipt and live-check receipt; the trusted server reports it (admin `create_rejected`), which fences at once, and the guardian releases the never-bound reservation as `reconciled` (`create_rejected_absent`) 30 s after max(report, last `ready`), only on its own exact-name 404 and complete project list | Done; see "Create rejected" |
| Create shape pre-validation | Salad's documented create limits (whole vCPUs 1..16, memory 1024..61440 MB, storage a whole GiB within 1..250 GiB, GPU class UUIDs) checked at setup, route proposal/approval, the create action and in the transport before the POST (never at start of an existing group); refused before any reservation exists; stored proposals keep parsing | Done; see "Create rejected" |
| Estimate-only labelling | every status/receipt carries `actual_billed_cost_cents: null`, `financial_hard_cap: false`, `price_basis: published_gpu_hourly_rate_not_actual_cost` | Existing; verified |

The estimate is conservative but no longer charges unbilled provisioning time.
Salad bills only while an instance runs, so accrual starts when the service
first observes the bound instance running (`first_running_at`) and is zero
before that. It is charged at the published GPU "high" rate, is still an
estimate (`actual_billed_cost_cents: null`, `financial_hard_cap: false`), and
keeps accruing past a deadline while a stop is unconfirmed.

## Two windows

- **Route window** (`resources.max_seconds`, <= 7200 s; see `route_window_limit`): the outer bound and
  the provisioning deadline. Image pull and allocation (up to about 45 minutes
  on a cold node) happen inside it and are unbilled. If the instance is never
  seen running, the guardian stops the group at this deadline.
- **Running window** (`running_seconds`): the billed-time bound. Source: the
  smoke workload's own runtime argument (`MAX_SECONDS` in the approved
  `profile.execution.argv`, parsed by the existing `_request`). It is already
  bound by the package digest, checked `<=` the route window and capped at
  300 s by the smoke recipe, so no new schema or caller-supplied field exists.
  For `argv_binding="workload_v1"` (ADR 0036) the source is the reviewed
  contract's `workload_seconds` (<= 3600) plus a 120 s upload grace.
  Once the instance is first seen running, the guardian stops the run at
  `min(route deadline, first_running_at + running_seconds)`.

## Total budget cap

`total_cap_cents` (integer 1..1,000,000, config key, absent = no total cap) is
held durably by the guardian service, not the driver. On `reserve`:

```
committed = sum over every OTHER reservation of
              accrued estimate                       if stopped/reconciled
              max(accrued estimate, its per-run cap) otherwise
            + this reservation's profile.execution.max_cost_cents
```

If `committed > total_cap_cents` the reservation is refused with
`total_budget_exhausted` before any provider effect. A stopped or reconciled
reservation contributes only its frozen accrued estimate. The per-run cap
(`max_cost_cents`) still applies as before. Admin `status` reports
`total_cap_cents`, `total_committed_cents` (decimal string, current
reservations only) and `total_remaining_cents`; all are estimates, never an
invoice. The deployed guardian sets `total_cap_cents` to the approved real-run budget
plus a small smoke allowance. A run that would exceed the total
real-run budget needs the owner.

Deleting a stopped group never releases liability or budget; see [cleanup of stopped groups](salad-proxy.md#cleanup-of-stopped-groups).

## Off-host stop switch

Use the Salad Portal: open the project and organization that hold the deployment
and stop (or delete) the `attempt-*` container group. This works from a
phone and does not depend on the always-on host, the desktop or Tessera. An
endpoint on that host was considered and rejected as the primary switch: it
fails in exactly the case that matters (the host is down).

On the always-on host, an operator can also run the guardian's exact stop through
the admin channel (`cancel`), or `reconcile` an uncertain orphan once Salad
shows the group stopped with zero instances.

The service does not wait for the operator to end the spend. After an allocation
change the guardian cannot seal custody (a provider read cannot prove node-local
writers are dead), but the provider's own read is authoritative for what it runs
and bills. Once a fresh exact read shows the group stopped, all four instance
counts zero, no pending change and no instances, the service loop marks the
operation `reconciled` with reason `provider_confirmed_exact_group_stopped`
(event `provider_confirmed_stop`): stop retries end, the accrued estimate
freezes and the orphan fence releases. Custody stays unsealed and outputs stay
unimportable; the live driver reports `provider_stop_confirmed` (never
`stop_confirmed`) and ends its stop wait with reason
`provider_stopped_custody_unsealed`. An operator `reconcile` before that close
records its attestation under `operator_reconciled_exact_group_stopped`.

## Lost create

If the Salad create fails (for example `provider_unavailable`) the reservation
has no `provider_id`. Its status is `reserved` and the orphan fence blocks every
new reservation, because a create that Salad received may still appear. The
guardian keeps reading the reserved group name each loop; if the group appears
it is bound and stopped as usual.

The reservation becomes terminal automatically when all of these hold:

1. recovery is set and it never recorded a `provider_id`, allocation, machine,
   begin, output or running sighting (any of those keeps the stop/reconcile path);
2. now >= route deadline + `LOST_CREATE_MARGIN_SECONDS` (900 s). Status shows
   `lost_create_eligible_at` until then;
3. an authenticated `GET` of the exact reserved group returns a fully read
   HTTP 404 (body length equal to `Content-Length` when sent) with a
   ProblemDetails-shaped JSON object body (`status` 404 and `title`
   "Not Found" when present; `detail` is not required or interpreted). A
   gateway HTML/empty/plain/non-object body, a timeout, transport error,
   redirect, 401/403, 429 or 5xx is never absence (a 404 is only accepted by
   this one read). The body cannot tell a missing group from a missing
   project: a read-only live GET returned the identical
   `{"type": ".../rfc9110#section-15.5.5", "title": "Not Found", "status": 404,
   "traceId": "..."}` (no `detail`) for both. So this 404 proves only that the
   name is not there; that the project exists comes only from step 4;
4. the project's group list (the only proof that the project exists) is a
   complete page (HTTP 200, fewer than 100
   well-formed items, no pagination keys such as `next`, `page`, `cursor`,
   `total` or `continuation`) with no group of that name. Salad documents no
   paging for this read (no query parameters, `items` with `maxItems: 100`), so
   100 or more items may be truncated and are never this proof
   ([ADR 0031 addendum](decisions/0031-salad-single-container-gpu.md#addendum-the-project-group-list-proves-absence-only-when-complete)).
   The list read accepts a body up to 4 MiB; every other read keeps 64 KiB.

The margin: a create is only dispatched by the trusted server right after a
guardian `ready`, which is refused at the stop deadline (never later than the
route deadline) and once recovery is set. What remains after that admission is
local: the IPC reply (at most 12 s), an authority and credential recheck (a 6 s
allowance, not an enforced timeout) and one create POST with its own 12 s total
client deadline (DNS, connect, TLS, send and response; reads keep 2 s), so a
legitimate create is sent by about deadline + 30 s (`DISPATCH_CHAIN_SECONDS`).
900 s is 30 times that, to also cover Salad finishing, or asynchronously
materialising (202), a request accepted after our client gave up: live, Salad
created a 32 GiB group whose create the former 2 s client deadline had already
abandoned. The worst-case block after a failed create is the route window (<= 7200 s)
plus 900 s.

The result is `status: "reconciled"`, `reason: "lost_create_absent"`, never
`stopped`, with a `lost_create` record (kind, machine attestation, deadline,
margin, `name_read: "not_found"`, `list_read`, accrued estimate). The
estimate stays zero, custody writers are closed, the service's `events` table
records `lost_create_absent_reconciled`, and the fence admits new work. For 24
hours after that the guardian still reads the reserved name once a minute; a
watch read is `read_absence` again: anything but a definite exact-name
404 (a 200/202 on the name, a listed group, or an unknown read) voids the
resolution and records `lost_create_late_group_observed` or
`lost_create_resolution_voided` before any bind attempt. The fence then blocks
new work again until the ordinary loop binds and stops a group found by name,
or a fresh definite absence (with all automatic preconditions) resolves it again.
Every loop also re-closes the closed row's custody writers, so a resolution
interrupted before closing them cannot leave them open.

Manual path: if the automatic path cannot run (typically a full list page of
100+ groups), an operator may send admin `reconcile` with a human attestation
(1..256 chars, stored as a SHA-256) for the never-bound reservation. It needs
the same preconditions 1–3 and a list read that reached the project: HTTP 200
with well-formed items, no pagination keys, and no group of that name (100+
items allowed). A failed list read or a listed name refuses it; the result is
`reason: "operator_lost_create_absent"` and event
`operator_lost_create_absent_reconciled`. Guardian state is never hand-edited.

Residual risk: nothing watches the reserved name more than 24 hours after
resolution. A group that first appears later (only possible if a create request
was held for over a day somewhere) would not be stopped by the guardian. It
would cost nothing on its own: the create spec has `autostart_policy: false`,
so it stays stopped and unbilled, and `start` cannot be dispatched because
the guardian refuses `ready` for a recovering reservation. Remove it in the
Portal if it is ever seen.

## Create rejected

A create that Salad cleanly refuses is not a lost create: nothing was received,
so waiting for the route deadline plus 900 s only blocks new work. Live, a
`storage_amount` of 30000 MiB (not a whole GiB) was refused in about 1.3 s
while the availability read reported the shape available; a whole-GiB request
worked.

Classification (`salad_http.py`, the create POST only): a fully read (length
equal to `Content-Length` when sent), in-deadline HTTP 400 or 422 with
`Content-Type` `application/problem+json` or `application/json`, whose body is
a ProblemDetails JSON object (a non-empty string `type`, a URL or a short code
such as `invalid_query_syntax`; a string `title`; an integer `status` equal to
the HTTP status; `detail`, `instance` and `traceId` strings when present;
`errors`, when present, an object of field name to messages) is
`provider_rejected_request`. This matches the 400 bodies Salad served live on a
read-only endpoint (a code `type` with `instance`, and the ASP.NET validation
shape with `errors`) and the live 404. The result stays `status: "unknown"`. The summary
holds the status, `title` and `detail` made single-line printable and cut to
200 characters, and up to 16 `errors` keys (field paths, never their values),
and is dropped (back to `provider_unavailable`) if it contains the credential.
Every other answer stays `provider_unavailable`/unknown: 401, 403, 404, 409 (the
group may already exist), 429, 5xx, redirects, timeouts and transport errors,
and any 400/422 with another media type or an HTML, empty, non-JSON, non-object,
incomplete or inconsistent body (for example a generic gateway JSON error). The
classification is diagnostic only, never absence evidence.
The summary appears as `provider_rejection` on the dispatch receipt
(`proxy_reason: "provider_rejected_request"`) and as `create_rejection` in the
live-check receipt (`reason: "provider_rejected_request"`).

Never sent: a create whose POST provably never left the process (a DNS or connect
failure before any request bytes were written, the credential guard, or any local error
before the request) cannot create a group later, unlike a reply lost after sending. The
transport connects explicitly first and marks the POST sent only just before writing it;
a failure before that is `request_not_sent`, with the summary `status: 0` ("The create
request was not sent", no detail, no fields). It takes the same release path below, so
the guardian still releases only after the settle bound and its own absence reads, not
at the route deadline plus the lost-create margin. The live-check receipt reports
`reason: "create_not_sent"`. A failure after the POST was sent (a reset, a timeout
waiting for the reply) stays `provider_unavailable`/unknown.

Pre-start failures: when a step before `start` fails (reading the route, create, the
pre-start settle wait, input ticket mint, guardian delivery confirmation, the start
dispatch, or a bounded wall-clock operation), the live-check receipt keeps its status and
`reason` and adds `prestart_failure: {"class", "exception"}`. `class` is one of the closed
set `route_state_unreadable`, `create_failed`, `group_unsettled`, `ticket_mint_failed`,
`input_delivery_unconfirmed`, `start_failed`, `wall_bound_exceeded`, and the named fallback
`prestart_unclassified`; `exception` is the exception's type name only, never its text.
It is `null` when no pre-start step failed (and absent from older receipts).

Settle read: each exact observe of the new group during the pre-start settle wait is retried
only when it fails transiently (a timeout, or a failed fresh provider read that is not
deterministic and carries a 5xx or no HTTP status), at most `SETTLE_READ_ATTEMPTS` (3)
attempts with pauses `SETTLE_READ_BACKOFF_SECONDS` (2 s, 4 s), never past the run deadline,
heartbeating the guardian in between. A route conflict, a 3xx/4xx refusal, a deterministic
read failure or any other error is not retried. The last failure still ends the run before
start (`group_unsettled`, or `wall_bound_exceeded` for a timed-out bounded operation; status
stays `inconclusive`, exact cancel and stop follow), so a group that cannot be read settled is
never started. The receipt then adds `prestart_settle_read: {"call", "reason",
"http_status_class", "attempts", "outcome"}` with `call` `observe` or `keepalive`, `reason`
one of `read_unavailable`, `conflict_state`, `timeout`, `other`, `http_status_class` one of
`2xx`, `3xx`, `4xx`, `5xx`, `none` (the class only), the attempt count, and `outcome`
`failed` or `recovered` (a read that succeeded on retry). No exception text, body, URL or
header is recorded. The field is absent when no settle read or heartbeat failed.

Release: the trusted server sends admin `create_rejected` with the summary. The
guardian re-validates it (closed keys, status 400/422, or 0 for a create that was never
sent, bounded printable text).
It accepts the report only for a reservation that never recorded a
`provider_id`, allocation, machine, begin, output, running sighting or
allocation uncertainty, that had a `ready` admission (`last_ready_at`, so a
create could have been sent) before the report, and whose fast release was
never voided. Otherwise it fences as `cancel` does (recovery, inputs dropped,
writers closed, guardian cancel when bound) and refuses with
`create_rejection_not_applicable`. An accepted report sets recovery at once
(no further `ready`, so no further create), closes custody writers, drops
inputs, records `create_rejection` (shown in status with
`create_rejected_eligible_at`) and the event `create_rejection_reported`. The
report is a request, not evidence. In the honest flow the safety comes from
one create effect per route and its one-use claim nonce: the rejected POST was
the route's only create. As defence in depth the guardian waits
`CREATE_REJECTED_SETTLE_SECONDS` (30 s, at least `DISPATCH_CHAIN_SECONDS`:
IPC 12 s + recheck 6 s + the 12 s create deadline, asserted in code) after
max(report, last `ready`), so a create admitted by that ready has been sent,
then runs the lost-create reads itself: the exact-name ProblemDetails 404 and a complete project list
(HTTP 200, no pagination keys) without the name. Only then is the reservation
`reconciled` with `reason: "create_rejected_absent"`, a `lost_create` record of
kind `create_rejected` carrying the summary, and the event
`create_rejected_absent_reconciled`; the fence reopens and the 24 h late-group
watch runs as for a lost create. A missing or failed read, an incomplete list,
or a listed name leaves it blocking; the ordinary deadline + 900 s path still
applies. If the 24 h watch then sees a group under the name, it voids the
release, clears the rejection and sets `create_rejection_voided`: only the
margin path can resolve it again and a later report is refused. An unknown
watch read only re-blocks: the rejection is kept, so a fresh definite absence
resolves it again through the fast path. A failed report changes nothing: the
reservation stays on the ordinary lost-create path.

Pre-validation (`salad_resources.py`): the container-group create and get pages
of the Salad API reference (docs.salad.com) give prose limits stricter than the
published schema; Tessera uses the prose limits: `cpu` 1..16 whole vCPUs,
`memory` 1024..61440 MB, `storage_amount` a whole number of GiB within
1..250 GiB, and `gpu_classes` UUIDs. They are checked by `salad_live_setup.py`
(for example `--disk-mb 30000` stops with "storage must be a whole number of
GiB"), at route proposal, approval and the create action (before any
reservation), and again by the transport just before the create POST
(`admission_denied`, no request sent). They are not applied at start, so a
group created earlier is never stranded by these documentation-derived limits. They are never a model validator, so a
stored proposal outside them still parses for status, stop and reconcile.

## Residual risk

The always-on host runs both the driver and the guardian. If that one host dies
mid-run, nothing stops the group automatically. The container exits after its
own runtime limit, but with `replicas: 1` Salad keeps scheduling replacements;
each replacement fails to begin (custody unreachable) and bills only its brief
running time. Mitigation until a second host exists: keep runs short, use the
Portal stop switch, and verify the scheduled-scaling backstop.

## Reconciling actual cost

There is no usage API. After a run, read the organization's Billing & Usage
page in the Portal (usage per project), then record the amount as a human
charge assertion: `POST /experiments/{id}/budget/charges` with the Portal
screenshot or invoice reference as `source_reference` and its digest as
`evidence_digest`. The record remains a human assertion, not verified billing.

## Observe budget

A route's effect list is capped, not fixed at 128. The cap is
`clamp(ceil(max_seconds / 10) + 32, 128, 1024)` from the proposal's
`selection.resources.max_seconds` (3500 s gives 382). create, start, stop and delete
stay one attempt each; only observes use the rest. `max_seconds` is capped at 7200, so
the effective maximum is 752. Each effect plus its receipt is ~0.7 KB of route state and
every save appends the full state as an event row, so stored history grows with the square
of the effect count (~100 MB per route at 382, ~400 MB at 752); accepted until execution
frames (ADR 0041) replace route-state history. While the route reports `unknown` (the
server maps allocating, downloading, creating, pending and deploying to it, and also uses
it for provider failures and reallocation, which therefore see the slower cadence too;
the guardian, not the driver, stops work), the live driver observes every 30 s instead of 10 s, still
heartbeating every 10 s, so a long cold image pull does not exhaust the budget.
