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
It also fences every credential-access lease bound to that worker: handle issuance,
local fake-backend resolution, and access-lease renewal recheck the retired
credential/registry state and fail closed.

The registry is metadata admission only. `capability_probe` is a bounded,
read-only synthetic informational probe: it can compare supplied versions,
inspect private roots, canonicalize a declared storage description, and observe
an optional pinned OpenCode version. A passing report explicitly has no
authority, does not verify a cryptographic build or capability attestation, and
does not enable dispatch. Only schema v21's metadata-only credential-access lease
and private in-memory test handle seam are implemented. Production secret backends,
real provider credential integration, provider dispatch, result acceptance, and
restore fencing remain unimplemented.

The Stage 1 native development runner does not enroll itself: it sends this
heartbeat before requesting its one supplied grant lease, then sends distinct
periodic heartbeats on its renewal cadence. Each heartbeat key is durably
recorded before sending; a lost reply replays that key, while a completed beat
gets a new key. A missing, stale, drained, quarantined, or retired registration
therefore blocks it before Docker start. Its credential is read only from a
private file and is never accepted on its command line.
