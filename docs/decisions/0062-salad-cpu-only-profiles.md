# 0062 — CPU-only Salad profiles

## Status

Accepted and implemented (2026-11-21). No schema change. Extends the Salad campaign profiles of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) and the provider plugin of
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md). The runner core is unchanged. Amended by [ADR 0191](0191-salad-cpu-fallback-and-concurrent-frames.md): a CPU profile may list operator `fallback_profiles`.

## Context

Every Salad campaign profile named a GPU class, and the Salad route, transport and guardian all
assumed one GPU. A stage that needs only CPU (a simple baseline) could therefore not be placed on
Salad at all, or had to occupy a GPU it did not use. The lower layers were already tolerant: the
route's resources allow `gpu_count: 0`, and the resource check names a CPU-only shape.

## Decision

1. **A profile is a GPU profile or a CPU profile.** A profile has exactly one of `gpu_class`
   and `cpu_rate`. The CPU rate is the operator's two published USD-per-hour numbers
   (`vcpu_hour_usd`, `memory_gb_hour_usd`); vCPU, memory and disk stay the profile's own fields,
   with the same Salad shape limits. Nothing else about a profile (account, project, worker,
   credential reference, image allowlist, route window, cent ceilings) changes.
2. **The route carries the rates.** A CPU route has `gpu_count: 0` and no GPU class, and its
   execution profile carries `cpu_rate`, so the rates are part of what a human reviews and of the
   route digest; the target digest of a CPU route differs from any GPU one. A GPU route's
   serialization has no `cpu_rate`, so every existing route byte and digest is unchanged. A route
   with a GPU and rates is refused when it parses; a stored CPU route without rates still parses,
   so stop and reconcile keep working, and admission and the transport require the rates.
3. **Rendering.** The transport sends `gpu_classes: []` and makes no GPU-class read. A group read
   back with the empty list, or without the field, matches.
4. **Cost.** There is no provider read for a CPU price. The transport reports the operator rate
   for the route's shape (`vcpu × rate + memory GB × rate`) with the basis
   `operator_cpu_rate_not_actual_cost`; the guardian accepts exactly that basis, and no GPU class,
   for a CPU route, and refuses it for a GPU route. Everything downstream of the hourly rate (the
   running-time accrual, the per-run cap, the budget and fencing) is the existing guardian code.
   The estimate is an input, never invoice or cost proof, like the published GPU rate.
5. **Placement.** A stage declares `salad.gpu` (default `true`: every existing Salad stage;
   absent from the serialized spec unless `false`, so spec digests are unchanged). A stage and a
   profile of different kinds are refused with 409 at submit and approval: a stage that declares no
   GPU runs only on a CPU profile, and a GPU stage never on one.
6. **The per-run cap at admission.** A CPU profile has static rates, so admission computes the
   worst case of the stage's whole `max_duration_seconds` and refuses (409) a stage whose
   `max_exposure_cents` does not cover it. `max_exposure_cents` is already bounded by the profile's
   per-run and per-task cents, which is the operator's cap (for example 50 cents). The guardian
   enforces the same cap while the run is billed.

## Consequences

- A simple baseline can run on Salad without a GPU, at a CPU rate, under the same trust, data,
  custody and review rules as a GPU stage.
- A preflight for a CPU route reads the replica quota but not availability: Salad publishes only a
  GPU availability read. Its `available: true` is a constant, **not evidence that Salad has CPU
  capacity**; allocation is left to Salad and a stalled one ends at the route deadline.
- Not verified against Salad itself: that a created CPU group is returned with an empty or absent
  `gpu_classes`, and that its running time is billed at the operator's rates. The tests use the
  recorded-response transport factory only; the operator checks the rates against Salad's
  published pricing before using a profile.

## Follow-up

`scripts/salad_cpu_live_check.py` is the bounded, opt-in live check (a dry run by default, outside
default pytest, at most 9 cents, always stops and deletes its group; see the
[provider page](../campaign-providers.md#checking-a-cpu-profile-against-salad-opt-in-bounded)). It
confirms the CPU group shape and compares the billing at the operator's rates with a figure a person
copies from Salad's usage page. It has not been run against Salad: until a person runs it and records
an agreeing receipt, both stay documented as unverified. Its tests use a stateful in-process fake of
the container-group API behind the transport's connection factory.

## Tests

`tests/test_salad_cpu_profile.py`: profile validity (exactly one of GPU class and rates, rate
shape), the CPU route (no GPU fields, rates reviewed, digest and byte stability for GPU routes,
stored routes still parsing), the transport wire (empty GPU list, group match, operator rate with
no provider call, preflight without the availability read, refusals), the guardian's basis checks
in both directions, placement both ways, and the cap at admission.
