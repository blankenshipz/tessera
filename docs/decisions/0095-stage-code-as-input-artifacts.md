# ADR 0095: a stage's code reaches a provider as a content-addressed input, not as part of an image

Status: accepted. Extends ADR 0039 (data-class guard, code approval), 0040 (providers and
data-class routing), 0047 (agent-routed data destinations), 0076 and 0077 (the Salad stage adapter and
its launcher). No new transfer path.

## Context

A stage runs an operator-pinned image (`name@sha256:`) whose argv runs inside it. The easy way to
ship a stage's own code to a provider is to bake it into that image. For a third-party provider such as
Salad the image has to sit in a registry the provider can pull from, so private experiment code would
be published to a registry. Nothing about the receipt then says which code bytes ran: the image digest
is on the spec, not on the receipt, and code approvals record per-file digests only for approved paths.

The pieces for the right shape already exist: a snapshot input pinned by digest is mounted at `/inputs`
(or `/inputs/<name>/`) on every provider; human code approval and the `private_code_approved` trust
class gate private code per provider; provider trust and the registered-providers-only rule (ADR 0047)
limit where any non-public input may go; and the Salad stage adapter's launcher runs a stage's argv
against the staged input with the image holding only the runtime and dependencies.

## Decision

1. **Code is an input, the image is generic.** Private code must not be baked into an image that is
   pushed to a registry, for Salad or any provider. A stage's code reaches local Docker, a self-hosted
   server or Salad as a project snapshot pinned by digest, mounted by the existing input path, into a
   generic runtime image the operator built with hash-pinned dependencies and pinned by digest
   (`Dockerfile.cpu` or the GPU recipe: no experiment code). On Salad it travels through the existing
   staged-input ticket and the stage adapter's launcher (ADR 0076, 0077); on the worker providers it is
   the ordinary read-only input mount.
2. **`code_input`.** A v2 stage may name one of its `snapshot` inputs as `code_input`. The field is
   omitted from the serialized spec when absent, so every existing spec digest is unchanged. Submit
   and approval refuse a `code_input` that is not one of the stage's snapshot inputs, names a snapshot
   with no files, or names a snapshot whose code approval does not match its files. Every other check
   stays as it was: the input is admitted per provider by provider trust and its recorded data class,
   a candidate that is not trusted for it is refused or skipped, and the data-destination invariant of
   ADR 0047 (registered providers only; no non-public input to egress or workload fetch hosts) is
   untouched.
3. **Receipts name the code and the image.** For a stage with a `code_input` the receipt's stage entry
   carries a `code` block: `input` (the input's name), `snapshot_id`, `snapshot_digest`, `code_digest`
   (sha256 of the canonical JSON of the snapshot's sorted `[path, sha256]` pairs), `files` (the
   integer count of files in the pinned snapshot; file paths and per-file hashes never appear on a
   receipt, and `code_digest` binds the exact content), `approved` (whether a valid human code
   approval covers it when the receipt is built) and `image_digest` (the digest of the stage's
   pinned image). Each attempt entry carries a `code_transfer` block: `provider_id`,
   `worker_principal_id`, `code_digest`, `image_digest`, `trust_policy_digest`, `admission_basis`,
   `granted` (true only when the attempt's `campaign.code_granted` event exists), and `approved`
   and `approval_digest` (the approval state recorded in that event at grant time; null when there
   is no event). The receipt therefore says which code bytes were granted to which image on which
   registered provider, under which trust setting; a grant does not prove the code was fetched or
   ran.
4. **The grant is recorded.** When an attempt's grant is issued the runner writes one
   `campaign.code_granted` event for it in the same transaction that sets the attempt's grant (and
   under the same once-per-`stage_key`-and-`attempt` check), so a crash leaves both or neither. The
   event records that the platform granted the attempt's worker access to the pinned code input, not
   that the worker fetched it: `stage_key`, `attempt`, `attempt_id`, `provider_id`,
   `worker_principal_id`, `input`, `snapshot_id`, `code_digest`, `image_digest`,
   `trust_policy_digest`, `admission_basis`, and the code approval as it stood at grant time:
   `approved` (whether a valid human code approval covered the snapshot) and `approval_digest` (the
   canonical digest of that stored approval record, or null when there was none). The event holds
   ids and digests only, never file contents, a file list, file paths or argv.
   The attempt's `code_transfer` block reads `granted`, `approved` and `approval_digest` from that
   event only: `granted` is true exactly when the event exists for the attempt (never derived from
   the grant id or the stage's status), and `approved` is the approval state at grant time, so an
   approval recorded or invalidated later does not change it. With no event (for example a grant
   written by an earlier build without one) `granted` is false and `approved` and `approval_digest`
   are null (unknown), never read from current state. The stage `code` block's `approved` is a
   readout of the approval when the receipt is built.
5. **What it does not do.**
   - It does not make code approved. Approval stays a human act (ADR 0039). A composed snapshot
     carries no approval (ADR 0039), so a stage that needs approved code uses the code snapshot as its
     sole snapshot input; otherwise the code is `private_derived` data and is admitted by that trust
     class, and `approved` on the receipt (in the stage's `code` block and, at grant time, in each
     attempt's `code_transfer`) is false.
   - It does not build or push images, and it does not stop an operator from baking code into an
     image anyway. The platform records the image digest; whether an image may hold code is operator
     policy.
   - The stage adapter is unchanged: its argv may run files from `/inputs/<name>/`.

## Consequences

- The registry never sees private code, only the generic runtime image.
- The receipt names exactly which code bytes (by digest) ran in which image, on which registered
  provider, under which trust setting, with a recorded grant of the code to that attempt's worker
  and the code approval as it stood then. A grant is not proof the worker fetched or ran the code;
  the attempt's own evidence fields say what ran.
- The same stage runs unchanged on any provider: moving a stage between local Docker, a self-hosted
  server and Salad moves the input, not an image.
- Existing specs and receipts without a `code_input` are byte-identical.

## Limits

- Nothing was run live; the Salad path is the existing adapter path with fakes only.
- `code_digest` covers the snapshot's files, not what the argv actually executes: an argv may run
  files outside the code input, or an interpreter built into the image.
- A mounted code input is readable by whatever runs in the container. It is not secret from the
  provider that runs the stage, which is why provider trust must admit the input's class for it.
- Salad has provider-wide egress, so a stage with a non-public input may not declare fetch or redirect
  hosts (existing rule).

## Tests

`tests/test_campaign_code_input.py` (the field, its validation at submit and approval, unchanged
digests without it, the refusals) and `tests/test_campaign_code_receipts.py` (the `code` and
`code_transfer` blocks, the digest, the code-grant event, no contents in events or receipts; a
grant without its event reports `granted: false`; a failed event write rolls back the grant; an
approval recorded or invalidated after the grant leaves `approved` as it was at grant time).
