# Worker lifecycle

Schema v14 records an owner-approved registry entry for one existing, scoped
worker credential. Registration records the project, approved build identifier
and digest, trust class, region, lifecycle state, and server-recorded heartbeat
metadata. It does **not** verify a cryptographic build attestation or prove any
worker capability.

Only the local bootstrap owner can register, drain, quarantine, or retire a
worker. Worker credentials can send an empty typed heartbeat and read their own
registry status. Every lifecycle command, heartbeat, event, and idempotency
record commits atomically. Reusing a key with different content conflicts.

`ready` workers with a heartbeat no older than 60 seconds may acquire or renew
leases. `draining`, `quarantined`, `retired`, unregistered, and stale workers
cannot. Existing observations remain attributable while their current worker
credential remains valid; draining and quarantine do not erase history.
Retirement revokes the registered workload credential in the same transaction,
but preserves grants, leases, observations, and exposure for reconciliation.

The registry is metadata admission only. `capability_probe` is a bounded,
read-only synthetic informational probe: it can compare supplied versions,
inspect private roots, canonicalize a declared storage description, and observe
an optional pinned OpenCode version. A passing report explicitly has no
authority, does not verify a cryptographic build or capability attestation, and
does not enable dispatch. Credential brokering, provider dispatch, result
acceptance, and restore fencing remain unimplemented.
