# Tessera design

Status: implementation contract and roadmap; the full pilot is not implemented.
The control plane, bounded CPU execution components, desktop research records,
and reasoned finding review are implemented in slices. See the
[implementation status matrix](docs/implementation-progress.md#design-status-matrix)
for what is implemented, partial, blocked, or deferred, with remaining acceptance
work. README.md describes the runnable scope.
This document defines the pilot, the contracts it must satisfy, and the order of
implementation. “Required” describes intended behavior, not a verified guarantee.

## 1. Product and scope

Tessera lets a researcher direct local and remote agents, run bounded experiments,
and follow the investigation in a labbook. The desktop agent helps plan work and
communicates with remote researchers or lab technicians. The researcher retains
control of direction, access, cost, methodology, and publication.

Desktop, control plane, and execution plane are independently deployable.
“Remote” means outside Electron, not necessarily on another machine. Network
placement does not change authorization requirements.

The first user is a hands-on researcher or research engineer with an existing
script/notebook investigation and an available execution host, local or elsewhere.
The first workflow is:
adopt that investigation, author a skill, run a bounded activity, intervene when
needed, and review/export an honest record of the result.

This is a computational-research pilot with a domain-neutral evidence model.
Literature, theory, observation, analysis, and experiments must not all be modeled
as GPU jobs or numerical hypothesis tests. A scientific method becomes supported
when its protocol, evidence checks, and acceptance fixture exist.

| Pilot: build now | Next: after pilot validation | Later: demand-driven |
|---|---|---|
| One user, desktop partner, one supported harness and trusted remote supervisor | Second harness and compute adapter to test portability | Organization/team administration, shared registries, federation |
| Local files/chat, skill authoring/testing, bounded remote CPU jobs | GPU target; Kubernetes adapter tested locally with Minikube and remotely | Spark and other workload engines, broader provider coverage |
| Plan/access/cost approval, status, cancellation, stop/edit/handback | Bounded multi-agent task discovery/delegation | Schedules, automatic memory, broad connector/plugin catalogs |
| Labbook, evidence review, existing-work adoption, portable export | Research branches/comparisons, richer method checks and tracker imports | Advanced graph editors, domain viewers, browser/computer automation |
| Tenant-scoped records and permission checks from the start | Optional research-scoped GitHub publisher | Direct instrument/robot control, subject to separate safety design |
| Process-based control plane with authenticated desktop/worker APIs | Local-cluster packaging (including Minikube) and organization identity integration | Production organization-managed and paid Tessera-hosted offerings |

No autonomous budget increases, public release, or policy relaxation. Do not build
a scholarly index, general workflow engine, GPU scheduler, full experiment tracker,
or a feature-for-feature replacement for existing desktop agents.

The likely team buyer is a research lead or platform owner; the infrastructure
approver may be someone else. Validate those roles and a recurring paid use case
before expanding. Portability, multiple agents, and a dedicated desktop are
value hypotheses, not evidence of differentiation.

## 2. User workflows

### 2.1 Start or adopt work

A researcher first selects a named control-plane connection, authenticates,
and chooses an authorized organization/project. Show endpoint, identity, data
ownership, and connection health; changing connections never silently migrates work.

A researcher can start with a question and cited literature review, or select an
existing repository, notebook, results folder, or manifest.

Adoption begins with a read-only inventory of files, environments, sources,
results, external IDs, and possible running jobs. The desktop partner proposes
relationships and identifies missing provenance. The researcher confirms the
mapping and a baseline snapshot. Imports retain their original and recorded
timestamps, source, and verification status; never invent historical approvals
or execution receipts. Discovery does not authorize attaching to or stopping jobs.

Ordinary local discussion and file analysis use a scoped session grant. A remote
activity needs an approved plan, but not a long proposal or invented hypothesis.
The minimum plan contains purpose, input snapshot, command/tool, outputs,
destination, access, cost/exposure limit, deadline, and stop behavior. Default
evidence status is exploratory, with no accepted scientific claim.

### 2.2 Author and use a skill

The researcher describes a procedure to the desktop partner, edits it in the UI,
or imports a skill directory. An instruction-only skill needs a `SKILL.md` with
name, description, and guidance. Scripts, references, templates, and tests are
optional. A skill can guide the desktop partner or selected remote roles.

The flow is draft → validate → bounded test → review → release → enable.
Show trigger examples and non-triggers, inputs/outputs, requested tools/access,
compatibility, cost, failures, and stopping behavior. Installing never executes
setup code or grants permissions. Private exploratory releases may be self-reviewed
where policy allows.

Explicit invocation is required in the pilot; optional automatic selection follows.
Use qualified names to resolve collisions. Record the exact content digest loaded
by each agent. Remote rollout shows desired, downloaded, verified, and active
versions. Running tasks remain pinned until an approved revision applies a change
at a supported boundary. Rollback affects future work, not past evidence.

Keep standing project instructions, reusable skills, assistant definitions,
policy, and research memory separate. Show the effective instructions and their
origins. Import/export portable skill files; harness-specific metadata may remain
attached, but unsupported behavior must be disclosed.

A skill passing execution tests does not prove its method is valid. Scientific
skills declare assumptions, applicability limits, counterexamples, references,
maintainer, and methodological review status. Protocols decide when that review
is required. Method corrections flag affected findings through recorded digests.

### 2.3 Plan, approve, and execute

The partner proposes a bounded activity, including method, inputs, expected
outputs, validation, allowable adaptations, estimated cost, and stop rules.
The researcher reviews a structured proposal. Changes to the proposal invalidate
approvals whose basis changed.

The control service checks authority, capabilities, data destinations, and budget,
then reserves resources and issues a task lease/grant. The supervisor loads pinned
skills and inputs and starts the harness or experiment adapter. Jobs report
operation IDs, usage, logs, checkpoints, and output manifests.

An agent may propose additional work, but cannot execute beyond its grant.
When enabled, child tasks need bounded objectives, dependency/output contracts,
review owners, deadlines, narrowed permissions, and reserved parent budget.
Opaque fan-out is disabled unless equivalent enforcement is demonstrated and
the loss of child-level visibility is explicitly permitted.

### 2.4 Discuss, steer, and decide

Users address remote agents through the desktop partner, selecting a task,
finding, file, or evidence passage as context. Persistent threads outlive harness
sessions. Messages retain sender, recipient, selected context versions, intent,
and queued/delivered/acknowledged/answered state.

Questions do not implicitly change work. Instructions produce typed commands
with a revision and impact preview. The UI distinguishes requested, delivered,
applied, and confirmed effects. A batch-only harness queues messages to a supported
boundary rather than pretending to support live steering. Context handoff to a
different harness does not claim to transfer private session state.

An approval panel shows scope, cost, access, expiry, affected revisions, and the
safe default. Approval requires the eligible human session, not an agent tool
claim or quoted chat message. Silence never grants authority. Direction, spending,
sensitive access, and publication can require different reviewers.

Set a review owner, priority, and backlog limit. Deduplicate equivalent requests
and group notifications without merging approval scopes. When consequential
review capacity is exhausted, hold new dependent work or route to an eligible
backup; do not weaken the gate.

### 2.5 Stop, take over, and hand back

The user chooses whether to stop the agent session, its jobs, or both. Graceful
stop has a deadline; emergency revocation does not wait for a checkpoint.
Unconfirmed termination stays visible with unresolved cost exposure.

For human edits, block new agent writes and confirm existing writers are quiescent.
Continuing jobs must have immutable inputs or disjoint outputs; otherwise stop
them or create an isolated workspace. Unknown quiescence cannot authorize shared
writable takeover. The pilot can use confirmed stop → edit → new attempt.

Record human changes against the baseline. On handback, review the diff, assumptions,
environment, and external job state; issue a new revision/epoch and fresh context
before agent writes resume. Undo restores safe file versions only; it cannot
reverse charges, disclosures, publications, or physical actions.

### 2.6 Follow and revisit the labbook

The labbook answers: what happened, what supports this finding, what changed,
what is unresolved, and what needs human judgment?

Start with a timeline and finding cards, not a graph-editor project. Filter by
question, branch, activity, actor, method, time, and outcome. Finding cards link
sources, observations, opposing evidence, alternatives, excluded attempts, review
status, and cost. Filtered views are labeled as subsets.

Users can ask for a recap, inspect a historical interpretation, annotate evidence,
challenge a finding, or compare alternatives. Generated summaries cite versioned
records and remain distinct from human notes and raw evidence. Historical views
preserve what was known then while visibly linking later corrections.

Browsing or replaying history never executes work. A rerun, robustness check,
replication, or alternative branch creates a new proposal with differences, costs,
and approval requirements.

### 2.7 Review, correct, and export

Review separates executed work, observations, interpretations, and claims. Null
or inconclusive findings can be valid research outcomes; a successful process
exit does not establish scientific validity.

A correction to a source, dataset, observation, method, or skill creates an impact
set of dependent findings, summaries, and reports. Notify authorized owners and
mark affected work for review. Owners reaffirm, narrow, supersede, or retract with
reasons. Preserve historical versions; dependencies indicate possible impact,
not automatic falsity.

Export a consistent snapshot containing the plan, complete in-scope study ledger,
labbook, sources/assertions, manifests, code/environment references, assessments,
corrections, report, and reproduction instructions. Disclose missing or restricted
history without leaking it. A filtered export cannot claim completeness.

The optional publisher uses a separate identity to create a research-scoped PR.
It checks allowed paths, evidence, secrets, and content restrictions. It cannot
modify platform code, policy, or CI. External amendments require publication
approval; Tessera cannot erase copies already exported.

## 3. Architecture and ownership

    Desktop client / CLI / API clients
                      |
             Authenticated control API
                  Control plane
       policy | budget | tasks | conversations
       credentials | ledger | evidence | publication
                      |
            Authenticated fleet gateway
                      |
                 Execution plane
            Worker supervisor (trusted)
                 /              \
          Agent harness     Experiment adapter
                              local / remote jobs

The control plane is a separately installable/runnable backend with its own
lifecycle, configuration, identity and persistent storage. Electron connects as
a client even when both run on the same machine; its renderer or main process
must not own authoritative control-plane state. Start with a modular control
application and background workers, not one service per domain object.
Proposed storage is PostgreSQL for shared authority and object
storage for artifacts. Use a transactional outbox and deduplicating consumers.
SQLite/filesystem storage may serve isolated local mode, not multi-writer remote
synchronization. Choose the backend language before implementation.

| Boundary | Owns | Must not do |
|---|---|---|
| Electron renderer | Editors, chat, labbook, review and status | Hold secrets, execute shell, or decide authorization |
| Main/preload and local integration | Narrow OS/keychain integration, connection setup, optional service startup | Expose a generic privileged IPC bridge or tie control-plane lifetime to Electron |
| Control API | Revisions, decisions, grants, reservations, leases and command admission | Treat client state or model text as authoritative |
| Background modules | Scheduling, reconciliation, evidence checks, projections, publication | Retry uncertain external effects blindly |
| Fleet supervisor | Worker identity, isolation, tool mediation, expiry and control channel | Depend on a live agent session for cancellation |
| Harness adapter | Agent sessions, supported interaction, tool events and usage | Conceal unsupported steering, limits, or billing changes |
| Experiment adapter | Submit/query/cancel/account for provider operations | Claim exactly-once execution or universal hard cost caps |
| Evidence/labbook module | Ledger snapshots, manifests, assessments, notes and corrections | Let a generated narrative overwrite evidence |

Each project has one authoritative control-plane home. Workers initiate outbound
authenticated connections to that endpoint. A local-only loopback endpoint cannot
serve workers on another host: require an explicitly configured reachable HTTPS
endpoint or trusted private-network route. Do not silently expose laptop ports or
install a public relay. The desktop need not accept inbound worker connections.

Closing Electron leaves the independent backend and connected, authorized work
running. Sleeping the machine hosting the control plane makes it unavailable;
workers then obey bounded disconnect policies and expiring grants. The same rule
applies to a local process, Minikube, or an unavailable hosted deployment. Explicit
backend shutdown must warn about active work and offer drain/stop; it cannot report
unknown provider jobs as terminated. Offline edits are drafts, never approvals or
assumed cancellations. Reconnect uses snapshots/cursors and reconciles state.

### 3.1 Configurable execution and harness contract

Keep assistant host, harness, model/account, compute target, workload engine,
environment, and artifact destination separate in a versioned execution profile.
Spark can run on Kubernetes; Minikube is a local Kubernetes target, not an agent
harness. Capability checks must reject incompatible combinations before dispatch.

OpenCode is an optional, pinned harness implementation behind Tessera's harness
contract. It is not a required Tessera service and its sessions/jobs remain
independently managed by OpenCode. Other harnesses use the same contract. Personal
account authentication stays owner-bound; do not pool it for team workers.
Subscription usage, API charges, and compute costs are separate. Unknown allowance
is displayed as unknown; quota exhaustion never silently switches billing mode.

Harness adapters declare start/status/events/cancel and optional resume, steering,
approvals, skills, fan-out, and usage support. Compute adapters declare preflight,
submit/status/logs/cancel/accounting, with provider IDs and checkpoint limitations.
Provisioning infrastructure is a separate approval from attaching a target.
Pin tested versions; use conformance tests to grow the support matrix.

The adapter translates Tessera's plan, approved inputs, lease/epoch, capability
limits, and cancellation request into the pinned harness interface, and translates
responses into attributed observations and events. Every translated event carries
the Tessera organization/project/task, operation, lease epoch, harness instance,
and upstream session/job identifiers; unknown or unauthenticated events are
quarantined. Tessera remains authoritative for plans, approvals, budgets,
leases/epochs, credentials/access, artifacts, provider operations, result
acceptance, and the labbook. OpenCode permission prompts, tools, MCP, skills,
session events, provider authentication, and UI are not Tessera authority
boundaries. A harness acknowledgement never proves provider termination, artifact
acceptance, budget settlement, or scientific result acceptance.

OpenCode's current external capabilities include a headless HTTP server with an
[OpenAPI endpoint](https://opencode.ai/docs/server/), a typed [JavaScript/TypeScript
SDK](https://opencode.ai/docs/sdk/), [SSE events](https://opencode.ai/docs/server/#events),
session/message/abort/permission APIs, and documented [skills](https://opencode.ai/docs/skills/)
and [MCP servers](https://opencode.ai/docs/mcp-servers/). It also documents
[ACP support](https://opencode.ai/docs/acp/) and is released under the [MIT
license](https://github.com/anomalyco/opencode/blob/dev/LICENSE). These are current
upstream capabilities, not Tessera guarantees; the pinned adapter must pass
compatibility and security tests before any capability is enabled.

### 3.2 Node setup and enrollment

Node setup is infrastructure administration, separate from research control. An
operator installs the approved worker supervisor, harness build, experiment
adapter, isolation profile, and pinned OpenCode build on a node. Enrollment
records node identity, owner/tenant, endpoint, trust class, versions, capabilities,
credential references, and recovery policy. It issues a scoped workload identity
and binds it to the control plane; it does not copy a human login or grant a
research plan. Re-enrollment after restore, replacement, or suspected compromise
requires fresh credentials and invalidates the old node epoch.

Node health, upgrades, drain, quarantine, and retirement are independently
managed lifecycle operations. Research control may admit or revoke a lease, but
must not infer node trust from OpenCode's UI, session state, permission prompt,
MCP response, or provider login. Conversely, node enrollment does not approve a
plan, reserve a budget, authorize data access, accept an artifact, or accept a
result.

### 3.3 Control-plane deployment and connections

| Mode | Operator and responsibility | Identity and secrets |
|---|---|---|
| Personal | Researcher runs backend/storage as processes or on a local cluster; owns uptime and backups | Explicit local enrollment, owner-bound credentials, local keychain or configured secrets service |
| Organization-managed | Organization operates backend, storage, availability, upgrades and recovery on its infrastructure | Organization identity provider/membership; organization-owned secrets and workload identities |
| Tessera-hosted | Planned paid service operates backend, isolation, upgrades and recovery under declared service terms | Hosted organization membership or federated login; scoped managed secrets or an approved customer-backend connection |

Use the same versioned domain/worker APIs and policy semantics across modes.
Hosting does not imply hosting experiment compute or transferring ownership of
customer credentials. A customer secrets backend must be reachable through an
explicitly approved integration, not assumed accessible from the hosted service.
Control-plane hosting charges are separate from model/API and compute charges.

Organizations are logical security boundaries across database rows, artifacts,
queues, events, conversations, secret references and workers. Shared deployments
must enforce tenant scope throughout. A dedicated deployment may additionally
separate processes, database/storage and secret-backend paths; Kubernetes namespaces
can assist placement but do not alone establish isolation. Organization users
cannot administer shared backend processes merely by owning a project.

A desktop connection stores endpoint, control-plane identity, organization,
authenticated account and API compatibility. Keep credentials, caches, event
cursors and pending commands scoped to that connection. Switching accounts or
endpoints must not replay queued mutations or attach context to another project.
Start with one active connection; multiple saved connections use the same rules.

Project migration is an explicit future export/import or ownership-transfer
workflow with worker, credential and operation reconciliation. Selecting another
connection is not migration. Cross-control-plane synchronization/federation is
out of pilot scope; do not clone active grants into a second authority.

### 3.4 Authentication contract

Authenticate local and network deployments alike. Personal setup uses a one-time
enrollment flow and owner-scoped session credentials stored outside the renderer;
do not trust a caller just because it reaches localhost. Bind the default personal
service to loopback and validate browser origins on browser-accessible endpoints.
Non-loopback deployment requires encrypted transport and explicit server trust.

Organization/hosted login uses a system-browser identity-provider flow and scoped,
expiring API sessions. Validate identity issuer, intended audience and membership;
the exact federation provider/protocol is an implementation decision. Worker
enrollment issues separate workload identity, not a reusable copy of a human login.
API/CLI clients use scoped identities; human-only approval requires a human session.
Control-plane credentials cannot be reused as model-provider or compute credentials.

Apply logout, account removal and membership revocation to session renewal, caches,
streams and pending decisions. Already running work follows grant ownership,
revocation and expiry policy; logging out of the desktop is not implicitly a
project-wide cancellation. Check authorization again on reconnect and renewal.

## 4. Data and API contracts

| Record group | Required purpose |
|---|---|
| Project, charter, plan/activity/task revision | Research intent and versioned execution scope; an experiment is one activity type |
| Workspace session, source snapshot, adoption manifest | Lightweight local authority, selected inputs and honest imported history |
| Assistant definition/instance, worker, skill release, execution profile | Responsibility, admitted identity, compatibility and pinned instructions |
| Decision, delegation/resource grant, lease | Who authorized what, for whom, until when, under which restrictions |
| Task attempt, external operation, execution receipt | Actual work, side effects, provider state, usage and provenance |
| Source assertion, observation, artifact, claim, assessment | Evidence distinct from interpretation; method-specific validation |
| Study ledger, labbook entry, correction impact | Complete in-scope accounting, readable history and downstream review |
| Conversation/message, workspace handoff, publication | Attributed interaction, editing ownership and controlled export |

Use tenant-scoped IDs and constraints from the start. Domain records and revisions
are authoritative; search, embeddings, summaries, notifications, and dashboards
are derived views with the same access restrictions.

Mutations carry an idempotency key and expected revision. Commit state, audit,
and outbox event in one transaction. Reusing a key with different content is a
conflict. Consumers deduplicate deliveries; ordering is per aggregate, with project
replay cursors, not one global order across providers.

Events contain schema/version, actor and delegated actor, resource revision,
correlation/causation IDs, timestamp, and policy decision reference. Commands,
decisions, usage, and terminal events are durable. Bounded logs may drop chunks
only with visible gap markers. Keep audit payloads minimal and secret-free.

Implement typed operations rather than unrestricted record mutation:
create/adopt project; revise plan; approve decision; issue/revoke grant; lease task;
submit/reconcile operation; checkpoint/commit result; request cancel; request
takeover/confirm handback; register candidate/close study snapshot; assess evidence;
propose correction; export/publish. Each defines source states, permission,
preconditions, transactional effects, external effects, and retry behavior.
Return command IDs; expose desired and observed state separately.

Read APIs cover projects, threads, tasks, operations, skills, labbook, ledger
snapshots, findings, artifacts, and correction impacts. Historical queries are
read-only. Object uploads are resumable and quarantined until size/checksum and
manifest validation pass. Missing or corrupted artifacts cannot support accepted
claims. Short-lived object URLs are scoped bearer access; use an authenticated
proxy where immediate revocation is required.

## 5. Authorization, secrets, and client safety

Evaluate every privileged operation as principal + action + resource + context.
Humans, desktop partners, remote agents, supervisors, jobs, evaluators, publishers,
and integrations have distinct identities. Roles supply defaults, not unlimited
authority. Separate read/edit/execute/delegate/approve/publish/administer.

Effective authority is the intersection of current organizational restrictions,
principal/resource eligibility, approved session/task scope, and active parent
grants. Lower scopes cannot weaken an explicit deny. Policy relaxation does not
expand existing grants; restrictions block new operations/renewals and initiate
revocation of active work. Offline revocation is bounded by expiry, not instantaneous.

A grant includes issuer, subject, parent lineage, actions/resources, purpose,
audience, revision, epoch, expiry, budget, and delegation depth. Children cannot
widen or outlive parents. Enforce at API, scheduler, tool/credential broker,
supervisor, data/retrieval, and publication boundaries. Critical operations
recheck policy; caches have bounded expiry and invalidation.

A local session grant names selected roots, actions/tools, model destinations,
classifications, time and cost limits. Remote execution requires the approved
minimal plan or study plan plus an active lease. Chat can propose either, not
grant it. The UI exposes effective permissions, denial reasons, and bounded access
requests. Viewing, using, rotating, and granting a secret are separate permissions.

### 5.1 Credentials and data transfer

No secrets vendor is selected. The broker stores owner-scoped references and
exchanges authenticated workload identity for narrowly scoped, short-lived access
where the provider supports it. Prefer tool proxies/handles over revealing values.
Record lease metadata, parent grant, audience, expiry, renewal and revocation state.
Renew only under current authority. Revocation, expiry, cancellation, and incidents
stop renewal and invoke backend revocation where supported.

Static keys and persistent personal logins do not become short-lived credentials
because they are in a secret store. Keep them in trusted owner-bound custody.
If raw credentials must enter an authorized process, disclose and isolate the
exposure. Redaction is defense in depth. Reject execution profiles that cannot
meet the task's credential or network requirements.

Local selection creates a classified source snapshot before external transfer.
Check destination/recipient authorization and required secret, sensitive-data,
and license restrictions inside an approved trust boundary. Unknown classification
blocks external transfer until resolved. Scanning helps but cannot prove absence
of sensitive data.

Show the transferred manifest, exclusions, destination, and retention boundary.
Journal transfer as an authorized operation; verify checksums and detect changed
files. No hidden bidirectional mirroring. Resolve symlinks and archive paths
against approved roots. Apply these rules to model context as well as worker
uploads. Derived notes, summaries, embeddings, and notifications inherit source
restrictions until an authorized release decision.

### 5.2 Isolation and operational access

Treat sources, skills, peer messages, tool outputs, and artifacts as untrusted.
MCP/A2A are transport integrations, not authorization boundaries. Validate tool
arguments and destinations, including redirects, private networks, metadata
endpoints, arbitrary uploads, and shell escape paths. Sandboxes must not expose
host sockets, broad credentials, or unapproved filesystem roots.

Worker enrollment requires an authenticated owner/admin and bounded bootstrap.
Record approved supervisor/harness builds, environment provenance, trust class,
region and capabilities; probe capabilities instead of trusting self-description.
Use ready/draining/disconnected/quarantined/retired states. Drain blocks new leases;
retirement revokes identity and reconciles jobs and cleanup. A personal worker
cannot protect secrets from its own administrator; sensitive tasks need a suitable
trust boundary.

Electron disables renderer Node access and uses sandboxing, context isolation,
CSP, restricted navigation, and sender-validated typed IPC. Untrusted artifact
previews cannot execute privileged code. Keep credentials out of the renderer.
Sign updates and stage rollout. Client caches are identity/project-scoped and
invalidated on access changes; revocation cannot erase information already seen.

Audit privileged allow/deny decisions. Time-limited break-glass access requires
justification and alerts; it cannot silently satisfy scientific review. Protect
operator dashboards and diagnostic logs with the same tenant rules as product data.

## 6. Execution, cost, and recovery

### 6.1 Attempts and side effects

Freeze an execution package: input/code/environment digests, method, skills,
harness/profile versions, outputs, validation, allowed adaptations, grant, expected
and maximum exposure, checkpoints, and stop rules. Fields can be inapplicable to
non-computational activities; do not fabricate compute receipts for observations.

Attempt progression is admitted → preparing → running → collecting → validating
→ succeeded. Waiting/checkpointed are explicit states; failure, cancellation,
supersession and expiry are terminal outcomes. Scientific null results may have
successful execution. Uncertain external effects require reconciliation before
replacement where duplication could violate cost or safety limits.

Persist each external-operation intent before submission, with stable idempotency
key, request digest, provider ID when known, grant epoch, reservation, retry class,
and last observation. Lost responses trigger lookup/reconciliation, not a new
submission identity. Exactly-once external execution is not promised.

Retry classes: safe repeat, provider-deduplicated, compensatable, or manual
reconciliation. Bound attempt count, backoff/jitter, elapsed time and total cost.
An intended scientific replication is new planned work, not a transport retry.
Never blindly repeat an uncertain physical procedure or purchase.

Lease/grant epochs increase monotonically within their authority lineage. Reject
older epochs on starts, renewals, and authoritative result commits; reassignment
or handback must not reuse an earlier epoch.
Result acceptance is conditional on revision, lease/epoch, attempt state, and
validated manifest. Late outputs remain quarantined history, not replacement
success. Fencing cannot stop a running provider job by itself.

### 6.2 Budgets, stops, and telemetry

Atomically reserve against project and parent limits before admitting spend.
Track reserved, incurred, estimated-unbilled, settled, released, and disputed usage
in explicit currency/units. Deduplicate internal ledger deliveries, not real
duplicate provider charges. Record minimum charges, cancellation delay, storage,
egress and unbounded/unknown exposure. Reject hard-cap work a provider cannot
bound, or require explicit approval of the disclosed exposure.

Limit concurrency, fan-out, model/tool calls, retries, duration, compute and storage.
Keep interactive replies and emergency controls from being starved by experiment
queues. Stop for achieved criteria, insufficient/no-progress evidence, repeated
failure, no feasible next step, limits, policy, or human direction. Method-specific
rules assess information value; model self-confidence is not a stopping criterion.
Do not optimize for favorable results, report count, or agent activity.

Cancellation records independent milestones: accepted, delivered, new actions
fenced, writers quiesced, provider termination confirmed, credentials revoked or
expired, workspace disposition, and charges reconciled. Graceful deadlines
escalate to termination where supported. Unknown termination remains an incident
with retained exposure, not a green “stopped” badge.

The current bounded control projection records only cancellation requested,
unresolved/unknown, and worker-confirmed termination from immutable attributed
observations. It does not infer provider termination, writer quiescence, charges,
or scientific success from a local terminal state.

Operational claims in chat attach timestamped control/supervisor/provider
observations and freshness limits. Authoritative status wins over conflicting
agent prose. Checkpoint/resume needs compatible inputs/environment, saved method
state where relevant, reconciled prior ownership and fresh authority. Label a
restart as a restart.

| Failure | Required response |
|---|---|
| Desktop closes or loses connection; control plane stays available | Continue eligible work; persist decisions and replay state on reconnect |
| Control-plane host sleeps, stops, or becomes unreachable | Workers follow bounded disconnect/expiry rules; preserve unknown job state and reconcile on recovery |
| Worker loses control connection | No grant expansion; obey bounded disconnect/expiry policy |
| Agent crashes | Supervisor survives and reconciles jobs before authorized restart |
| Submission reply is lost | Query provider using recorded operation identity; hold unsafe replacement |
| Control crashes after database commit | Replay outbox; deduplicate consumer effects |
| Revision races with completion | Conditional acceptance preserves histories and rejects stale authority |
| Policy/credential broker is unavailable | Fail closed for new privileged work; existing work obeys expiry |
| Artifacts are incomplete or invalid | Quarantine/resume upload; block evidence acceptance |
| Worker compromise or unexplained spend | Quarantine, revoke descendants, reconcile jobs, preserve forensic metadata |

## 7. Scientific integrity and labbook

The study ledger records registered candidates, attempts, adaptations, exclusions,
and closure. Populate it from committed control transitions, not a reporter's
chosen runs. Register new variants before execution; record reasons for failed,
canceled, abandoned, excluded and superseded work.

Closure reconciles all registered work and operations to terminal or explicitly
unresolved states. Claim review/export references an immutable ledger snapshot.
Confirmatory claims must satisfy their protocol's closure and exclusion checks;
exploratory reports disclose adaptive selection and incomplete work. Completeness
covers registered mediated work, not unknowable off-platform experiments. Opaque
tools must supply an analysis manifest or expose an accounting limitation.

The labbook is a permission-filtered projection of the ledger and versioned
evidence, plus attributed notes. It is not a second source of task authority.
Store event/record IDs, actor, event and recorded times, revisions, provenance,
and access labels. Show projection lag. Corrections append records and propagate
review flags through dependencies; historical findings and summaries are not
silently rewritten.

A scientific protocol selects required controls: hypotheses or questions, baselines,
method/environment locks, uncertainty and multiple-comparison handling, evaluation
isolation, replication, source support, data rights, and review. Keep study mode,
design, provenance, replication and review as separate assessments. A signed
receipt establishes attribution under a worker's trust assumptions, not truth.

Accepted claims cite validated execution outputs, assessed observations, or
supported source assertions. Agent prose and a file hash alone cannot certify
execution, a metric, or a finding.

Source assertions need identifier/version, passage locator, retrieval date,
permitted excerpt or snapshot/hash, extraction method and support assessment.
Separate author claims, observed data, secondary summaries and agent inference.
Unsupported assertions cannot become accepted evidence where source support is
required; speculative ideas remain labeled. Respect source licensing.

An independence profile declares required and observed differences/overlap across
data, implementation, preprocessing, measurement/metrics, analysis, evaluator
context, human review and infrastructure trust where relevant. Distinguish rerun,
robustness check, same-pipeline replication and independent replication under a
named profile. Different seeds, workers or models alone do not establish it.

Protected evaluation uses separate identities/storage and controls outputs,
query/reveal budgets, and hidden context. Domain-based human measurements require
sample identity, operator, procedure revision, raw-record linkage, timing, custody,
instrument/calibration where applicable, and review state. Low-risk annotations
use lighter schemas; required missing checks hold dependent confirmatory work.

## 8. Implementation sequence and acceptance

The table below is the target sequence, not a completion checklist. Current
completion evidence and outstanding work live in the
[status matrix](docs/implementation-progress.md#design-status-matrix).
A component passing synthetic tests does not complete a workflow or the pilot.
Update that tracker with every behavior-changing implementation checkpoint.

Implement vertical slices through the modules, not complete horizontal platforms.
These are the pilot's engineering tasks; each produces code, tests and a short
operational note. No paid or sensitive run precedes its required enforcement.

| Step | Deliverable | Acceptance evidence |
|---|---|---|
| 1. Contracts and harness spike | Independently runnable process-based control plane, domain states, selected execution profile, mediated harness and CPU adapter; decide backend stack | Same authenticated API works co-located and over a trusted network; demonstrate tool/network limits, ownership, cancellation and cost boundaries |
| 2. Durable execution | Control API/store, enrollment/session auth, policy grants, reservations, supervisor, operation journal and broker | Unauthenticated/expired/stale grants denied; duplicate delivery safe; lost submission reply reconciled; backend survives Electron exit |
| 3. Local workspace and adoption | Named connection/login, desktop isolation, scoped files/snapshots, read-only inventory, minimal-plan approval, skill editor/test/release | No cross-connection context or credential reuse, path escape or unclassified upload; user edits and imported unknowns preserved; worker loads pinned skill |
| 4. Interaction and handoff | Persistent threads, contextual questions, typed revisions, inbox, telemetry, stop/edit/handback | Agent prose cannot approve or confirm a stop; queue saturation holds dependent work; conflicting writers blocked; handback gets fresh context |
| 5. Labbook and evidence | Timeline/findings, complete in-scope ledger, source support, method labels, corrections and export | All variants visible; incomplete history disclosed; source support and replication labels checked; corrections trace downstream; history browsing executes nothing |
| 6. Recovery and pilot | Fault tests, signed packaging/update path, backup/restore, runbooks, measured real investigation | Restore fences old grants; unknown jobs/spend alert; compare quality and total human effort with existing workflow |

### Harness proof of concept (synthetic, not pilot completion)

Before integrating OpenCode into a pilot workflow, run a small synthetic POC with
one pinned OpenCode server, one enrolled node, one Tessera task, and no real
provider call or sensitive data. The adapter must prove that it can create and
attribute a session, translate a bounded message and event stream, reconnect from
a cursor, and request cancellation while Tessera retains authority. OpenCode
session/job state may continue independently; Tessera records the uncertainty and
reconciles it rather than inventing termination.

Acceptance cases: (1) a plan revision or expired lease prevents session/job
start; (2) a permission, tool, MCP, or provider-auth event is recorded as an
upstream observation and cannot widen access or budget; (3) duplicate/reordered
SSE events are idempotently attributed; (4) disconnect and reconnect preserve
the same operation and cursor; and (5) cancellation records requested,
confirmed, or unresolved status without accepting output or result.

Failure cases: reject an unpinned or incompatible OpenCode version; quarantine an
event with the wrong tenant, operation, epoch, or unknown session; reject a
redirected/unauthenticated endpoint; deny a prompt that exceeds the Tessera
grant; and leave provider termination and costs unresolved when abort has no
authoritative confirmation. Passing this POC demonstrates adapter conformance
only; it does not complete execution, artifact release, result acceptance, or
the pilot.

Required adversarial fixtures:

- Close Electron while the backend remains running, then separately stop the
  control plane or simulate its host sleeping. Verify continued execution in the
  first case and bounded disconnect behavior/reconciliation in the second.
- Reject unauthenticated loopback callers, wrong-audience tokens, and cross-tenant
  reads of artifacts, secrets, events and tasks. Revoke membership during a stream;
  prevent subsequent unauthorized delivery and renewal.
- Switch between two test control-plane connections with queued commands and cached
  context. Neither credentials, context nor pending mutations may cross the boundary.
- Run 80 registered variants, select three favorable results, and verify export
  still accounts for the other 77 and any exclusions.
- Supply a citation that does not support its assertion; block acceptance rather
  than treating the URL as verification.
- Rerun the same faulty pipeline on another worker; deny the independent label.
- Correct a used skill or observation; flag dependent findings without rewriting
  them or leaking restricted content in notifications.
- Lose a submit reply, disconnect a worker, and race a revision with completion;
  preserve both cost exposure and authoritative result ownership.
- Ask an agent to self-approve, access another project's data, widen a child grant,
  or leak a secret through an artifact; enforcement must deny the action.
- Saturate the decision inbox and request human takeover during active writes;
  neither condition may bypass authorization or quiescence.
- Restore control state from backup while old workers exist; invalidate previous
  authority and reconcile provider operations before admitting replacement work.

Before enabling additional scientific methods, add their protocol/observation
checks and an unrelated acceptance fixture. Human/instrument procedures and
stronger replication claims stay unavailable until their requirements are tested.

## 9. Operations and product gates

Use separate development, staging and production credentials. Pin dependencies,
scan executable packages, verify releases, canary worker updates, drain safely,
and document rollback. Schema/events need compatible migrations and a supported
client/worker version window; reject incompatible dispatch.

Define per-profile heartbeat/disconnect bounds, credential TTL, cancellation
timeouts, usage-reconciliation lag, queue limits and retention before admission.
Define hosted availability, recovery-point and recovery-time targets before a
hosted pilot; demonstrate restore of metadata, objects and key access. Reconcile
jobs and invalidate old grants after restore. Do not publish universal guarantees
that provider behavior cannot support.

Monitor command lag, queue/review age, orphan jobs, expiry, unresolved spend,
artifact failures and evidence coverage. Correlate user decisions to attempts
and provider IDs. Logs are bounded, redacted and access-controlled; do not collect
prompts/source text by default. Track human effort through disclosed measurement.

Retention covers inputs, artifacts, conversations, notes, audit metadata, indexes
and backups separately. Deletion invalidates future retrieval and caches subject
to disclosed holds/backup expiry; minimal audit tombstones preserve history without
retaining unnecessary sensitive payloads. Published Git/model/provider copies have
separate retention boundaries disclosed before export. Runbooks cover compromised
credentials, runaway spend, orphan jobs, provider outage and claim correction.

Before coding, record these decisions: backend language/framework; control-service
packaging and local service lifecycle; reachable-endpoint/transport configuration;
first harness/compute/profile versions; isolation threat model; credential backend,
personal enrollment and future organization federation; and database orchestration versus a
workflow engine. Record alternatives, feasibility evidence and rollback. They
must not remain hidden assumptions in adapters.

Before the customer pilot, agree on representative tasks, baseline workflow,
quality criteria, measurement method and success/failure thresholds. Include
onboarding, troubleshooting, review, verification, model/tools/compute cost,
ability to revisit findings, and repeat use without coaching. Identify the user,
buyer and infrastructure approver. No willingness-to-pay claim follows from a demo.

After the pilot, record continue, narrow, or stop:

- If execution is valuable but the labbook/app is not used, prioritize API/tools.
- If one agent plus scripts wins on quality and effort, leave fan-out optional.
- If portability is unused, maintain one supported deployment recipe.
- If review costs exceed saved effort, improve evidence presentation and reduce
  autonomous scope before adding throughput.
- If no recurring job shows value, pause expansion rather than finish the catalog.

## 10. Design references

These sources informed earlier reviews; they are not proof of product demand or
implemented guarantees. Recheck provider interfaces and authentication before
implementation. Research results are bounded, not universal scientific validation.

- [Electron security](https://www.electronjs.org/docs/latest/tutorial/security):
  desktop isolation and IPC boundaries.
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
  and [Temporal activities](https://docs.temporal.io/activity-definition):
  retries require application/provider idempotency.
- [Vault leases](https://developer.hashicorp.com/vault/docs/concepts/lease) and
  [SPIFFE](https://spiffe.io/docs/latest/spiffe-about/overview/): examples of
  credential lifecycle and workload identity, not selected vendors.
- [OpenCode server](https://opencode.ai/docs/server/), [OpenCode SDK](https://opencode.ai/docs/sdk/),
  [OpenCode ACP](https://opencode.ai/docs/acp/), and [OpenCode license](https://github.com/anomalyco/opencode/blob/dev/LICENSE):
  current external capabilities for the optional harness, subject to compatibility testing.
- [Skills](https://learn.chatgpt.com/docs/build-skills),
  [Codex App Server](https://learn.chatgpt.com/docs/app-server), and
  [authentication](https://learn.chatgpt.com/docs/auth): other candidate harness interfaces.
- [MAST](https://arxiv.org/abs/2503.13657) and
  [AgentDojo](https://arxiv.org/abs/2406.13352): coordination/verification failures
  and adversarial tool inputs motivate external enforcement and evaluation.
- [Adaptive defense evaluation](https://arxiv.org/abs/2606.26479) and
  [real-world Co-Scientist research](https://arxiv.org/abs/2608.26701):
  promising but limited evidence for agent security and closed-loop research.
- [Claude Science](https://www.anthropic.com/news/claude-science-ai-workbench) and
  [Microsoft Discovery](https://learn.microsoft.com/en-us/azure/microsoft-discovery/overview-what-is-microsoft-discovery):
  substantial feature overlap; app, skills, and remote execution alone do not
  establish differentiation.
