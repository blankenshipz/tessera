# Salad server proxy — GPU milestone and activation runbook

**Default and fake entry points remain provider-disabled.** A separate trusted-server
composition implements real Salad HTTP calls; implementation is not deployment or
live acceptance. No real provider call, GPU run, deployment, build/pull, credential
discovery or paid effect was authorized for this milestone's local validation.
See [decision 0031](decisions/0031-salad-single-container-gpu.md) and the preserved
[original boundary](decisions/0024-salad-server-proxy-boundary.md). Salad is not a
provider-isolation witness or trusted autonomous sandbox.

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
   MixFM review remain separate existing controls.
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
unknown with a closed reason code; raw exception text is discarded. Changed
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
5. Approve the exact public-data task, package, route, account/project/image/GPU,
   capability declaration and exposure ceiling through existing human authority.
   Review startup/runtime/output limits and orphan/unknown usage policy. Account
   credit is not a hard cap; unknown usage must block new work, not exact stop.

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
retention; deletion of the stopped group is separately scoped cleanup, not this
milestone's automatic fallback. Never tear down the independent service merely
because the live-check process exits.

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
| `max_seconds`, `max_polls` | 1–300 seconds, 1–30 polls; profile runtime must fit |
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

Scheduling bounds are 300 seconds, 30 polls per observation phase, one create/start/stop
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
