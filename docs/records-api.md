# Research and interaction records

These authenticated API workflows record attributed human and agent decisions.
They do not dispatch jobs, prove scientific validity, deliver messages to a
running harness, or authorize access/spending/publication. Every mutation requires
`Idempotency-Key`. Authorization is rechecked before replay. Unknown outcomes
should be retried with the same key and body on the same connection.

## Research ledger

| Route | Behavior |
|---|---|
| `POST /projects/{id}/studies` | Record purpose, protocol and exploratory/confirmatory mode |
| `GET /projects/{id}/studies` | List authorized studies with offset/limit |
| `GET /studies/{id}` | Read complete bounded study detail |
| `POST /studies/{id}/variants` | Register name, method/input digests and explicit metadata |
| `POST /variants/{id}/record` | Record completed, failed, excluded or abandoned outcome |
| `POST /studies/{id}/sources` | Record source URI, passage, content digest and optional locator |
| `POST /sources/{id}/assess` | Human records supported/unsupported/unverified assessment |
| `POST /studies/{id}/findings` | Propose statement linked to source and variant IDs |
| `POST /findings/{id}/accept` | Human accepts only after evidence checks |
| `POST /findings/{id}/reaffirm` | Human reaffirms unchanged evidence with a reason |
| `POST /sources/{id}/correct` | Preserve old source; add replacement and flag dependent findings |
| `POST /studies/{id}/close` | Close only when all registered variants are accounted for |
| `GET /studies/{id}/export` | Export all in-scope variants, sources, findings, corrections and events |

Revision-sensitive mutations require `expected_revision`. A finding needs cited
passages with a human support assessment; a URL alone is insufficient. Manual or
imported outcomes have no execution receipt. Replication independence is
unavailable in this slice. Recording a mode or accepting a finding is an
attributed decision, not automated methodological verification.

Export uses a consistent database read transaction and includes negative,
failed, excluded and abandoned variants. It fails with a conflict when the
requested byte cap or record bound is exceeded, rather than silently dropping
records. `max_bytes` defaults to 1,000,000 and can be at most 10,000,000; each record
type is bounded at 1,000 for export. Export is JSON, not a portable artifact-file
bundle or publication action.

## Conversations and decision inbox

| Route | Behavior |
|---|---|
| `POST /interaction/threads` | Create thread with project_id, optional task_id and title |
| `POST /interaction/messages` | Append attributed body and intent to thread_id |
| `GET /interaction/projects/{id}/threads` | List project threads |
| `GET /interaction/threads/{id}/messages` | List immutable messages |
| `POST /interaction/decisions` | Request bounded human review |
| `GET /interaction/projects/{id}/decisions` | List requests by status |
| `POST /interaction/decisions/resolve` | Designated human or local owner approves/rejects |
| `POST /interaction/projects/{id}/decisions/expire` | Record expiry of elapsed pending requests |

Messages have `question`, `direction`, `status`, or `evidence` intent, a body up to
8,000 characters and an optional same-thread `reply_to`. Their delivery status is
`queued`; no harness acknowledgement is implied. Prose does not revise plans.

Decision requests have `project_id`, `kind` (direction/spending/access/publication),
bounded JSON `scope`, a timezone-aware `expires_at` within 24 hours, and optional
`task_id`, `expected_task_revision`, `reviewer_principal_id` and priority (0–100).
Task requests must bind the current revision. An agent must name an eligible human;
a human requester defaults to themselves. Equivalent pending requests retain the
original scope, expiry and reviewer. Each project permits at most 50 pending
requests; explicit expiry releases stale capacity.

Resolution supplies `decision_id`, `expected_revision`, `decision` (approve/reject)
and optional rationale. Stale task basis and expired requests cannot be resolved.
Approval changes only the inbox record; task approval is a separate command.
Lists accept offset (0–100,000) and limit (1–100, default 50).

Reaffirmation requires `needs_review`, `expected_revision`, and a nonblank `reason`
(up to 4,000 characters). Every existing citation must be supported and
unsuperseded; confirmatory studies must be closed and accounted for. It preserves
the statement, citations, variant links, and original acceptance attribution.
The new human decision and reason are recorded in `research.finding_reaffirmed`.
A correction that supersedes a citation cannot be resolved by reaffirmation:
versioned finding edits, narrowing, supersession, and retraction remain pending.
