# 0031 — Explicit trusted-server single-container Salad GPU execution

## Status

Accepted implementation direction; local code and isolated tests do not establish
deployment, GPU execution or live provider conformance.

## Decision

Implement the first GPU milestone through the existing single-container route,
server-proxy and one-shot dispatcher contracts. Preserve decision 0024's disabled
default/fake APIs; add a separate administrator-composed trusted server rather than
an enable flag or injectable fake transport. AI Gateway, pools, queues, autoscaling,
Kelpie and automatic interruption resumption are outside this milestone.

Execution requires the exact approved public-data immutable linux/amd64 package,
human grant/reviews, capability declaration, tenant/project identity, lease/epoch,
current known financial exposure and exact target admission. Account credit is
not a spending cap. A driver option, workload claim, signed monitor statement or
provider status cannot replace authority or independent enforcement.

The external guardian/custody service must independently survive client/control/
proxy failure, authenticate workload identity, retain bounded quarantined bytes,
prevent operation-wide repeated GPU execution, revoke writers and reconcile the
exact provider group. Unknown allocation, provider response, usage or continued
writes stays uncertain and blocks new work. Stop-only authority is preserved;
202, missing group and outage do not establish termination.

## Acceptance and limitations

The [runbook](../salad-proxy.md) separates one-time setup from everyday admitted
execution, visible retained output, confirmed stop and same-record reconnect.
The [GPU recipe](../../workloads/salad_gpu_smoke/README.md) is deliberately unbuilt
until image/registry authorization exists. Default tests use synthetic effects;
the live driver is separate, zero-network by default and has no credential
discovery. Its concrete secure-file CLI/session binding is implemented, while
independent published-rate/conservative exposure enforcement remains distinct
from actual billed cost (unknown, not a hard cap). Authenticated pre-stop read-only
output metadata triggers exact cancellation without sealing early; raw retained
bytes are verified only after confirmed stop. Unknown rate/unresolved exposure
blocks new work. Coordinated full-journey and live/deployed proof remain separate.

Live acceptance requires an approved exact account/project/image/GPU mapping,
secure credential references, independent deployed guardian/custody and a concrete
financial exposure bound. This decision grants no deployment, image build/pull,
provider call, credit purchase or paid workload authorization. Retained output
stays quarantined; CUDA workload assertions are not scientific or hardware
attestation. Existing source-use, evaluation and security safeguards remain intact.
