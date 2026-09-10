# 0012: Pluggable credential store and explicit delivery

Status: accepted design direction; unimplemented.

## Decision

Future credential stores and delivery mechanisms are separate pluggable contracts.
An explicitly authorized local environment source may deliver locally; Kubernetes
Secret materialization requires separate explicit human authorization bound to the
target, namespace, task, lease, and epoch. Raw values remain outside control
records, prompts, skills, and evidence. Skills may plan/request access but cannot
approve delivery or use raw `kubectl` or cloud credentials.

## Rationale and links

This preserves the existing opaque-reference lease boundary while allowing narrow,
auditable delivery implementations later. It adds no secret backend, environment
source, Kubernetes Secret, or cloud integration. See [DESIGN §5.1](../../DESIGN.md#51-credentials-and-data-transfer),
[decision 0007](0007-credential-access-lease-boundary.md), and the
[external-operation journal](../external-operation-journal.md).
