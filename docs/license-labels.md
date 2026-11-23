# Licence labels and the provider placement guard

Contract: [ADR 0113](decisions/0113-license-placement-guard.md). A licence label says what a piece
of data may be used for. A provider may be limited to data whose labels it allows, and a stage whose
inputs carry any other label is refused for that provider, fail closed.

## Label a snapshot

A person adds labels to a project snapshot; labels can only be added.

- `POST /projects/{project_id}/snapshots/{snapshot_id}/license-labels` with `{"add": ["cc-by", "share-alike"]}`
  and an `Idempotency-Key`. Human session only. The answer is `{snapshot_id, labels, added, revision}`
  and the change is recorded as the event `snapshot.license_labels_added`.
- Read: `GET` the same path (a person, or an agent with `snapshot:read`), or the MCP tool
  `tessera_get_snapshot_license_labels` (read-only). A snapshot with no label reads `["unlabelled"]` with
  `labelled: false`.

A label is a lowercase slug (letters, digits, `_` and `-`, starting with a letter, at most 32 characters),
at most eight per snapshot. The vocabulary is yours; Tessera gives labels no meaning of its own beyond the
two reserved names `unlabelled` and `unknown-lineage`.

## Limit a provider

In the provider trust file add `allowed_license_labels` to the provider's entry (see
[provider trust](provider-trust.md)):

```json
{"schema": "tessera.provider-trust.v1",
 "providers": {
   "salad": {"data_classes": ["public", "private_derived"],
             "worker_principal_ids": ["<worker-principal-id>"],
             "allowed_license_labels": ["cc-by", "public-domain"]}}}
```

A provider the file lists without the key is unrestricted; one the file does not list receives only
`unlabelled` data (fail closed). With the key, the provider receives a stage only if every label of
every input is in the list. Unlabelled data needs `unlabelled` in the list; data whose lineage cannot be
resolved carries `unknown-lineage` and is always refused by a restricted provider (the trust file does not
accept `unknown-lineage` in the list).

## Derived data inherits

The effective labels of a stage's inputs include everything upstream: a stage that reads a labelled
snapshot, and any stage that reads that stage's outputs, in the same campaign or in a later one through a
pinned artifact, carries the label. Labelling the source once is enough; copying data does not remove a
label.

Snapshots the platform builds from labelled sources (a composed snapshot, or one staged from a collected
output manifest) are labelled with the union of their sources' labels when they are created, so staging or
composing never launders a label.

## What a refusal looks like

At submit or approval a spec with a candidate that may not receive its data is refused with a 409:

> Stage 'train': provider 'salad' may receive only licence labels ['cc-by', 'public-domain']; this stage's
> inputs carry ['non-commercial'] (not allowed: ['non-commercial']). ...

The refusal covers every candidate, alternates included. At run time a placement skips that candidate
(reason `license_label`) and the next one is tried; if none can receive the data the stage fails
`input_not_admitted` with the event `campaign.stage_license_refused`, and nothing is held. The campaign
receipt lists the stage's `license_labels` as checked when the stage was dispatched
(`license_labels_checked: "dispatch"`); labels added later do not rewrite it.

## Limits

Labels are your assertions, not verified licences. Label sources when you stage them; an unlabelled
source is refused by a restricted provider. The guard covers what a stage reads, not what is written to a
destination (see [ADR 0047](decisions/0047-agent-routed-data-destinations.md)).
