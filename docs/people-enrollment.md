# Enrolling a person

A person is an enrolment root (see [ADR 0084](decisions/0084-research-review-journeys.md) section 5). Agents
and sessions the owner creates share the owner's root. Owner-only person enrolment creates a human principal
with its own root, so that human-only decisions, such as resolving review findings, can be made by someone
control treats as a different person. Control cannot verify that two roots are two humans.

## What an enrolled person is

- A human principal whose session credential (kind `human_session`) is its own enrolment root: no parent
  credential and no creating principal. An `enrolled_people` row records it.
- Can: resolve findings and take other human-only decisions that need a person.
- Cannot: issue credentials, enrol people or act as the root owner.
- Cannot yet own agents under its own root; that path does not exist (open question in ADR 0084).

## API (owner only)

- `POST /people` with `{token, label, expires_at}`. The token is a client-generated secret; control stores only
  a hash and never echoes or logs it. `expires_at` must be within 7 days.
- `GET /people` lists enrolled people.
- `POST /people/{principal_id}/renew` extends expiry up to 7 days from now. It never revives a revoked or
  expired credential.
- Revoke with the existing `POST /credentials/{id}/revoke`.

Every enrol, renew and revoke is audited in the same transaction as the change; the token is never recorded.

## MCP (operator opt-in)

Start a separate server with `--person-enrollment-tool` and the owner's credential file. It exposes
`tessera_enroll_person` (generates the token, writes the person's credential file with mode 0600, created
exclusively, and returns only ids, label, expiry and the path, never the token), `tessera_list_people` and
`tessera_renew_person`.

## Review jobs in default mode

Only an agent holding `review:findings` can post findings, and only the owner issues agents, so posting agents
share the owner's root. The complete default flow needs three roots: an enrolled person P submits, an owner-enrolled
reviewer agent posts, and a second enrolled person Q resolves (the owner's sessions get 403 `same_person`; P, the submitter, gets 403 `same_principal`). A
job submitted by an owner-enrolled agent or by the owner's session cannot receive findings in default mode. The
operator can use `--review-single-owner` instead. Workers and a person's session cannot post findings.

## Restore

A restored state gets a new self-rooted owner. Persons and roots recorded before a restore are not the same
person as the new owner; `enrolled_people` roots survive restore only as recorded rows.
Today a restored state also stays held: every old credential is revoked and the control API refuses all
writes, including resolving findings, until a supported promotion path exists (`tests/test_people_restore.py`).
If promotion is ever added, the same human holding a new owner root would count as a distinct person, so that
change needs its own separation test.
