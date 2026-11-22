# 0047 — Agents route data across configured providers, never to the public internet

## Amendment (2026-11-22)

By owner direction, 2026-11-22, the reasoning plane's enrolled model destinations
([ADR 0082](0082-agent-loop-anthropic-destination.md)) are an operator-attested destination
class, separate from provider trust: they are not providers in the provider-trust file and
this record's "registered" does not describe them. The hard public-internet invariant is
unchanged for campaign stages. Sealed holdout data (ADR 0068) is never admissible as a model
input; for the reasoning plane that is now checked in code before the prompt is sent
(`refuse_sealed_inputs`, failing closed). The Anthropic destination is called directly through
Tessera's Messages API client with an API-key reference; no subscription login is involved.

## Status

Implemented (no schema change). Extends the provider trust setting of
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) §4.

## Context

The trust setting binds data classes to provider instances (a provider id plus listed worker
principals). Each provider entry carries its own class list, and the built-in default admits
public data only. A deployment that runs every compute provider itself, or under contracts
it accepts, still had to widen each entry by hand, and a missing class blocked agent-planned
work across providers (for example private derived training data on a GPU provider).

## Decision

1. **Owner-set mode.** The trust file takes an optional `mode`:
   - `listed_classes` (default, omitted from the canonical form, so existing digests are
     unchanged): a listed principal receives its entry's `data_classes`.
   - `all_registered_providers`: a principal listed under a provider id that is registered
     on this platform (built in, or named in the operator's provider file) receives every
     class: `public`, `private_code_approved` and `private_derived`. Platform agents and
     campaign specs then choose when and where data goes among those providers, with no
     per-run human approval of the destination.
2. **Fail closed.** In every mode, a principal listed nowhere, or listed under a provider id
   no module registered (a typo, a removed plugin), receives public only. A process that
   did not load the operator's provider file sees only built-in providers as registered.
3. **Hard invariant, not configurable.** No data class leaves to the public internet or to an
   unregistered destination: a stage with non-public input cannot use egress (local CPU) or
   declare workload fetch or redirect hosts (Salad); these refusals are in spec validation
   and do not read the trust mode. Publishing stays forbidden (a run output is never
   recorded public), and every run receipt records the provider, the admitted input class,
   the input digests and the trust setting digest in force.
4. **Narrowing only.** Specs (`send_classes`) and the trust setting recorded at approval time
   still narrow: an approval taken under a narrower setting never gains a later widening.
5. **Audit.** A mode change is part of the setting digest and of the
   `provider_trust.policy_changed` difference (`_mode: {from, to}`).

## Consequences

- A fresh open-source install keeps the conservative default; an operator opts in by adding
  `"mode": "all_registered_providers"` to its trust file and reloading.
- Trusting a provider for every class is a statement about that provider's operator and
  contract, made once by the deployment owner; it is not a per-dataset license review.
  Dataset licences still travel with the data and are not relabelled.
- A workload that must download public bytes (model weights, packages) cannot take
  non-public input on a provider whose egress is provider-wide; it must receive those bytes
  through a snapshot instead.

## Addendum (2026-11-22): operator-registered model destinations

This addendum clarifies decision 3 and does not change it. The hard invariant concerns the public
internet and unregistered destinations. An operator-registered model destination (enrolled by the
operator, limited to the classes its registry entry lists, and reached only through a model-runtime
allowlist that is not the stage or sandbox allowlist) is a registered destination, never a
public-internet exception. A private class may reach it only if the registry entry lists that
class. Stage specs still cannot declare egress for non-public inputs. The destination registry is
decided in ADR 0082; client-requested review jobs ([ADR 0084](0084-research-review-journeys.md))
rely on it and refuse private inputs until it exists.
