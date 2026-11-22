# ADR 0077: campaign preparation from the spec (multi-input adapter, optional adapter config, exact refusals)

Status: accepted and implemented. No schema change. Builds on ADR 0038 (campaign runner, composed
inputs), 0040 (providers and data-class routing), 0047 (agent-routed data destinations) and 0076
(the Salad stage adapter).

## Context

After ADR 0076 an operator still had to compose a project snapshot by hand (the adapter took one
pinned snapshot), write a `stage_adapter` section with a program template and an input size, build
and allowlist an image, and list the Salad worker in provider trust. The first two can be derived
from what the spec and the platform already hold. The last two are policy or supply-chain decisions.

## Decision

### 1. Several pinned inputs

The adapter accepts any mix of pinned `snapshot` and pinned `artifact` inputs. The runner composes
more than one input into a single snapshot before staging; that snapshot's digest is
`inventory_digest(composed_inventory(entries))`, a pure function of each file's prefixed path,
sha256 and size. At submit and validate the adapter computes it from the platform's recorded inputs
(a snapshot's stored manifest, a resolved artifact), read only, and writes it into the explicit
contract as `input.snapshot_digest`, with the exact total size as `input.max_bytes`. The size must fit
the profile's `disk_mb` and, when set, `stage_adapter.max_input_bytes`.

Nothing composes at submit. The runner still composes at run time under the approved campaign's
existing delegation (composition stays human-only or delegated) and the route requires the composed
digest to equal the contract's, so a changed source fails closed. An output of another stage of the
same campaign has no digest at submit and is refused with a message saying to pin it as an artifact of
a finished campaign or as a snapshot. An `artifact` input must exist when the spec is submitted.

### 2. Optional adapter configuration

`stage_adapter` on a CPU profile is an opt-in section; every field is optional. `program` defaults to a
generic, neutral template that names only the stage; `max_input_bytes` is an optional ceiling;
`grant_ttl_seconds` keeps its default. The program template is still never taken from the stage.

### 3. Images: operator-built, pinned by digest (non-goal: a builder)

Tessera does not build images, and this ADR adds no builder. The existing path stands: the operator
builds an image, pins it as `name@sha256:...`, and lists the digest in the profile's
`allowed_image_digests`. A builder would need a build service, registry and package egress and a
supply-chain policy; that is a separate decision. What changes is the refusal: an image outside the
allowlist names the exact `"sha256:..."` entry and the profile to add it to, in the adapter and in the
Salad provider's check.

### 4. Provider trust stays explicit operator policy

Nothing lists a provider or worker automatically. A stage input a provider's worker is not trusted to
receive is refused as before, and the 409 now ends with what to merge into the provider-trust file's
`"providers"` entry (the class and worker principal filled in; merge, because replacing the entry
would drop other listed workers). The refusal carries the class whose admission would lift it
(`DataClassRefused.admit_class`); a refusal no trust setting could lift (a code-approval mismatch, an
unreadable manifest, public data without evidence) carries no hint. The owner mode is not suggested.

## Consequences

- A stage that reads several pinned snapshots and artifacts can be adapted without composing a snapshot by hand.
- The adapter's digest is checked twice: by the explicit contract the approver sees and by the
  route at run time.
- No new authority, egress or data destination; ADR 0047 is unchanged.

## Limits

- Nothing has run live: no adapted stage on Salad, no image built. Tests use fakes.
- Stage-output inputs of the same campaign are still unsupported by the adapter.
- An artifact's digest is known only if the artifact exists when the spec is submitted.
- Image building, and listing a provider in trust, remain operator work.

The adapter's `tenant` argument is required (`expand_spec`, `expand_with_registered`): a caller that has
an organization passes `(org_id, project_id)` and a profile of any other organization is unknown to it;
`None` is for offline validation only and must be written out. Offline checks outside this repository
that call `expand_spec` must pass `tenant=None`.

## Tests

`tests/test_salad_prep_automation.py`: composed digest equal to the runner's for several snapshot and
artifact inputs, exact input size and its bounds, the stage-input refusal, the default program, the
exact allowlist and trust hints. `tests/test_salad_stage_adapter.py`: the adapter's expansion and
refusals, adjusted for multi-input.
