# Tessera design

Status: implementation contract and roadmap; the full pilot is not implemented.
The control plane, bounded CPU execution components, desktop research records,
and reasoned finding review are implemented in slices. See the
[implementation status matrix](docs/implementation-progress.md#design-status-matrix)
for what is implemented, partial, blocked, or deferred, with remaining acceptance
work. Component guarantees do not constitute pilot completion. The latest bounded
installed-candidate and native-slice evidence, together with its synthetic inputs
and unresolved acceptance, is summarized in the
[release evidence note](docs/release-evidence-intent9.md). README.md describes the
runnable scope; the release checklist tracks the remaining release gates.
This document defines the pilot, the contracts it must satisfy, and the order of
implementation. “Required” describes intended behavior, not a verified guarantee.

## 1. Product and scope

Tessera's differentiator is a durable path from a research goal through bounded
experiment design and remote execution to linked evidence and a human decision.
It lets a researcher direct local and remote agents, run bounded experiments, and
follow the investigation in a labbook. The desktop agent helps plan work and
communicates with remote researchers or lab technicians. The researcher retains
control of direction, access, cost, methodology, and publication.

Desktop, control plane, and execution plane are independently deployable.
“Remote” means outside Electron, not necessarily on another machine. Network
placement does not change authorization requirements.

The first user is a hands-on researcher or research engineer with an existing
script/notebook investigation and an available execution host, local or elsewhere.
The first workflow is: state a goal, establish a baseline, design one bounded
intervention, execute it on an approved target that need not be the laptop, link
the resulting evidence, and make a human decision. Adoption and skills can support
that path; they are not its substitute.

This is a computational-research pilot with a domain-neutral evidence model.
Literature, theory, observation, analysis, and experiments must not all be modeled
as GPU jobs or numerical hypothesis tests. A scientific method becomes supported
when its protocol, evidence checks, and acceptance fixture exist.

| Pilot: build now | Next: after pilot validation | Later: demand-driven |
|---|---|---|
| One user, desktop partner, independently runnable remote control plane and trusted supervisor | Second harness and compute adapter to test portability | Organization/team administration, shared registries, federation |
| Local CPU development target and one parallel remote CPU target | GPU target; Kubernetes adapter tested locally with Minikube and remotely | Spark and other workload engines, broader provider coverage |
| Plan/access/cost approval, status, cancellation, stop/edit/handback | Bounded multi-agent task discovery/delegation | Schedules, automatic memory, broad connector/plugin catalogs |
| Labbook, evidence review, existing-work adoption, portable export | Research branches/comparisons, richer method checks and tracker imports | Advanced graph editors, domain viewers, browser/computer automation |
| Tenant-scoped records and permission checks from the start | Optional research-scoped GitHub publisher | Direct instrument/robot control, subject to separate safety design |
| Process-based control plane with authenticated desktop/worker APIs | Local-cluster packaging (including Minikube) and organization identity integration | Production organization-managed and paid Tessera-hosted offerings |

No autonomous budget increases, public release, or policy relaxation. Do not build
a scholarly index, general workflow engine, GPU scheduler, full experiment tracker,
or a feature-for-feature replacement for existing desktop agents.

MLflow, Databricks, Spark, and similar systems may be useful integrations for
execution, tracking, or data processing. They are not Tessera's product authority:
Tessera owns the goal-to-decision record, approvals, bounded attempts, evidence
links, and human conclusions. Until the thin vertical slice in §8 has accepted
evidence, freeze broad provisioning/cloud work, skill-catalog expansion, and
desktop expansion; only work required to complete that slice or preserve existing
evidence should proceed.

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

#### Remote agent execution routing

Agent experiment routing is remote-only: explicit `kubernetes` or
`salad_container`, with `salad_inference` reserved and non-executable. The desktop
is control-only; desktop/local-process and local CPU aliases cannot be selected
by this route. Local CPU/Docker profiles below remain explicit development and
conformance fixtures, not fallbacks. The [offline implementation](docs/remote-agent-execution.md)
adds exact human-reviewed tenant/plan/attempt/package/epoch/manifest-bound target
selection, durable one-shot intents and provider-neutral receipts. It does not
activate live Kubernetes enforcement or Salad effects. See
[decision 0023](docs/decisions/0023-remote-agent-execution-routing.md).

#### Historical development execution-target direction

The opt-in [direct Agent Sandbox adapter](docs/agent-sandbox.md) implements a
one-shot `agent-sandbox` network-none plan using existing grants, leases,
operation intents and artifact custody. It creates upstream suspended/gated
Sandboxes, fences release to one Pod UID, retains PVCs on stop/uncertainty and
separately gates cleanup on placed artifacts. Live isolation/admission witnesses
and a verified operator target remain prerequisites, not available flags. This
does not replace batch Jobs or enable a general networked agent session; see
[decision 0030](docs/decisions/0030-direct-agent-sandbox.md).

`local-cpu` remains an explicit development fixture target. Kubernetes preparation
may proceed in parallel, but does not promote a cluster profile to supported or
live execution. A future execution profile composes an `EnvironmentAdapter`
(target identity, preflight, provisioning/attachment boundary, and credential
delivery) with an `ExecutionAdapter` (submit, observe, logs, cancel, and
accounting). Both contracts must preserve the frozen package, operation identity,
lease epoch, and explicit limitations; neither adapter may infer authority from a
client, environment variable, or ambient Kubernetes context.

Remote CPU execution and a remotely reachable control plane are first-class pilot
requirements, not a later portability feature. Local CPU remains the primary
development target, while one explicitly identified remote target proceeds in
parallel. Neither target is promoted by a desktop-connected demonstration: the
control plane and approved work must continue within their bounded disconnect
policies when the desktop disconnects.

Every Kubernetes action names and validates the target identity (kubeconfig
reference and digest, context, HTTPS API server and CA identity, cluster and
namespace identities). No current-context fallback is permitted. Minikube
bootstrap is a separate local-administration workflow from attaching an already
identified target to a Tessera execution profile. Control-plane provisioning is
also separate: a scoped operator may reconcile only an approved immutable
provisioning plan and its named resources. A human approves the exact plan digest
before it is applied; a research-plan approval, worker, harness, or skill cannot
approve it. No automatic destructive teardown is authorized; deletion requires a
separate explicit human decision and remains planned.

Minikube and Docker Desktop authenticated development conformance are not
interchangeable target identities: the opt-in Docker Desktop fixture uses the
existing admitted provider-disabled package and outbox, a fixed pinned local
image and exact cluster/namespace UIDs. Its terminal Job/Pod observation is
operational evidence only; cleanup acknowledgement is not namespace absence,
and neither promotes the fixture to supported remote execution or scientific
success.

The constant-compute pilot narrows this contract to an operator-provisioned
per-organization/project execution namespace, separate from control storage,
restricted Pod admission and fail-closed Job/Pod policies. Named read-only
cluster identity permissions do not authorize cluster administration. Policy
readback and authorization dry runs are not NetworkPolicy or live execution
evidence; see [decision 0017](docs/decisions/0017-pilot-workload-isolation.md).

Credentials use pluggable store and delivery contracts. A local environment source
is planned only for explicitly authorized local delivery. Kubernetes Secret
materialization is planned only after an explicit human authorization bound to the
target, namespace, task, lease, and epoch; it must not use ambient `kubectl` or
cloud credentials. Skills may plan or request scoped access, but cannot approve a
plan/provisioning/secret operation or use raw `kubectl` or cloud credentials. See
[decisions 0010](docs/decisions/0010-execution-adapter-composition.md),
[0011](docs/decisions/0011-control-plane-kubernetes-provisioning.md), and
[0012](docs/decisions/0012-credential-delivery-boundary.md).

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

#### Stage 1 development backend profile

The first executable backend profile is explicitly opt-in
`development-local-cpu-v1`: one native, one-shot worker consumes an already
human-issued grant with a private project-scoped worker credential file. It
heartbeats a previously owner-registered worker before lease acquisition,
verifies the frozen `local-cpu-v1` package and local pinned Docker identity,
and uses the existing supervisor/journal. It persists renewal intent keys
before calls and replays them after uncertain responses. It can report running,
unknown, or worker-confirmed exited observations, then seal/upload only declared
outputs into quarantine before reclaiming its marked development workspace.
It cannot self-register, grant/approve/reconcile, release artifacts, commit a
result checkpoint, or claim provider/scientific success. This is not Minikube,
persistent-output admission, or a pilot profile; Linux hard quota and restart
evidence remain required.

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

### 4.1 Lifetime boundaries

Do not collapse these lifetimes, even when a first slice represents them with a
small number of records:

| Lifetime | Meaning | Must not imply |
|---|---|---|
| Session | A human or harness interaction context; it can end on disconnect | Experiment completion, attempt termination, or a conclusion |
| Experiment | The bounded goal, baseline/intervention design, and registered scope | A particular execution or a successful result |
| Attempt | One admitted execution of an experiment revision | One external submission or accepted evidence |
| External operation | A provider/worker side effect with its own identity and reconciliation | Attempt success, artifact validity, or provider settlement |
| Artifact or measurement | Retained output or observation with provenance and validation state | Interpretation, scientific validity, or a conclusion |
| Conclusion | A human-reviewed interpretation linked to evidence | A live session, rerunnable operation, or immutable truth |

An experiment may have multiple attempts, an attempt may require multiple external
operations, and artifacts/measurements may outlive both. Conclusions remain
versioned review records: they can be narrowed, superseded, or retracted without
rewriting the underlying evidence.

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
manifest validation pass. Schema v22 persists validated development output to
private project storage through stage/commit/reconcile; worker slots are not
project storage. Placement is not classification, release, result/finding
acceptance, cost settlement, or provider/scientific success. Missing or corrupted artifacts cannot support accepted
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

[Decision 0025](docs/decisions/0025-administration-workspace.md) adds a project-scoped
administrative metadata workspace, synthetic-only secret lifecycle and exact
administration-only delegation. Configuration review and local binding verification
cannot activate providers or alter deployed policy. [Decision 0026](docs/decisions/0026-protected-secret-enrollment.md)
adds one-shot protected native macOS Keychain enrollment for a same-host control
database, with independent human review, versioned references and crash-safe claims.
No secret value enters the renderer or HTTP API; remote/Windows/Linux enrollment,
organization invitations and workload delegation remain unavailable. Principal-level
review with local bearer credentials is not proof of independent human identity.
The [administration runbook](docs/admin-setup.md) describes the enforced scope,
transactional operator-ledger state, held recovery and remaining enablement gates.

No remote secrets vendor is selected. The broker stores owner-scoped references and
exchanges authenticated workload identity for narrowly scoped, short-lived access
where the provider supports it. Prefer tool proxies/handles over revealing values.
Record lease metadata, parent grant, audience, expiry, renewal and revocation state.
Renew only under current authority. Revocation, expiry, cancellation, and incidents
stop renewal and invoke backend revocation where supported.

Credential-access leases are distinct from Tessera human, agent, and worker
authentication credentials. A human binds each one to the current organization,
project, task, grant, execution lease, worker, and epoch; its opaque secret
reference, provider-neutral audience/purpose/scope, and expiry cannot widen or
outlive that parent lease. A worker derives its identity from its workload
credential and rechecks every binding before receiving only a short-lived handle
or resolving locally inside a trusted boundary. Raw secret values do not enter
control records, events, API responses, receipts, diagnostics, prompts, or clients.

Static keys and persistent personal logins do not become short-lived credentials
because they are in a secret store. Keep them in trusted owner-bound custody.
If raw credentials must enter an authorized process, disclose and isolate the
exposure. Redaction is defense in depth. Reject execution profiles that cannot
meet the task's credential or network requirements.

[Decision 0024](docs/decisions/0024-salad-server-proxy-boundary.md) places Salad
management authority in a server-only proxy with versioned opaque references and
closed operation admission. Its live entry point blocks before secret resolution
or network; only synthetic transport/resolver conformance is implemented. The
[runbook](docs/salad-proxy.md) requires independent monitor, termination, cost and
external artifact custody conformance before any live activation implementation.
This boundary is not provider isolation or a trusted autonomous Salad sandbox.

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

[Decision 0020](docs/decisions/0020-bounded-computer-autonomy.md) adds bounded autonomy
sessions over the existing capability authority. Agents author plans and checkpoints;
humans review exact scope. Explicit capped public-prep policy may auto-approve a
preparation step only within an already human-approved capability envelope. It cannot
widen that envelope, create a grant or activate effects. Session/operation bindings,
leases and receipts are durable; unknown work never authorizes replay. The implemented
slice remains synthetic and provider-disabled, not unrestricted computer control.

Artifact downloads and package installation require a separate immutable,
human-approved capability declaration bound to the approved task plan revision
and digest. It names exact destinations/ports, package managers/channels,
registries, hash-pinned artifacts and lockfile (or explicitly supported install
commands), byte/time budgets, sandbox filesystem roots, environment/image identity
and opaque credential references. Agents may request but never grant or widen
these capabilities. Host/control-PVC access and ambient credentials are denied.
Use-time checks fence policy, grant, worker lease, epoch and revocation; every
effect has a durable unique intent and an attributed actual hash/version receipt.
Mismatches remain auditable, are quarantined and fence remaining authority.
Current schema 49 implements the control-plane policy. The broker adds signed
synthetic packages, stepwise checks, an append-only worker journal and reconciliation
into project byte custody/audit; real effects remain disabled until independently
verified isolation and enforcing transports exist. Worker journal schema 1 is
separate from the control database; unknown effects never authorize replay.
The [capability Kubernetes components](docs/capability-kubernetes-runner.md) add a
strict profile, raw-observation inspector, fake-client lifecycle and bounded
hook-free wheel installer. Object presence is not quota/network enforcement;
real-client activation and control receipt integration remain blocked pending
independent target conformance. These components do not widen signed authority.
The [authenticated evidence collector](docs/capability-kubernetes-collector.md)
binds explicit API/CA/cluster and tenant/plan identities, retains bounded raw API
records and requires fresh one-use collection at activation. Operator-signed
claims do not establish independence. Missing CNI/kernel/watchdog witnesses or
any unknown check keeps activation blocked; API collection never enables effects.
The [runtime witness boundary](docs/runtime-witnesses.md) adds four mandatory
authenticated challenge/report gates, separate daemon supervision and one-use
node-observation custody. CNI drops, kernel/filesystem quota exhaustion, external
termination/no-continued-writes and actual admission/defaulting must agree for the
exact operation. Unsupported node sensors/defaulting profiles remain unknown;
the protocol and fake tests are not evidence of deployed runtime enforcement.
The [target adapter contract](docs/runtime-witness-targets.md) adds explicit native
profile and mTLS pins, raw native/API agreement and bounded target orchestration.
Non-fake activation rejects legacy unpinned reports. Missing deployed native
drivers or unreviewed Kubernetes defaults remain unknown, not an ambient fallback.
See [the runtime boundary](docs/runtime-capability-broker.md). The declaration uses pinned locks,
not arbitrary install commands. See [decision 0018](docs/decisions/0018-agent-capability-policy.md)
and [the capability contract](docs/agent-capability-policy.md).

The [local networkless Docker fixture](docs/local-networkless-docker-fixture.md)
is a separate, explicitly opted-in closed conformance profile: prebuilt local image,
fixed harmless programs/inputs, no host bind mounts or network, independent process
supervision, exact nonce ownership and fixture-only quarantine. Unknown limits block
workload release; unknown execution never authorizes retry. Its additive local
journal evidence cannot activate capability downloads/installs, satisfy Kubernetes
activation, publish control receipts, or release artifacts. It does not relax the
persistent-output-storage or authenticated control-package contracts.

[Decision 0022](docs/decisions/0022-transferable-execution-monitor.md) places a
transferable monitor/guardian trust boundary at the execution target, never on the
control host by necessity. Immutable signed assignments bind exact tenant/plan/
package/operation/nonce/workload generation, adapter, privileges, limits and expiry.
Authenticated challenge/response, independent deadlines, local signed receipts and
stop-only recovery do not grant submit/restart authority. The control validates
enrolled monitor evidence without host access. The opt-in closed Docker attachment
and disabled Kubernetes/Salad contracts preserve unknown-as-blocking activation;
see the [monitor deployment and reconciliation contract](docs/execution-monitor.md).

[Decision 0021](docs/decisions/0021-independent-sandbox-supervision.md) selects only
an opt-in external guardian for that closed local fixture, after rejecting unproven
Kubernetes sandbox deployments. Reciprocal authenticated liveness and exact-owned
first-stop reconciliation add supervisor-loss handling without retrying uncertain
effects. The local live network witness still blocks; no authenticated autonomy
execution backend or real control receipt authority is created.
The [local namespace proof](docs/local-docker-namespace-proof.md) additionally
requires independent daemon-host process/net/mount namespace and sandbox inode
binding, namespace-local interface/address/route enumeration and bounded safe
connectivity controls. Missing host observation, shared namespaces, ambiguous sysfs
visibility, proxy uncertainty or unexpected connectivity block release; observer
unavailability blocks before create. Workload capabilities are never widened.

### 5.3 Governed agent identity, reasoning, and peer transports

This is the contract for remote research agents; it does not assert that the
integrated runtime is complete. Current implementation status and the runnable
operator boundary are tracked in the [remote-agent installation runbook](docs/remote-agent-platform-install.md)
and [implementation journal](docs/remote-agent-implementation-journal.md).

An enrolled agent is a distinct authenticated principal, never a human session
forwarded through a tool. A human with the required authority issues a bounded
grant to the agent identity. The agent may request authority and consume only an
active grant; it cannot issue, approve, widen, or self-reconcile one. Delegation
is allowed only when the parent grant expressly permits it, and each child is a
strict subset in actions, resources, audience/destination, expiry, budget, and
delegation depth. Reserve parent budget atomically with child creation; retries
with a different request body conflict rather than minting authority. At every
use, enforce tenant and project scope, subject identity, grant revision/epoch,
resource binding, destination/model, expiry, revocation, budget reservation, and
idempotency in the authoritative control path. Approval remains a separately
authenticated human operation; neither an MCP/A2A message nor agent-authored
prose is approval.

Agent actions, authority requests/consumption/delegation, messages, tools,
observations, experiments, and results produce correlated, append-only,
tenant-scoped provenance joined to the authorized notebook/export snapshot.
Record actor and delegated actor, peer/session/task IDs, model/tool identity,
permission decision references, revisions, correlation/causation IDs, and
outcome/usage where known. Redact secrets and minimize payloads: do not persist
credential values, unrestricted prompts, or private chain-of-thought. Explicitly
represent denial, failure, timeout, and unknown; a missing provenance segment or
watermark makes completeness false, never silently complete. Agent-submitted
observations are attributed claims, not verified execution or scientific truth.

Generated public research outputs use the additive
`generated_public_research_output_v1` family and typed optional
`AgentProvenanceRecord.generated_output` reference. The central registry records
the actual author, immutable output bytes/digest, human disclosure/admission and
recipient disclosure independently; registration is not a worker artifact
manifest, Job receipt, execution grant or proof of reasoning. See the
[public-capture contract](docs/public-capture-interface.md) and
[generated-output registry contract](docs/generated-public-output-interface.md).
The contract does not imply that the installed V2 harness/runtime emits or collects
this family; that remains a separate acceptance boundary.

Real model reasoning runs under a supervised, durable remote runtime with
restart/reconnect behavior, bounded stop/expiry/usage fences, and explicit
provider/model destination. It is distinct from the deterministic coordinator
and provider-disabled paths, which remain useful for orchestration and testing
but are not reasoning evidence. Provider use requires a separately authorized
runtime credential reference and bounded grant/budget; no API-key fallback,
automatic top-up, or unapproved spend. Closing the desktop does not transfer
authority or stop a remote process; requested stop and confirmed termination
remain distinct.

The supported MCP integration is the official Python SDK in session-based stdio
mode, launched beside the agent process. That process makes authenticated HTTPS
requests to the control service using its own scoped credential reference; stdio
is not a remotely exposed MCP HTTP endpoint. MCP exposes typed, permission-scoped
research operations and redacted reads, not arbitrary database, shell, or
filesystem access. Requests may propose work, request authority, consume an
already-human-issued grant, report attributed observations, request human review,
and read/export only authorized notebook data. Reconnect must reuse the same
idempotency key and exact arguments after an uncertain response. The adapter
transport is not itself an authorization boundary.

A2A requires independently authenticated protocol peers, durable task/message/
result identities, replay-safe exchanges, cancellation, and bounded delegation
under the same Tessera authority checks. Peer discovery or protocol compatibility
does not imply trust; peer content is untrusted input. Do not call an in-process
fixture or two clients sharing one identity independent A2A. The selected wire
profile and installed peer entrypoints must be documented and tested before
claiming interoperability.

Do not proxy an owner token, accept agent self-approval, widen grants, leak
secrets or private reasoning, or substitute deterministic/mock events for live
reasoning or independent peers. Local Docker Desktop remains laptop-local and
does not establish off-host/laptop-off acceptance.

Treat sources, skills, peer messages, tool outputs, and artifacts as untrusted.
MCP/A2A are transport integrations, not authorization boundaries. Validate tool
arguments and destinations, including redirects, private networks, metadata
endpoints, arbitrary uploads, and shell escape paths. Sandboxes must not expose
host sockets, broad credentials, or unapproved filesystem roots.

The ordinary Docker worker is a typed, separately authenticated worker adapter,
not remote shell or an owner-token/daemon proxy. It uses an explicit worker
credential, verified control-service TLS, and an explicitly configured approved
daemon endpoint (HTTPS Engine transport requires its configured CA). No ambient
Docker context, arbitrary shell, or daemon/agent socket is exposed to the agent or
research container. The
[control](docs/ordinary-docker-control.md) and
[HTTP binding](docs/ordinary-docker-http-binding.md) contracts describe its bounded
API. Passing isolated HTTP/SQLite tests or having a CLI does not establish an
approved guard image, installed worker, live container isolation, or off-host
execution.

Worker enrollment requires an authenticated owner/admin and bounded bootstrap.
Record approved supervisor/harness builds, environment provenance, trust class,
region and capabilities; probe capabilities instead of trusting self-description.
Use ready/draining/disconnected/quarantined/retired states. Drain blocks new leases;
retirement revokes identity and reconciles jobs and cleanup. A personal worker
cannot protect secrets from its own administrator; sensitive tasks need a suitable
trust boundary.

Credential-access handles are fenced by the same live worker, execution lease and
epoch checks. Grant cancellation, expiry, worker retirement, execution epoch
change, or access-lease revocation blocks access and renewal; backend revocation is
requested where it is supported.

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

For the provider-disabled Kubernetes development profile, a normal attributed
terminal observation may confirm termination only after the bound Job and its
nonempty observed owned-Pod set are terminal. Incomplete/contradictory observations
remain unknown. Cancellation retains its separate fresh Job/Pod absence proof;
see [decision 0015](docs/decisions/0015-normal-kubernetes-completion.md). Neither
path automatically records a human outcome, settles charges, or accepts evidence.

Schema v17 additionally records a bounded confirmed-stop workspace handoff: a human
can bind one selected captured local baseline to a current grant/lease, request its
cancellation/fence, and become recorded owner only after the matching attributed
worker reports stable-provider, confirmed exit with no newer unknown observation.
Human handback compares separately captured immutable snapshots and creates a fresh
context epoch; it does not mediate arbitrary filesystem writers or imply provider
quiescence beyond that observation.

Schema v18 records a durable external-operation intent before returning a
development-local CPU package. It binds the tenant/authority chain, deterministic
operation and Docker lookup identities, frozen package/request digests,
adapter/profile versions, manual-reconciliation retry class, and independent
desired/observed states. Revocation changes desired cancellation transactionally;
it does not dispatch or prove termination. v17 lease/observation history remains
unbackfilled legacy/null rather than receiving invented provider receipts.

Schema v19 adds a one-to-one bounded receipt only for an injected deterministic
fake provider in the development worker. Its `local-provider-v1` test adapter
can reach only an injected literal-loopback HTTP fixture; it is not a real
provider integration. The supervisor persists local submit intent, rechecks
authority, and recovers lost submits by stable lookup rather than resubmitting.
Receipt/provider identity is immutable; stale authority, unknown observations,
unknown exposure, and cancel uncertainty remain unresolved. This records
fake-provider operational evidence, not real dispatch, accounting, Docker truth,
artifact release, result acceptance, or scientific success.

Schema v44 adds an empty tenant-scoped recovery reconciliation record keyed to
the original grant, lease, epoch, operation, and worker identity. After that
identity is revoked, expired, retired, or missing, a separately authenticated
human reconciler can record bounded lookup/cancellation evidence for that exact
operation. It cannot renew or report as the old worker, issue a replacement
lease/package/operation, replay submission, or infer termination from a
cancellation request. Only attributable confirmed terminal evidence or an
explicit unknown observation closes recovery; evidence references remain
auditable and credential-free in read projections.

The original Kubernetes worker has a separate, narrower **never-submitted**
reconciliation path. Only its intact exclusive journal can atomically establish
cancel-requested with zero create intents and no Job UID for the exact frozen
binding/manifest; fresh bound namespace-UID, deterministic Job-name and
operation-labelled Job/Pod absence readbacks must then succeed. The authenticated
worker reports this attestation against the cancelled grant/dispatch/lease/epoch;
a human may release held exposure at zero settlement only while that report is
fresh and no adverse operation observation or provider identity exists. This is
not a termination confirmation, does not resurrect lost journals, and cannot
be applied to any operation for which a create intent could have reached Kubernetes.
The distinct `pre_manifest` branch covers a retained, exclusive adapter+Job
journal with an exact cancelled adapter intent and **no** Job row: lack of the
network-none witness rejected manifest rendering before any Job intent or create.
It transactionally persists an insert-denying SQLite trigger on the original
Job journal before provider absence reads, including for an older worker process.
It records `manifest_not_generated`, never backfills a fabricated manifest/Job
row, and rejects lost/replaced journals. The normal `bound_job_intent` branch
still requires the pre-existing exact manifest and cancelled zero-create row.
If that original worker credential has expired, the *original human grant
creator* may instead make a separately typed `pre_manifest` owner attestation
from the retained exclusive journal and fresh target-bound absence readbacks.
Control checks the cancelled original dispatch/lease/epoch/target, absence of
provider identity and anything beyond unconfirmed worker-unknown evidence, and
commits that owner attribution, zero-settlement held release and distinct events
in one revision/idempotency-fenced transaction. This is an explicitly
owner-attested trust boundary: control cannot independently inspect the worker
journal or Kubernetes from submitted booleans. It never renews or impersonates
the expired worker or calls this provider termination.

For a cancelled **never-claimed** dispatch, a distinct five-minute stop-only
maintenance authorization supports expiry of the exact original worker credential.
Only the separately authenticated original human grant issuer can issue and consume
it, using that same human session. It binds the old principal/credential, grant,
operation, dispatch, package, revision/epochs, actual namespace/target, exclusive
stopped-workspace custody digest, and an existing authorized provider-read reference.
This is an event-backed non-bearer authorization, not a worker credential: it grants
no execute, renew, dispatch, spending, delegation, or Kubernetes RBAC permission.
The expired bearer remains unauthorized. No migration or v51 table is changed.
The collector uses the retained original journals and durable insert fences; any
claim, descriptor, adapter/Job/helper intent, failure, observation, receipt or
provider identity remains held. Fresh complete actual provider readbacks are
mandatory, including operation-labelled Jobs/Pods and output helpers; a 403,
incomplete list, wrong namespace or unknown state is not absence. The human submits
a digest-bound proof against the exact unexpired maintenance authorization; control
rechecks all history and commits once-only zero settlement, interruption and
`not_executed` events atomically. Provenance names the actual human maintenance
actor/session and issuer separately from the expired original worker identity.
This remains explicitly human-attested journal/provider evidence, not independent
inspection, provider termination or scientific validity. See the
[stop-only maintenance runbook](docs/queued-dispatch-maintenance.md).

Schema v20 adds an append-only, revision-fenced human bookkeeping ledger over a
previously committed quarantined checkpoint. It rechecks the live reconciled
grant, receipt, operation, lease/epoch, manifest digest and retained bytes, but
does not turn fake/development evidence into provider success or scientific
validity. Acceptance means only authoritative Tessera result ownership; retained
records explicitly preserve `scientific_validity=not_assessed`,
`provider_success=not_established`, and `artifact_release=not_performed`.

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

### Outcome program review profile

The bounded Outcome 0/1 profile uses a project as the program container and a
study as an outcome ledger. Typed `research_program` decision scopes retain a
scientific brief, explicit selected/deferred condition accounting, source-use
intent and go/no-go direction. These are review records, not execution grants.
Proposal edits append a linked replacement; an agent suggestion or human rejection
does not revoke a prior approved review. Approval of a replacement supersedes its
review lane, while history remains visible. A changed evidence basis marks prior
reviews stale and prevents resolving an old panel. Go checks registered variant
accounting, accepted supported findings, current source-use review and explicit
linked experiment termination; no-go can record an early stop recommendation.
Neither direction stops jobs automatically. Use separate revocation/cancellation
and verify termination. Full scientific protocols, data-rights verification,
provider execution and later patent/physical-loop controls remain separate gates.
Metadata-only Outcome 0/1 dataset snapshot declarations may retain caller-supplied
digests, schemas, units, missingness, split/leakage, validation and source/license/
provenance records for review, but never establish a download, content validation,
rights verification, protected split, or execution authority.
See [the requirements matrix and API workflow](docs/mixfm-interactions.md).

Plans may additionally declare a versioned metric protocol and data/code/environment
manifest identities. Evaluation decisions bind caller-reported observations to that
immutable plan scope and protocol; deterministic threshold reports are retained
with the request event. Missing evidence remains explicitly inconclusive. A go
under such a protocol requires a passing current human-reviewed evaluation, not
just a human acknowledgment of a failed/incomplete record. Legacy prose-only plans
remain unevaluated. These checks grant no execution authority or scientific truth.
The [evaluation and Salad contract](docs/mixfm-evaluation-and-salad-contract.md)
also defines opt-in target validation and neutral capability/health discovery;
Salad live operations remain unsupported and fail closed without credential access
or network calls, even when configuration is provided.

## 8. Implementation sequence and acceptance

The table below is the target sequence, not a completion checklist. Current
completion evidence and outstanding work live in the
[status matrix](docs/implementation-progress.md#design-status-matrix).
A component passing synthetic tests does not complete a workflow or the pilot.
Update that tracker with every behavior-changing implementation checkpoint.

Implement vertical slices through the modules, not complete horizontal platforms.
These are the pilot's engineering tasks; each produces code, tests and a short
operational note. No paid or sensitive run precedes its required enforcement.

### Next milestone: thin goal-to-decision vertical slice

Before broad provisioning/cloud, skills, or desktop expansion, complete one
public or otherwise simple CPU workflow with a baseline and one registered
intervention. The slice must demonstrate: a bounded experiment design; execution
on the selected remote target while the desktop can disconnect; linked
artifacts/measurements and operation provenance; explicit succeeded, failed,
cancelled, and inconclusive outcomes; and human review of the resulting evidence
and conclusion. Local CPU remains the primary development path; the remote target
is a parallel acceptance target, not an excuse to defer laptop-independent
execution.

Compare total researcher effort (setup, intervention, review, verification, and
recovery) with the baseline workflow. A proposed **25% effort reduction** is a
testable product hypothesis, not evidence, a release criterion, or a claim until
the selected workflow is measured. Record the measurement method and all outcomes,
including inconclusive ones, before deciding whether to expand scope.

| Step | Deliverable | Acceptance evidence |
|---|---|---|
| 1. Contracts and harness spike | Independently runnable process-based control plane, domain states, selected execution profile, mediated harness and CPU adapter; decide backend stack | Same authenticated API works co-located and over a trusted network; demonstrate tool/network limits, ownership, cancellation and cost boundaries |
| 2. Durable execution | Control API/store, enrollment/session auth, policy grants, reservations, supervisor, operation journal and broker | Unauthenticated/expired/stale grants denied; duplicate delivery safe; lost submission reply reconciled; backend survives Electron exit |
| 3. Local workspace and adoption | Named connection/login, desktop isolation, scoped files/snapshots, read-only inventory, minimal-plan approval, skill editor/test/release | No cross-connection context or credential reuse, path escape or unclassified upload; user edits and imported unknowns preserved; worker loads pinned skill |
| 4. Interaction and handoff | Persistent threads, contextual questions, typed revisions, inbox, telemetry, stop/edit/handback | Agent prose cannot approve or confirm a stop; queue saturation holds dependent work; conflicting writers blocked; handback gets fresh context |
| 5. Labbook and evidence | Timeline/findings, complete in-scope ledger, source support, method labels, corrections and export | All variants visible; incomplete history disclosed; source support and replication labels checked; corrections trace downstream; history browsing executes nothing |
| 6. Recovery and pilot | Fault tests, signed packaging/update path, backup/restore, runbooks, measured real investigation | Restore fences old grants; unknown jobs/spend alert; compare quality and total human effort with existing workflow |

### Harness proof of concept (synthetic, not pilot completion)

The implemented synthetic POC uses an in-process OpenCode-shaped fixture, not an
enrolled node or selected OpenCode server. It has no real provider call or
sensitive data and proves that the adapter can create and
attribute a session, translate a bounded message and event stream, reconnect from
a cursor, and request cancellation while Tessera retains authority. OpenCode
session/job state may continue independently; Tessera records the uncertainty and
reconciles it rather than inventing termination.

Before integrating OpenCode into a pilot workflow, repeat these cases against one
selected pinned OpenCode server and one enrolled node, then meet the real
OpenCode/provider activation gates in the [compatibility matrix](docs/compatibility-matrix.md).

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
and backups separately. Project-object deletion requires a durable manual tombstone
and deletion intent, tenant/project binding, an exact backend identity check, and
visible degraded incident state for missing/corrupt/uncertain outcomes; no automatic
deletion is enabled by default. Held, referenced, checkpointed, or accepted objects
cannot delete. Deletion invalidates future retrieval and caches subject to disclosed
holds/backup expiry; minimal audit tombstones preserve history without retaining
unnecessary sensitive payloads. Published Git/model/provider copies have separate
retention boundaries disclosed before export. Runbooks cover compromised
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
