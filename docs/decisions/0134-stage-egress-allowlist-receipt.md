# ADR 0134: The stage egress allowlist and every reported transfer land on the receipt

Status: accepted

## Context

ADR 0034 phase 1 enforces an egress allowlist for local Docker only; its Salad phase was design
only. Since ADR 0097 a heavy Salad stage fetches pinned public files on the provider, and
admission already requires the reviewed workload contract's fetch rows and redirect hosts to
equal the approved spec's pins exactly, so the launch cannot carry a destination the spec does
not name. What was missing is the same list on the record, and a check of what the provider says
it actually transferred: a reported row for another host or an unpinned file was silently dropped
from the receipt.

## Decision

`stage_egress_allowlist.py` is provider-neutral and pure. The allowlist is derived from the
approved spec only: the hosts of pinned `public_artifact` files (`pinned`), their
`redirect_hosts` (`redirect`), and the stage's registered egress destinations (`registered`,
ADR 0034). Nothing else is allowed.

The `public_fetch` receipt entry (ADR 0097) has one shape. It extends the `egress_allowlist`
object the ADR 0097 amendment (2026-11-23) already writes; every field of that object keeps its
name and meaning, and nothing is removed or renamed, so receipts written before this change stay
valid:

- `egress_allowlist` (unchanged rule: present only for a provider that is not a local
  `worker_grant` provider) is the object `{hosts, declared, redirect_enforced, network_enforced,
  statement, entries}`. `hosts` (the sorted pinned and redirect hosts, equal to the entry's
  `hosts`), `declared: true`, `redirect_enforced: true`, `network_enforced: false` and
  `statement` are exactly as ADR 0097 states. The one added key is `entries`: the sorted
  `{host, port, source}` rows of the stage allowlist above. `entries` can name a `registered`
  destination that `hosts` does not, since `hosts` lists only pins and redirects;
- `egress_verdict`, a sibling key of `egress_allowlist` on the entry: `declared_only` (no file has usable provider-reported evidence: the provider
  reported no transfer, or only rows the receipt could not use), `within_allowlist` (at least one
  file has provider-reported evidence and every reported transfer is to an allowlisted host and
  is a pinned file), or `violation`. Hosts are compared in canonical form (trailing dot stripped,
  lowercased, IDNA-encoded), so one name written two ways is one host;
- `refused_transfers`, a sibling key present only on a violation: `{host, reason}` with
  `not_allowlisted` or `not_pinned`. Only hosts are recorded, never URLs.

`egress_verdict` and `refused_transfers` are not gated like the `egress_allowlist` object: they
appear on every `public_fetch` entry, local worker and unknown provider included, because the
check of reported transfers applies wherever rows are reported. The allowlist they are checked
against is computed the same way for every provider; only its display (the `egress_allowlist`
object with its declared-not-enforced statement) is limited to rented providers. A local worker's
recorded rows (ADR 0097) are only those matching a pin, so its verdict is `within_allowlist` or
`declared_only`.

Per-file transfers stay in `files` (source host, digest, bytes, seconds, `provider_reported` or
`declared_only`). Enforcement at launch stays with the provider adapter (Salad: the contract
equality of ADR 0097); the runner core holds no provider logic.

## Consequences

The change is additive: the entry of a stage with `public_artifact` inputs gains
`egress_verdict` (and `refused_transfers` on a violation), and a rented provider's
`egress_allowlist` object gains `entries`, so receipt digests of such stages change; stages
without them are unchanged, and a reader of the ADR 0097 object finds every field it had. A `violation` is recorded on the
receipt and fails the attempt (see the amendment), and a `declared_only` stage
is not evidence that nothing else was transferred. Salad keeps provider-level broad egress (ADR
0034 section 5); this record states what was approved and what was reported, not what the
network carried.

## Amendment: a violation fails the attempt

The runner's single observation point checks the provider's reported transfers against the
allowlist, whatever the provider's own verdict. A transfer to a host outside it, or of an unpinned
file, turns a succeeded observation into `failed_permanent` with failure class `egress_violation`
(host names only in the detail). The attempt is not recorded as a usable result, its evidence is
not taken, and the receipt still records the refused transfer. A provider failure is left as it
is. The runner holds no provider logic: a provider that reports no transfers is not checked
(`declared_only`), and today only the local worker's recorded rows are reachable.

A stage with no `public_artifact` inputs has no pinned hosts, so every transfer its provider
reports is refused (registered destinations aside, a file there is still not pinned). Its receipt
then gains a `public_fetch` entry with no `files`, `egress_verdict: violation` and the
`refused_transfers`, so the violation that failed the attempt is on the record. With nothing
reported the entry stays absent and the receipt keeps its shape.

## Follow-up note: Kubernetes phase

ADR 0034 phase 2 (Kubernetes Agent Sandbox) is separable from this change: it needs a cluster
adapter and its own enforcement point. It is not built here. When it is, it should derive its
policy from `stage_allowlist` and report transfers into the same receipt fields.
