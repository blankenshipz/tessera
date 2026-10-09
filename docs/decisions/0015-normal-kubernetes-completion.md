# 0015: Report verified normal completion through the worker path

Status: accepted for the provider-disabled development profile; fake-client
verification only, not selected-cluster conformance.

## Decision

The normal dispatch supervisor may report `exited, termination_confirmed=true`
when the adapter has validated the immutable Job identity/spec and provider UID,
observed a terminal Job, and observed a nonempty set of its owned Pods all in
terminal phases. Success additionally requires all observed Pods to have
succeeded. Failed Jobs with missing/nonterminal Pods, contradictory observations,
and read failures remain unknown and under supervision. A failed Pod alone does
not establish that its Job has terminated.

This is an attributed worker observation under the existing trusted-supervisor
assumption, not independent attestation. It permits the existing human outcome
and reconciliation commands to consume ordinary worker evidence instead of
requiring a fixture to manufacture a separate terminal report. No schema change
or historical backfill is needed. Old unconfirmed reports remain unconfirmed.

The same slice uses the existing journal-aware upload operation validation for
project placement and manifests. Their earlier CPU-derived check rejected normal
Kubernetes packages even though direct upload accepted them. Manifest digests now
bind the actual issued operation ID; legacy unjournaled CPU identity is retained
only on its existing compatibility path. Epoch/provenance, quarantine, tenant and
retention checks are not relaxed.

Cancellation continues to use decision 0009's stricter fresh Job-and-Pod absence
proof. Normal completion must not override a cancellation request or failed
deletion. Cost settlement, artifact custody, human execution outcome, and
scientific review remain separate commands. The control observation records
termination, not a process exit code or an automatically validated metric.

## Limits

Default tests inject external Kubernetes effects and synthetic output bytes;
they neither execute research code nor establish live/provider support. Production
target correctness, exhaustive Pod visibility, effective isolation and collection
remain selected-target acceptance gates. See [the local API workflow](../local-api-workflow.md).
