# 0018: Explicit fenced artifact/package capability policy

Status: accepted for the control-plane and worker-contract slice; effect adapters
remain unavailable and disabled.

## Decision

An approved experiment may request extra artifact/package capabilities, but plan
approval alone is insufficient. Bind a separate immutable, human-approved
declaration to the exact task plan revision/digest. Declare exact network
destinations/ports, managers/channels, registries, hash-pinned artifacts and locks,
budgets, sandbox roots, environment/image identity and opaque credential
references. Agents can propose but cannot approve or widen authority.

Use the pinned-lockfile alternative, not arbitrary install shell commands.
Installation is offline from fully enumerated packages. Reject profiles that
cannot enforce isolation, dependency closure, no hooks, byte/time limits and
broker-only egress. Never use host/control-PVC mounts or ambient credentials.

Reserve a single durable operation per action. Recheck lease, epoch, revocation
and approval at use time; cap authority windows at five seconds. Retain immutable
actual-effect receipts, including late or mismatched effects, without treating
worker claims as independently verified evidence. Audit failure rolls back
admission; mismatched effects quarantine and revoke remaining authority.

## Consequences

Existing adapters remain fail-closed (`execution_enabled: false`); this policy is
not a network-on switch. Live support requires a trusted enforcing broker and
separate runtime conformance evidence. Revocation does not claim termination or
undo effects. Unknown intents cannot be retried as fresh work; widening or
renewing policy requires a new human-approved plan revision.

See [the API, audit and worker contract](../agent-capability-policy.md) for exact
limits, migration behavior, credentials and offline verification.
