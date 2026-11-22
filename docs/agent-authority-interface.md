# Governed agent authority: integration interface freeze

Shared task `tessera-remote-agent-platform`, intent revision 1.
Initially an interface reservation; the backend implementation and scoped local
verification below are now complete. Existing dirty changes remain in place.

## Ownership

The authority lane owns new `src/tessera_control/agent_authority.py`,
`agent_authority_models.py`, `agent_authority_api.py`, and
`tests/test_agent_authority.py`. Narrow integrations are reserved in
`models.py` (enrollment action literals), `store.py` (additive schema bootstrap),
`api.py` (store/router registration), `experiments.py` and `execution.py`
(transaction-local commands, not identity substitution), and `external_agents.py`
(withdrawn artifact-context discovery). The lane also owns this interface note.
It does not own MCP, remote reasoning, A2A, worker pagination, desktop, or the
implementation journal. Coordinate additional existing-file edits with the parent.

## Supported HTTP contract

All mutations require the existing `Idempotency-Key`. External-agent calls use
their own bearer credential plus existing `X-Agent-Destination`; human issue and
revoke calls use separately authenticated human credentials.

- `POST /projects/{project_id}/agent-authority-requests`: agent submits a typed
  bounded-grant request. This creates no execution or approval authority.
- `GET /projects/{project_id}/agent-authority-requests`: human sees project
  requests; agent sees only its own requests.
- `POST /projects/{project_id}/agent-authority-grants`: human issues a grant.
- `GET /agent-authority-grants/{grant_id}`: scoped current status including
  remaining reserved budget, revision, epoch, expiry, lineage and revoked state.
- `POST /agent-authority-grants/{grant_id}/revoke`: human revokes using
  `expected_revision` and `expected_epoch`; descendants are fenced by ancestry.
- `POST /agent-authority-grants/{grant_id}/delegate`: subject delegates a strict
  subset to another already enrolled project/destination-scoped agent credential.
- `POST /agent-authority-grants/{grant_id}/consume`: subject executes a closed
  command union; arbitrary method/path/payload forwarding is not supported.

`app.state.agent_authority` is `AgentAuthorityStore(state_dir)`. Public methods:
`request_authority(actor, project_id, key, payload)`,
`list_requests(actor, project_id, offset=0, limit=100)`,
`issue_grant(actor, project_id, key, payload)`, `get_grant(actor, grant_id)`,
`revoke_grant(actor, grant_id, key, payload)`,
`delegate_grant(actor, grant_id, key, payload)`,
`consume(actor, grant_id, key, payload)`.
Mutators return `(status_code, response_dict)`; reads return records. Integrators
should use HTTP contracts rather than direct database access.

## Closed request shapes

`AgentAuthorityBounds` fields:

- `subject_credential_id`, `destination`, `model`, `purpose`;
- `actions`: unique subset of `attempt:admit`, `attempt:dispatch`,
  `attempt:status`, `execution:cancel`, `authority:delegate`;
- `bindings`: nonempty list of exact resources, each with `variant_id`,
  `expected_variant_revision`, `expected_design_revision`, `execution_grant_id`,
  `expected_execution_grant_revision`, `expected_execution_epoch`,
  `task_id`, `expected_task_revision`, `plan_digest`, `target_digest`;
- `budget_cents`, `max_duration_seconds`, offset-aware `expires_at`,
  `delegation_depth` (bounded nonnegative integer).

`RequestAgentAuthority` and `IssueAgentAuthority` carry `bounds` (the latter
may include `request_id` to record the human response). Project and tenant are
derived from the route and authenticated identity, never caller tenant claims.
`DelegateAgentAuthority` carries `expected_revision`, `expected_epoch`, `bounds`.
`RevokeAgentAuthority` carries `expected_revision`, `expected_epoch`.

`ConsumeAgentAuthority` carries `expected_revision`, `expected_epoch`, `model`,
and `command`, discriminated on `action`:

- `attempt:admit`: `variant_id`, `expected_variant_revision`,
  `expected_design_revision`, `execution_grant_id`;
- `attempt:dispatch`: `attempt_id`, `expected_revision`, `target_digest`,
  `profile_version` (existing supported Kubernetes profile literal);
- `attempt:status`: `attempt_id`;
- `execution:cancel`: `execution_grant_id`, `expected_revision`.

Proposal and context APIs remain supported separately; `authority:request` and
`authority:consume` enrollment scopes are additive capabilities, not approval.
Existing proposal/create and context/read scopes do not implicitly acquire them.
Delegate requires both enrolled consumption scope and an explicit delegated action.
Caller model is attested, not cryptographically verified model provenance.

## Approval trust and the automation label

The root `local_owner` credential (the `owner.header` written by `tessera init`)
sits on disk, so any local script can use it. It therefore cannot silently make
human decisions. These endpoints refuse a bare `local_owner` credential with 403:
task-plan approval (`/tasks/{id}/approve`), execution-grant issuance, agent-authority
grant issuance, experiment attempt admission, experiment budget
authorization/charges/bounds, remote route `approve`/`start`, autonomy session
`approve`/`resume`, credential-access lease create/renew, snapshot release,
capability approval, conclusion review, program/source review approval
(`/interaction/decisions/resolve`) and dataset-ingestion approval.
Use a separately issued interactive `human_session` credential
(`POST /credentials`, `kind: human_session`) for these.

Owner-operated automation may still use the owner credential if the request carries
`X-Tessera-Automation: <label>` (non-empty, printable, no surrounding whitespace,
at most 128 characters; an invalid value is rejected). The header only labels the
caller: it never grants authority, and agent and worker credentials gain nothing
from it (every role check still applies). The label is not a secret and is not
proof of identity.

### Gated and ungated actions

Rule: actions that grant, approve, start, release or spend are gated. Actions that
only stop, cancel, pause, reject, revoke or revise (they reduce authority) are not
gated, so an owner can always stop things, but they still record provenance. Ungated:
`reject` on review decisions and dataset-ingestion review; route `pause`/`cancel` (routes have no `reject` command: only `approve` and
`start`, gated, and `pause` and `cancel`); autonomy `reject`/`cancel`/`revise`; credential-lease and snapshot-release revoke;
agent-authority and execution-grant revoke. Bare-owner reduce-only actions are
recorded with `decision_class: "owner_unlabelled"`.

### Decision provenance and `decision_class`

Each decision event (`task.approved`, `execution.grant_issued`,
`execution.grant_cancel_requested`, `experiment.attempt_admitted`,
`experiment.budget_*`, `experiment.conclusion_reviewed`, `capability.approved`,
`decision_resolved`, `research.dataset_ingestion_reviewed`, `agent_authority.issued`,
`agent_authority.revoked`, `credential_access.lease_created`/`_renewed`/`_revoked`,
`workspace.snapshot_released`, `workspace.snapshot_release_revoked`) records
`credential_kind` (`local_owner`, `human_session`, ...), `automation` (true when a
label was sent), `automation_label` when present, and `decision_class`:

| `decision_class` | Meaning |
| --- | --- |
| `human_session` | A separately issued interactive `human_session` credential. The only class that points at a person's credential. |
| `owner_automation_unverified` | Root `local_owner` credential labelled `desktop-owner`. |
| `owner_automation_labelled` | Root `local_owner` credential with any other label (for example `salad-live-setup`). |
| `owner_unlabelled` | Root `local_owner` credential without a label; only possible on ungated reduce-only actions. |
| `other_credential` | Any other credential kind. |

`desktop-owner` is unverified. The desktop app sends it when connected with the
owner credential, but any holder of the owner token can send the same string, so it
does not show that a human clicked. Receipts must not present it as a human
decision; `human_session` is the only class that does. Remote route state records the
same provenance under `approval_provenance` / `start_provenance` / `stop_provenance`,
and autonomy sessions under `approval_provenance` / `resume_provenance` /
`stop_provenance`; the state events carry them. `scripts/salad_live_setup.py` sends
`X-Tessera-Automation: salad-live-setup` on every owner-made request. A route stopped by
the executor's worker credential records `credential_kind: worker`, `decision_class:
other_credential` and `cancel_authority: executor_worker` in `stop_provenance`
([ADR 0075](decisions/0075-executor-worker-credential-cancels-own-route.md)).

Next: desktop `human_session` login. The desktop app should obtain and use a
`human_session` credential instead of the labelled owner token, so its decisions
record `decision_class: "human_session"`. Not implemented yet.

## Atomicity, execution boundary and migration

Additive `create_agent_authority_schema(connection)` is installed through the
existing schema bootstrap and explicit v50-to-v51 migration. Schema 51 also adds
provenance and research collaboration tables. New authority tables contain
requests, grants with resource-bound JSON, and immutable consumptions.
Grant rows hold revision/epoch/status and reserved
budget; immutable consumption rows link grant/agent/command/result identities.
Existing historical migrations are preserved. Upgrade old databases with
empty authority records; existing credentials gain no new actions or grants.

The historical schema-52 milestone introduced explicit v51-to-v52 migration for empty immutable
`agent_output_provider_bindings` before output notebook projection; use
`tessera migrate --state-dir <existing-state>` before restarting the service.
No historical provider Job UID is synthesized. At that milestone fresh bootstrap
installed schema 52; old schema-51 readiness remained unavailable until explicit
migration. Only active state lost backup-success freshness; held-restore forensic
metadata and authority remained unchanged. Existing typed provenance `output_proof`
can append verified bindings; migration itself creates none.

Schema 53 later added explicit v52-to-v53 independently human-admitted
generated-public-output grants, immutable byte registrations and recipient
disclosures. See [generated output contract](generated-public-output-interface.md).
No generated output is assigned fabricated worker/Job/lease provenance.

Later migrations add ordinary-Docker target/dispatch and runtime-binding records
with immutable-history triggers. Startup refuses older state until explicit
migration. Source contracts and local tests do not establish that an installed
database was upgraded; follow [compatibility](compatibility-matrix.md) and the
[release checklist](release-checklist.md) for the intended artifact.

Consumption rehydrates external-agent identity and validates enrollment,
credential lifetime, tenant/project, destination/model, resource pins, execution
grant revision/epoch, task/design revisions and digest, target, grant lineage,
expiry/revocation, budget and time before transaction-local execution. State,
reservation, consumption identity, event and idempotency response commit together.
Replay rechecks active authority but does not reserve or dispatch twice.
Delegation reserves a subset budget against its parent; descendants cannot widen
resources/actions/destination/model/time or outlive ancestors. No human role or
owner token is substituted. Existing human-only routes remain human-only.
Execution grants are separately human-issued: the new grant permits consuming
named approved execution resources, not minting arbitrary execution authority.
Prepare the ordinary worker lease before issuing the agent grant, and pin its
current execution epoch: lease creation advances that epoch and never silently
widens an existing agent grant. Authority expiry must cover the named execution
grant's expiry for admission/dispatch; execution cannot outlive its agent fence.

## Provenance integration hook

Every successful mutation writes ordinary correlated control events, attributed
to the real actor. Event types use `agent_authority.requested`, `.issued`,
`.delegated`, `.revoked`, `.consumed`; payloads contain only record IDs, action,
resource pins and budget counters (no prompt, tool output, credentials or secrets).
Consumption links execution grant, admitted attempt and operation IDs when present.
The provenance lane can join these existing events/consumption records into the
authorized notebook. Denials continue through the existing minimal operator audit
path. A separate provenance writer must accept an existing SQLite connection if
called inside admission; it must not open a second transaction or demand secret
material. Exact writer naming is owned by that lane and will be integrated once
published.

## Remaining proof

### Research-workflow extension (integration addition)

Reserved additive enrollment scopes: existing `proposal:create`/`proposal:read`,
`observation:create`, `review:request`, `notebook:read`. No scope grants approval, conclusion,
publication, artifact custody or arbitrary project data access.

- `POST /projects/{project_id}/agent-workflow-proposals`: body exactly matches
  `src/tessera_mcp/mcp_workflow.py` `ResearchProposal`: `proposal` discriminated by
  `kind` (`project`: `name`,`purpose`; `study`: `study: CreateStudy`; `experiment`:
  `study_id`,`experiment: CreateExperiment`), plus `provenance: AgentProvenance`.
  Project proposals request a
  separately created project; they do not broaden the caller's enrolled project.
- `POST /agent-research-proposals/{proposal_id}/promote`: separately authenticated
  human adopts the proposal with `expected_revision: 1`, `expected_digest`.
  Creates ordinary project/study/experiment records with real human attribution;
  never approves execution or scientific conclusions.
- `GET /projects/{project_id}/agent-workflow-proposals`: human sees project queue,
  agent sees its own records; `offset`, `limit` pagination.
- `POST /projects/{project_id}/agent-observations`: `attempt_id`,
  `expected_attempt_revision`, `summary`, `artifact_ids`, `provenance`.
  Appends an explicitly
  caller-attested observation, not worker execution evidence or accepted finding.
- `POST /projects/{project_id}/agent-review-requests`: `subject_type` (`proposal`,
  `experiment`, `attempt`, `observation`), `subject_id`, `expected_revision`,
  `reason`. Queues separate human review; never records an approval.
- `GET /projects/{project_id}/agent-notebook` and `/agent-notebook/export`:
  `after_sequence`, `after_event_cursor`, `limit`, `max_bytes` (up to 2 MiB).
  `offset` is a backwards-compatible provenance sequence cursor alias.
  Returns content-free project provenance/event metadata only under explicit
  `notebook:read`, with page cursors/completeness markers. It is not
  unrestricted project/study export. Existing authorized disclosed context APIs
  remain the only content-disclosure route. Humans use normal full research export,
  joined with same-snapshot project provenance metadata and separate pagination.

New collaboration records/schema/models/routes are owned by this authority lane.
Proposals/observations/review requests are immutable and idempotent, tenant and
project bound; referenced research subjects must exist in that same project.
No raw research/context/artifact contents are included in metadata export.

2026-10-04 backend checkpoint:

- `.venv/bin/pytest -q tests/test_agent_authority.py tests/test_external_agent_authority.py tests/test_agent_provenance.py`:
  **63 passed**, two dependency deprecation warnings, 103.07 seconds. Exercises
  actual temporary SQLite and in-process HTTP admission; no model/provider/cluster
  effects. Includes grant fences, replay, concurrent idempotency/budget reservation,
  delegation/ancestor revocation, dispatch/status/cancel, attested collaboration,
  redacted metadata export, denial/failure survival, schema migration/readiness and
  withdrawn-context discovery.
- Scoped Ruff check/format check and scoped `git diff --check`: passed.
- `uv.lock` updated with optional `a2a-sdk[http-server]==1.2.1`; uv was installed
  only in an isolated private tooling directory outside the repository. Reasoning and A2A
  console entrypoints added; installed entrypoints are not exercised here.
- Broader combined research/experiment/Kubernetes regression runs exceeded 120- and
  300-second command limits without a reported test failure; they are incomplete,
  not passing evidence. Parent owns the final full integration gate.

Ordinary live MCP/A2A/reasoning/Docker journey acceptance remains separate.
The subsequent bounded collector integration registers `agent_provenance_api.router`
and `app.state.agent_provenance = AgentProvenanceStore(state_dir)` using the existing
schema 51 tables. Human enrollment supports `provenance:append` (nine unique actions
maximum); old immutable enrollments gain nothing. Four focused app HTTP tests cover
grant-bound append/replay/conflict, scoped notebook/export, destination/project/
subject/revocation denial and explicit schema-50 migration/readiness. Runtime/A2A
outbox delivery and live joined-notebook acceptance remain separate integration work.
No live provider, protocol, Docker or independent-host evidence
is claimed by this backend lane. Schema 50 installations require explicit
`tessera migrate`; silent authority inference or auto-migration is not supported.
