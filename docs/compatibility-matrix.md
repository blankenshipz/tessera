# Compatibility matrix

This is the current, evidence-based compatibility record for a source-only
developer alpha. A **fixture pin** is
only a deterministic test input; it is not an approved upstream deployment.
Nothing in this matrix enables dispatch, credential handoff, provider/model
calls, artifact release, result acceptance, or unattended research work.

| Boundary/profile | Exact pin or identity | Available evidence | Unavailable / not enabled |
|---|---|---|---|
| Python development environment | Python `>=3.11`; dependencies resolved by committed `uv.lock` (`uv sync --locked`) | Native locked workflow and Docker Compose/native synthetic fixtures | A supported production Python/OS matrix, remote control-plane deployment, and hosted-service support are not declared |
| Synthetic OpenCode HTTP/SSE fixture | OpenCode-shaped server `1.2.3`; canonical fixture `/doc` SHA-256 `b88fd046a0438e416207539e04ea3fd8b9b4e4f17f48c7580cf507fa835b9053` | Exact-version and canonical-schema pin enforcement; authenticated literal-loopback HTTP; session/message/abort and SSE replay fixtures | This is not an installed OpenCode build or a selected real-server pin; no real OpenCode compatibility result, TLS deployment test, skills/MCP/permission/provider-auth surface, provider/model call, or credentials |
| Opt-in real OpenCode node fixture | Runtime-only absolute executable, exact semantic version, and canonical `/doc` SHA-256 supplied through `TESSERA_OPENCODE_*` | No selected executable or recorded real result. The CLI reports non-opt-in as `skipped`, unusable configured pins as `unavailable`, verified clean probes as `passed`, and all protocol/cleanup errors as `failed`; local HTTP-double tests only validate fixture behavior. | Not compatibility evidence until that exact real executable returns `passed`; no provider/model/MCP/shell/credential call, node enrollment, dispatch, artifact release, result acceptance, or provider-cost/termination claim |
| OpenCode live adapter seam | Caller must supply one exact `OpenCodeServerPin(version, schema_sha256)` and `OpenCodeHarnessInstancePin` (endpoint/server identity/incarnation, build/config digests, workspace scope) | Private immutable instance journal, authority rechecks, fixed routes, redirect/proxy/cookie rejection, bounded events and unresolved abort semantics | Synthetic fixture seam only: no control-plane registration, worker enrollment, node activation, deployment configuration, real-server extension support, or real provider dispatch |
| Development native worker | `development-local-cpu-v1`; Docker image `python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84` | Opt-in loopback/Docker fixture with temporary state, a fixture worker credential, fake-provider receipt, and quarantine-only output | Not a persistent-output or pilot backend; no Linux quota/restart acceptance, real provider accounting, secret backend, artifact release, or result/scientific acceptance |
| Provider protocol | `FakeProvider` / `fake-provider-v1`, injected only by tests and development fixtures | Stable submit lookup, lost-reply reconciliation, receipt attribution and visible cancellation uncertainty | No production provider implementation, cloud endpoint, model call, provider credential, spend settlement, or provider-success claim |
| Minikube Job conformance contract | Explicit `development-minikube-opencode-v1` cluster/profile/namespace/CA/server identity and digest-pinned Job shape | Pure dictionary/callback unit tests for fixed security fields, immutable observation, deterministic create reconciliation, and fresh-absence cancellation | No Minikube call, enrollment, storage quota, OpenCode activation, provider dispatch, credential path, or pilot acceptance |
| Synthetic integrated experiment workflow | Schema v35 temporary state; injected OpenCode-shaped `1.2.3` client and explicit synthetic Kubernetes identity | API-shaped baseline/intervention workflow joins admitted-attempt identity, lost-create replay, cancellation fence, contradictory observation hold, quarantine/placement, measurement, reviewed conclusion, bounded export/reopen, and held recovery | No OpenCode executable/provider/model call, credential, Kubernetes client/command, remote target, desktop disconnect, artifact release, scientific acceptance, or pilot evidence |
| Planned adapter/provisioning/credential direction | Local CPU primary; future explicit-identity Kubernetes target; future scoped operator and authorized delivery adapters | Approved design only; existing pure Job contract remains unit evidence | No `EnvironmentAdapter`/`ExecutionAdapter`, target attachment, bootstrap, provisioning, local-env delivery, Kubernetes Secret materialization, raw credential use, or teardown implementation |

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
