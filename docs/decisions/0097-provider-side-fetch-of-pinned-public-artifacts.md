# ADR 0097: Pinned public artifacts are fetched on the executing provider

Status: accepted

## Context

A research stage often needs large public inputs the control plane has no use for: model
weights, public datasets. The only path was a fetch stage that downloaded the bytes on the
control host, uploaded them as a large output into custody, and then shipped them out again
to the stage that uses them (ADR 0050). The bytes crossed the control plane twice for nothing:
slow, disk-hungry, and an upload of multi-gigabyte public files through the path meant for our
own data.

## Decision

A v2 stage input of kind `public_artifact` names pinned public files: for each, a file name,
an https source URL, the expected sha256 and a byte bound (`max_bytes`, at most 8 GiB), plus
optional `redirect_hosts` (the only other hosts a download may be redirected to, for example
a content network behind the pinned host) and an optional `cache` flag. The spec digest covers
all of it, so a person approves exactly what will be fetched and from where.

The **executing provider** fetches the files inside the stage's own run, verifies the sha256
and size before anything uses them, and fails closed on any mismatch, leaving nothing behind.
The control plane never downloads or stores the bytes. Only a receipt returns: per file the
source host, the digest, the byte count, the provider and the timing, marked `via_control:
false`. Where the provider's own report is not available the receipt says `declared_only`
instead of inventing numbers.

- **Salad.** The in-container runtime already fetches, verifies and size-caps files named in
  the operator-reviewed workload contract before it pulls any staged input. Admission now
  requires the contract's fetch rows and redirect hosts to equal the spec's pins exactly, so a
  reviewed spec can never fetch something else. This is also the one exemption to ADR 0040
  section 4 (a stage with a non-public input declares no fetch hosts): a stage with private
  inputs may carry fetch rows that exactly match its own `public_artifact` pins, because the
  fetch happens in Tessera's runtime before the private input is delivered and never in stage
  code. Any other fetch or redirect host on such a stage stays refused.
- **Local worker (fallback).** The worker supervisor, not the container, downloads into a
  private directory before the container starts, then mounts it read-only; the container keeps
  `--network none`. A fetch error fails the attempt before anything runs.
- **Cache.** With `cache: true` and a worker-configured private cache directory, verified
  bytes are kept by digest so a rerun does not refetch. A cache hit is re-hashed and verified
  on every use; a corrupt entry is discarded and refetched; nothing is cached before it
  verifies. Salad containers are ephemeral, so the cache is a local-worker feature.

### Egress

The invariant is that our data never leaves to unregistered places. Downloading a pinned
public artifact by digest is allowed; the fetch contacts only the pinned hosts and the listed
redirect hosts, over https on port 443, with no credentials, no query strings, no IP literals
and at most three redirects, and uploads nothing. The hosts are recorded on the receipt. A
stage with `public_artifact` inputs declares no `egress` of its own; the narrow allowlist is
derived from the pins. A stage that also reads private data is admitted only through the
provider paths above, where the fetch precedes the delivery of private input.

## Consequences

- No more fetch-then-upload stage for public weights; the second hop through control is gone.
- A pin is a trust decision: the digest is what makes an https download safe to use, so a
  spec author must pin real digests (a pins file such as `weights_pins.json` feeds the spec).
- Salad's own egress remains provider-wide and not enforced by Tessera (ADR 0036); this
  decision does not change that, it only binds what the fetch step may request.
- A stage whose only inputs are `public_artifact` (no snapshot) fails closed at the grant step
  with `public_only_stage_needs_a_snapshot_input`, on every provider, because an execution
  grant binds a snapshot; the supported shape is a stage with a snapshot input plus public
  files (the intended use). The Salad receipt carries `bytes` and `seconds` only when the
  provider's retained `fetched` rows are readable by control; today it records the pins as
  `declared_only`. Adapter-built Salad stages do not generate fetch rows yet (the contract
  must carry them, and a mismatch is refused). A mixed stage on a worker without the
  operator's `--public-root` is refused at run time, not at submit.
- The plan carried to a worker is validated with the same host rules as the spec (public fully
  qualified hosts, https on 443 only, no credentials, query or fragment on a pinned URL, every
  pinned host in the plan's hosts), and the worker applies them again, including before a cache
  hit; a redirect target may carry a signed query but never an internal host. The cache copies,
  never links, so each mounted file keeps one link.
- One host rule serves the spec, the plan and the worker (`canonical_public_host`): one trailing
  dot is stripped, the name lowercased and IDNA-encoded, and it must be at least two ordinary DNS
  labels whose last label starts with a letter. That refuses every IP spelling (dotted quad, hex,
  octal, a single integer, short forms such as `127.1`, IPv6 and IPv4-mapped IPv6) without
  parsing them, and internal names (`localhost`, `*.local`, `*.internal`, `*.lan`,
  `*.localdomain`, `*.home.arpa`, single labels). Not covered: a public name that resolves to a
  private address (DNS rebinding); the fetch's own https request is the only network use.
- Follow-ups from review: local-worker fetch rows are recorded as an event but not yet read into
  the campaign receipt (still `declared_only`); fetched files stay under the worker's public root
  after a successful run until the operator clears it; the receipt does not say whether a fetch
  succeeded; each input's fetch may use the hosts of every input in the stage.
- Not covered: fetching on other providers (they need a pre-stage hook), signed or
  authenticated sources, and a Salad-side cache.
