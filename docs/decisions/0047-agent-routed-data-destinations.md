# 0047 — Agents route data across configured providers, never to the public internet

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
