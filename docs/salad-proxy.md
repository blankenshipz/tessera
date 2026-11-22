# Salad server proxy — GPU milestone and activation runbook

**Default and fake entry points remain provider-disabled.** A separate trusted-server
composition implements real Salad HTTP calls; implementation is not deployment or
live acceptance. No real provider call, GPU run, deployment, build/pull, credential
discovery or paid effect was authorized for this milestone's local validation.
See [decision 0031](decisions/0031-salad-single-container-gpu.md) and the preserved
[original boundary](decisions/0024-salad-server-proxy-boundary.md). Salad is not a
provider-isolation witness or trusted autonomous sandbox.

Runtime spend controls, the off-host stop switch and cost reconciliation are
recorded in [Salad runtime spend safety](salad-spend-safety.md).

## Deployment topology and trust

```text
desktop / agent -- authenticated control requests --> control API + authority DB
                                                        |
                                             enrolled remote dispatcher
                                                        |
                                           trusted server proxy (explicit composition)
                                              |                 |
                                  secret resolver boundary   fixed Salad API
                                   (private server backend)  (bounded HTTPS transport)

workload -- bounded output/checkpoints --> external quarantine custody
independent target monitor -- authenticated evidence --> control receiver
```

The last two lines have local implementations but still require independent deployed
services and live conformance. The proxy must not share a process, filesystem,
credentials or administrative
channel with desktop renderer, agent harness or provider workload. The control API
accepts proposals/reviews/cancellation; it does not expose the proxy or its config.
The server composition authenticates a worker through existing audience/tenant/project
checks. No request may declare itself a trusted proxy or supply approval booleans.

Default API instances have no allowed targets or dispatch loop. `SaladProxyAdapter`
now projects the blocked typed boundary. `SaladProxyFakeAdapter` composes only an
exact `FakeSaladProxy`, `FakeSaladTransport` and `FakeSecretResolver`; these execute
no processes and call no provider. Passing an arbitrary injected transport cannot
enable the fake path. The deployable `SaladProxy.request` is always blocked,
including when dependencies have been injected. There is deliberately no enable
flag or network client hidden behind a test flag. `TrustedSaladServer` and
`SaladTrustedAdapter` are a separate administrator-owned composition, never a
client-request switch or substitution in the fake adapter.

## Secret custody

The [protected enrollment helper](protected-secret-enrollment.md) now implements
same-host native macOS Keychain custody, independently of this proxy. It is **not
wired into live proxy activation** and does not provide remote Kubernetes custody
or service identity. The fake resolver/transport below remain unchanged; native
enrollment is not completion of the activation checklist.

* `ProxyPolicy.management` is server-only reference/version/purpose metadata and
  is excluded from policy serialization. Do not put it into route packages, plans,
  prompts, client settings, receipts or artifact manifests. Actual key values have
  no config/request/env field and no ambient discovery path. The explicit isolated
  server backend uses owner-only files in an administrator-selected private directory;
  file values never belong in CLI/configuration, workload or receipts.
* Only `credential-ref:<opaque-name>` is accepted. Env/prompt/request locators,
  raw values and account-key overrides are invalid. The management purpose is
  `salad_management`; AI Gateway keys must never use this resolver or be substituted
  for management keys. Gateway functionality itself is forbidden here.
* `SecretResolver.metadata` must return fresh server-observed version, revocation,
  purpose and expiry. Resolution must **atomically** recheck those conditions and
  return an exclusively owned mutable buffer. Current fake checks allow five-second
  metadata age and require expiry beyond the two-second request budget. Neither
  metadata nor value resolution occurs at all on the live-disabled entry point.
  The trusted composition resolves for authorized read-only preflight and rechecks
  current authority after resolution; mutations additionally require a durable
  one-use claim and fresh use-time authority before sending.
* `SecretBuffer` has redacted repr, denies pickling and wipes its owned bytearray on
  exit, including response errors and exceptions. **This is not guaranteed Python
  process-memory erasure:** immutable copies, backend caches, stack traces, native
  clients, core dumps and tracing need deployment-specific controls. Local native
  custody does not establish a production proxy backend or hardware-backed custody.
* Future operational rotation: disable new admissions, rotate at the secret backend
  and provider under separate authorization, advance the server-pinned version,
  invalidate caches, test revocation with synthetic fixtures, then obtain new
  admission. Never retry an already consumed nonce. Retain stop-only recovery under
  independently authorized credentials. Rotation does not terminate existing work.
* A provider key can have wider authority than a route. Restrict secret-backend access
  to the proxy identity; restrict outbound traffic at deployment; forbid debug/body/
  header logging, tracing dumps and proxy environment inheritance. Static keys do
  not become short-lived provider credentials merely because metadata has an expiry.

## Closed operations and admission

`ProxyRequest` contains only `route_id`, committed `effect_id` (nonce), and one of:
`read_project`, `read_quota`, `read_availability`, `create_stopped`, `read_group`,
`read_instance`, `read_events`, `read_logs`, `start`, `stop`, `delete`.
These are closed normalized operations. `salad_http.py` implements their current
fixed wire mapping with isolated contract tests, not live API conformance. Read
operations reconcile an admitted group; fake preflight
supplies project/quota/availability/absence before create without network or secrets.
No project-creation endpoint is implemented. The fixed prospective management
origin is `https://api.salad.com/api/public`; the legacy rejecting config also
retains its historical `.invalid` fixture endpoint. Arbitrary endpoints, redirects,
HTTP methods/paths, headers, proxy overrides and workload-supplied IDs are rejected.

Before create/start, the server rechecks:

1. Live authenticated tenant/project/worker, task/plan, human execution grant,
   program/source-use reviews and exact human route approval. Agent requests cannot
   grant authority; held recovery cannot mutate. Source data release and scientific
   program review remain separate existing controls.
2. Exact server target allowlist including Salad org/project, country, CPU/memory/GPU,
   disk/runtime bounds, one replica and immutable image/manifests. Group must be
   `attempt-<operation_id>` and globally reserved in the route ledger.
3. `salad.execution`: `linux/amd64`, fixed `/tessera/bin/run`, exact bounded argv
   matching the retained approved package, exact non-secret environment allowlist,
   output bytes/files, opaque artifact/checkpoint destinations, watchdog/monitor
   profile hashes, explicit public-released-only data policy and broad provider-egress
   trust acknowledgement. Only `PYTHONUNBUFFERED=1` and `OMP_NUM_THREADS=1|2|4` are
   supported environment settings. Runtime install/download, queues, autoscaling,
   gateway, SSH, exec and interactive shell are forbidden.
   The optional `argv_binding="smoke_operation_v1"` resolves only the reviewed
   smoke slots: retained plan `execution_spec.argv` is
   `[/tessera/bin/run, v1, public-integers-v1, operation-id, operation-epoch,
   CUSTODY_DNS, WATCHDOG_SHA256, MAX_SECONDS, MAX_OUTPUT_BYTES, 1, DIMENSION]`.
   Only those two literal placeholders are replaced with the current admitted
   operation/epoch; every other token stays pinned. The approved retained package
   is never rewritten, no deterministic nonce is invented, and human concrete-route
   review still applies. Default `argv_binding="exact"` preserves legacy equality.
4. Current approved capability declaration for the same tenant/project/task/image;
   actions, credential refs and runtime network capabilities must be empty in this
   supported slice. No capability expansion, downloads, installs or broker activation
   is inferred from the declaration. The provider-egress acknowledgement is **not**
   network isolation; tasks needing restrictive egress cannot use this as a witness.
5. Budget/output caps within the parent plan, unknown-exposure policy acknowledging
   credit is not a hard cap, fresh exact five-second target preflight and exact
   stopped-group identity before start. Monitor/profile/destination hashes are
   reviewed requirements, **not evidence those systems exist or enforce limits**.
   The independent service uses official positive published GPU rate observations
   and a conservative durable exposure ledger to fence stale/unknown/rising rate,
   unclosed orphan exposure and estimate-over-cap. Estimates never establish actual
   billed cost, a credit balance or a hard financial cutoff.
6. Retained package, operation, attempt, lease, epoch, manifests, route digest and
   one-use effect nonce. A `salad_proxy.claim` event commits before fake resolution/
   effect. Claims survive process death and restart; a later read may reconcile but
   never authorize another create/start. A new target cannot reuse an operation.

Legacy routes without the executable profile remain readable and usable by the
older offline adapter, but cannot pass the new proxy admission. Default/fake dispatch
remains disabled; trusted-server dispatch separately requires current authority,
private credential custody and fresh independent safety-service readiness.

## Responses, evidence, cancellation and custody

One bounded call, zero retries per effect. Unknown create reconciles the same reserved
name/identity; never creates a duplicate. The simulator uses the operation ID as
its provider ID; the trusted transport discovers the actual provider-assigned ID.
Lost-create reconciliation still needs live conformance before acceptance.

Stop/delete 202 is `pending`, never confirmed termination. Outage, timeout,
redirect, identity mismatch, malformed/oversized response or echoed marker returns
unknown with a closed reason code; raw exception text is discarded. A create
answered with a clean ProblemDetails 400/422 is still unknown, with reason
`provider_rejected_request` and a bounded non-secret summary; the guardian then
releases the reservation early only on its own absence reads (see
salad-spend-safety.md, "Create rejected"). The resource shape is checked against
Salad's documented create limits at proposal, approval and create, and before the
POST. Reads have a 2 s total client deadline; the create POST has 12 s (DNS,
connect, TLS, send and response). Changed
allocation marks uncertainty and blocks continuation. A later exact synthetic
readback can confirm a fake stop but cannot establish live no-continued-writes.
Quota/credit loss does not revoke narrowly scoped stop-only authority. Deleted
credentials/worker identity require separately authorized operator recovery.

Receipts append to the control event ledger with operation/route/effect attribution,
status and unknown cost/usage. Provider logs/events are bounded SHA-256 references;
raw untrusted text is not placed in API projections. These hashes alone do not
retain bytes or prove custody. Artifact/checkpoint links must match retained
same-project/operation/epoch/plan manifests. Byte upload, quarantine retention,
custody release and scientific acceptance use existing APIs, not provider claims.
The GPU recipe uploads bounded canonical bytes to authenticated external quarantine
custody; independent deployment, actual byte retention and stop conformance are
still prerequisites, not facts established by a signed receipt or local tests.

## Generic workload contract (workload_v1)

ADR 0036 phase 1 adds a third `execution.argv_binding`, `workload_v1`, beside
`exact` and `smoke_operation_v1` (both unchanged). It carries a reviewed
`workload` object (`tessera.salad.workload.v1`, strict, extra fields refused);
that object is required for `workload_v1` and forbidden for the other bindings.
It is reviewed profile data, never an execution grant.

- **Command and argv.** The command stays the fixed `/tessera/bin/run`.
  `argv_template` (1-32 printable tokens of 1-256 characters) holds literals or
  exactly one of `{operation}`, `{epoch}`, `{custody_host}` per token. Nothing
  else is substituted and a token containing braces that is not exactly a
  placeholder is refused. The approved spec argv must equal
  `[command, *argv_template]`; `execution.argv` must equal the derived argv on
  every use.
- **Environment.** `env` replaces the PYTHONUNBUFFERED/OMP_NUM_THREADS allowlist
  for these profiles only and must equal `execution.env`. Keys match
  `^[A-Z][A-Z0-9_]{0,63}$`, at most 32 entries, values are 0-512 printable
  characters, and keys containing TOKEN, SECRET, PASSWORD or CREDENTIAL, or
  ending in `_KEY`, are refused. Secrets never belong here.
- **Runtime.** `workload_seconds` (1-3600) is the workload's own runtime. The
  guardian's billed running window for the reservation is
  `workload_seconds + 120` (upload grace), never longer than the route window.
  The route window itself may now be up to 7200 s (45 min unbilled provisioning,
  up to 60 min billed, grace); the live driver polls at most 10 s apart.
- **Outputs.** `outputs` declares 1-16 unique files (`name`, `max_bytes` up to
  64 MiB each). Custody holds at most 16 MiB in total per operation (the same
  bound as `execution.max_output_bytes` and the plan's `max_output_bytes`); setup
  and reserve refuse a larger total with `outputs_unsupported`. The approved
  spec's `output_paths` must equal the declared names in contract order. See
  "Declared multi-file output custody" below.
- **Egress.** `egress.kind` is `provider_broad` with the list of `fetches`
  (https URL, sha256, `max_bytes`) the workload will download and must verify.
  Salad has provider-wide egress: the declaration is recorded and reviewed, not
  enforced, and such a route never claims network-none (the base plan's
  `network_enabled=false` is a plan-schema constant, not an isolation claim).
  Optional `egress.redirect_hosts` (at most 16 sorted, unique, exact lowercase DNS
  names; no wildcard, port, or all-digit or `0x` label; only with `runtime: gpu_runtime_v1`, which binds
  and enforces it) lists the only hosts a fetch may be redirected to, for pinned
  URLs that answer with a redirect to an expiring signed CDN URL.
  Empty (the default) follows no redirect and is not serialized, so existing
  contract and proposal digests are unchanged. The per-fetch sha256 and
  `max_bytes` still bind the bytes; see workloads/gpu_runtime_v1/README.md.
- **Capability declaration.** For `workload_v1` the approved capability's
  `network` list must equal the sorted distinct (host, port) destinations of the
  fetches (port 443 unless the URL names one) plus every redirect host on port
  443; `actions` and `credential_refs` stay empty. Smoke and exact profiles still
  require an empty `network`.
- **Custody host.** `custody_host` is the exact reviewed lowercase hostname and
  must equal the safety service's destination.
- **Custody host binding is declarative.** Salad has provider-wide egress, so the
  workload could reach another host: a literal argv token or env value may name
  one. `custody_host` is the reviewed destination the safety service checks and
  the one custody accepts, not a network restriction.
- **Route window versus plan.** The plan's `max_duration_seconds` stays within
  the shared 3600 s limit; Salad routes may exceed it by a fixed 3600 s unbilled
  provisioning allowance (`route_window_limit`), up to 7200 s.
- **Safety service.** `_request_for(proposal)` dispatches: smoke profiles keep
  parsing their argv; `workload_v1` derives the same request dictionary
  (operation, epoch, destination, watchdog, `max_seconds`, output caps) from the
  profile and the admitted operation/epoch. Reserve, begin-once/output checks and
  the billed running window all use it.
- **Setup.** `scripts/salad_live_setup.py --workload-contract PATH` (a private
  0600 JSON file holding one contract; `custody_host` must equal
  `--custody-dns`; `--workload-seconds` is forbidden) builds a `workload_v1`
  route and derives the capability network list from the fetches. Without it the
  smoke route is unchanged. A contract that declares an input also needs
  `--input-dir DIR --data-class public --license ID --source URL_OR_REF`: Salad is
  third-party compute and admits only a staged snapshot recorded public with evidence
  ([ADR 0039](decisions/0039-data-class-guard.md)). Setup never defaults to public.
  Admission prefers a public staging of the same bytes in the project; bytes the org
  already holds as derived or pre-v62 (migrated) data cannot be declared public.
  `--approve-private-code PATH` (repeatable) approves exact code or config files of the
  input for this remote run (ADR 0039 amendment 1): they stay private and unlicensed,
  every other file still needs the public declaration, and with approved code the
  manifest's input entry must be one of them. The summary lists each approved path,
  its sha256 and the recorded decision class.


## Declared multi-file output custody (ADR 0036 phase 3)

Reserve registers a `workload_v1` operation's inventory with every declared output
name and per-file cap (the fixed smoke keeps its single undeclared output). All
workload calls below go through the same checks as begin-once: Salad IMDS JWT for
the bound machine, exact binding, fresh guardian loop, armed guardian, live
heartbeat and the stop deadline; they also require begin-once to have been
granted and refuse fenced or unknown-rate reservations.

- `POST /v1/workload/outputs` `{binding}` returns `tessera.output-plan.v1`
  with `chunk_bytes` (1 MiB) and the declared `outputs` (`name`, `max_bytes`).
- `POST /v1/workload/output-chunk` `{binding, name, sha256, bytes, seq,
  chunk_sha256, content_base64}` stores one chunk. Its body may be up to about
  1.34 MiB; every other path keeps the 128 KiB cap, checked before the body is
  read (see "Request time budget" below). The first chunk of a name reserves its declared `bytes` and whole-file
  `sha256`; later chunks must repeat them exactly. A chunk must be exactly the
  declared slice (`seq * 1 MiB` onward, the last one short, an empty file is one
  empty chunk) with a matching `chunk_sha256`. Chunks may arrive in any order;
  an identical duplicate is a no-op and a different one at the same `seq` is
  refused. Undeclared names, a size above the file's cap and a total above the
  inventory cap are refused before any byte is stored. When a file's last chunk
  lands, custody re-hashes the stored parts in order; a mismatch discards that
  file's parts (the declaration stays fixed, so a wrong declaration can never
  complete). Every chunk call, refused ones included, counts against a budget of
  twice the planned chunks plus 16.
- Only when every declared output is complete does the guardian record the stop
  intent (`output_retained`), set the output flag and then close writers, in that
  order; each step is idempotent, so a retried chunk repairs an interrupted
  completion. After completion an exact replay of a stored chunk (a lost final
  response) is acknowledged read-only until the group is confirmed stopped;
  anything else is refused. Loop freshness for workload calls is judged when the
  listener accepted the connection, so a slow chunk body is not refused for it.
- The chunked endpoints also charge a per-operation request budget (twice the
  planned chunks plus 32) after the token is verified against the bound group
  and machine and the binding matches, but before the Salad read; an
  unauthenticated caller cannot spend it. A partial upload never extends the heartbeat,
  stop deadline or running window; at the deadline the guardian stops and
  closes writers as before, and uploads after completion, after the deadline
  or after sealing are refused.
- The original `POST /v1/workload/output` (one canonical JSON `output.json`)
  stays for the fixed smoke and for a contract whose only output is
  `output.json` of at most 64 KiB; it is refused for other declarations.

**Request time budget.** The listener is single-threaded, so each request gets
one wall-clock budget from accept (TLS handshake, request line, headers, body,
work, reply), enforced by the whole-request alarm plus a per-body deadline:

| Phase | Small paths (128 KiB cap) | `output-chunk` (1,402,197 B cap) |
| --- | --- | --- |
| Handshake, request line, headers | 3 s from accept | 3 s from accept |
| Body = `length / 187,500 B/s + 1 s` (floor 1.5 Mbit/s) | 1.70 s | 8.48 s |
| Reserve: auth, provider read (2 s), custody write, reply (2 s) | 5 s | 5 s |
| Request alarm | 10 s (3 + 1.70 + 5 = 9.7) | 20 s (3 + 8.48 + 5 = 16.5) |

The floor rate is the sustained upload a 1.34 MiB chunk needs to fit; a slower
sender is cut off and retries. Before any body byte is read, the request must
pass a cheap pre-auth check from its headers: the admin token for `/v1/admin`,
or for workload paths a Bearer token that parses as a Salad ES256 JWT, is
unexpired and verifies against the cached JWKS (no network call; the tenant,
group and machine scope check still runs after the body). Only a request that
passes it has its alarm extended past the 3 s header phase, so an unauthenticated
client, including one that reconnects and trickles header bytes, holds the
guardian thread for at most 3 s per connection; the loop runs between requests.
An authenticated chunk upload may hold it up to 20 s. `UNSEEN_RUN_GAP_SECONDS`
(the billed time a run may hide between two guardian polls, used to backdate
accrual and in admission budgeting) is 45 s: between two loop passes the guardian
may handle one unix admin frame (2 s read plus the 10 s admin budget) and one
authenticated chunk request (20 s), plus a worst-case 12 s loop pass (two 4.5 s
provider-read retry windows, one first-attempt read at the 2 s transport deadline and
1 s of local work), i.e. 44 s; module asserts keep these consistent. The reply resets the
socket timeout to 2 s.

`status.retained_outputs` is read-only progress for declared outputs (exact
`binding`, sorted `declared` names, completed `files` with `sha256`/`bytes`,
`complete`, `quarantine`); it is `null` for the smoke. `import` after confirmed
stop seals custody as before. For the smoke, or a single `output.json` retained
through the original endpoint, it returns `{manifest, output}` unchanged.
Otherwise it returns `{manifest, chunk_bytes}` and control pulls each file with
the admin action `import_chunk` `{name, seq}` (also only after confirmed stop).
`SaladSafetyClient.import_retained` returns `{manifest, output}` (no `files`) for
the two original-shape cases above, `{manifest, files, output}` for a single
`output.json` uploaded in chunks (`files` is the verified `{name, sha256, bytes}`
list; `output` is parsed strictly, rejecting NaN, Infinity and duplicate keys),
and `{manifest, files}` for every other declaration. It verifies the signed manifest, writers closed,
quarantine, completeness, the exact binding, the exact declared name set and the
per-file and total caps, then recomputes every chunk's and every file's sha256
from the streamed bytes; with `destination` (an existing 0700 directory) it
publishes each verified file with a no-replace hard link and never overwrites;
a retry after a partial import accepts already published files whose digest
matches and refetches the rest. The live check does
the same through the pinned HTTPS admin channel and its receipt carries
`outputs: [{name, sha256, bytes}]` (a smoke receipt keeps `output_sha256`).

The guardian's private custody database gains a `parts` table and additive
columns (`inventories.outputs`, `inventories.chunk_calls`, `files.chunked`) on
open; the control database schema is unchanged. Guardian memory stays bounded by
one request body and one chunk at a time; its disk by the declared caps.

## Local validation (no network, opt-in file read)

```sh
.venv/bin/python -m tessera_control.salad_proxy_cli
.venv/bin/python -m tessera_control.salad_proxy_cli \
  --validate-config docs/examples/salad-proxy.offline.json
.venv/bin/pytest -q tests/test_salad_proxy.py tests/test_salad_contract.py \
  tests/test_remote_execution_routing.py
```

Without the option, no configuration is read. With it, at most 64 KiB is validated;
errors never echo the file, exception text or reference. Exit 0 means local schema
validity (or no file requested), not admission/live readiness; exit 2 means invalid.
The example contains only invented identities, hashes and an opaque reference,
**not a project, key, existing image, grant or usable deployment**. Output always
reports transport disabled, effects 0 and the safety prerequisites. Do not use
configuration validation to discover credentials or test an account.

<a id="live-enablement-checklist--all-currently-unresolved"></a>

## Live enablement checklist — local implementation is not deployed proof

- [ ] Reviewed native Salad package and provider API/version/serialization contract,
      exact stopped creation, immutable identity, no redirects/retries/proxy inheritance.
- [ ] Deployed authenticated proxy/service identity and approved secret backend with
      rotation, version pinning, revocation, no-leak/dump/log testing and least privilege.
- [ ] Separately approved tenant/provider account mappings, sources/data release and
      provider trust limitations; no private data or unbounded autonomous capability.
- [ ] Independent monitor/watchdog enforcement of startup/run/storage/output limits
      despite control disconnect, proxy death, provider outage and worker compromise.
- [ ] Allocation/reallocation conformance and independently confirmed termination,
      no continued writers, exact group cleanup and lost-response reconciliation.
- [ ] Real usage/cost attribution, credit/exposure accounting, stop on unknown spend,
      orphan reconciliation and an explicitly approved maximum financial exposure.
- [ ] External checkpoint/output byte custody, integrity, quarantine, writer quiescence,
      retention/recovery and authorization at exact destinations.
- [ ] Separately authorized bounded live acceptance with provider prerequisites and
      spending limits, outside default pytest/CI. Fake/monitor signatures are not passes.

This checklist is documentation, not a set of booleans that enables transport.
The separate server implementation removes code gaps; deployment and explicitly
bounded live evidence still must satisfy every requirement.

## First GPU journey: setup versus normal operation

### One-time setup (not authorized by this runbook)

1. Choose an existing Salad organization/project and reviewed GPU resource/country
   mapping. Do not create projects, purchase credits or infer spending approval.
2. Build/publish the [GPU smoke recipe](../workloads/salad_gpu_smoke/README.md) under
   separate authorization; retain the resulting immutable linux/amd64 image digest.
   The recipe is currently unbuilt; installed torch or a Dockerfile is not GPU proof.
3. Provision the trusted Linux server identity and private version-pinned management
   credential backend. Enroll through the administrator-only backend, not argv,
   environment, a client request, config values or a desktop Keychain search.
4. Independently supervise the guardian/custody service, its durable private database,
   exact peer identity and HTTPS workload destination. Review the native Salad IMDS
   JWT verification and exact operation/group/machine/image bindings. Guardian must
   survive control/proxy disconnect/death; sharing a thread is not independence.
5. For routes that carry approved private code (or other non-public classes), enroll
   one stable executor before setup and list its principal in the provider trust file;
   pass `--executor-dir` to every setup. See
   [provider trust: stable executor](provider-trust.md#stable-executor-route-setups-that-mint-principals).
6. Approve the exact public-data task, package, route, account/project/image/GPU,
   capability declaration and exposure ceiling through existing human authority.
   Review startup/runtime/output limits and orphan/unknown usage policy. Account
   credit is not a hard cap; unknown usage must block new work, not exact stop.
7. Authorize the experiment budget (`POST /experiments/{id}/budget/authorization`)
   **before** admitting the GPU attempt: Salad create/start requires the reservation
   written at admission. Declare every bound as a known amount (`0`, not `null`);
   an unknown bound blocks admission permanently. Earlier attempts are allowed only
   when all are terminal and prior settled/pending costs are declared.

### Everyday operation and return

1. Enter through the existing authenticated control workflow and select the approved
   route. Obtain current grant/reviews/lease/epoch; command-line `--run` grants none.
2. Dispatch create-stopped and start only through `RemoteDispatcher` and the concrete
   trusted adapter. Each is one-shot and consumes durable intent before provider
   effect. A lost response means reconcile the original group, not relaunch.
3. Observe the CUDA smoke and its bounded quarantined canonical output. Re-read the
   retained bytes and verify SHA-256; workload-reported CUDA is not GPU attestation
   or scientific acceptance. Provider log hashes are not retained artifact bytes.
4. Cancel the exact route, revoke writers and issue narrowly scoped exact-group stop.
   Require observed stopped identity plus independent writer quiescence. HTTP 202,
   outage, missing group, allocation change or an acknowledgment is not a stop pass.
5. Exit the client without shutting down guardian/custody or deleting its database.
   Reconnect/restart reads the same route, effect history and retained bytes without
   create/start. Setup credential/JWT expiry must not erase custody or consumed
   claims. Management credential expiry may require independently authorized
   stop-only recovery; it is not termination proof.

The workload's operation-wide begin-once claim survives startup failure/reallocation;
there is no automatic GPU retry, new epoch reset, resumption or replacement group.
Guardian must stop uncertain allocations and retain unresolved resource/cost exposure.
After normal confirmed stop, keep quarantine and audit records according to approved
retention. Deleting the stopped group is the separate, audited cleanup below, never
an automatic fallback. Never tear down the independent service merely because the
live-check process exits.

### Cleanup of stopped groups

Stopped groups still count against the project's group quota, so Tessera can delete
the ones it created. `tessera_control.salad_group_cleanup` works **only from
Tessera's own route records**. It never lists the provider's groups and never reads
or deletes a group the records cannot prove Tessera created, so other groups in the
project are untouched by construction.

A route is eligible only if all of these hold; each failure is a distinct reported
skip reason: the route targets the Salad container group; it is `stopped` with
confirmed termination and `desired == stop`; it was not reallocated; it has a bound
provider id **and** the recorded group name is exactly `attempt-<operation id>`
(a group whose creation response was lost has no bound id and is never deleted);
no delete was ever attempted and no effect is unresolved; and the stop was recorded
at least `min_age_seconds` ago (default 3600).

For each eligible route, one at a time in deterministic order (bounded by
`max_groups`), `apply` does exactly this:

1. Read that group by its bound id (read-only, through a provider port). The id,
   `stopped` state, no pending change and zero instances must all match, otherwise
   the route is skipped and audited; an identity mismatch or provider error stops the
   whole run. Any future port must pin the group itself: the HTTP transport deletes by
   name after an id-verified GET, and a port that fills the observation name from the
   route's selection proves nothing by that name, so the port's own identity check is
   what counts.
2. Commit the one-shot `delete` effect on the route (`begin`). `begin` is the durable
   intent commit. It is called with the route revision the candidate was read at and
   fails, in the same transaction, if the route changed since. A route can never have
   a second delete.
3. Write the audit event `remote.group_cleanup_recorded` (phase `intent`), which
   carries the effect id. A run that loses a race to another run writes no intent
   event.
4. Read the group again immediately before the delete. It must still be the same
   stopped, idle group. On any drift (or a failed read) nothing is sent: the outcome
   `delete_aborted_drift` is audited with no receipt and the route stays `unknown` for
   read-only reconciliation. The one-shot delete effect is consumed; that is the safe
   direction.
5. Send the provider delete. `202` means pending, never confirmation.
6. Read the group back once. Confirmed gone records state `deleted`. Anything else
   records state `unknown` with termination not confirmed and `accepted_pending`
   (a delete was accepted and is unreconciled), flags `needs_reconciliation` and is
   never retried. Such a route can neither be started nor deleted again.
7. Record the receipt on the route and write the audit event (phase `outcome`). A
   crash after step 2 leaves the route `unknown` with a delete effect, so a later run
   never deletes again. A crash after the receipt leaves the recorded receipt and no
   outcome event; the route is already `deleted` or `unknown` and is never deleted
   again either.

Authorization comes before any provider read: the route list is the calling
worker's own routes only (same organization, accessible project, and an admitted
external operation naming that worker principal). A route failing any check never
reaches a port. A store refusal before a delete was sent is recorded as
`not_authorized` and stops the run without raising. A store refusal after the delete
was sent (receipt or outcome audit refused, for example a revoked credential or a
late-receipt conflict) stops the run and reports `delete_unrecorded` with
`delete_status: sent` and `needs_reconciliation`; a sent delete is never reported as
skipped. `plan(record_plan=True)` counts audit events it could not record
(`unrecorded`) and completes. Once a delete effect has been attempted, even if it is
later reconciled (observe, then a receipt back to `stopped`), the route stays
`delete_already_attempted`, so such a group can only be removed in the Salad portal.
The audit event shape is validated strictly by the store (unknown keys, a true
`liability_released`, a `readback_source` other than `non_live_port` and malformed
fields are refused).

The receipt is `synthetic` like every receipt today, carries no cost and names no
cost relief; its evidence link is a hash of the marker "cleanup of a stopped
zero-instance group". Receipt evidence and audit events also carry
`readback_source: non_live_port`. **Deleting a group does not release experiment
liability or budget** ([spend safety](salad-spend-safety.md)).

**Applying is not available against the live service in this milestone, and there is
no production port.** Live group deletion has no activation gate (no one-use claim,
preflight freshness, rebinding, stop or input revocation), and no adapter over the
real HTTP transport exists. `apply` accepts only a port marked `live is False`, and
`TrustedSaladServer.cleanup_settled(cleanup, port=...)` additionally refuses unless
the server's own transport is marked non-live too. The real HTTP transport carries no
such mark, so **against a real server the hook always refuses**: recording a delete
through a fake port on a real service's routes would fake provider status. The hook
is off by default, needs a cleanup bound to the same worker and store, and refuses to
run inside a store transaction. Nothing calls it. The `live is False` marks guard
against accidental live use; they are not a security boundary (a subclass or an
instance attribute can satisfy them). The no-transaction guard sees only store-mutate
lineage; it cannot see an arbitrary caller's own database transaction.

Operator dry run (opens the state read-only, makes no provider call, changes
nothing, exit 0; usage errors exit 64; there is no apply option). It is a local
operator tool and reads every organization in the state directory:

```sh
tessera-salad-cleanup --state-dir /path/to/state --min-age-seconds 3600 [--json]
```

A dry run does not read the provider; every candidate is re-verified at apply time.

Honest limits: the live DELETE client path has not been exercised and is unreachable
from this code; the HTTP client cannot yet tell a 404 from an outage, so even a
future live delete would read back as unconfirmed and stay flagged for read-only
reconciliation; groups with a lost creation response are never deleted (an operator
may remove them in the Salad portal).

### Opt-in live check and current service prerequisites

```sh
.venv/bin/python -m tessera_control.salad_live_check
```

The default reads **no config, secrets, environment or provider state**, performs
zero network calls/effects, and prints static missing prerequisites. Supplying
`--server-config` or `--receipt-directory` without `--run` also remains static:
these paths are not probed. This is not an account probe. Only an explicitly
authorized operator run reads a server config:

```sh
.venv/bin/python -m tessera_control.salad_live_check --run \
  --server-config /absolute/private/live-config.json \
  --receipt-directory /absolute/private/new-receipt-directory
```

Neither this example nor `--run` supplies missing execution/spending authorization.
Run only on the enrolled trusted Linux server with the exact preexisting human-
approved route, not from the desktop, agent, workload or a generic developer shell.
Exit 0 with `--run` means the bounded driver passed its provider/custody journey;
without `--run`, exit 0 means only that static readiness printed. Neither exit
proves actual billed cost, node-local writer death, or scientific acceptance.
Exit 2 with `--run` means setup/acceptance is unresolved.
Errors are closed codes, never raw exception text or config/credential values.

The callable `salad_live_check.SaladLiveSession(config_path)` loads the concrete
server composition in that same module. `salad_live_check.run(session, ...)` uses exact
`TrustedSaladServer`/`RemoteDispatcher`,
authenticated external `evidence`, `retained_bytes` and `reopen`, exact `state`
and `cancel`. Runtime use-time admission remains authoritative. The external
evidence must independently establish output retention/digest/byte cap, provider
identity, stop and writer quiescence; user-supplied booleans cannot supply it.
HTTPS admin status/import uses `SafetyHTTPSClient`, CA hostname validation and an
exact certificate SHA-256 pin; provider mutation remains behind the one-shot
dispatcher and independent Linux peer-authenticated guardian socket. Reopen
reloads/re-authenticates the same config and durable database; no launcher or claim
is reset. Default/fake APIs are not changed by this CLI.

#### Private config contract

Config is a closed JSON object, <=64 KiB, mode 0600, owned by the server uid,
single-linked, with no symlink path components. Referenced state/credential
directories must already exist (0700); referenced files must be 0600/owner-only,
single-linked and non-symlinked. Paths are absolute; no env interpolation, relative
paths, raw token/key fields, arbitrary provider endpoints or extra fields exist.
TLS CA is a referenced reviewed file, not embedded configuration bytes.

| Field | Required binding |
|---|---|
| `schema` | `tessera.salad.live-config.v1` |
| `state_dir` | Existing private authority database directory, never initialized by this harness |
| `route_id`, `task_id`, `org_id`, `project_id`, `proposal_digest` | Exact existing approved route and raw 64-hex retained routing-package digest |
| `policy` | Exact server `ProxyPolicy`, including management **reference/version/purpose only**, immutable image/account/project/GPU and executable profile |
| `worker_credential_file` | Explicit private file containing an already-enrolled worker-audience token |
| `cancel_credential_file` | Explicit private file containing existing human control authority for exact cancellation; never a newly issued approval |
| `credential_directory` | Existing `PrivateFileSecretResolver` directory, separately enrolled by administrator |
| `guardian_socket`, `guardian_uid` | Independent Linux service's exact Unix socket and uid; peer pid must differ |
| `custody` | Closed object: `host`, `port` (443), `token_file`, `ca_file`, `certificate_sha256` (64 hex) |
| `approved_max_cost_cents` | Existing explicitly approved exposure ceiling, 1–10000; admitted plan/profile cap must fit and independent conservative ledger must remain within the profile cap |
| `max_seconds`, `max_polls` | 1–900 seconds, 1–90 polls (route window incl. allocation and image pull); profile runtime must fit |
| `source_revision` | Git source reference plus observed installed package inventory: `40hex+sha256:64hex` |

After package writers are paused, obtain the inventory digest locally without
network/credentials using
`.venv/bin/python -c 'from tessera_control.salad_live_check import source_digest; print(source_digest())'`.
The digest covers sorted names and content hashes of installed `tessera_control`
and `tessera_worker` Python inputs. The driver rechecks it before effects; changing
inputs invalidates the candidate. The git reference is supplied provenance; the
inventory digest is observed. Keep transient receipts outside those package inputs.

#### Independent service observations (not configuration switches)

Actual billed cost remains unknown; there is no assumed billing endpoint. Admission
requires fresh official GPU compatibility and positive high-priority USD/hour rate
plus the independent service's conservative exposure ledger within the reviewed
amount. Published rate/exposure estimates are not invoices, account credit or a
hard spending cap. Unknown rate, stale price, unresolved orphan exposure or unknown
ledger state fence new work and trigger narrowly scoped stop/reconciliation.
The harness consumes authenticated `published_hourly_rate_usd`,
`estimated_exposure_cents`, `rate_unknown`, `budget_fenced` and
`escalation_pending` status after fresh enforcing reservation. It independently
checks accrued estimate plus remaining deadline at that rate against the reviewed
profile cap. It does not require a nonexistent actual-billing endpoint or trust a
config flag as current usage. Exact cancellation and stop remain available.

`status.retained_output` is a read-only verified metadata projection: exact
`binding` (tenant/project/operation/epoch/artifact/kind), `name="output.json"`,
`retained`, `sha256`, `bytes` and `quarantine`. It neither seals/closes writers
nor releases/returns output bytes. The harness validates the exact binding, one
approved file, cap, size and digest shape, and pins the first retained identity.
Retained quarantine metadata triggers cancellation while the group is running;
it does **not** establish stop, writer quiescence or execution/scientific acceptance.

`import` seals custody and closes writers. The harness never polls it before
confirmed stopped status; afterward it verifies the sealed inventory, actual
canonical bytes and unchanged metadata digest/size, then reopens the same durable
route and custody without relaunch. Unknown/reallocated/unsealed state never
passes. Isolated metadata/driver tests are local coverage, not deployed service
or real GPU proof. Full integrated journey and live acceptance still need the
coordinated runtime/service test and actual deployed prerequisites.

After launch, every poll first renews the exact soft execution lease and heartbeats the
guardian (polls are spaced `max_seconds/max_polls`, at most 10s apart); the guardian
deadline is bounded by the lease hard expiry, not the 30s renewal window. Stop
confirmation gets its own 120s window. Scheduling bounds are up to 900 seconds and 90 polls per observation phase (the workload's own runtime argument stays <=300s), one create/start/stop
attempt, no retries and no direct mutation bypass. On any issue the driver attempts
exact cancel/stop/reconcile and retains sanitized inconclusive evidence; unresolved
cleanup remains an exposure incident. Receipt directory must already be private
(0700); exclusive 0600 receipts retain source revision, actual provider identity,
verified output digest/size, stop and same-record reconnect. Raw credentials,
responses, output bytes and exception text never belong in these receipts.
Provider calls retain their two-second transport bounds; authenticated HTTPS
admin exchanges have their service client's ten-second socket timeout. Cleanup
may extend beyond the scheduling deadline for one exact stop/reconciliation;
no timer grants authority or proves shutdown. Interrupted runs retain the durable
started receipt and independent service recovery; they are not acceptance passes.
The standalone main-thread driver additionally applies wall-clock bounds to IPC,
HTTPS and each dispatcher operation; it refuses an existing active alarm rather
than overwriting another component's timer.

Current inspected references are offline examples, `compose.yaml` and
`deploy/remote-agent-platform/docker/compose.yaml`, not verified Salad service
deployments. No live account mapping, approved exposure limit, secure credential
handoff, published GPU digest or independent running service was established.

## Rollback and incident response

1. Keep/restore the live-disabled boundary; halt new admissions and revoke affected
   control authority. Do not equate revocation or service shutdown with termination.
2. Preserve control event/claim/receipt history and external artifact custody. Use
   held recovery for forensics, never to reissue effects. No new operation/group or
   empty journal may be used to retry uncertain work.
3. For a future leak, revoke/rotate the provider key through an independently trusted
   operator path; isolate proxy access and preserve sanitized audit metadata. Do not
   paste keys, raw headers, dumps or provider logs into tickets/prompts.
4. Reconcile every exact group and allocation through authorized stop-only recovery.
   Keep 202, outage, missing group or reallocation unknown until independent termination
   and no-continued-writes evidence passes. Escalate unresolved resource/cost exposure
   to the account owner; never silently retry, scale or purchase credits.
5. Quarantine outputs; invalidate compromised capability/monitor authority. Resume only
   after fresh human approvals, new valid authority, reviewed incident evidence and
   all live prerequisites. Existing claims remain consumed after credential rotation.

No incident step here authorizes an actual Salad call during local implementation.

## Recorded offline task validation (2026-09-23)

On the existing dirty `main` tree, without installing dependencies:

```sh
.venv/bin/pytest -q tests/test_salad_proxy.py
# Final: 107 passed, 2 existing dependency deprecation warnings, 150.32s.

.venv/bin/pytest -q tests/test_kubernetes_execution.py tests/test_capabilities.py \
  tests/test_runtime_capability_broker.py tests/test_capability_collector.py \
  tests/test_execution_monitor.py tests/test_credential_access_leases.py \
  tests/test_artifact_custody.py tests/test_program_evaluations.py tests/test_autonomy.py
# 396 passed, 2 existing dependency deprecation warnings, 287.80s.

node --test desktop/test/remote-execution.test.mjs
# 1 passed; dependency-free control connection test, not a visible GUI run.
node --check desktop/renderer.mjs
# Passed.
```

The earlier combined proxy/legacy-Salad/remote-routing run reported 166 passed and
one new custody-test fixture failure (an unsupported upload `size` field). Removing
that fixture field fixed it; its targeted repair/read-operation run passed all 9
cases, and the final 107-case proxy suite above includes the repaired case and
post-resolution lease/version/revocation/freshness fencing. All legacy-Salad and
remote-routing cases passed in the combined run. An initial 120-second run timed
out without reported failures; subsequent runs used explicit larger bounds.

Scoped Ruff lint and format checks passed for the 13 touched Python files; tracked
diff whitespace checks passed. Both documented CLI commands exited 0, reported
effects 0 and transport disabled, and printed neither references nor key values.
No full-repository integration gate, GUI launch or live provider conformance is
claimed. No real credentials, Salad API calls, projects, cloud resources, paid
effects, dependency installs, commits or pushes occurred.

## Workload input delivery in the campaign driver

For `workload_v1` profiles the live driver (`salad_live_check`) now asks the trusted server
to deliver the route's inputs once, after create has settled to `stopped` and before start
(ADR 0037's delivery, a narrow ADR 0033 exception). Smoke and exact profiles, reconnects and
reruns with prior launch effects never deliver. A delivery failure takes the existing exact
stop path and never reports `passed`. The campaign executor
(`tessera_control.campaign_salad_executor`, see [campaigns](campaigns.md)) is the only caller
that starts this driver for a campaign stage.
