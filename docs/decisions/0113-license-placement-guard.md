# ADR 0113: licence labels and a placement guard (a provider receives only data whose labels it allows)

## Status

Accepted and implemented. Schema change: one table (`snapshot_license_labels`). The migration is schema
version 85 (reserved as 86 on the branch; renumbered to the next contiguous version when merged after schema 84). Builds on [ADR 0039](0039-data-class-guard.md) (data classes),
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) (provider trust and placement of data),
[ADR 0047](0047-agent-routed-data-destinations.md) (registered destinations only) and
[ADR 0060](0060-capacity-aware-stage-placement.md) (candidate placement).

## Context

Reviews found that experiment specs could route data that its licence does not allow on rented
capacity (non-commercial and derived data) to a third-party provider. The experiments' own specs and
audits now avoid it, but nothing in the platform stopped it: public/private data classes say whether
data is private, not what it may be used for. The platform should refuse this generically, for any
data, any provider and any label vocabulary, without naming a dataset or a vendor.

## Decision

1. **A labels dimension, separate from the data class.** A project snapshot may carry licence labels:
   lowercase slugs (`^[a-z][a-z0-9_-]{0,31}$`, at most 8) from the operator's own vocabulary. Data with
   no label carries the implicit label `unlabelled`. Labels do not change the public/private class and
   do not widen anything.
2. **Add-only, set by a person.** A human session adds labels
   (`POST /projects/{project_id}/snapshots/{snapshot_id}/license-labels`, idempotent, recorded as the
   event `snapshot.license_labels_added`). A label is never removed or changed, and a trigger refuses
   deletion and any update that drops a label, so a copy cannot launder it. Agents can read them
   (`snapshot:read`, `tessera_get_snapshot_license_labels`) and cannot write.
3. **Lineage inheritance.** The effective labels of a stage's inputs are computed from the approved
   spec: a snapshot input carries its labels; a stage input carries everything the upstream stage read,
   recursively; an artifact input (an output of an earlier campaign) carries what its source stage read,
   computed the same way from the source campaign's stored spec. A lineage that cannot be resolved
   (a missing campaign or stage, a cycle, or more than 8 levels) carries `unknown-lineage` as well as
   `unlabelled`, so it is refused by any restricted provider.
4. **The provider declares what it may receive.** In the provider trust file
   ([provider trust](../provider-trust.md)) a provider entry may carry `allowed_license_labels`
   (1 to 16 labels). A provider the file lists without the key is unrestricted, exactly as before, and the policy
   digest is unchanged; a provider the file does not list may receive only `unlabelled` data (fail
   closed). When present, the provider receives a stage only if **every** effective label of **every**
   input is in the list (fail closed: unlabelled data needs `unlabelled` in the list, and
   `unknown-lineage` can never be allowed: the trust file refuses a list that names it). It applies in every trust mode,
   including `all_registered_providers`, and narrows only.
5. **Plan time and dispatch time.** Submit and approval check every candidate of every stage, the
   alternates included, and refuse the whole spec with a 409 naming the provider, the labels carried and
   the labels not allowed. Placement skips a restricted candidate with the permanent reason
   `license_label` (recorded on the placement event and receipt). The runner repeats the check when it
   reserves a stage and fails the stage `input_not_admitted` with the event
   `campaign.stage_license_refused` if the labels, the trust setting or the lineage changed after
   approval. Nothing is held for a refused stage.
6. **Receipts.** A stage's receipt entry carries `license_labels` (the effective labels) whenever a
   restricted provider is involved or any label is present; a stage with neither is unchanged.

## Consequences

- A person can say once that a snapshot is non-commercial (or any label), and every provider the operator
  has restricted refuses that data and everything derived from it, including through later campaigns,
  with a clear message and a record.
- The guard is a placement rule over inputs. It adds no destination and widens no trust.

## Limits

- Labels are operator assertions, not verified licences. Tessera does not read licence text or detect a
  licence; a person must label the sources. A source nobody labelled is `unlabelled`, which a restricted
  provider refuses until it is labelled or the list includes `unlabelled`.
- The guard covers a stage's inputs. What may be written to a destination is covered by ADR 0047, not here.
- Lineage is read from approved specs and recorded campaigns. A snapshot the platform derives (composed
  from snapshots and released outputs, or staged from a collected output manifest) is given the union of
  its sources' labels in the same transaction that creates it, and a `snapshot.license_labels_added` event
  records it (`inherited_from` names the sources); a derivative of unlabelled data stays unlabelled.
  Data that entered by another route (an export a person re-imported by hand) carries only the labels a
  person gave it.
- There are no heuristics: no names, hosts or file contents are inspected.
- Nothing here was run against a live provider.

## Tests

`tests/test_license_labels.py` (storage, add-only triggers, lineage, cycles and depth),
`tests/test_provider_trust_license.py` (the trust key and digest stability),
`tests/test_license_placement_guard.py` (submit, approval, placement, runner and receipt),
`tests/test_license_labels_api.py` (routes, authority and tenancy) and
`tests/test_mcp_license_labels.py` (the read tool).


The runner records the labels it checked at reservation as `campaign.stage_license_checked`; the
receipt reports those (`license_labels_checked: "dispatch"`) rather than recomputing at close. A busy
database (`sqlite3.OperationalError`) during the check propagates for a retry; it is never a refusal.

## Amendment 2026-11-23: a label is admitted only where a trust entry names it

Context. Decision 4 left a provider the trust file lists without `allowed_license_labels`
unrestricted. Derived data (`private_derived`) built from a labelled source is admitted by data-class
trust alone, so a provider listed for `private_derived` without a label list received non-commercial
or restricted-derivation data and everything derived from it; only a campaign spec's
`forbid_providers` list kept such data off. Labels are open slugs, so a label nobody had named (a typo)
also reached every such provider.

Decision (narrows only; no change for unlabelled data):

1. **Closed for third-party capacity, fail closed.** A provider listed without the key whose
   registered descriptor declares `third_party` (every Salad route must), or that is not registered
   in the process checking it, may receive only `unlabelled` data, exactly like a provider the file
   does not list. Any explicit label, whatever the vocabulary (a label no entry names included), and
   `unknown-lineage` are refused; the placement skip reason stays `license_label` and the refusal
   names the labels. To admit a label, name it in the provider's `allowed_license_labels` (with
   `unlabelled` if the provider should keep receiving unlabelled data). A provider registered as the
   operator's own capacity (`third_party` false: `local-docker`, self-hosted worker grants) listed
   without the key stays unrestricted, as decision 4 said: the labels describe what may not go to
   rented third-party capacity, and refusing them on the operator's own host would block labelled
   data everywhere. The trust document, its canonical form and its digest are unchanged;
   `lists_license_labels` (receipt shape) still reports only an explicit key.
2. **Lineage within a campaign is not depth-limited.** A stage input is resolved through the spec's
   stage graph (memoized, cycle-checked); the depth limit (8) now counts campaign hops only, so a long
   pipeline in one campaign no longer reads as `unknown-lineage`.
3. **Recorded outputs keep their labels.** When a stage's outputs are first indexed (the runner's
   `campaign_stage_io` output rows), the labels its inputs carry are stored in
   `campaign_output_license_labels` (one add-only row per campaign stage, org and project scoped; a
   later attempt adds labels, never removes them), in the same transaction. An `artifact` input (and
   so a resolved `receipt_output` input) carries the recomputed labels united with the recorded ones;
   where recomputation stops at the hop limit the recorded labels are used instead of
   `unknown-lineage`. Outputs recorded before the table existed are recomputed only, as before.
   Schema change: the table, migration schema 89 (reserved as 100; contiguous after 88).
4. **Public fetches are unlabelled.** A `public_artifact` input (ADR 0097) is `unlabelled`, as any
   source nobody labelled, instead of `unknown-lineage`.

Consequences. An operator who listed a third-party provider without the key and labelled data meant
for it must now name the labels on that provider (the 409 or placement skip says which). Deep cross-campaign
chains recorded after this change no longer depend on the recompute limit. A label added to a source
after a downstream output was recorded reaches it through recomputation (within 8 campaign hops), not
through the recorded row (the same limit as derived snapshots).

Tests: `tests/test_lineage_use_restriction_labels.py`.

## Note (2026-11-25): a research-only label on a rented provider

An operator may admit a label such as `non-commercial` (research use allowed, no commercial use) on a
rented provider by naming it in that provider's `allowed_license_labels`; removing it (and restarting
serve) reverts the decision for every stage not yet placed, which is re-checked at placement and
reservation. The worked example and the revert steps are in [licence labels](../license-labels.md).

