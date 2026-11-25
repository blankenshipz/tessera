# 0163 — The reasoning admission is a person's recorded delegation

## Status

Proposed. Builds on [ADR 0119](0119-reasoning-service-install-command.md) (the installer never
admits), [ADR 0122](0122-owner-signed-approval-delegations.md) (owner-signed delegation records),
[ADR 0155](0155-reasoning-only-grant.md) (the reasoning grant) and
[ADR 0109](0109-reasoning-first-call-readiness.md) (first-call readiness). No schema change and no
migration.

## Context

The reasoning unit starts only while `/etc/tessera/reasoning.admitted` exists
(`ConditionPathExists`). The operator created it by hand (`install -m 0600 /dev/null ...`), so the
file said nothing: not who admitted the service, under which caps, or until when. Nothing could
expire or revoke it except deleting the file, and readiness could not tell an admission someone
deliberately gave from a stray empty file.

## Decision

1. **The admission is a record on receipts.** `POST /projects/{id}/reasoning-admissions` records the
   project, provider, bare model, caps (`max_cost_cents_per_call`, `daily_cost_cap_cents`,
   `daily_token_cap`, optional `max_calls`) and an expiry at most 30 days ahead (the delegation
   maximum of ADR 0122), with the admitting principal, credential and decision class. The record is
   the payload of an append-only `reasoning.admission_recorded` event carrying the record's canonical
   digest; revocation is a `reasoning.admission_revoked` event. Each is written with its tenant check
   and idempotency row in one transaction (`Store._mutate`). Like an ADR 0122 delegation, the record
   is signed once and never edited; the only change is `active` to `revoked`, and `revoked` beats
   `expired`.
2. **Only a person admits.** Creating a record needs a `human_session` credential. The owner token is
   refused with or without an automation label, as are agents, workers, external agents and smoke
   approver sessions. An admission widens what may run, so it is not a decision automation may
   take. Revoking only reduces what may run: any human-role credential (a person's session or the
   owner token) may revoke; agents and workers may not.
3. **The marker references the record.** `tessera reasoning admit` (root) sends the person's session
   header file, never an automation label, and only after the record exists writes the marker
   atomically (exclusive temp file, 0600, root-owned, fsynced, renamed, directory fsynced) holding a
   non-secret reference: the admission id, the record digest, the project and the expiry. It refuses
   an existing marker unless `--replace` is given, before anything is recorded.
   `tessera reasoning revoke-admission` revokes the record and removes a marker that references it.
4. **The unit is unchanged.** It still checks only that the file exists. A bare, hand-made marker
   keeps working for the unit exactly as before.
5. **Readiness reports the admission truthfully** (`tessera_agent.reasoning_admission`, check name
   `admission`):

   | Marker | Status | Code |
   | --- | --- | --- |
   | absent | `warn` | `not_admitted` (the unit is skipped; the installer never admits) |
   | bare (empty, or not a JSON object) | `warn` | `unrecorded`: never reported as a recorded admission |
   | symlink, not regular, group/world writable, too large | `fail` | `marker_unsafe` |
   | a JSON object that is not a valid reference | `fail` | `marker_malformed` (never read as bare) |
   | reference past its expiry | `fail` | `admission_expired`, even without the record |
   | reference, record not read (no control state, or the marker unreadable to this user) | `unchecked` | not a pass |
   | record missing, digest altered, revoked, expired | `fail` | `admission_record_missing`, `admission_record_invalid`, `admission_digest_mismatch`, `admission_revoked`, `admission_expired` |
   | another project, provider or model; a profile limit above a cap; `max_calls` reached | `fail` | `admission_scope_mismatch`, `admission_caps_exceeded`, `admission_calls_exhausted` |
   | an active record covering the profile | `ok` | names the record, the admitting principal and the class |

   `max_calls` counts the model-spend ledger rows for the project, provider and model that are not
   released, from the record's creation on. The check opens the control database read-only and
   never writes.
6. **Start-up can refuse offline.** `startup_refusal(marker)` refuses a malformed or expired recorded
   marker; a bare marker passes, as before. Revocation needs the record, which the readiness check
   (or a control read) sees.

## What it does not do

The admission grants nothing on its own. Every call still needs the reasoning grant (ADR 0155), the
project's model-spend limits and the profile's caps; the admission's caps bound what the profile may
be configured to, and readiness checks it. It does not make a bare marker fail, and it does not let
any identity other than a person's session create an admission.

## Alternatives considered

- **Reusing the approval-delegation table (ADR 0122).** Rejected: its terms are campaign authority
  (budget ceiling, per-run cap, allowed campaign providers), and a row there is usable by the
  delegated-decision commands. Storing an admission there would let a reasoning admission stand for
  campaign approvals. The human-signer rule, decision provenance, expiry maximum and
  active/revoked/expired states are reused instead.
- **A dedicated table with a migration.** Not needed: the record is written once, read by id within
  a project (the events index on project), and the event log is already the receipt of record. It can
  move to a table later without changing the contract.
- **Making the unit check the record.** Rejected: the unit must stay a plain file condition and must
  not depend on the control plane being reachable at boot.
