# Schema-54 native HTTP binding checkpoint

## Current continuation: readback and runnable CLI binding

The earlier blocked checkpoint below is historical and superseded by backend
revision `e752635898d04fca4987ee72b905ce4d55719032f289e29c7e4e1eb07407e5b1`.
`NativeDockerHttpClient.readback` now binds POST `/worker/docker/readback` with
the actual closed fence plus optional expected container and delivery key. It
validates original worker/task/grant/lease/epoch pins, execute versus stop-only,
effective action, actual container, and keyed native report response/digest.

`HttpDockerWorkerBackend` binds the service to actual poll/claim/current-authority,
renewal/registry-heartbeat, readback/native report and generic artifact upload/
placement/manifest routes. Local dispatch identity is explicitly the immutable
operation ID; it is not a new control dispatch ID. Poll selection and exact
claim key/descriptor are journaled before claim; restart replays the same claim.
Report wire payload/local payload digest translation is committed before delivery.
ACK translation is based on the actual exact HTTP echo or persisted keyed report
receipt, never fabricated control acknowledgement. Output ACK requires validated
actual quarantined control manifest custody; replay uses content-derived keys and
retained bytes without duplicate manifest creation.

Provider `pending` maps to wire `unknown`, not running. Terminal stop maps to
`cancelled` only with adapter-owned stop intent and confirmed container termination.
Success/failed use the actual inspected exit code exposed by `adapter.exit_code`.
Readback stop-only never calls claim/renew or revives authority. Expired worker
credentials return 401; the existing exact-ID local stop can still be attempted,
but control delivery remains pending and human secure maintenance is separate.
Terminal retained byte reads do not require fresh execution permission; running
capture-stop still does. Quarantine authorization remains enforced by control.

### Ordinary CLI setup

After a human enrolls the exact target/image/resources and approves the task,
and the worker principal is registered with current credentials:

```sh
python -m tessera_worker.ordinary_docker_worker check \
  --workspace /owned/private/worker --worker-principal-id WORKER \
  --control-url https://CONTROL:8765 --credential-file /secure/worker.header \
  --control-ca-file /secure/control-ca.pem --docker-endpoint unix:///run/docker.sock \
  --docker-daemon-id APPROVED_ENGINE_ID --image 'approved/guard@sha256:APPROVED_DIGEST'
```

Use the same flags with `run` for the ordinary continuous service. `check` is
shape-only and reads no credentials. `run` constructs the real native backend,
fixed Docker transport and exclusive service; it reads only the explicit private
owned regular credential file (no symlink, maximum 4096 bytes, bearer/header text),
and uses normal verified control TLS with the referenced CA. HTTPS Engine transport
additionally requires `--docker-ca-file`; there is no plaintext Engine, host-shell,
owner proxy, ambient context, agent socket or auth value CLI flag. The reusable
HTTP client permits literal loopback development HTTP only by explicit opt-in;
the installed CLI intentionally requires HTTPS.

Install ordinary OS supervision externally, running a single instance for this
private workspace. SIGTERM/SIGINT persist local stop intent and attempt owned stop;
`request-stop --workspace /owned/private/worker` records intent only. Unknown stop,
401 or pending delivery is not termination or clean reconciliation. Journals and
stopped fences are retained; don't replace the workspace to bypass uncertainty.

Remaining live prerequisites: approved Linux/amd64 image built from `Dockerfile.guard`,
human enrollment of its **actual resulting digest**, current original worker
credential/normal TLS chain, fixed approved daemon endpoint, installed exclusive
supervision and actual isolation/deadline/output enforcement witnesses. No image
build, credential read, installed migration, Docker, SSH, garage or deployment
occurred during these source tests. Real off-host success/restart/cancellation,
planner/critic integration and laptop-off proof remain unverified.

### Final assigned source gate (2026-10-05)

`pytest -q tests/test_ordinary_docker_http.py tests/test_ordinary_docker_service.py
tests/test_ordinary_docker_execution.py`: **112 passed in 67.85s**, with only the
two existing Starlette/httpx/AnyIO dependency deprecations. Scoped Ruff lint/format
passed for eight owned Python files. The unchanged guard/transport tests from the
earlier 89-test adapter checkpoint were not rerun. The one initial owner-negative
assertion expected 403; the actual worker audience correctly returns 401, and the
assertion was corrected and passed. No backend behavior was altered to fit a test.

The gate covers active submit/report/output, known and ambiguous-start restart,
lost claim reply, actual keyed report ACK recovery, output manifest replay without
duplicate custody, original-worker stop-only after target revocation/lease expiry,
expired-credential 401/no renewal, container mismatch, wrong actor, strict payload
bounds and CLI construction of the real backend/service with injected test-only
transport/credential references. Real temporary ASGI HTTP/SQLite and synthetic
Docker effects are distinct: no off-host network, installed TLS or live container
acceptance is inferred. Existing hard deadline/authority/custody regressions pass.

Changed this continuation: six worker modules (`ordinary_docker_http`, `_backend`,
`_worker`, `_messages`, `_service`, `_execution`), the HTTP and service test files,
this document and the historical service document's current-binding pointer.
No control, agent runtime, Compose or guard recipe edits were needed. The module
CLI source is runnable; actual credentials, guard build/approval, host installation
and supervision verification are still prerequisites for a live run.

## Historical five-test blocked checkpoint

`NativeDockerHttpClient` in `ordinary_docker_http.py` implements the actual native
poll, claim, current-authority and container-report HTTP contracts documented in
[ordinary-docker-control.md](ordinary-docker-control.md). It is deliberately **not**
a complete `DockerWorkerBackend` implementation. No CLI or service was switched to
an incomplete backend. Existing `run` remains fail-closed, not a functional default.

The transport uses the existing bounded control channel: normal HTTPS certificate
verification, no environment proxy, no redirect, no cookies, no automatic retry,
bounded JSON and finite sanitized errors. Plain HTTP requires explicit
`allow_loopback_http=True` and a literal loopback address. No auth file was read;
tests supplied only synthetic fixture credentials. Custom CA/private credential
reference loading and installed CLI wiring remain unfinished, not tested readiness.

Receipts are closed DTOs. Poll validates all immutable package/fence/lease pins,
duplicate operations and requested target. Claim validates the entire package and
package digest; the caller must journal its selected descriptor and key **before**
calling claim. Current-authority has no idempotency key/cache revival. Report uses
actual schema-54 states and fields and validates the exact echoed payload plus
`scientific_validity:not_assessed` and `human_resolution_required:true`.

## Required backend extension (control owner only)

Inspection of `ordinary_docker_api.py:101-118` and `ordinary_docker.py:399-437`
found no worker readback endpoint. Poll is insufficient: it excludes terminal
records and filters expired/revoked authority, and contains neither actual
container identity nor desired cancellation state nor report delivery receipts.
The service must not replace readback with an always-run fake, cached lease,
owner-token query or synthetic ACK. This blocks safe complete CLI binding.

Request a separately authenticated worker-only read endpoint accepting the existing
closed `DockerOperationFence`, returning the exact operation/package/target/epoch
pins, actual container ID or null, actual desired run/cancel state, latest exact
native report and its delivery key/payload digest (or a keyed receipt query). It
must remain readable by the original assigned worker after grant/target expiry or
revocation for owned-work stopping/report-drain recovery, without granting fresh
execution rights; tenant/credential ownership must remain enforced. Closed receipt
types and a finite bounded response must be documented by the backend owner.

The existing ordinary service's `dispatch_id` has no schema-54 wire counterpart;
binding must use the actual operation fence and retain an explicit local identity,
not invent a control dispatch ID. Its ACK protocol likewise is not a schema-54
endpoint: native report returns an exact echo, not `DockerDeliveryAck`. Pending
provider state has no wire equivalent and must remain unknown unless actual
running/terminal evidence exists. Failed terminal reporting requires actual
nonzero exit-code evidence; the current provider-neutral observation does not
expose that number. These are concrete integration changes still to implement
once required readback is finalized, not permission to fabricate evidence.

Output intake exists through the generic artifact transfer/placement/manifest
routes and accepts actual Docker operation IDs; no Docker-specific upload endpoint
is required. Its exact quarantine receipt mapping and durable custody transfer
must still be bound to the service without blocking ongoing authority refresh.
Renewal/registry heartbeat APIs exist but have not been bound by this checkpoint.

## Evidence and next action

`pytest -q tests/test_ordinary_docker_http.py`: **5 passed in 11.17s**, with two
existing Starlette/httpx/AnyIO dependency deprecations. Tests exercise actual
temporary FastAPI HTTP routes and SQLite transactions through an ASGI transport,
not an off-host network socket: native poll/claim/report replay, current authority,
wrong-credential denial, target revocation and honest synthetic stopped-container
reporting, explicit loopback development opt-in and redirect denial. Docker IDs
and termination evidence are synthetic; no daemon/container/model ran. Scoped Ruff
lint/format passed. Prior 89 adapter and 31 service tests are reused, not rerun.

Only the HTTP client, its test and this document are added at this checkpoint.
No control source/test edits, runtime/profile edits, live auth reads, Docker, SSH,
garage, image pull/build, migration, install, inference, commit or full gate ran.
Parent should request the exact readback extension from the existing backend owner
and rebind this same worker to finish protocol/service/CLI integration and targeted
authority/restart/custody tests. Guard prerequisites remain: approved linux/amd64
image built from `Dockerfile.guard`, actual resulting digest enrolled by a human,
fixed Engine endpoint and verified isolation, private exclusive workspace,
worker-only credential and normal TLS chain. No image approval/build or installed
enforcement witness is inferred from source tests.
