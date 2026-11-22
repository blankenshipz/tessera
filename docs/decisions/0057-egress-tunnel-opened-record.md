# 0057 — An egress tunnel is journaled when admitted, not only when it closes

## Status

Implemented (no schema change). Narrows [ADR 0034](0034-governed-sandbox-egress-and-dataset-staging.md)
§3 (the proxy journal) and keeps the invariant of
[ADR 0047](0047-agent-routed-data-destinations.md): every transfer is recorded on the receipt.

## Context

The allowlist proxy appended one journal record per allowed `CONNECT` tunnel, when the
tunnel closed. A tunnel interrupted by a process kill or crash never reached that point, so
bytes could have flowed with no record, and the receipt silently omitted the transfer.

## Decision

1. **Opened record.** After the destination passes the allowlist, the cap check and the
   public-address check and the upstream connection is made, and before the `200` reply or
   any byte is relayed, the proxy appends an `allow` record with reason `opened`, zero bytes
   and a random `tunnel` id.
2. **Closed record.** At close it appends the existing record (reason `closed`, `idle` or
   `cap`, byte counts, duration) carrying the same `tunnel` id.
3. **Summary.** `summarize_journal_bytes` counts one connection per tunnel. A tunnel with an
   `opened` record and no closing record is counted as an allowed connection with bytes
   unknown (reported as zero) and flagged: the summary gains `connections_unclosed` and each
   destination an `unclosed` count (both default 0). A closing record whose opened record is
   missing, and legacy records without a `tunnel` id, still count once.
4. **Receipt.** Control validates `connections_unclosed` as the sum over destinations, the
   same way as the other totals, and the allowlist check covers unclosed tunnels. The campaign
   receipt carries the summary, so an interrupted tunnel is recorded, never missing. Byte
   totals and the `cap_exceeded` flag only reflect observed bytes; a nonzero
   `connections_unclosed` tells a reader they are a lower bound.

Unchanged: only exact allowlisted destinations are tunnelled, any non-public resolved
address is refused, and the public internet and unregistered destinations stay forbidden.
Denials are still one record each.

## Consequences

Older journals and older readers keep working: records without `tunnel` are counted as
before, and the new summary fields default to 0. A proxy killed mid-tunnel leaves the
`opened` record on disk (flushed per record) or in the pod log.
