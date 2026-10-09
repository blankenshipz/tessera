# 0016 — Opt-in comparison compute pilot through ordinary dispatch

Status: experimental implementation; offline evidence only, live not run.

DESIGN §6 requires immutable admitted execution, bounded external effects and
truthful unknown outcomes. The existing two-variant workflow and Kubernetes
adapter provide this authority path; the standalone OAuth fixture does not.

Add a separate opt-in client harness that admits two already approved/granted
constant probes and invokes the existing worker/adapter. Kubernetes is the one
selected **compute provider**, not an enabled model provider. Do not change the
provider-disabled package or activate a broker by labelling a synthetic receipt
live. Model execution remains an implementation gap.

The new HTTPS callback transport uses explicit existing runtime references,
pinned target identity, namespace ServiceAccount and tenant/project binding.
It adds conservative submit/cancellation uncertainty fences to the existing
adapter, not execution authority. A supplemental journal stores only forensic
request/response digests and identity; no schema migration or grant bypass.

The narrow collector turns actual owned-Pod stdout into the declared quarantined
`result.txt` artifact. It does not claim PVC byte collection or input delivery.
This is acceptable only for the exact two constant probes, not arbitrary research
plans. Human outcomes and review remain pending and provider accounting unknown.
Only an independently verified preallocated no-incremental-charge target may run.

See [the pilot contract](../comparison-pilot.md) for bounds, prerequisites,
failure/recovery rules and the explicit unrun live gate.
