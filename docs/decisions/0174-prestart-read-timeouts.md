# 0174 — Pre-start provider read timeouts and the settle-step conflict

## Status

Proposed. No schema change. Narrows the pre-start read retry added with the guardian's
pre-start window; no budget, authority or custody check is weakened.

## Context

Live runs reached start on Salad and then stopped at $0: the guardian's group/instance read
timed out, the pre-start window (60 s) ran out, and the run was refused as custody unready
although no instance had ever been allocated. A separate run failed in the settle step with a
bare store conflict that carried no closed reason on the receipt.

## Decision

- The per-request transport timeout stays short (2 s) and is separate from the overall
  pre-start read deadline, which now defaults to 300 s (the existing 600 s cap still applies).
  The window applies only while the route is pre-start, unallocated and no custody began, so
  waiting is free.
- Idempotent reads (rate, group, instance) are retried inside their window with jittered
  backoff (50-100% of the backoff, so the window arithmetic still holds). Creates and starts are
  never retried; the start preflight read keeps its own bounded retry.
- An instance resource that does not exist yet (404 on a group that is not running, an empty
  list, or a pending state) is "not yet", not an unavailable read. This was already the
  transport's behaviour and is now pinned by the tests.
- A store conflict in the settle step records a closed `reason` on the receipt's
  `prestart_failure` (`route_revision_changed`, `provider_read_unavailable` or `conflict`; never
  message text). Only `route_revision_changed` (another writer advanced the route between
  evaluation and dispatch) is retried, once, as it is a read; any other conflict, or a second
  one, fails closed as before.

## Consequences

A slow Salad answer no longer ends a run that has cost nothing. A read that never answers
still refuses custody and stops at $0 when the deadline passes.
