# Desktop workspace

For the newcomer local macOS setup and supported screen sequence, see the
[Tessera alpha quickstart](../docs/quickstart.md).

For the normal native **new plan → new execution** path and its explicit human
operator/worker API boundaries, see [EXECUTION.md](EXECUTION.md). Reviewing old
attempts or a proposal-only draft is not acceptance of that path.

The **Your research workflow** guide stays visible across connection states and
spells out connect → create/import observations → propose → approve → separately
grant/admit/dispatch → review/export → reconnect. Execution grants are
currently API-first; admission and dispatch have separate experiment controls.
Importing a study observation is not project migration. Null-spec record-only
drafts remain visible and editable; no command or input digest is invented.
Approved cards display both task and immutable plan revisions because recording
approval advances only the task revision.

Connect accepts private owner-controlled header files containing
`Authorization: Bearer <control-token>` (43–4096 URL-safe token characters), with
an optional line ending. Provider keys are not control credentials. Errors give
safe next actions without displaying server response bodies or replaying commands.

Desktop gates: `npm test` and `npm run check`. On a native graphical macOS host,
`env -u ELECTRON_RUN_AS_NODE npm run test:native-screen` opens a visible window,
captures the disconnected guide and Connect dialog under
its private local evidence directory, then exits. This opt-in screen check does
not select credentials or verify the connected research journey; see the
[desktop acceptance boundary](EXECUTION.md#acceptance-evidence) for the distinction
between screenshot evidence and OS-picker acceptance.

`test/electron-bench-acceptance.cjs` is an opt-in installed-service harness. Supply
a private JSON file of endpoint/CA/project/experiment/credential **references**,
and launch Electron with `NODE_EXTRA_CA_CERTS` pointing at that public CA. It uses
the production main credential reader, creates a proposal-only draft, reviews
existing ordinary executions and quarantine metadata, saves already released
retained output bytes, exports a reproduction bundle, reconnects, and captures native
renderer pixels. It never starts a job or reports scientific success. Targeted
System Events automation attempts the actual OS picker only when available;
an unavailable Accessibility probe is recorded and the explicit main-process
file-selection fallback is **not** actual-picker acceptance. Receipts distinguish
this boundary. On uncertain mutation outcomes, inspect the existing proposal
before another run; the harness reuses an existing revision-1 draft rather than
blindly replaying create. OS-dialog pixels are not captured.
Proposal create/revise/approve keys remain stable across interrupted harness runs.
An existing matching approved proposal is inspected without repeating mutations.
Known UI fence defects are reported in the receipt; completing independent
review/export steps never relabels those defects as passed acceptance.
When references name an integration-owned managed route, its readiness/expiry is
checked before reading credentials; this harness never manages that route.
`TESSERA_BENCH_TRY_ACTUAL_PICKER=1` makes a bounded attempt at the production OS
picker. A timed-out OS interaction is not a permission-denial claim. On an OS
picker blocker, the receipt retains a private credential-header **path reference**
for a human to select through Connect; remove that 0600 header after verification.

**Administration** opens a project-scoped Setup / Targets / Secrets / Delegations /
Readiness / Audit workspace. Forms preview typed metadata before explicit confirmation;
reviews and lifecycle commands bind exact revisions. The secret form accepts no
plaintext. It supports protected same-host native macOS enrollment and explicit
synthetic fixtures. Remote enrollment, organization invitations, workload delegation
and live enablement are unavailable.
Uncertain administrative commands are never automatically retried; refresh history
before another command. See [the setup and security runbook](../docs/admin-setup.md)
for supported actions and production blockers. Remote-execution and administration
methods are wired through the self-contained sandbox preload and sender/context fences.

Task details include **Inspect / reconnect remote execution** with target,
provider/cancellation state, evidence references and live go/no-go. Human review,
queue, pause/stop and cancel are separate revision/digest-bound control commands.
The desktop never starts an agent workload; disconnect/close does not cancel one.
The [routing slice](../docs/remote-agent-execution.md) is offline-only and
default-disabled, not a live Salad or Kubernetes activation claim.

When the connected control service reports a held recovery state, the desktop shows
recovered history in read-only mode. It cannot clear the hold or resume mutations.
The recovery banner does not claim that the original control service or a provider
job was stopped; see [the recovery runbook](../docs/recovery.md).

This desktop slice keeps credentials and HTTP requests in Electron's main process.

Task cards offer **Inspect autonomy sessions**, a bounded read-only state/plan/
checkpoint/receipt projection. It labels synthetic-only effects, pending human
review, blocked reasons, unknown billing and unassessed scientific validity.
Authoring/approval/resume are API-first; there is no terminal/browser or new effect
bridge. Stale project/connection replies are discarded. See
[bounded autonomy](../docs/bounded-autonomy.md).
The sandboxed renderer has a small operation-specific IPC bridge, a restrictive
CSP, no Node access, no remote navigation, and no external-content execution.
Each data request names its connection; switching connections cancels in-flight
reads and rejects stale replies. Mutation retries are explicit and stay with
their original command and connection.

The initial interface covers connections, projects, record-only plans, approval,
labbook history, attributed interaction threads/messages, and pending human
decisions. The decision inbox displays the returned scope, expiry, task basis,
and decision revision; an eligible session can explicitly approve or reject that
exact revision with an optional rationale. Resolution is an intent record only:
it does not issue a grant, start an experiment, or spend funds. Credential files are selected through a native dialog and held only in
main-process memory; saved encrypted sessions and organization login are later
work. Closing the desktop does not stop the backend.

First-run onboarding also offers a deliberately narrow **Kubernetes namespace
only** flow backed by `tessera astra-namespace`. A native picker keeps the
kubeconfig path and bytes in the main process; the renderer gets only an opaque
selection reference and basename, then supplies an explicit context and
namespace name. Inspection is read-only and labels zero-write semantics. The
contract returns target identity but intentionally does not expose arbitrary
namespace labels, so the UI does not infer ownership. Existing namespace use is
an explicit zero-write confirmation and truthfully says “connected to namespace;
control plane not installed.” A dedicated plan displays its exact digest and
expiry before a second explicit confirmation submits one `Namespace` create.
The receipt UID/resourceVersion is shown only after a returned receipt and the
success state remains “namespace created; control plane not installed.”

The namespace bridge has no renderer shell, argv, kubectl, apply, patch, delete,
Secret, or generic filesystem capability. Status/reconcile is the only follow-up
operation and is read-only and bounded. A timeout, duplicate, conflict, stale or
expired plan, or unknown create result never auto-replays create: the UI fences
the old plan and offers reconciliation. Switching project or control connection
clears the namespace UI and main-process references. The in-memory desktop
receipt is not a durable operator receipt; use the CLI's private `--receipt-output`
path for cross-restart reconciliation. Live Kubernetes use remains opt-in.

The coordinator workbench projects provider-disabled Astra conversation messages,
non-authoritative local coordinator proposals, human decision receipts, execution
milestones, and the selected experiment's evidence links and conclusion cards as
typed records. Conversation and Labbook filter that one projection rather than
copying history; Labbook includes the selected experiment's revision-bound evidence
and conclusion cards. Conversation messages, proposals, and explain-status turns
page independently, so a long category cannot hide records from another one. A
new experiment selection immediately removes the prior projection while its
complete ledger loads. Proposal cards show the complete structured candidate
(goal, criterion, protocol, variants, digests, metadata, and execution references)
before the separate human registration action. Its composer has explicit Discuss, Draft experiment, Explain
status, and Review results modes; selected task/experiment revision chips are sent
only when the human requests a turn. A turn neither approves nor executes work.
The renderer does not show or ask for raw harness/session identifiers, and stale,
offline, or held state disables its command controls.

The research-ledger panel can create a study with purpose, protocol and mode,
then register variants with method/input digests and explicit metadata. These are
attributed records; they do not launch work or prove that the supplied digests
identify a validated scientific method. Registration requires a loaded open study. A registered variant can receive a
revision-bound completed, failed, excluded, or abandoned record with explicit
manual/imported provenance. Exclusion and abandonment require a reason. Closing
a study requires every registered variant to be accounted for; it preserves all
variants and does not establish scientific validity.
Held recovery disables authoring. Context changes clear authoring drafts, and
uncertain submissions retain their command key for an explicit retry.

A loaded ledger also supports source recording, attributed support assessments,
finding proposals from explicitly selected source/variant records, and source
corrections. Acceptance shows the exact cited passages and requires an explicit
human action; unsupported or superseded citations block it. Confirmatory studies
must be closed and accounted for. Corrections preserve source versions and mark
dependent findings for review. A finding needing review can be explicitly
reaffirmed with a reason after its existing citations are supported again.
Reaffirmation preserves the statement, references, and original acceptance
attribution; superseded citations still block it. Explicit reasoned retraction
preserves a finding and its history in a terminal state, including when its
citations are no longer supported. Revision lets the reviewer narrow or supersede
a claim, explicitly selecting replacement evidence. It retains the original and
creates a linked candidate requiring separate acceptance, including in closed
studies. Superseded and retracted findings offer no further review actions.
The UI records judgment; it does not automatically
verify scientific claims or fetch cited URLs.

The panel lists a project's studies and renders
only a control-plane `complete_in_scope_study_ledger` envelope: every returned
variant, source, finding, correction, and ledger event. Recorded outcomes remain
manual or imported assertions where identified and have no execution receipt in
this slice. Source URIs are displayed as text; the renderer does not open them,
fetch external content. If the server cannot
produce its bounded complete ledger, the desktop shows an error rather than a
partial view labeled complete.

The goal-to-decision panel separately lists a project's experiments and renders
only a complete historical chain. Every immutable design revision retains its
goal, success criterion, protocol, baseline/intervention snapshots, registered
execution references, and unattempted variants. Legacy attempts and admitted
execution attempts are separate, linked projections: admitted attempts retain their
complete bounded semantic state history, latest operational sample state/cursor/time,
and exact operation/package binding, while
operation-backed legacy attempts display desired and observed states separately,
including unresolved termination. Conclusion cards retain any explicit human
supersession or retraction lineage with reason, actor, and recorded history, while
leaving the reviewed conclusion and its evidence intact. Human-reported observations
explicitly say that no operation was observed.
Explicit `legacy_operation_unverified` history remains visible as unverified;
the desktop does not invent its missing package, plan, or termination details.
Artifacts, measurements, and conclusions remain linked to their recorded
revision snapshots. The response is rejected unless its collections, identifiers,
unique records, digests, revision membership, state-history sequencing, correction
lineage, and all evidence/operation cross-links agree. After that complete read,
a human can separately approve the exact task plan, admit a variant against an
existing grant, and authorize a target-bound dispatch only for an already
packaged admitted attempt. These are distinct revision-fenced commands: approval
creates no grant, admission returns no package or dispatch, and dispatch makes no
worker or Kubernetes call. Retained artifact links, measurements, conclusion
drafting, and conclusion review stay separate human/evidence records. A requested
stop fences new work but is not a confirmed cancellation; only attributed
reconciled termination evidence is labeled confirmed, while unknown state and
exposure stay visible. Attempt `succeeded` and a reviewed conclusion remain
explicitly labeled operational/human records with scientific validity
`not_assessed`; they never display scientific success. Project or connection
switches discard pending experiment reads and commands, and incomplete,
stale-revision, or digest-inconsistent responses are not rendered. Disconnect and
reconnect re-read the durable workflow projection and never replay admission,
dispatch, stop, evidence, or conclusion commands.

“Save study ledger JSON” fetches a fresh, complete selected-study snapshot and
uses a native save dialog to create a private JSON file. Choose a new filename;
existing files are never replaced. Connection or study changes before publication
cancel the save. This also works for held restored history. The file preserves the
entire returned ledger envelope; it is not a reproduction bundle containing code,
artifacts, reports, or environment files.

Run the dependency-free boundary tests with `npm test --prefix desktop`.
Electron 44.2.0 and its dependencies are pinned in `package-lock.json`.
Use Node 22.12 or newer, then `npm ci --prefix desktop` and
`npm start --prefix desktop` against the current control plane. The boundary tests
are dependency-free; Electron installation is required to launch the interface.

Locked dependency installation and the native Electron smoke now pass. Run:

```sh
env -u ELECTRON_RUN_AS_NODE node desktop/test/electron-smoke.cjs
```

Run this from the repository root in a desktop-capable environment. The smoke
creates a temporary npm package, then launches that package through Electron
with temporary application state and a hidden native window. It checks the real
application URL, sandboxed preload bridge, renderer errors, Node-disabled
renderer, isolation settings, and a harmless renderer-to-main `status` IPC round
trip. It confirms Electron exits before reporting success and does not connect
to a control plane. It does not establish visual, signed-package, or full
desktop E2E acceptance.

`.venv/bin/python scripts/check_desktop_control.py` additionally passes a live
synthetic decision through the actual sandboxed renderer, IPC and loopback control
API. It uses temporary control and application state, with a test-only native
file-picker stub selecting the fixture credential. The fixture verifies human
attribution and absence of execution grants or leases. It also displays all 80
synthetic registered variants and source-correction history. It selects a complete
provider-disabled goal-to-decision history, verifies historical/current design and
inconclusive-outcome labels, creates and selects a separate project through the
UI, disconnects and reconnects, and confirms the same
read-only history returns without a control-plane mutation. It also creates a new
study and registers two variants through the UI, records failed/abandoned
outcomes, closes the accounted study, and checks the stored attribution and
metadata. The fixture also verifies unsupported-citation blocking, explicit
acceptance after assessment, and correction-induced review before export. It
then verifies that
browsing left the study ledger unchanged. It saves JSON on both the active and
held restored branches and compares each file with the complete control ledger. This exposed and corrected
the desktop's identity-field mismatch with the real `/me` response. It remains a
platform fixture, not a research investigation or visual acceptance test.

Renderer tests execute the application code with synthetic DOM nodes and deferred
API replies. They verify pagination, task/thread and research-record draft/dialog
clearing on project, connection, and authentication context switches, delayed
thread selection, overlapping refreshes, and retry keys after uncertain message
or decision-resolution responses. They also verify that stale decision actions do
not cross a project switch. They do not establish visual or Electron runtime
acceptance. Unsent message drafts are cleared when switching thread, project, or
connection.

Sources: [Electron security](https://www.electronjs.org/docs/latest/tutorial/security),
[stable releases](https://releases.electronjs.org/),
[credential storage](https://www.electronjs.org/docs/latest/api/safe-storage).
## Protected secret enrollment

Administration → Secrets supports metadata proposals and independently reviewed,
one-time native macOS Keychain enrollment/import. No renderer token field or
secret-read IPC is added. Configure the trusted native Python executable and
same-host state directory explicitly; remote/Kubernetes, Windows and Linux custody
are unavailable. See [setup, topology, recovery and threat model](../docs/protected-secret-enrollment.md).
Provider live activation stays disabled. Default tests use fake custody only.
