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
  The entry is read once: the same bytes are hashed and copied to a temp file, which replaces
  the served file only after the digest equals the pin, so the verified bytes are the served
  bytes. An entry is deleted only when the whole file was read and its digest differs; an I/O
  error, a symlinked or non-regular entry, or a size above this pin's `max_bytes` is a miss that
  leaves the entry in place.

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
  provider's retained `fetched` rows are readable by control and platform-authored; today it
  records the pins as `declared_only`. Readability alone is not enough on Salad: the stage entry
  runs in the runtime's container with the runtime's identity and network, so it can upload its own
  `output.json` through the same custody channel. Upgrading the evidence needs the entry separated
  from the runtime or rows the runtime attests with a key the entry cannot reach. Adapter-built Salad stages do not generate fetch rows yet (the contract
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
- Fetch report required on the worker-grant path: the local worker posts its fetch rows before
  it uploads outputs or posts its run receipt, and control accepts only rows covering exactly
  the frozen plan's pins. An exited worker-grant attempt whose stage declares `public_artifact`
  pins and has no report recorded (in the campaign's project) before its run receipt fails
  closed as `failed_permanent` with failure class `public_fetch_unreported`, whatever its exit
  code; its receipt keeps the files `declared_only` and adds `public_fetch.refused` with that
  code and the reason. A report arriving after the receipt does not count, so the outcome is
  stable once the attempt exited. A contradicting report is refused as before (no event; the
  worker stops before outputs or a receipt). Stages without pins are unchanged. Salad has no
  fetch report channel (the report endpoint accepts local-cpu operations only): its attempts
  are judged by their own verdict, the requirement does not apply, and its receipt evidence
  stays runtime-reported or `declared_only`.
- The local worker's fetch ignores proxy environment variables. Its only proxy is the operator's
  explicit `--public-fetch-proxy http://host:port` (`public_fetch_proxy`), used only when that host
  is on the stage's allowlist (pinned plus redirect hosts), else `proxy_not_allowed`
  (`proxy_invalid` for a malformed value). An explicit proxy host must itself be on the stage
  allowlist (there is no operator-registered proxy), so in practice the fetch is direct-only and
  fails closed otherwise; the Salad in-container fetch is always direct (`ProxyHandler({})`).
- Follow-ups from review: fetched files stay under the worker's public root after a successful
  run until the operator clears it; a Salad receipt without retained rows does not say whether
  the fetch succeeded; each input's fetch may use the hosts of every input in the stage.
- Not covered: fetching on other providers (they need a pre-stage hook) and signed or
  authenticated sources.

## Amendment: a provider-side cache

[ADR 0116](0116-provider-fetch-cache.md) adds an optional cache by digest inside the Salad runtime,
used only when the provider environment supplies persistent storage; it replaces the earlier note that
a Salad-side cache was not covered. Every reuse is re-hashed, nothing cached reaches the control plane,
and the receipt records hit or miss.

## Amendment (2026-11-23): adapted Salad stages fetch their pins

Owner decision (2026-11-22): large public inputs the control plane does not need are fetched inside
the stage on the execution provider, Salad first. The stage adapter ([ADR 0076](0076-salad-cpu-stage-adapter-and-cpu-runtime.md))
now generates the fetch rows, replacing the note above that adapter-built Salad stages do not: the
derived contract's `egress.fetches` are exactly the stage's pins (url, sha256, `max_bytes`), its
`egress.redirect_hosts` exactly the pins' redirect hosts, and the inline manifest holds the image entry
plus one `file` item per pin (`dest` `<input>/<name>`, `cache` as the source asks). The runtime
fetches and verifies them before the claim, so the ADR 0040 section 4 exemption above applies
unchanged. The workload's reviewable destinations are therefore exactly the pinned and redirect hosts;
uploads still go only to the registered custody host. The provider refuses an adapted workload whose
fetch rows, redirect hosts or manifest differ from the pins in any way (`public_artifact_contract_mismatch`
first, then the adapted-stage manifest check), and a stage with free-form `egress` stays refused by the
adapter and recorded `egress_bound` by [ADR 0141](0141-heavy-stage-remote-candidate.md). The pins'
`max_bytes` plus the staged input must fit the profile's `disk_mb`. The receipt is unchanged: it already
carries per file the source host, digest, bytes (when reported), the provider and the hosts.

Not changed: a `public_artifact` input carries the licence label `unknown-lineage` (ADR 0113 reads it
as an input of unknown lineage), so every provider that receives such a stage must be listed in the
trust file with no `allowed_license_labels` limit excluding it; whether pinned public data should carry
its own licence declaration is open. Tests: `tests/test_heavy_download_on_provider.py`.

## Amendment (2026-11-23): fetch evidence on the receipt

Control records a provider-side stage's retained `fetched` rows as the same
`execution.public_fetch_recorded` event a local worker produces, after validating each row
against the stage's pins (url, sha256 and the pinned file). The rows are derived from the
stage's retained output, never from a worker claim. Bytes, seconds and cache outcome then
appear on the receipt's `public_fetch` files as `provider_reported`; a file with no validated
row stays `declared_only` with null numbers, so the label is `provider_reported` only when a
row exists.

For a provider other than a local worker the `public_fetch` object also carries
`egress_allowlist`: `hosts` (the sorted pinned and redirect hosts, equal to the entry's
`hosts`), `declared: true`, `redirect_enforced: true`, `network_enforced: false` and a
statement: "The hosts are the approved pinned sources and their redirect hosts. They are
declared for review and the redirect set is enforced by the workload runtime; the provider's
network does not restrict the stage to them." Salad's egress is provider-wide
([ADR 0036](0036-generic-salad-gpu-workloads.md)), so the receipt never claims network enforcement. A stage without pins has no
`public_fetch` object and so no allowlist. [ADR 0134](0134-stage-egress-allowlist-receipt.md)
adds one key to this object, `entries` (the per-host `{host, port, source}` rows of the stage
allowlist), and the sibling keys `egress_verdict` and `refused_transfers` on the `public_fetch`
object; the fields above are unchanged.

Limitation, a follow-up: contacted-host journaling (which redirect hosts were actually
contacted during the fetch) is not recorded. Tests: `tests/test_public_fetch_receipt_allowlist.py`.
