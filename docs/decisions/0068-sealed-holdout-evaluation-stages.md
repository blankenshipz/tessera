# 0068 — Final evaluation stages open a sealed holdout partition once, on the receipt

## Status

Implemented (schema 72: one additive table). Extends the campaign spec of
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) and keeps the data-class invariant of
[ADR 0047](0047-agent-routed-data-destinations.md).

## Context

An experiment that selects settings on a validation partition and then scores once on a sealed
test partition with private labels had to score outside Tessera: a script opened the test set
behind a local "claimed" file. That run had no receipt, the one-time rule was a convention that
a deleted file undid, and a person had to say "go" before each scoring run. The platform already
runs a scorer image as an ordinary stage, delivers private inputs from custody only to registered
providers and records stage metrics on the receipt. What was missing was a durable, platform-held
"this partition was opened once for this purpose" record.

## Decision

1. **Evaluation stage.** A v2 stage may declare `evaluation: {final, holdout}`. An evaluation
   stage must declare at least one `metrics` entry; those are the scored values, derived from the
   stage's own output as for any stage and written on the receipt. The scorer is the spec's
   pinned image; Tessera ships no scorer and no experiment-specific code. Absent, every spec
   digest is unchanged.
2. **Final.** `final: true` designates the stage as final: no stage of the spec may consume its
   outputs, so a held-out score can never feed back into selection.
3. **Sealed partition.** `holdout: {input, partition_digest, claim_scope}` is allowed only on a
   final stage. `input` names one of the stage's own `snapshot` or `artifact` inputs, and its
   pinned digest must equal `partition_digest` (the content digest of the custody snapshot or
   artifact). It may not be declared `public`, so the stage input is non-public and ADR 0047
   applies unchanged: no egress, no workload fetch hosts, only a registered provider trusted for
   `private_derived` receives it. No other stage of the spec may take an input with that digest.
   A holdout stage has `retry.max_attempts` of 1 and no `infra_retries`: a scorer that exits on
   purpose must not be able to see the partition again.
4. **One claim per (project, partition digest, claim scope).** The claim is a row in
   `campaign_holdout_claims` with that unique key. The runner opens it, with a
   `campaign.holdout_claimed` event in the same transaction, when the final stage's first attempt
   is about to release its input to a worker, so after its budget hold and after every stage it
   depends on has succeeded (re-checked in that transaction). If another campaign holds the
   claim, the attempt fails permanently with `holdout_already_claimed` and nothing is released.
   Submit and approval also refuse a spec whose holdout is already claimed.
   An attempt of a holdout stage is never followed by another once the claim is open: a failed
   attempt, including one reconciled as not started (ADR 0046), fails the stage
   (`holdout_spent:<class>`) instead of being requeued, so the partition is released at most once.
5. **Sealed inputs.** A partition is sealed from the first human approval of a spec that names
   it as a holdout (no separate registration step: the approved declaration is the
   registration), and stays sealed once claimed. A submitted but unapproved spec seals nothing,
   so an agent cannot block other campaigns' data. A stage that is not a holdout stage naming it
   as its holdout input may not read a sealed partition, and an `artifact` input may not come
   from another campaign's holdout stage (its outputs, per-example predictions for example, must
   not feed back into selection). Both are refused at submit and approval and checked again by
   the runner just before each input release, so a campaign approved before the partition was
   sealed fails with `sealed_partition` and nothing is released. A holdout stage's outputs also
   cannot be staged as an input snapshot (`snapshot.staged_from_artifacts` refuses its
   manifests), which closes the human staging path back into selection.
6. **Closing.** When the stage succeeds the claim becomes `scored` with the succeeded attempt's
   metric values (same transaction as the stage). A campaign that finishes with the claim still
   open marks it `spent` with the stage's status as the reason. Either way the partition stays
   claimed for that scope: there is no reopen, delete or reset path, and database triggers
   refuse a delete, any change to a closed claim and any change to a claim's identity.
7. **Receipt and reads.** The receipt carries a `holdout_claims` section (absent when no claim
   exists, so other receipts keep their shape). `GET /campaigns/{id}/holdout` and
   `GET /projects/{p}/holdout-claims[?partition_digest=]` (`campaign:read`, tenant-scoped) back
   the read-only MCP tools `tessera_campaign_holdout` and `tessera_campaign_holdout_claims`.
   Nothing in MCP opens, reopens or releases a claim.

The one human approval of the spec digest is the only decision: it covers the scorer image, the
partition digest and the claim scope. No per-scoring "go" is needed.

## Consequences

- A scored claim records measured numbers under the campaign's approval; it is not a
  scientific verdict (`not_assessed`), and a gate decision stays human.
- A claim opened for a run that then failed (including a grant that expired before any worker
  leased it, or a launch the worker reported as refused) is spent. One-time is the stricter
  reading: control does not try to prove the labels were never read.
**Known limits / future decision.** The next three bullets (the third covers seal
permanence and seal start) are documented limits, not defects; changing any of them needs its own
decision.

- Claims and seals are project-scoped, because the project is the tenant boundary every
  other campaign check uses: control does not compare a digest across projects. The same bytes
  staged in another project, or a different claim scope, are a new claim, so an actor with
  several projects can score the same labels once per project. Choosing the scope and project
  is part of the approved spec; a deployment that needs one claim across projects keeps the
  labels in one project.
- The claim key is the pinned digest of the input's kind (a snapshot's digest or an artifact's
  sha256), so the same labels pinned once as a snapshot and once as an artifact are two
  partitions to the platform; like a new scope, that needs a newly approved spec.
- A stage that read a partition before any holdout spec naming it was approved is not undone;
  from that approval on every release to a non-holdout stage is refused. An approved holdout
  spec that never runs (cancelled, for example) still seals its partition, and there is no
  release path for that seal yet (an operator escape hatch would be its own decision).
- **Upgrade note.** After upgrading, a campaign approved earlier that reads a digest which an
  approved holdout spec now declares fails that stage with `sealed_partition` at its next input
  release, even though it was approved first. Before upgrading, let such campaigns finish, or
  expect those stages to fail and resubmit them without the sealed input.
- Each approved spec's declared holdouts are parsed once and cached per campaign (keyed by the
  stored spec text), so the seal check before each release does not re-parse every approved
  holdout spec in the project.
- Bytes a person downloads from custody and re-stages by hand outside these paths are outside
  what control can see; the staging refusal covers the platform's own path.
- The scorer stays in the experiment's own repository and reaches Tessera only as a pinned
  image.

Tests: `tests/test_campaign_holdout.py`.
