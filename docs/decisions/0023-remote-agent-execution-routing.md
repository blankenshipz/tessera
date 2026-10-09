# 0023 — Remote agent execution, desktop control only

Status: accepted for offline routing; live activation blocked.

## Decision and authority

Agent experiment routes select `kubernetes` or `salad_container`.
`salad_inference` is reserved and non-executable. `desktop`, `local_process` and
local CPU aliases are rejected. Existing local Docker/monitor fixtures remain
explicit development conformance tools, not an unavailable-provider fallback.

Keep the authenticated desktop-to-control connection separate from execution
target identity. No control endpoint, port, credential, provider key, arbitrary
command, environment, mount or install instruction is a routing input. Tenant and
project come from authenticated storage. Agents may propose target/resources but
cannot approve, queue, grant or change the operator allowlist.

One immutable versioned routing package binds tenant/project/task, operation,
attempt, base execution-package digest, plan, lease/epoch, target, resources and
image/code/data/model/environment manifests. Human review covers its exact digest.
Current program/source-use reviews, approved task, admitted attempt, grant, input
release, enrolled worker and current lease remain independently required. Changed
targets/manifests require fresh authority, not editing an attempted route.

This initial slice consumes retained admitted **provider-disabled Kubernetes
packages**. Salad is an offline routing/conformance overlay, not a reinterpretation
of Kubernetes network-none authority as permission for real Salad egress. A live
Salad-specific executable package/profile is a prerequisite for later activation.

## Durable effects and evidence

Use the existing transactional control event/idempotency ledger for proposals,
state, one-shot effect intents and receipt snapshots. No schema migration or new
authoritative database is introduced. Route reservation and a *separate* legacy
Kubernetes dispatch decision are mutually exclusive. An exact reviewed Kubernetes
route's start now atomically queues the existing dispatch outbox for its admitted
operation, with a durable `dispatch_id`; it does not call a provider. The enrolled
fake Kubernetes supervisor claims/reconciles that row, not the route-effect adapter.
No duplicate route create/observe/stop effects are accepted for outbox-backed routes.
Worker observations are not mirrored into route receipts. Existing grants,
operations and custody remain
the authority boundary; synthetic receipts are not live operational evidence.

For Salad route effects, the trusted dispatcher commits unknown/intent before
create/start/stop/delete or observe. Kubernetes instead uses the existing worker
dispatch journal's claim and immutable Job intent. Mutations are attempted at
most once. Crashes/timeouts/lost replies mean
read-only reconciliation of the same identity, never resubmission or replacement.
Fresh authority and target preflight gate create/start. Historical exact identity
permits stop/readback after lease revocation; dispatcher credential loss requires
separately authorized recovery, not self-enrollment.

Receipts bind target/route digest, effect, provider/allocation IDs, timestamps,
status, log/checkpoint/artifact references, cost and termination. Null cost means
unknown. Provider identity substitution is rejected; reallocation is sticky unknown
and cannot authorize restart. Quarantine references must match an existing
operation/epoch/plan artifact manifest. References are not bytes, scientific
acceptance or exposure settlement.

## Target comparison

| | Kubernetes | Salad first container profile |
|---|---|---|
| Identity | Explicit cluster/namespace/Job/Pod/PVC | Exact organization/project/group/image/country/resources/allocation |
| Workload | Existing admitted Job and collection contracts | One immutable single-replica group per attempt |
| Isolation | Independent CNI/quota/watchdog/admission witnesses required | Explicit provider trust and broad egress; no equivalent isolation claim |
| Stop | UID-bound deletion and observed termination | 202 is pending, not stopped; exact fresh readback required |
| Storage | Pinned output and custody contracts | Ephemeral node storage; external artifacts/checkpoints required |
| Recovery | No uncertain resubmit | Reallocation is unknown; no automatic retry/adoption |
| Cost | Attributable accounting required | Credits are not a hard cap; unknown exposure remains held |
| This slice | Offline existing-adapter integration | Strict fake adapter; disabled trusted proxy seam |

Salad has no autoscaling, queues, interactive shell, arbitrary argv, agent-managed
key or runtime download/install in this profile. Credentials belong to a trusted
server-side proxy referenced only by operator configuration, never the desktop,
agent, workload or package. Injecting a transport cannot enable live effects:
`live_enabled` is constrained to false until a later reviewed implementation and
separately authorized conformance establish the missing contracts.

## Control lifetime

Start queues reviewed work, not a desktop process. Pause means stop/hold, not
portable process suspension. Cancellation is requested/pending/unknown until
attributed evidence confirms termination. Reconnect reads current state; closing
Electron sends no cancel or lease renewal. Continued remote work still depends on
remote supervisor/control uptime and bounded grants. Held restore cannot dispatch
or replay historical authority. See the [workflow](../remote-agent-execution.md).
