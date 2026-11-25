# 0164 — Campaign inputs sealed for a role

## Status

Proposed. Builds on [ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) (v2 specs) and complements
[ADR 0068](0068-sealed-holdout-evaluation-stages.md) (one-time holdout claims). Amends
[ADR 0069](0069-rerun-a-finished-campaign.md) (a re-run plan carries seals). No schema change and no migration.

## Context

A campaign that fits on a training slice and scores on held-out labels keeps those labels away from
the fit stages only by how the spec is written: each fit stage happens to mount only its training
slice. Nothing in the platform checks it, so the guarantee lived in each experiment's spec and in
offline audits. ADR 0068 seals a partition across campaigns once a final holdout stage claims it,
but it does not stop a stage of the same spec from mounting data derived from the held-out slice,
and it needs a one-time claim, which many evaluations (few-shot or out-of-distribution scoring on
several arms) do not want.

## Decision

1. **Seals.** A v2 spec may list `seals` (at most 32). Each has a `name`, a `role` (for example
   `evaluation`) and exactly one target: `digest`, the pinned digest of a `snapshot` or `artifact`
   input (sealed wherever any stage mounts that digest, under any input name), or `stage` and
   `output`, one declared output of a stage in the spec (for example the test slice of a split
   stage). A seal whose digest no stage mounts, or that names an unknown stage or output, is
   refused, so a mistyped seal never looks like protection.
2. **Roles.** A v2 stage may declare `roles` (at most 8, distinct). A stage that mounts sealed
   data must declare the seal's role. The producing stage of a sealed output needs no role.
3. **Lineage.** A stage that reads sealed data taints every output it produces with those seals
   (the platform cannot tell which output came from which input). A stage that mounts a tainted
   output must declare the role as well, at any depth, so a seal cannot be laundered through an
   intermediate stage.
4. **Where it is enforced.** The check is part of `validate_structure`, so it runs whenever a spec
   is parsed: at submit (including dry-run validation), at approval, and every time the runner
   reads the stored spec. A refusal names the stage, the input, the seal and the role, and says when
   the read is derived through another stage.
5. **Receipt.** A stage's receipt entry carries `sealed_access`, one record per sealed read: `seal`,
   `role`, `input`, and `via` (`direct`, or `lineage` with `from_stage`). Computed from the approved
   spec by the same function as the check, so they cannot disagree.
6. **Re-runs.** A re-run plan (ADR 0069) replaces stages that are not re-run with pinned artifacts.
   Each pinned artifact keeps every seal it carried, as a digest seal on its sha256 (named after the
   seal and the digest prefix); a digest seal stays while a re-run stage still mounts it and an
   output seal stays while its stage is re-run. A re-run never reads more than the original was
   allowed to.
7. **Compatibility.** Both fields are omitted from the canonical form when absent, so every
   existing spec, digest and receipt is unchanged. v1 specs have neither.

## What this does not do

* It is a spec-level rule inside one campaign. An `artifact` input from another campaign is
  sealed only if this spec seals its digest; lineage across campaigns is not inferred at parse time
  (ADR 0068 still refuses another campaign reading a holdout stage's outputs).
* A role is a declaration the approver reviews, not an identity: any stage may declare any role.
  The guarantee is that no stage reads sealed data without saying so in the approved spec and on
  its receipt, and that every reader is visible to the person approving the digest.
* It changes no runtime, provider or custody path, and opens nothing once: combine it with an
  ADR 0068 holdout claim when the score itself must be one-time.

## Consequences

The "fit stages never see the held-out labels" rule moves from experiment convention into the
platform: a spec that breaks it cannot be stored or approved, and the receipt lists every stage
that read sealed data and for which role.
