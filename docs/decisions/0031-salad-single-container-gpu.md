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

## Addendum: the project group list proves absence only when complete

The public API reference (`list_container_groups`, `GET
/organizations/{organization_name}/projects/{project_name}/containers`, schema
`https://docs.salad.com/api-specs/salad-cloud.yaml` 0.9.0-alpha.17) defines no query
parameters for this read and no paging fields in its response: the body is
`ContainerGroupCollection`, `{"items": [ContainerGroup, ...]}` with `items` required,
`minItems: 0` and `maxItems: 100`. Collections that do page (for example inference
endpoints and queue jobs) take `page` (int32, minimum 1) and `page_size` (int32,
1..100) and return `page`, `page_size` and `total_size`; the group list has none of
them, so there is no documented way to read past 100 groups.

The transport therefore makes this read in one place (`_list_groups`), used by the
start/create preflight and by the guardian's lost-create absence read. A reached,
well-formed list (HTTP 200, an `items` list of objects with string names) proves the
project exists. It is complete, and can prove a name absent, only with fewer than 100
items and no pagination-looking key. A list of 100 or more items is not proof (it may
be truncated):

- the preflight then reads the exact reserved name: a fully read ProblemDetails 404
  is absence (the list has just proven the project exists), a 200 is the group
  (checked like any exact read), and anything else stays a failure, never absence;
- the guardian's automatic lost-create path is unchanged: it still needs a complete
  list (`list_read: "absent"`); 100+ items stay `absent_partial` (operator path only).

Each group object runs to kilobytes, so a project with a few dozen stopped groups can
outgrow the transport's 64 KiB response cap and fail the list read as
`malformed_response` before 100 groups (an estimate from the schema's fields; the live
body size was not measured). The list read alone now takes a bounded
4 MiB body (room for 100 groups of about 40 KiB each); every other read keeps 64 KiB.
Failures stay closed codes (`malformed_response`, `provider_unavailable`,
`identity_mismatch`) with `request_kind` `list` or `read_group`; requests stay
read-only on the fixed allowlisted paths, with no query string. Deleting Tessera's own
stopped groups (retention) is a separate change.
