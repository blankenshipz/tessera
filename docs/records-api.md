# Research and interaction records

These authenticated API workflows record attributed human and agent decisions.
They do not dispatch jobs, prove scientific validity, deliver messages to a
running harness, or authorize access/spending/publication. Every mutation requires
`Idempotency-Key`. Authorization is rechecked before replay. Unknown outcomes
should be retried with the same key and body on the same connection.

Schema v23 additionally records project storage bindings, immutable placement
metadata, and durable intent/reconcile/commit state for validated worker artifacts.
These records intentionally expose no credential, URL, or worker path.
They retain quarantine/classification/result/finding/cost/provider dimensions: a
committed placement is durable custody only, not a release or scientific claim.

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
| `POST /findings/{id}/retract` | Human retracts a finding while retaining its history |
| `POST /findings/{id}/revise` | Human narrows or supersedes a finding with a linked candidate |
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

## Goal-to-decision ledger

`POST /projects/{id}/experiments` and `POST /experiments/{id}/designs` record a
baseline-plus-intervention design. Every design revision includes its own immutable
`goal`, `success_criterion`, `protocol`, and baseline/intervention digest snapshots.
`POST /experiments/{id}/conclusions` accepts an optional `design_revision`; when
omitted for compatibility it freezes the current design revision. A conclusion and
its `POST /conclusions/{id}/review` response expose that `design_revision` and a
complete `design_snapshot`. Supplying a different revision while reviewing or
revising a conclusion is rejected. `GET /experiments/{id}/export` includes complete
historical designs, legacy attempts, admitted attempts with their complete append-only
semantic state-transition histories and latest operational sample
state/cursor/timestamp, linked operations, and every conclusion's snapshot and correction
lineage, without dispatching work or establishing scientific validity. It uses one
read transaction and fails with `409` rather than truncating when any exported
collection (including attempts, admitted-attempt semantic state history, operations, evidence
links, artifacts, measurements, nested conclusion evidence, designs, variants,
conclusions, or amendments) exceeds 1,000 records, or when its complete JSON exceeds
`max_bytes`. `max_bytes` defaults to 1,000,000 and is capped at 4 MiB, matching the
desktop control-response limit; callers need a separate bounded snapshot mechanism
for larger ledgers.

Routine worker polling remains in the operational observation journal and updates
the admitted attempt's latest sample projection without appending a duplicate
semantic transition or labbook event. State/observed-state/termination-confirmation
changes still append complete semantic history; polling is neither silently dropped
nor exposed through unbounded experiment detail or export paging.

`POST /conclusions/{id}/amendments` is human-only and accepts
`expected_revision`, `expected_design_revision`, `relation` (`supersedes` or
`retracts`), and a nonblank `reason`. It only admits a reviewed conclusion whose
immutable design snapshot matches the request. The initial lineage revision is the
reviewed conclusion revision; the request compare-and-swap fence rejects stale or
conflicting terminal relationships. It appends one attributed correction record and
does not alter the reviewed conclusion's statement, evidence, review attribution,
or revision. Export/reopen exposes both each conclusion's `lineage` and the complete
`conclusion_amendments` collection. An amendment is a recorded human correction,
not plan/execution authority, result acceptance, or scientific validity.

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
A correction that supersedes a citation cannot be resolved by reaffirmation.
Use explicit revision to choose the replacement evidence.

Retraction requires a human, `expected_revision`, and a nonblank `reason` (up to
4,000 characters). Candidate, accepted, and needs-review findings can be
retracted regardless of support or study closure. Retraction is terminal: later
source assessments or corrections cannot reactivate the finding. Statements,
citations, variants, and prior acceptance attribution stay intact; the reason,
actor, and previous status/revision enter `research.finding_retracted` history.

Revision requires `expected_revision`, `kind` (`narrow` or `supersede`), `reason`,
`statement`, and explicit `source_ids`/`variant_ids` from the same study. It marks
the original finding `superseded`, retaining its statement, links, creator, and
original acceptance attribution, and creates a new **candidate**. The response
contains `original` and `replacement`. The replacement stores
`supersedes_finding_id`, `revision_kind`, and `revision_reason`; ordinary findings
have null values. The event records the review actor, reason, prior status and
revision, and both IDs. Narrowing describes the human's decision; Tessera does
not automatically establish semantic implication or scientific validity.

Revision is allowed in closed studies to review existing evidence without adding
unaccounted variants. Replacement acceptance remains a separate human action with
current source support and confirmatory closure checks. Superseded and retracted
findings are terminal; subsequent versions form a linked chain. Exports retain
every version and its original citations, including negative or withdrawn claims.
