# Salad server proxy — offline boundary and activation runbook

**Live transport stays disabled. No provider request, real credential resolution,
project creation, resource, paid effect or dependency installation is part of this
implementation or its validation.** This is not a provider isolation witness and
does not make Salad a trusted autonomous sandbox. See [decision 0024](decisions/0024-salad-server-proxy-boundary.md).

## Deployment topology and trust

```text
desktop / agent -- authenticated control requests --> control API + authority DB
                                                        |
                                             enrolled remote dispatcher
                                                        |
                                          trusted server proxy (disabled)
                                              |                 |
                                  secret resolver boundary   fixed Salad API
                                  (fake implementation)     (NO transport)

workload -- bounded output/checkpoints --> external quarantine custody
independent target monitor -- authenticated evidence --> control receiver
```

The last two lines describe required deployment boundaries, not conformance achieved
here. The proxy must not share a process, filesystem, credentials or administrative
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
flag, live CLI or network client hidden behind a test flag.

## Secret custody

The [protected enrollment helper](protected-secret-enrollment.md) now implements
same-host native macOS Keychain custody, independently of this proxy. It is **not
wired into live proxy activation** and does not provide remote Kubernetes custody
or service identity. The fake resolver/transport below remain unchanged; native
enrollment is not completion of the activation checklist.

* `ProxyPolicy.management` is server-only reference/version/purpose metadata and
  is excluded from policy serialization. Do not put it into route packages, plans,
  prompts, client settings, receipts or artifact manifests. Actual key values have
  no config/request/env field and no environment or file discovery path.
* Only `credential-ref:<opaque-name>` is accepted. Env/prompt/request locators,
  raw values and account-key overrides are invalid. The management purpose is
  `salad_management`; AI Gateway keys must never use this resolver or be substituted
  for management keys. Gateway functionality itself is forbidden here.
* `SecretResolver.metadata` must return fresh server-observed version, revocation,
  purpose and expiry. Resolution must **atomically** recheck those conditions and
  return an exclusively owned mutable buffer. Current fake checks allow five-second
  metadata age and require expiry beyond the two-second request budget. Neither
  metadata nor value resolution occurs before admitted one-use intent, and neither
  occurs at all on the live-disabled entry point.
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
These are normalized **offline** operations, not a claimed validated Salad HTTP
wire implementation. Read operations reconcile an admitted group; fake preflight
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
4. Current approved capability declaration for the same tenant/project/task/image;
   actions, credential refs and runtime network capabilities must be empty in this
   supported slice. No capability expansion, downloads, installs or broker activation
   is inferred from the declaration. The provider-egress acknowledgement is **not**
   network isolation; tasks needing restrictive egress cannot use this as a witness.
5. Budget/output caps within the parent plan, unknown-exposure policy acknowledging
   credit is not a hard cap, fresh exact five-second target preflight and exact
   stopped-group identity before start. Monitor/profile/destination hashes are
   reviewed requirements, **not evidence those systems exist or enforce limits**.
6. Retained package, operation, attempt, lease, epoch, manifests, route digest and
   one-use effect nonce. A `salad_proxy.claim` event commits before fake resolution/
   effect. Claims survive process death and restart; a later read may reconcile but
   never authorize another create/start. A new target cannot reuse an operation.

Legacy routes without the executable profile remain readable and usable by the
older offline adapter, but cannot pass the new proxy admission. Live dispatch is
blocked regardless of whether any of the above offline checks pass.

## Responses, evidence, cancellation and custody

One bounded fake call, zero retries. Unknown create reconciles the same reserved
name/identity; never creates a duplicate. The simulator uses the operation ID as
its provider ID; a live implementation must persist actual provider-assigned ID
discovery and prove lost-create reconciliation before activation.

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
No automatic live collector or external destination transport is implemented.

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

## Live enablement checklist — all currently unresolved

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

This checklist is documentation, not a set of booleans that enables transport. A
reviewed implementation change is required even after evidence exists.

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

No incident step here authorizes an actual Salad call during this offline task.

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
