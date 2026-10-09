# 0019: Durable runtime capability broker with blocked real activation

Status: accepted for offline runtime and reconciliation; real isolation unproven.

## Decision

Extend decision 0018 with a control-signed immutable package and a worker-owned
effect journal. Require explicit human-approved protocol/data/code/environment
digests and exact artifact size/content type in addition to the original policy.
Legacy declarations cannot implicitly gain these capabilities.

Keep the existing API's `execution_enabled: false`. Issue only **synthetic**
packages through an explicitly composed control-side signer, never an agent-facing
enable flag. No signing key is generated, loaded from the environment, or delivered
to a worker. The HMAC verifier remains control-side behind fresh workload authority;
this is not an offline bearer capability or asymmetric worker verification scheme.

Use the existing transactional control event/idempotency sink for package issuance,
global claims and historical reconciliation. Use separate worker SQLite schema 1
for atomic receipt/chunk writes, immutable package retention and no-overwrite
custody publication. Do not squeeze unknown termination or progress into schema
49's single final `CapabilityReceipt` model. A replay repeats only evidence transfer,
not effects. Unknown operations retain their slot, including across worker restart.

Reconcile bytes through the existing private project-artifact backend, retaining
the receipt chain in control audit. Object publication and the control transaction
are not a distributed transaction: the worker journal remains the recovery source
until acknowledgement. Never classify/release evidence or report scientific results.

## Consequences

No control-plane subprocess/network installer, database migration, provider
activation, signing-key provisioning or default-CI live effects are introduced.
Local/Kubernetes backend configuration is opt-in but currently **always blocked**;
a policy/fence object is not proof of isolation. Salad and outcome0 stay separate
and provider-disabled. A live implementation requires a separately verified sandbox,
pinned-IP TLS transport, quotas, watchdog and authenticated remote journal channel.

The exact interfaces, supported lock format and remaining custody/catalog limits
are in [the runtime contract](../runtime-capability-broker.md).
