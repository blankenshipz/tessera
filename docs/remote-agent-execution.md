# Remote agent execution routing — offline implementation

**No live provider effects are enabled.** This implements authenticated target
proposals, exact human review, durable dispatch and offline Kubernetes/Salad
composition. It does not install a dispatcher service, run a real agent/model,
create a Salad project, expose desktop ports or spend credits. Default API instances
have an empty target allowlist and no dispatch loop. See [decision 0023](decisions/0023-remote-agent-execution-routing.md).

## “Start a Salad project” becomes a proposal

1. Treat this as a proposal, not authorization to call Salad or provision resources.
   A Tessera project contains research; a Salad project is separately provisioned
   by an authorized operator.
2. Use [program interactions](program-interactions.md) for the objective, variants,
   manifests, data/source use, evaluation protocol, budget and stop conditions.
   Set compute target to `salad_container` or `kubernetes`. Historical `local_cpu`,
   `configured_remote` and `salad_gpu_future` reviews remain readable but do not
   authorize an exact remote route. Program/source approvals must be current
   proposal heads, same study/outcome, and cover all declared data sources.
3. Obtain ordinary task approval, input release, project limits, worker enrollment,
   human execution grant and admitted experiment attempt. The study must explicitly
   link that experiment. Autonomy approval cannot replace these steps.
4. For offline conformance, prepare the existing provider-disabled Kubernetes
   package, then `POST /tasks/{task_id}/remote-executions`:

   ```text
   operation_id, package_digest, attempt_id, plan_digest, lease_id, epoch
   study_id, program_review_id, source_review_id
   selection:
     target: kubernetes | salad_container | salad_inference (reserved)
     target_digest: exact operator target identity
     desktop_role: control_only
     placement: remote_execution_plane
     resources: cpu_millicores, memory_mb, gpu_class, gpu_count, disk_mb, max_seconds
     manifests: image (@sha256), code, data, model, environment (SHA-256 manifests)
     salad: null for Kubernetes; otherwise exact organization/project/group/country,
       replicas=1, external_quarantine custody, external checkpoints,
       autoscaling/queues/interactive_shell/runtime_install/runtime_download=false,
       provider_trust=broad_egress_ephemeral_reallocation_credit_not_hard_cap
   ```

   `RemoteProposal` is the strict schema. `routing_package` includes normalized
   defaults and server-derived tenant/project/task. `proposal_digest` hashes that
   complete versioned package; review it rather than hashing abbreviated JSON.
   Base package image/data must match; an evaluation protocol also pins
   code/data/environment. Operator allowlist plus human route review pin all other
   manifests/resources exactly. Hashes do not prove rights or retained bytes.
5. A trusted server composition supplies exact immutable `TargetPolicy` entries
   and offline adapters; there is no policy/credential write API. A human calls
   `POST /remote-executions/{id}/commands` with `command=approve`,
   `expected_revision` and `proposal_digest`, then separately `command=start`.
   Approval is not preflight; queueing is not provider execution.
6. For Kubernetes, `start` atomically writes one `kubernetes_dispatches` outbox
   identity for the reviewed route and already admitted provider-disabled package.
   It makes no Kubernetes call. The separately started enrolled
   `KubernetesDispatchWorkerService`, configured for the same target digest/profile,
   claims that identity and uses its durable local descriptor and existing fake
   Kubernetes adapter to submit/reconcile exactly once. Replayed commands cannot
   queue another dispatch; the standalone admitted-attempt dispatch endpoint cannot
   dispatch a routed operation. The old route-effect `create` path is fenced for
   outbox-backed Kubernetes routes. The route's receipts remain empty and its queued
   state is **not** a projection of worker results: inspect the admitted operation,
   dispatch, and worker observations for execution status. No supervisor auto-starts
   from an API request. Salad retains its separate synthetic stopped-group route
   effect, with fresh exact readback before a separate start. No arbitrary
   shell/command is executed. A future live Salad package must not inherit
   Kubernetes network-none or resource-enforcement claims.

`tests/test_remote_execution_routing.py` is an executable offline example, not
deployable provider configuration or proof that remote processes actually ran.
This slice does not supply target attachment, a live adapter, independent runtime
witnesses, credentials, deployment, or a route-to-result/artifact custody projection.
The authorized local Docker Desktop K8s fixture cannot safely demonstrate this
synthetic composition as target-level remote deployment; no cluster was operated.

## Control, reconnect and evidence

`GET /tasks/{task_id}/remote-executions` lists at most 32 routes, failing closed
rather than truncating. `GET /remote-executions/{id}` exposes route, authority,
provider status, receipts and go/no-go (see [Reading a route](#reading-a-route)). Desktop task detail has **Inspect /
reconnect remote execution**, exact review/start and pause/cancel controls.
Structured proposal authoring remains API-first. Logs/checkpoints/artifacts are
textual evidence references, not arbitrary URLs, automatic downloads or shell tools.

Pause/cancel records stop intent and requests cancellation of the existing grant
in one transaction. A 202 stop reply is `pending`; missing evidence/timeout is
`unknown`; exact observed termination is `confirmed`. An uncertain mutation is not
retried. Read-only `observe` reconciles the same identity. Delete requires stop
intent and confirmed termination and is itself one-shot. Restart does not reset
any intent, operation, epoch, provider identity, bounded observation count or lease.

### Reading a route

`GET /remote-executions/{id}` has two shapes, and both are read-only: nothing in them
enables, authorizes or renews anything.

* **Any route that is not a campaign Salad attempt** reports `control_surface:
  desktop_control_only` and `go_no_go: no_go_live_activation_blocked`; a Salad route also
  carries `proxy_safety`. Live activation of control's built-in `SaladProxy` stays blocked.
* **A route owned by a campaign Salad attempt** is dispatched by the Salad executor through the
  guardian, not by the built-in proxy, so it reports `control_surface:
  campaign_executor_guardian`, a `live_path` object, and a labelled `builtin_proxy` object. The
  top-level `go_no_go` (`no_go_live_activation_blocked`) and `proxy_safety` stay for existing
  readers such as the desktop renderer: the built-in proxy and desktop-initiated activation remain
  blocked, and the campaign path is reported in `live_path`.

`live_path` fields: `dispatcher` (`salad_executor_guardian`), `campaign_id`, `stage_key`,
`attempt`, `attempt_phase`; `executor` (`registered`, `lifecycle_state`,
`heartbeat_age_seconds`, `heartbeat_max_age_seconds`, `freshness` fresh|stale); `lease` (`state`
none|held|expired, `expires_in_seconds`); `guardian` (`evidence_recorded`,
`live_receipt_recorded`, `final_recorded`, `conformance`); `readiness`; `reasons`;
`authorizes_nothing: true`.

`readiness` is `finished` once the attempt is no longer in an executor-driven phase, `not_ready`
when the store would refuse the executor's worker outright, `degraded` for the remaining reasons
(`executor_heartbeat_stale`, `no_lease`, `lease_expired`), and `ready` when there are none.
`reasons` may contain `executor_retired`, `executor_not_registered`,
`executor_not_ready:<state>` (any other lifecycle state `require_ready_worker` refuses:
`draining`, `disconnected`, `quarantined`, even with a fresh heartbeat),
`executor_credential_revoked|expired|expiry_unknown` (the worker credential, derived by
`workers.derive_credential_status`, the same derivation worker discovery fences on),
`executor_project_mismatch` (the worker is registered to a different project than the campaign),
`executor_heartbeat_stale`, `no_lease` and `lease_expired` (the two lease reasons are not raised
while the attempt still awaits its lease). Heartbeat freshness uses the worker registry's own
`HEARTBEAT_MAX_AGE`. Limitation: the view is a reading of recorded rows; it does not evaluate the
live principal behind a request (the store's `require_ready_worker` also matches the presenting
credential), so it reads the credential bound to the registry row. `conformance` is always `not_attested_by_this_view`: the view reports what is
recorded, and deliberately does not claim the guardian conforms.

`builtin_proxy` is the state of control's own proxy (`state: disabled`, `go_no_go:
no_go_live_activation_blocked`, `control_surface: desktop_control_only`, `proxy_safety`). Campaign
stages do not use it; it is shown so the disabled built-in proxy is not mistaken for, or hidden
by, the campaign path. The `salad_safety` prerequisites are unchanged. There is no MCP tool that
reads a route today.

Desktop close/disconnect sends no stop or heartbeat. Reconnect reads current state;
missing UI connectivity proves nothing about termination. Continued execution
depends on remote control/supervisor uptime and bounded grants, not desktop
liveness. Tests establish this with offline state/effects, not a live deployment
or native graphical run.

Receipts retain provider-neutral attribution plus provider/allocation IDs, effect
and route digest, timestamps/status and typed evidence references. Exceptions are
reduced to unknown, never retained as potentially secret-bearing text. Only
references matching an existing same-project operation/epoch/plan artifact manifest
can claim quarantine custody. Byte transfer, custody release, result checkpoints,
human result acceptance and program scientific go/no-go remain separate workflows.
Routing go/no-go always reports live activation blocked; it is not a scientific
verdict. Unknown cost stays null, not zero or settled exposure.

Route history uses the existing event ledger and control backup. Held restore
cannot mutate/replay it. Unknown/reallocated work retains its original identity;
do not create replacement groups to retry. Dispatcher credential loss needs
separately authorized operator recovery; no observer self-enrollment is added.

## Live prerequisites, not activation switches

* **Kubernetes:** independently deployed CNI/CRI/kernel/quota/watchdog witnesses,
  reviewed pinned image, collector freshness/one-use activation, exact target/PVC
  identity and separately authorized conformance. API readback or the fake Job
  adapter is not enforcement proof. Existing capability runner/collector real-client
  guards are unchanged. Local Docker/monitor conformance is not a remote target.
* **Salad:** API/version, exact provider policy, separate provisioning authority,
  provider-specific immutable executable package admission, trusted credential/proxy
  deployment, read-only discovery/stopped-group readback, allocation and termination
  conformance, independent deadline supervision, external custody, data rights and
  explicit broad-egress/provider trust acceptance, usage/cost attribution and
  unknown exposure handling. Credit balance is not a hard spend cap. Ephemeral
  storage and reallocation are not persistent custody or transparent restart.
* The [server proxy boundary](salad-proxy.md) adds exact executable-profile admission,
  a proxy-only resolver interface and closed fake transport. `SaladProxyAdapter`
  projects its typed disabled result without invoking a live transport;
  `live_enabled=True` is invalid. There is no live SDK/client, production key
  resolver, ambient credential discovery, automatic retry or paid test here.
  Later implementation/configuration **and** separately authorized conformance are
  required, not simply a human plan approval or valid config.

## Offline checks

```sh
.venv/bin/pytest -q tests/test_remote_execution_routing.py
node --test desktop/test/remote-execution.test.mjs
```

These use temporary control state and fake effects only: no Salad, Kubernetes,
Docker, model/provider calls, credentials or workload processes.
