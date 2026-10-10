# Compatibility matrix

This is the current, evidence-based compatibility record for a bounded local
developer alpha. A **fixture pin** is
only a deterministic test input; it is not an approved upstream deployment.
Nothing in this matrix enables dispatch, credential handoff, provider/model
calls, artifact release, result acceptance, or unattended research work.

Current control state uses schema **50**. Rows naming schema 47 preserve the
historical component/fixture checkpoint, not the current installation schema.
See [installation](installation.md) for source/wheel and Node runtime requirements;
artifact-bound local bench/client receipts remain distinct from production/off-host claims.

| Boundary/profile | Exact pin or identity | Available evidence | Unavailable / not enabled |
|---|---|---|---|
| Python development environment | Python `>=3.11`; dependencies resolved by committed `uv.lock` (`uv sync --locked`) | Native locked workflow and Docker Compose/native synthetic fixtures | A supported production Python/OS matrix, remote control-plane deployment, and hosted-service support are not declared |
| Control-plane SQLite runtime | Built-in deterministic + innocuous `json_extract`; lab/dev/remote base `python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce` | Actual Linux amd64 base probe: Python 3.11.17 / SQLite 3.46.1, unique JSON expression index and defensive schema read with `trusted_schema=OFF`; fail-closed prerequisite before state writes | Other index architectures untested; base probe is not full image/application/deployed recovery acceptance. CPU worker and stdlib guard deliberately retain their separate Bookworm fixture pin |
| Ordinary installed Kubernetes worker and clients (2026-10-04) | Owned Docker Desktop target; installed control/worker image generation 4, schema 50; separate installed CLI/MCP component provenance, pinned collector/PVC/journals; Python MCP SDK 1.26.0 / TypeScript SDK 1.32.0 | Automatic baseline/intervention custody (385/36 bytes, 2870/37 bytes), original Job UID on restart, same late 29-byte quarantine including newline (historical 28-byte summary corrected), terminal ACK/human accounting; installed normal tunnel authenticated reconnect; actual two-SDK MCP plus new native plan/admit/dispatch-to-ordinary-new-Job, automatic timely 385/36 bytes, explicit quarantine review/separate release/save/export/reconnect without redispatch; [exact runbook](ordinary-worker-collection.md) | Laptop-local route, not permanent ingress; actual human OS selection unverified after timeout (not denied); graphical model-host/off-host acceptance untested; science null. Earlier missing-UID proof and Job-list 403 histories preserved. Final desktop 192 tests and separate guard checks are not a 196-test total |
| Legacy synthetic OpenCode HTTP/SSE fixture | Explicit adapter profile `legacy-synthetic-v1`; OpenCode-shaped server `1.2.3`; canonical fixture `/doc` SHA-256 `b88fd046a0438e416207539e04ea3fd8b9b4e4f17f48c7580cf507fa835b9053` | Exact-version and canonical-schema pin enforcement; authenticated literal-loopback HTTP; synthetic numeric SSE replay fixtures | This is not an installed OpenCode build or a selected real-server pin; numeric SSE replay is a legacy fixture behavior, not an upstream claim |
| Source-inspected OpenCode context contract | Release `v1.18.30`, source commit `3104c1428ec91f809e5ab86631300de41eb6952e`, adapter profile `opencode-v1.18.30-context-only`; runtime `/doc` API schema version/digest remain separately required | Unit fixtures enforce documented Basic/no-auth selection, `noReply` context append/readback, boolean abort, and opaque optional SSE IDs with Tessera-owned sequencing | No executable installed or run; no selected `/doc` digest, live server result, provider/model/tool/MCP/credential activity, dispatch, or provider termination claim |
| Opt-in real OpenCode node fixture | Runtime-only absolute executable, exact semantic version, and canonical `/doc` SHA-256 supplied through `TESSERA_OPENCODE_*` | No selected executable or recorded real result. The CLI reports non-opt-in as `skipped`, unusable configured pins as `unavailable`, verified clean probes as `passed`, and all protocol/cleanup errors as `failed`; local HTTP-double tests only validate fixture behavior. | Not compatibility evidence until that exact real executable returns `passed`; no provider/model/MCP/shell/credential call, node enrollment, dispatch, artifact release, result acceptance, or provider-cost/termination claim |
| OpenCode live adapter seam | Caller must supply one exact `OpenCodeServerPin(version, schema_sha256)` and `OpenCodeHarnessInstancePin` (endpoint/server identity/incarnation, build/config digests, workspace scope) | Private immutable instance journal, authority rechecks, fixed routes, redirect/proxy/cookie rejection, bounded events and unresolved abort semantics | Synthetic fixture seam only: no control-plane registration, worker enrollment, node activation, deployment configuration, real-server extension support, or real provider dispatch |
| Astra Explain-status mediator | Schema v42 feature; historical schema v47 checkpoint, scoped agent credential, one immutable experiment design revision, and `read_experiment_status` only | Deterministic provider-disabled control/agent path records one tool intent, exact bounded args/result digests, observed time, and an attributed answer with deterministic model/harness provenance; explicit restart resumes without another facade invocation after a lost result acknowledgement | Not an OpenCode adapter activation, harness session, provider/model call, approval, grant, dispatch, experiment mutation, result acceptance, or compatibility result |
| Development native worker | `development-local-cpu-v1`; Docker image `python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84` | Opt-in loopback/Docker fixture with temporary state, a fixture worker credential, fake-provider receipt, and quarantine-only output | Not a persistent-output or pilot backend; no Linux quota/restart acceptance, real provider accounting, secret backend, artifact release, or result/scientific acceptance |
| Provider protocol | `FakeProvider` / `fake-provider-v1`, injected only by tests and development fixtures | Stable submit lookup, lost-reply reconciliation, receipt attribution and visible cancellation uncertainty | No production provider implementation, cloud endpoint, model call, provider credential, spend settlement, or provider-success claim |
| Provider activation gate | Pure default policy contract; no selected provider adapter, live fixture record, or credential broker | Default tests require `provider_enabled: false`, `disabled` status, unknown usage/billing, fail-closed credential lifecycle, and no API-key/billing fallback | No provider-enabled compatibility claim, live/provider call, credential delivery, usage receipt, bounded-spend proof, or cancellation proof; see [provider activation and collaboration gates](provider-activation-and-collaboration.md) |
| Opt-in two-variant compute pilot | Existing admitted `kubernetes-job-v1` package/worker; one explicit remote target, namespace ServiceAccount, pinned image and preallocated cost contract | Offline fake effects exercise both variants, authority/replay fences, lost replies, cancellation/unknown and output bounds; [pilot invocation and exact contract](comparison-pilot.md) | Live not run; no selected-target compatibility/cost/isolation proof, LLM activation, credential broker, input/PVC-byte collection, human review or scientific acceptance |
| Organization collaboration gate | Pure prospective-role contract; local bootstrap owner remains the only human authority | Default tests require researcher/approver/reviewer/operator action separation | No organization enrollment/invitation, shared writer, role assignment, multi-user audit/recovery fixture, or shared-organization claim; see [provider activation and collaboration gates](provider-activation-and-collaboration.md) |
| Kubernetes execution fake adapter | Schema v47 state with v35 admitted `kubernetes-opencode` package semantics; `kubernetes-job-v1` / `development-minikube-opencode-v1`; explicit cluster/namespace/PVC and network-policy evidence identity | Fake-client control-package → durable local target/Job intent → canonical Job, lost-create lookup, cancellation, owned-Pod/status reconciliation, OpenCode adjacent-session association, collection handoff, and lifecycle heartbeat/checkpoint/reconnect tests | No Kubernetes client/call, target attachment, enrollment, credential/Secret path, OpenCode activation/provider/model call, storage or network enforcement claim, byte collection, artifact release, or pilot acceptance |
| Synthetic integrated experiment workflow | Schema v47 temporary state; provider-disabled Astra proposal/status records, injected OpenCode-shaped `1.2.3` client, explicit synthetic Kubernetes identity, and held recovery | API-shaped baseline/intervention workflow joins human proposal registration and approval, one mediated status read, admitted-attempt identity, lifecycle checkpoint/reconnect metadata, lost-create replay, cancellation fence, contradictory observation hold, quarantine/placement, measurement, reviewed conclusion, bounded export/reopen, and held recovery | No OpenCode executable/provider/model call, provider credential, Kubernetes client/command, remote target, desktop disconnect, operational failover, artifact release, scientific acceptance, or pilot evidence |
| Synthetic remote reconnect simulation | Schema v47 temporary state; loopback control service and separate fixture worker | Opt-in synthetic heartbeat, checkpoint, control-outage/unknown observation, reconnect, and cancellation-fence evidence reusing one operation identity | No remote target, laptop-independent execution, provider/model call, external job termination, operational failover, or scientific evidence |
| Astra operator Kubernetes setup and conformance | Explicit human-supplied Docker Desktop context, pinned image, retained profile; separate opt-in probe identity | Operator fixture can validate/apply allowlisted Namespace, deny-all policy, and PVC, and the standalone probe records declared-versus-observed storage/network outcomes | No target attachment, worker enrollment, workload/provider execution, hard-quota support claim from object presence, automatic cleanup, or supported Kubernetes runtime |
| Opt-in remote Kubernetes OAuth reconnect fixtures | Existing remote runner/deployment, explicit kubeconfig/context/namespace, owner-bound opaque runtime reference, exact image/protocol `v43`, and separate human confirmation | The v1/v2 scripts define bounded preflight/journal/reconnect contracts and are default-disabled, outside pytest/CI; no live result is recorded in this repository | No enrolled runner, credential bridge, cluster target, or provider confirmation is present here; no supported remote execution, provider use, billing, termination, or scientific-validity claim |
| Planned live adapter/provisioning/credential direction | Local CPU primary; fake-client Kubernetes adapter has explicit target evidence only; future scoped operator and authorized delivery adapters | The versioned fake adapter is not live conformance; existing pure Job contract and fake integration remain synthetic evidence | No live target attachment, bootstrap, provisioning, local-env delivery, Kubernetes Secret materialization, raw credential use, byte collection, or teardown implementation |

## Combined synthetic harness evidence

Run the combined check only when Docker access and the pinned image are already
available locally:

```sh
TESSERA_RUN_SYNTHETIC_COMBINED_E2E=1 \
  .venv/bin/python scripts/check_combined_synthetic_e2e.py
```

The runner first executes the OpenCode in-process HTTP/SSE and harness tests,
then the existing native development-worker Docker fixture. It creates only
temporary state, local fixture bearer values, an in-process OpenCode-shaped
server, and a deterministic fake provider. It reads no provider credentials and
makes no provider/model/network calls (the Docker fixture itself uses a
networkless container). It is deliberately sequential: today's OpenCode seam
is not wired to the development worker or control-plane dispatch, so a combined
pass proves the two bounded seams coexist in one local environment, not an
OpenCode-to-provider workflow.

The runner is opt-in because it accesses the local Docker daemon and runs a
container. It does not pull images, modify global Docker/OpenCode configuration,
or clean up anything except containers and temporary files owned by its existing
fixtures.

## Security and activation gates

Node setup is infrastructure administration: an operator installs a pinned
supervisor/harness/adapter, verifies its build and isolation posture, and enrolls
a scoped workload identity. It must not copy a human OpenCode login or approve a
research plan. Research control separately creates the plan, human decision,
grant, lease, input release, and any credential-access lease. An OpenCode prompt,
session, tool event, MCP response, or abort acknowledgement never widens that
authority or proves provider termination.

Do not activate a real OpenCode/provider profile until all of the following have
accepted evidence for the exact selected versions and target node:

1. Record the exact OpenCode release/build digest and canonical `/doc` digest in
   an approved execution profile; reject version or schema drift.
2. Test authenticated TLS endpoint trust (including redirects, proxies, and
   certificate handling), event attribution/replay, expiry/revocation, and abort
   uncertainty against that real server with no sensitive data first.
3. Enroll the node with cryptographically verified supervisor/harness builds,
   capability/isolation evidence, scoped workload identity, recovery policy, and
   a tested drain/quarantine/retirement path.
4. Implement and test a provider-specific adapter with stable operation lookup,
   credential brokering that never records raw values, explicit cost/termination
   reconciliation, and no billing-mode fallback.
5. Complete the applicable storage, artifact-release, result-acceptance, and
   scientific-method gates in DESIGN.md. A synthetic receipt or harness session
   is not substitute evidence.

Until then, the only supported outcome is synthetic component/orchestration
evidence with provider success, costs, artifact release, and scientific validity
explicitly unestablished.

Kubernetes preparation must not use an ambient current context. A future target
must bind kubeconfig/context/server/CA and observed cluster/namespace identities.
Bootstrap, target attachment, control-plane provisioning, and destructive cleanup
are independent human-authorized workflows; automatic teardown is not enabled.
