# 0193 — Only an assigned machine latches the Salad allocation deadline

## Status

Accepted. Narrows the allocation deadline of the Salad guardian (ADR 0191 builds on it). No schema change
and no migration.

## Context

A started Salad CPU group stayed `unknown` for about 55 minutes: every instance-list read hit the adapter's
2 second deadline, the group read `running` at group level, and no machine was ever assigned. The guardian
latched "an instance was seen" on that group-level `running` and on a creating instance, so the allocation
deadline never fired and no fallback ran.

## Decision

1. **A machine counts as allocated only when the provider assigned one.** The guardian's instance count is
   the number of listed instances that carry a `machine_id`. A group-level `running`, an instance still
   allocating or creating without a machine, and counts with no instance list (which cannot name a machine)
   latch nothing, and a group-level `running` with a count of zero does not set the guardian's `first_running`. A count that is not said (None) still sets it, so the running-seconds cap stays fail closed for an adapter that cannot say; such a read never fires the allocation deadline. A read that does not say (no instance list
   and incomplete counts) still never fires the deadline; an instance list that was read and is empty is
   positive evidence of no machine, even when the group omits its counts.
2. **Assigned machine, not running evidence.** A listed instance with an assigned `machine_id` counts as
   placed in any state, including `allocating` and `downloading`. A slow but real image pull must not be
   killed by the allocation deadline. This keeps the existing recorded decision; it adds no running-evidence
   requirement. A candidate node that is assigned and then loops (assign, fail, reassign) keeps the latch and
   is bounded by the route deadline, not the allocation deadline.
3. **Failed reads past the deadline expire the wait.** With no machine ever seen, the clock started and no
   custody begun, repeated unknown or failed reads (a 2 second `read_instance` timeout included) past the
   allocation deadline stop the group as `allocation_timeout`, which allows the country fallback of ADR 0191.
   The 2 second read deadline is unchanged: the guardian's retry chain is sized on it.
4. **Failure reasons are recorded.** An unknown `ProviderReadback` carries the closed `proxy_reason`,
   `proxy_detail`, `proxy_field` and `proxy_request_kind` of the failed read (never a value, body, URL or
   path). The model refuses these diagnostics on any readback that is not unknown, and the server omits them
   on a reallocation.
