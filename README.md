# Tessera

**Run agent-assisted experiments. Keep a scientist’s record.** Tessera is being
built for computational researchers and research engineers who already have
scripts or notebooks, data, and access to compute—and want to delegate experimental
work without losing control of the method, access, cost, or conclusions.

An agent can suggest a change or produce an output; the harder research questions
are what was actually run, how it differed from the baseline, what the evidence
supports, and why you chose the next step. Tessera connects plans, permissions,
execution attempts, evidence and human decisions in a durable research ledger.
The investigation—not a chat session—is the unit you should be able to return to.

**Developer alpha · local evaluation, not a production platform.** The supported
starting point is a local control service and desktop client, with bounded CPU
execution through the maintained local Docker path (`docker-local-cpu-v1`, plus
campaigns). The Kubernetes lab profile is optional and frozen
([ADR 0033](docs/decisions/0033-freeze-over-built-planes.md)). General remote
execution, integrated agent reasoning and scientific acceptance are not demonstrated.

## From a research question to a defensible next step

Consider a tabular prediction experiment: does a proposed feature transformation
improve on the existing baseline, using the same data split and evaluation method?
The **intended agent-assisted workflow** is:

1. **Establish the question and baseline.** Identify the script, input snapshot,
   environment, metric and success criterion before changing the experiment.
2. **Review one bounded intervention.** Let an agent propose the change and checks;
   you review the exact plan, approved compute target, data access, cost/exposure
   limits, deadline and stop rules. A proposal is not permission to run.
3. **Follow the attempt.** Inspect progress, outputs and failures rather than
   relying on a reassuring summary. Keep a failed run distinct from a valid
   negative or inconclusive result; record changes of plan rather than silently
   replacing the original method.
4. **Stop and return.** Request a stop when needed and distinguish the request from
   confirmed termination. Revisit the same investigation to inspect what happened
   and what remains unresolved; returning to its history should not rerun work.
5. **Review the evidence and decide.** Compare baseline and intervention, check the
   method and uncertainty, and record whether to continue, revise or reject the
   idea—with links to the supporting and opposing evidence.

The aim is less reconstruction across chats, scripts and result folders, and a
clearer account of what you knew when you made a decision. **Traceability is not
scientific validity:** a complete record, successful process or agent critique
does not establish that the method is sound or the conclusion is justified.

This end-to-end agent workflow is a design target, **not today's integrated alpha**.
Today you can use the local desktop/control service for versioned designs, studies,
linked measurements, human conclusions/corrections and exports. Bounded CPU execution
uses a separate API/operator handoff; integrated planning, execution and agent
critique remain unverified. See [capabilities and limits](docs/implemented-capabilities.md)
for the implemented boundaries and the [tabular starter](docs/tabular-starter.md)
for the bounded baseline/intervention path. The intended compute target can be
outside the laptop, but demonstrated laptop-local execution does not prove
independent-host or laptop-off operation.

## A look inside

Actual Electron renderer and real isolated local backend with explicitly synthetic
UI test records. The credential picker is substituted; **no experiment execution
or model calls**. Scientific validity is not assessed. These renderer captures
are not native OS-picker proof.

### Record the experiment design

![Experiments workspace with a controlled test design, goal and success criterion](docs/assets/alpha-design.png)

An immutable design records intent, not execution authority. Provenance and prior
designs remain available without crowding the current research question.

### Keep uncertain observations visible

![Synthetic human-reported failed and inconclusive observations, with no execution observed](docs/assets/alpha-results.png)

Historical failure and current uncertainty remain attributed to human-reported
observations. No operation or scientific success is inferred from these records.

### Separate proposals from authority

Research MCP reads use `tessera_list_research_proposals` and
`tessera_read_research_proposal` for project/study/experiment proposals, including
immutable payload/digest and promotion state. The legacy `tessera_list_proposals`
and `tessera_read_proposal` tools cover task-plan proposals only. Both families
require scoped read authority; reading never promotes a proposal or permits execution.

![Plans and decisions workspace with a durable zero-budget record-only test draft](docs/assets/alpha-review.png)

The test draft survives reload and reconnect without replay. Drafting and human
approval remain distinct from execution admission, dispatch and scientific review.

## What you can evaluate today

The [quickstart](docs/quickstart.md) follows this bounded local journey. Execution
is a separate API/operator handoff, not a one-click desktop operation. The desktop has
admit, dispatch-authorize and resolve forms for the frozen Kubernetes lab path
(`desktop/EXECUTION.md`); its dispatch profile is a fixed lab profile, not derived from targets.

```mermaid
flowchart LR
  A[Connect to local service] --> B[Project and versioned design]
  B --> C[Separate human approval and execution grant]
  C --> D[Operator admission and CPU baseline dispatch]
  D --> E[Measurements and quarantined artifacts]
  E --> F[Review retention and separate custody release]
  F --> G[Exact result bytes and bundle record export]
  G --> H[Reconnect to durable records]
```

### Agent-assisted research: components, not an integrated demonstration

```mermaid
flowchart LR
  A[Agent proposes bounded plan] --> B[Human reviews exact revision and scope]
  B --> C[Grant and admission checks]
  C --> D[Direct Agent Sandbox adapter]
  D --> E[Persist outputs under custody rules]
  E -. Integrated reasoning and critique unverified .-> F[Agent critique proposal]
  F --> G[Human decision in research ledger]
```

Proposal/review records and the opt-in [direct Agent Sandbox adapter](docs/agent-sandbox.md)
have isolated component tests. The adapter targets upstream **v1.0.6** one-shot,
network-none Sandboxes (an opt-in allowlist egress-proxy profile exists, see ADR 0034); it does not replace batch Jobs. No live CRD/controller or
Sandbox runtime acceptance is claimed. Warm pools, read-only source integration,
input/reasoning credential delivery and a complete agent reasoning/critique loop
are not available in this demonstrated journey.

### Campaigns (ADR 0038)

A session can submit a typed [campaign spec](docs/campaigns.md): a digest-pinned DAG of
stages with a budget cap, retries for transient failures, data classes and stop rules. One
human approval of the exact digest lets the in-process runner drive every step (including
staging one stage's outputs into the next) through the existing checks. A terminal campaign
has an operational receipt, not a scientific verdict.

**Standing approvals and scheduled starts.** A human can instead make one decision for a
whole recurring or chained run: a [standing approval](docs/decisions/0042-standing-approvals-and-scheduled-campaign-starts.md)
binds one exact spec digest to explicit caps (starts, total budget, per-start budget,
interval, concurrency, a window of at most 30 days) and optional retries and a follow-on
campaign. The runner then starts campaigns inside those caps with no new approval per start.
Each start re-checks, in one transaction, that the creating credential is still live, the
digest, references, data classes and provider trust (which can only narrow), and the project
spend limits. A start is an ordinary campaign with an approval marked `standing_approval`
that names the standing approval, so it is audited and appears on the receipt. Only a human
creates or revokes one (agents cannot); revoking stops new starts, and running campaigns can
be cancelled separately. A standing approval grants nothing a human approval would not: the
same budget cap, expiry, data-class and trust checks apply, and it never resolves a Salad
review for a person. `tessera serve --no-campaign-scheduler` turns scheduled starts off.

**Grant lifetime.** A stage's execution grant must outlive its `max_duration_seconds` by a
settle margin, or the spec is refused with a clear reason. The runner starts a grant's
lifetime only when a provider slot is free (a worker's `worker_slots`, default one), so
queue time never eats it, and an expired campaign grant whose run has a confirmed exit and
a recorded cost is reconciled automatically (credited to the campaign's actor) so it cannot
hold a slot forever; it never settles without that evidence (see
[ADR 0048](docs/decisions/0048-grant-lifetime-and-slot-release.md)).

**Gates between campaigns and a program budget.** An approval (or a standing approval, on
every start) can carry an `unlock`: it names an earlier campaign in the same project and is
admitted only if that campaign has a recorded human gate `go`, or a platform-computed
paired-delta outcome (per-seed differences of typed metric records, with a confidence
interval) inside an allowed set that a human pre-registered. The check runs in the
admission transaction, fails closed on anything missing, unresolved or unpaired, and is
recorded on the receipt. A later human `no_go` vetoes further starts. The gate record is
human-only: agents and workers cannot write it, and a go counts only when a person's
session recorded it (an owner credential with an automation label cannot unlock). `tessera serve --program-budget FILE` adds an
operator cap on the money committed across campaigns; it only narrows the existing caps. See
[ADR 0045](docs/decisions/0045-campaign-gates-program-budget.md). A gate is a human
direction or an arithmetic outcome, never scientific acceptance. A v2 stage whose approved
spec declares `gate_evidence` (a human-created split lock, a seed and metrics, with retries
off) gets its split bound and its metrics recorded by the runner under that approval, so the
chain can run unattended ([ADR 0049](docs/decisions/0049-campaign-gate-evidence.md)); the
records are measured numbers, `not_assessed`, and the gate decision itself stays human. A requirement can also
carry a fail-closed `measurement` condition on one number in the earlier campaign's terminal
receipt; it narrows the go or outcome and never replaces it
([ADR 0051](docs/decisions/0051-measurement-unlock-predicate-and-go-authority.md)). By default
only a person's session records the go; `tessera serve --accept-automation-gate-go` lets an
operator also count an owner credential's labelled-automation go (the receipt keeps its class).
A v2 stage output of kind `evidence_rows` (per-variant, per-seed metric rows over a
pre-registered grid and split lock) is ingested by the platform itself, so a paired gate over a
stage that trains both arms needs no transcribed numbers
([ADR 0052](docs/decisions/0052-campaign-evidence-rows.md)).

**Providers and chaining.** A v2 spec names a compute *provider* and *runtime* per stage, up
to 64 stages. `local-docker` is built in (`tessera-worker development-poll` supplies the
container execution; its memory and scratch limits and its container uid:gid are operator settings in the
poller config, never plan fields (a stage may request more scratch than the default only up to the
operator's ceiling, refused at admission when above it,
[ADR 0059](docs/decisions/0059-operator-scratch-ceiling.md)); on native Linux the container runs as the poller's own
uid:gid by default so the 0700 output workspace stays writable), `self-hosted` runs the same worker on a server you operate, and
`salad` GPU stages are refused unless the operator supplies Salad profiles (a billable,
opt-in third-party path). A vendor is a plugin: the operator's
`--campaign-providers` file names it (entry point group `tessera.campaign_providers` or a
dotted module) and gives it a config section, so adding one never edits the runner.
Descriptors are validated at registration. `--provider-trust` sets which data classes each
provider's worker may receive (public only by default). With the owner-set mode
`all_registered_providers` (ADR 0047), agents and specs route any data class among the
configured, registered providers without a per-run approval; data never goes to the public
internet or an unregistered destination in any mode, because non-public inputs cannot use
egress or workload fetch hosts and every transfer is recorded on the receipt (the egress
proxy journals a tunnel when it is admitted and again at close, so an interrupted tunnel is
recorded with unknown bytes rather than missing, [ADR 0057](docs/decisions/0057-egress-tunnel-opened-record.md)). A stage input is a snapshot, the
declared output of an earlier stage (any provider), or an `artifact` pinned by campaign,
stage, output and sha256 from an earlier campaign; every input and output is indexed in
`campaign_stage_io`. Each attempt is settled once through a single settlement path with a
closed basis enum, and a finished or failed attempt releases its hold immediately.
Outputs above 16 MiB, such as model weights, use chunked [large-artifact custody](docs/large-artifacts.md):
one sha256 over the whole file, 4 MiB chunks, bounded memory, and an operator-configurable
ceiling (default 8 GiB per artifact). A Salad stage's guardian input path follows the same bound and is tested against a fake control serving a virtual 3 GiB artifact; no live Salad pull of that size has been run, and a Salad stage's own outputs stay at 16 MiB.
Example systemd units for the poller, Salad executor and guardian are in
[`deploy/systemd`](deploy/systemd/README.md).

Clients direct campaigns through `tessera_mcp`: `tessera_submit_campaign` takes v1 and v2
specs (providers, artifact inputs, chaining); `tessera_list_campaigns`,
`tessera_get_campaign_receipt`, `tessera_list_snapshots`, `tessera_campaign_outputs`,
`tessera_campaign_logs` and `tessera_campaign_budget` read recorded state under separate
scopes. Steering tools ([ADR 0055](docs/decisions/0055-mcp-steering-tools.md)) dry-run a spec
(`tessera_validate_campaign_spec`), build a retry spec for the stages that did not succeed
(`tessera_prepare_stage_retry`), show held grants and releases (`tessera_campaign_grants`) and
only reduce authority (`tessera_reduce_campaign_budget`, `tessera_revoke_campaign_release`);
approval, cap or concurrency increases and grant reconciliation stay human. A human still
approves the digest and stages snapshots. See
[docs/mcp-campaign-client-path.md](docs/mcp-campaign-client-path.md).

**What still needs a human (Salad stages).** By default every Salad stage stops for a
person at its source assessment and at each attempt's plan and source-use reviews; a bare
approval never implies otherwise. A human may opt in, per stage, by putting a `preapprovals`
entry in the approve body ([ADR 0044](docs/decisions/0044-salad-review-preapproval.md)),
never in the spec: it binds the exact digests of those reviews (see them with
`tessera campaign preapproval-digests`) and per-attempt and total cent ceilings. The runner
then resolves a review as `campaign_delegated` only when the content recomputed from the
stored row equals a bound digest, the entry is unexpired and the ceilings hold; any
mismatch, expiry or overrun leaves the review for a human. A changed spec, source or
provider needs a fresh approval and a fresh entry; an operator profile can only narrow this
(`preapprove_reviews: false`). Provider trust and the data-class guard still apply, and
every delegated review is cited on the receipt with the approval id. A person is still
required for: approving each campaign digest (and each re-approval), staging inputs with
their public declaration and code approval, gate go, conclusions, exports, publication and
data classification (never pre-approvable), recording compute charges, reconciling orphans
and blocked campaigns (except an attempt whose worker reported a refused launch with no
container, zero cost and no receipt, which the runner reconciles itself and records as no
evidence, bounded at two identical repeats per stage; a reboot or an unreported start still
needs a person, see
[ADR 0046](docs/decisions/0046-automatic-not-started-reconciliation.md); and an attempt whose
worker confirmed the exit with no contradiction, which the runner resolves, releasing its grant
and slot and never recording a pass, see
[ADR 0053](docs/decisions/0053-resolve-attempts-on-confirmed-exit.md)),
and anything the delegation declines, including an over-budget
attempt, an unclear billable frame or a guardian disagreement. A stage refuses to start
(`deadline_too_short`) when the remaining approval or credential window cannot hold its
maximum duration plus the settle margin, and an ambiguous billable frame is never settled
automatically (on a guardian-bounded or provider-billed basis) without a guardian verdict;
a human-recorded charge or reconcile can still settle it.

## Quickstart

### Opt-in Salad GPU execution milestone

The separate trusted-server single-container GPU transport, guardian/custody and
secure-file live-check composition have local implementations, not deployed/live
GPU acceptance. Default/fake APIs stay disabled. Run
`.venv/bin/python -m tessera_control.salad_live_check` for a zero-read/network/effect
readiness report. Actual execution uses explicit `--run --server-config PATH
--receipt-directory PATH` on the trusted Linux server, with existing exact human
authority and approved financial scope; unknown rate or unresolved exposure blocks
new work. Published-rate estimates are not actual billed-cost proof or hard caps.
See the [Salad runbook](docs/salad-proxy.md#first-gpu-journey-setup-versus-normal-operation)
for one-time setup, normal execution, confirmed stop and same-record reconnect. Routes
that carry owner-approved private code reuse one owner-enrolled executor enrollment
(`scripts/executor_enrollment.py`) whose worker principal is listed in the
[provider trust](docs/provider-trust.md#stable-executor-route-setups-that-mint-principals)
file. `--existing-project` enrolls an executor of any provider into a project that already
exists, so one campaign can mix CPU and Salad stages with their data and receipts together
([ADR 0056](docs/decisions/0056-executor-enrollment-into-an-existing-project.md)). Control issues worker credentials for at most 24 hours, renews them only up to
seven days after issue, refuses them at use time past that point and never revives an
expired one, so the enrollment needs a renewal
at least daily and a rotation within the week; it is revocable at any time.

Since schema 57 (the current schema number is `LATEST_SCHEMA_VERSION` in
`src/tessera_control/migrations.py`) the source also implements an experiment-parent admission ledger (up to
USD50), atomic full-grant reservations and a derived control-to-guardian binding.
Unknown costs block admission; cancellation and stopped status do not release
liability. These local implementations neither migrate a running service nor prove
provider billing bounds, native Salad grants or a completed Tessera research job.

### Local desktop/control setup

Requires Python 3.11+, uv, Node **22.12.0+** and npm. The control service and
desktop connection path are POSIX-portable (CI runs on Linux); only Keychain-backed
secret enrollment needs macOS. Platform acceptance is stated per check, not as a
universal OS claim. Python's SQLite must
provide built-in deterministic, innocuous `json_extract` for defensive recovery;
an incompatible build is rejected before control-state writes. See
[runtime prerequisites](CONTRIBUTING.md#setup). In a fresh state directory:

```sh
uv sync --locked --extra mcp
uv run --locked tessera init --state-dir .tessera
uv run --locked tessera serve --state-dir .tessera --port 8765
```

In another terminal:

```sh
npm ci --prefix desktop
npm start --prefix desktop
```

Connect to `http://127.0.0.1:8765` and select the private `.tessera/owner.header`
through the app's file picker; never paste its contents. Initialize only once;
existing state needs the documented backup/migration procedure (`tessera backup`,
`tessera migrate`, `tessera restore`; see the command index below). Backups are bounded
at 1 GiB unless the operator passes `--max-backup-bytes`
([recovery](docs/recovery.md)). Follow the
[screen-by-screen quickstart](docs/quickstart.md) for review/export and troubleshooting.
Native installation and picker/save acceptance require their own checks;
simulator evidence does not close those gates.

### Command index

Console scripts installed by the package, one line each:

- `tessera`: `init`, `serve`, `migrate`, `backup`, `restore`, `campaign`, `workspace`,
  `admin` (including `provider-trust`), `agent-proposal`, `agent-sandbox`,
  `connection-tunnel`, plus lab setup commands (`minikube-provision`, `astra-*`).
- `tessera-worker` / `tessera-worker-daemon`: worker CLI (`development-poll`) and its
  supervised daemon.
- `tessera-mcp`: MCP server for agent clients.
- `tessera-salad-operator` / `tessera-salad-cleanup`: human-only Salad setup and cleanup.
- `tessera-salad-executor` / `tessera-salad-guardian`: supervised Salad campaign executor and spend guardian (units in `deploy/systemd/`, launchd templates in `deploy/launchd/`).
- `tessera-garage-operator`: fixed-target stdio operator for a self-hosted control host
  (host and user come from `TESSERA_GARAGE_HOST` / `TESSERA_GARAGE_USER`).
- `tessera-k8s-operator`: fixed-target operator for a local Docker Desktop control namespace (admission, backup, activation; some tools write).
- `tessera-reasoning`, `tessera-a2a-peer`: remote reasoning runtime and A2A peer (frozen).

## Architecture

**Desktop → control plane → execution plane.** The desktop is a client; the control
service owns revisions, authorization and durable records; workers perform
explicitly admitted execution. One documented exception: with the opt-in
`--agent-sandbox-config`, `serve` itself calls the Kubernetes API for Agent Sandbox
(a frozen plane, ADR 0033; it holds cluster credentials, and moving those calls into a
worker is not done). Approvals, artifact custody, cost settlement and
scientific acceptance are separate decisions. External trackers are not authority.

See the [documentation guide](docs/README.md) for user, operator and developer
navigation, [capabilities and limits](docs/implemented-capabilities.md) for current
status, and [DESIGN.md](DESIGN.md) for authoritative contracts.

## Limits and safety

- Several planes (Salad guardian stack, capability broker, Kubernetes/minikube
  plumbing, Astra, OpenCode harness, A2A peer) are frozen to bug and security fixes by
  [ADR 0033](docs/decisions/0033-freeze-over-built-planes.md) and are lab code, not
  maintained product surfaces.
- Single runs outside campaigns have no automatic retry or scheduling; a failed or
  unknown run is reconciled by a person.
- Provider activation is disabled by default; no hosted service or production
  organization authentication is promised.
- Native OS dialogs, independent-host operation, live Sandbox isolation, integrated
  model reasoning, cost settlement and scientific validity need separate evidence.
- Results can be late, quarantined or unclassified even after authorized retrieval.
  Successful execution is not a scientific finding or publication approval.
- This source alpha is not a signed binary release. See the
  [release checklist](docs/release-checklist.md) for remaining gates.

## Contributing and security

Use [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and bounded live fixtures.
Read [SECURITY.md](SECURITY.md) before reporting vulnerabilities: do not post secrets,
research data or exploit details publicly; GitHub private vulnerability reporting
is enabled through the repository's Security page. Tessera source is [MIT licensed](LICENSE); bundled
third-party runtimes retain their own license notices.
