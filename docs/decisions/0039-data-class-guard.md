# 0039 — Data class guard for third-party compute

## Status

Accepted (2026-11-19). Implemented with default tests only,
against synthetic state and the fake provider. Resolves review finding M1 on the
Salad input path ([ADR 0036](0036-generic-salad-gpu-workloads.md)). Schema v62.

Numbered 0039 because the campaign runner track uses 0038.

## Context

Before this record nothing mechanical kept private or derived data off third-party
GPUs:

- Salad admission checked only that a snapshot with the declared digest was staged in
  the route's project.
- Every manifest entry is `imported_unverified` / `unclassified`.
- A snapshot release's `classification: "public"` is a human assertion at release
  time. It is not a property of where the snapshot came from, and a snapshot built
  from run outputs could be released as "public".

## Decision

1. **One source of truth.** `tessera_control.data_class` defines
   `DataClass = Literal["public", "private_derived"]` (the same values as the campaign runner track's
   campaign models), `DataClassDeclaration`, the `snapshot_data_classes` table and the
   readers and guards. The module has no module-level dependency on any store.
2. **Recorded on the source snapshot at staging.** Each snapshot has at most one
   immutable row in `snapshot_data_classes`. Triggers refuse an update, a delete of a
   row whose snapshot exists, and a second insert for the same id (so `DELETE` plus
   re-insert and `INSERT OR REPLACE` both fail). Only the begin-time declaration of an
   upload that never became a snapshot can be deleted, by upload garbage collection.
   A missing or malformed row reads as `private_derived`.
   - `public` is a human assertion at staging. It is never inferred. It needs a license
     identifier (an SPDX-style id; `NOASSERTION`, `NONE`, `UNKNOWN`, `UNLICENSED` and
     `PROPRIETARY` are refused) and a source URL or reference. The actor must be human
     and must pass `require_decision_actor`, so a bare root owner cannot claim it. The
     row records the actor, `decision_class` and the full decision provenance.
   - A public declaration is refused if any other snapshot in the org with the same
     digest is recorded `derived` or `migrated`, or has no record. Same digest means
     same bytes, so a re-stage cannot launder output-derived or pre-v62 bytes into
     public. This is checked at chunked begin and again when the snapshot is recorded.
   - Without a declaration a snapshot is `private_derived` (origin `default`).
   - A snapshot built from run outputs or artifacts (`/projects/{p}/artifact-snapshots`,
     small or chunked) is always `private_derived` (origin `derived`). Its request
     model forbids a `data_class` field, and `record_snapshot_data_class(derived=True)`
     refuses any public declaration.
   - There is no reclassification endpoint. A later human reclassification would need
     its own audited endpoint.
   - The human staging paths accept an optional declaration: chunked begin
     (`data_class` in the body, recorded under the upload id), chunked complete
     (optional body, which must equal any begin declaration), and
     `SnapshotStore.adopt_snapshot(..., data_class=...)` for legacy local adoption. The
     `workspace.snapshot_adopted` event carries the record, and `GET /snapshots/{id}`
     returns it.
3. **Third-party targets admit only public inputs.** `TargetSpec.third_party_compute`
   marks a target as third-party. Both Salad targets set it; Kubernetes does not, and an
   unregistered target counts as third-party (fail closed).
   `data_class.require_public_input` runs:
   - in `salad_workload_admission.check_workload_admission`, on the snapshot the
     admission resolves for the declared input digest, when a route is proposed,
     approved and admitted (create/start). Among the project's snapshots with that
     digest, admission prefers one recorded `public`, then falls back to the earliest;
   - in `ProviderInputStore.issue_ticket` and again on every manifest and chunk pull,
     on the grant's snapshot. Only an operation whose remote route targets a registered
     non-third-party target is exempt; a route-less operation counts as third-party
     (fail closed). This is defence in depth: the trusted server's `deliver_inputs`
     mints through it, and a ticket stops working as soon as its input is not public.
   Stop, heartbeat and reconcile are never gated by the input's class.
4. **Outputs are `private_derived`.** `data_class.OUTPUT_DATA_CLASS` is
   `private_derived`. Any snapshot staged from retained outputs is `private_derived`.
5. **Receipts carry the classes.** The worker run receipt (response and the
   `worker.execution_run_receipt_recorded` event) carries `input_data_class` (the grant
   snapshot's recorded class) and `output_data_class`. The Salad live-check receipt
   carries the same pair; `input_data_class` is `null` when no input is delivered. The
   artifact staging record (`snapshot.staged_from_artifacts`, the output-to-input
   lineage) carries `data_class: "private_derived"`.
6. **Setup.** `scripts/salad_live_setup.py --input-dir` requires
   `--data-class public --license ID --source URL_OR_REF`. It refuses to stage without
   them, never defaults to public, and checks that control recorded the input as
   public. The setup summary shows the declared evidence and the recorded
   `decision_class` for review.
7. **Migration v61 -> v62.** Installs the table and trigger and records every existing
   snapshot as `private_derived` (origin `migrated`), failing closed. It revokes every
   outstanding provider input ticket and marks the last backup stale.

## Consequences

- A Salad input must be re-staged with a public declaration and evidence. Snapshots
  that existed before v62 cannot reach Salad.
- The release classification (`ReleaseSnapshot.classification`) is unchanged and still
  needed for worker transfer. It is separate from the staging class; this record does
  not tie the two together for non-third-party targets.
- Admission resolves the input by digest and prefers a public staging of those bytes in
  the project. The ticket check uses the grant's snapshot. Bytes first staged without a
  declaration (origin `default`) can be re-staged public in the same project. Bytes the
  org holds as `derived` or `migrated` cannot be declared public anywhere in the org: a
  pre-v62 input must be staged from its original public source as new bytes, or this
  needs the audited reclassification endpoint that does not exist yet.
- The workspace CLI (`tessera workspace adopt`) uses the bare owner credential, so it
  can only adopt `private_derived` snapshots. Public local adoption goes through
  labelled automation or a human session.

## Campaign runner track integration

The campaign runner track defines its own `DataClass` in `campaign_models.py` and its
own migration v61 -> v62. When it merges after this record, it must:

1. **Renumber its migration to v63** (superseded by amendment 1 below: now **v64**)
   (`migrate_v62_to_v63`, `LATEST_SCHEMA_VERSION = 63`,
   `MIGRATIONS[62]`) and bump the schema version in `store.py`'s `schema_meta` insert and
   the version assertions in tests. Its ADR stays 0038.
2. **Import `DataClass`** from `tessera_control.data_class` instead of redefining it
   (`from .data_class import DataClass, OUTPUT_DATA_CLASS, PRIVATE_DERIVED`).
3. **Validate `SnapshotSource.data_class` against the record.** A campaign spec that
   names a snapshot as `public` must be refused unless
   `snapshot_data_class(connection, snapshot_id)["data_class"] == "public"`.
   Declaring `private_derived` for a public snapshot is allowed, because it is more
   restrictive.
4. **Record composed snapshots as derived.** `snapshot_composition.compose` inserts
   `source_snapshots` rows, in both the chunked and small paths. In the same
   transaction it must call
   `record_snapshot_data_class(connection, live, ident, derived=True, now=now)` and put
   the record in its `workspace.snapshot_adopted` payload. Without that call the
   snapshot still reads as `private_derived`, because a missing row fails closed, but
   it will not carry an explicit record.
5. **Use `target_is_third_party` / `require_public_input`** for the `salad-gpu-v1`
   backend's stage inputs rather than a backend-name check. A downstream stage's inputs
   come from composed snapshots, which are always `private_derived`, so under this
   record they can never reach a third-party target. A stage's
   `output_data_class: "public"` cannot make a public snapshot. The campaign track
   should either drop that option or document that it does not change the recorded class.
6. **Release classification.** The campaign track widened
   `ReleaseSnapshot.classification` to `Literal["public", "private_derived"]`; a `public`
   release of a snapshot recorded `private_derived` by derivation is refused
   (`public_release_refused`).

## Amendment 1 — owner-approved private code (schema v63)

### Status

Accepted (2026-11-19). Implemented with default tests only,
against synthetic state and the fake provider. Schema v63.

### Decision

Private code or config that the owner approves for a remote run may be staged to
third-party compute. Data rules are unchanged: only data declared public, with license
and source evidence, may go there, and derived data stays `private_derived` and is
refused. Approved code is **not** relabelled public; it stays private and unlicensed.

Rationale: code and config are the owner's own work product, not data about anyone, and
the owner decides who runs them. That is a human decision about one exact set of bytes,
so it is recorded like the public declaration: per file, immutable, with the deciding
actor's provenance.

1. **Per-path split inside one staged snapshot.** The staging request (chunked begin,
   chunked complete, and local `adopt_snapshot`) takes an optional
   `code_approval: {"paths": [...]}` of exact relative paths. Every other file must be
   covered by the snapshot's public data record. `DataClass` stays
   `Literal["public", "private_derived"]`; a snapshot's `data_class` record now describes
   its data files only.
2. **Record.** `tessera_control.code_approval` owns the `snapshot_code_approvals` table:
   one row per snapshot holding each approved file's path, sha256 and size, the actor,
   `decision_class` and decision provenance, and the time. The same three triggers as
   `snapshot_data_classes` refuse update, delete of a row whose snapshot exists, and a
   second insert (so delete-and-reinsert and `INSERT OR REPLACE` fail). A table CHECK
   admits only human decision classes. A begin-time approval is recorded under the
   upload id; completion may repeat it but never change it; upload garbage collection
   discards one whose upload never completed.
3. **Who approves.** Only a human principal that passes `require_decision_actor`:
   never an agent or worker credential and never a bare root owner. Labelled owner
   automation is accepted and recorded as such (`owner_automation_labelled`). The
   self-asserted `desktop-owner` label (`owner_automation_unverified`) is refused, and
   the table CHECK admits only `human_session` and `owner_automation_labelled`.
4. **What counts as code.** Suffixes `.py .sh`, plus structured config suffixes
   `.json .toml .yaml .yml .cfg .ini` that are approvable only when the request lists the
   file in `structured_config_confirmed` (the approver's explicit statement that it is
   configuration, not data), at most 64 KiB each; the confirmation is recorded per file in
   the immutable approval row and re-checked on every read. An unconfirmed structured
   file is refused. Also
   `requirements.txt`, `constraints.txt` and a single-token variant such as
   `requirements-gpu.txt` (`(requirements|constraints)(-[a-z0-9]+)?.txt`). Other `.txt` files, `.csv`, `.jsonl`,
   notebooks and binaries are data: text is the most common data format, so it is not
   code by default. At most 256 files, 1 MiB per file and 8 MiB in total.
5. **Anti-laundering.** A file cannot be approved when its sha256 equals a file of any
   other snapshot in the org recorded `derived` or `migrated`, or without a record, or
   staged without any declaration (origin `default`). The exception: a file whose sha256
   equals a file already validly approved as code in another snapshot, so the same code
   can be staged again. This is checked at chunked begin and again when the snapshot is
   recorded. Empty files are exempt (they carry no content). The mirror rule applies to
   the public declaration: every non-approved file of a snapshot declared public must not
   share its sha256 with a file of another snapshot (different digest) that is `derived`,
   `migrated`, `default` or unrecorded, so a derived file bundled with new approved code
   is refused (chunked begin and complete, and adopt). **The sha256 comparison is a
   tripwire, not a guarantee**: a re-serialized or reformatted file has other bytes and
   passes. The human approval is the control. Structured approved files (`.json`,
   `.yaml`, `.yml`, `.toml`, `.cfg`, `.ini`) can hold data, hence the explicit
   confirmation (setup: `--confirm-structured-config PATH`, repeatable) shown in the setup
   summary. The anti-laundering test is "not recorded public" (derived, migrated,
   undeclared, unrecorded or any future private origin), not a list of origins.
6. **Enforcement.** `data_class.require_public_input` keeps its name and call sites
   (route admission, ticket issue, every manifest and chunk pull) and now delegates to
   `code_approval.admitted_code_paths`: a third-party target admits the snapshot when
   every file is approved code or the snapshot is recorded public. A missing record,
   a malformed approval row, or one whose paths, sizes or digests no longer match the
   snapshot's manifest fails closed; route-less operations still count as third-party.
   Among the project's stagings of the declared digest, admission uses the first one
   that passes this check. With a code approval, an input-sourced `entry` item of a
   gpu_runtime_v1 manifest must be one of the approved paths; other input items need
   no extra rule, because every admitted file is either approved code or public data.
7. **Receipts.** The worker run receipt (response and event) and the Salad live-check
   receipt carry `input_data_class` (the class of the non-code files;
   `none_all_approved_code` when every file is approved code; `null` only for "no input
   delivered"), `input_code` (`private_code_approved`, `none`, or `invalid` when an approval row exists
   but no longer matches the snapshot; the live-check receipt
   uses `null` when no input is expected and `unknown` only when one is expected but the
   session cannot read it) and `input_code_files` (each approved file's path and sha256). Outputs stay
   `private_derived`.
8. **Setup.** `scripts/salad_live_setup.py --approve-private-code PATH` (repeatable)
   sits beside `--data-class public --license ID --source URL_OR_REF`. The data
   declaration is required whenever a file is not approved code. Setup refuses before
   any write when a file is neither, when an approved path is not a code or config
   file, is not in the input, or exceeds a cap, and when the input entry is not
   approved code. The setup summary shows each approved path with its sha256 and the
   recorded `decision_class`.
9. **Migration v62 -> v63.** Creates the table and triggers and marks the last backup
   stale. Nothing is approved by migration.

Entry rule coverage: the rule that an input-sourced `entry` item must be approved code
is checked at admission, against the staging admitted for the declared digest. Provider
input ticket issue and every pull re-check admission of the grant's snapshot (public or
approved code) but not the entry rule, because the runtime manifest is not available at
that point; that gap is accepted and documented rather than re-derived there.

Local transfer: a public `ReleaseSnapshot` is also what a remote route needs, so the
release itself is not refused. Instead, the local worker input delivery
(`POST /worker/leases/{id}/inputs` and its chunk reads) refuses any snapshot that has a
code approval, so approved private code is never handed to a local worker under a public
release label; it travels only through the provider input path, which `require_public_input`
gates. A malformed manifest fails closed with `DataClassRefused`. The refusal covers every
snapshot with an approval, including code-only `private_derived` ones, on purpose: local
delivery checks the release and the lease, but neither the per-file admission
(`admitted_code_paths`) nor the provider trust setting (ADR 0040), so it is not a path
that may carry approved code. The release response and its `workspace.snapshot_released`
event carry a derived `contains_approved_code` flag, so a `public` release of such a
snapshot is visibly labelled.

Public data versus approved code: a file that is approved code in any snapshot of the org
(public or not) cannot be declared public data in a different snapshot; approve it as code
there instead. Staging the identical snapshot again (same digest) is governed by the
whole-snapshot rule, so the ADR 0040 ranking of identical stagings still applies. A snapshot's own malformed manifest or approval refuses that snapshot
only; another snapshot's corrupt row never blocks a staging.

### Invariants

- No path turns code into public data: `snapshot_data_classes` is unchanged, and an
  approval never writes or reads as `public`.
- Data reaching third-party compute is always recorded public with evidence.
- An approval names exact bytes (path, sha256, size) and a human decision; it cannot
  name bytes the org holds in any snapshot not recorded public (derived, migrated,
  undeclared, unrecorded or any future private origin), unless the same bytes are
  already validly approved code in another snapshot.
- Run outputs remain `private_derived`, and snapshots built from them can carry no
  approval: their request model has no such field, and
  `record_code_approval(..., derived=True)` refuses one.

### Consequences

- A snapshot recorded public may now contain approved private code. A consumer that
  needs "every file is public" must also check that `snapshot_code_approval(...)` is
  `None`. The worker-transfer `ReleaseSnapshot.classification: "public"` is a release
  label and does not relabel approved code; such a release reports
  `contains_approved_code: true`.
- The anti-laundering check compares against staged snapshots only, like the public
  rule; bytes that exist only as unstaged run outputs are not compared.

### Campaign runner track changes

In addition to the list above, when the campaign runner track merges after this amendment it must:

1. **Renumber its migration to v64** (`migrate_v63_to_v64`, `LATEST_SCHEMA_VERSION = 64`,
   `MIGRATIONS[63]`), not v63, and bump `store.py`'s `schema_meta` insert and the
   version assertions in tests to 64.
2. **Not treat a public record as all-public.** Where it validates
   `SnapshotSource.data_class == "public"` or a `public` release, also refuse (or
   report as mixed) a snapshot whose `code_approval.snapshot_code_approval` is not
   `None`.
3. **Composed snapshots carry no approval.** `snapshot_composition.compose` must not
   copy approvals from its sources; a composed snapshot is derived and its code files
   become `private_derived` data.
4. **Use `require_public_input` unchanged** for `salad-gpu-v1` stage inputs; it now
   returns the approved code paths and already applies the per-path rule.
