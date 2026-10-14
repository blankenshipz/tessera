# Tessera local macOS alpha quickstart

Use this path to preserve the question, method and evidence behind your next research
decision, rather than reconstructing them from chat and result folders. This is
one developer-alpha path: start a local control service, open the desktop,
create a project and experiment design, then inspect results from a separately
authorized baseline. The app does not itself issue execution grants or establish
production readiness. Renderer checks used a real authenticated backend but
simulated platform interactions; native macOS picker/save acceptance is separate.

For example, ask whether one preregistered coefficient change improves a tabular
predictor against its baseline on unchanged data splits. The
[tabular starter](tabular-starter.md) specifies that bounded engineering example.
The expected result here is a durable design and, after the separate authorized
execution handoff, inspectable measurements and output records—not an autonomous
agent run or a scientifically validated conclusion.

## Prerequisites and setup

On macOS, install Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Node 22.12.0+, and npm. The Python control service and Electron desktop are
separate installs. From a trusted Tessera checkout:

```sh
uv sync --locked --extra mcp
uv run --locked tessera init --state-dir .tessera
```

Initialize **once in a new state directory**; `init` refuses an existing one.
Keep `.tessera/owner.header` private. It is the local control credential; never
paste its contents into the app, a terminal command, chat, or an agent. On later
starts, run only:

```sh
uv run --locked tessera serve --state-dir .tessera --port 8765
```

In another terminal, install and launch the desktop:

```sh
npm ci --prefix desktop
npm start --prefix desktop
```

In the sidebar's **Control plane** panel, choose **Connect**, use the endpoint
`http://127.0.0.1:8765`, and select `.tessera/owner.header` with the native file
picker. Confirm the displayed account/organization. The app does not start or
initialize the service for you. For an existing service, use only the endpoint
and private credential file supplied by its operator.

## Everyday use: plan, monitor, review and revisit

The project opens in **Experiments**. The sidebar groups projects and research
destinations. Use **Plans & decisions** for proposals and
human review, **Research ledger** for studies and findings, **Conversation** for
coordinator/thread records, and **Operations** for workers and remote recovery.
**Administration** is a separate destination; **Return to research** restores the
research view. Connection/recovery warnings remain outside these views. Switching
views does not change authority or run work. Expand **Identifiers & provenance**
for full design IDs/digests and **Historical design revisions** for prior designs;
unresolved states and custody warnings remain visible.

Choose **Appearance** at the bottom of the sidebar to select **System**, **Light**
or **Dark**, and a **Plum**, **Lagoon** or **Graphite** palette. Preferences apply
immediately and stay on this desktop across reload and reconnect; System follows
the OS. Escape closes the dialog and restores focus. Appearance never changes a
connection, research record or execution permission. At the 900px minimum width,
detail panes stack and actions wrap; long sidebar content and dialogs can scroll.

To include coordinator context, open **Experiments** or **Plans & decisions**,
choose **Include experiment in coordinator context** or **Include plan in
coordinator context**, then return to **Conversation**. Recording context does
not grant authority or request execution.

1. **Connect** to the running local service; the disconnected guide remains visible
   if it is unavailable.
2. Choose **New project** (or **Create your first project**), enter a project name,
   and choose **Create project**; otherwise select an existing project. Then in
    **Experiments** → **Goal-to-decision experiments**, choose **New experiment**, enter its goal,
   success criterion, protocol and baseline/intervention details with real
   method/input digests, then choose **Record design**. This records a design
   only—not approval, a grant, a dispatch, or a result.
3. **Execution is a separate operator handoff.** The exact task revision, plan,
   input snapshot, grant, configured target and worker must be authorized and
   prepared through the control API/operator workflow before admission and dispatch.
   The desktop controls do not create those authorities or start a job. Follow the
   bounded [desktop execution procedure](../desktop/EXECUTION.md); do not dispatch
   without its prerequisites and an operator-issued grant.
4. In **Experiments**, once a run exists, select the experiment to inspect its measurements, attempts
   and artifact inventory. For quarantined output, choose **Review quarantine
   retention** → **Load exact output metadata**, inspect it, then submit the human
    decision with **Record human retain-quarantined review**. Retrieval separately
    requires **Record custody release** → select **Release researcher retrieval**
    → **Record custody action**. Neither decision classifies data or accepts
    scientific results.
5. For an already released output, in **Experiments** choose **Input and artifact review** → **Inspect
   released artifact**, enter its released manifest and artifact IDs from the
   selected experiment's inventory, then **Load released preview**. **Save exact
   released bytes** opens the artifact-save path.
   **Export reproduction bundle record** creates a control-plane record, not a
   filesystem bundle. Full experiment-chain JSON export remains API-first.
6. To export a separate study ledger, open **Research ledger** → **New study**,
   enter purpose and protocol, choose exploratory/confirmatory mode, then choose
   **Create study**. Select it and choose **Save study ledger JSON**. This exports
   the ledger, not the experiment design or execution-result artifact. Reconnect
   and reselect the project/experiment to refresh durable records; reconnect does
   not replay commands.

Use the design's question and criterion when reviewing the measurements: record
whether to continue, revise or reject the intervention, and link the evidence in
the research ledger. A failed process is not a negative scientific finding; missing
or incomparable measurements leave the comparison inconclusive. Human conclusions
and corrections are records, not automatic validation by an agent.

To leave, close the desktop. For this local setup, stop your own `tessera serve`
process when finished and restart it with the same state directory on return; do
not initialize again. Closing the app or stopping the control service is **not**
an execution-stop procedure. If an attempt is active, use the separate operator
workflow and retain confirmed termination/reconciliation evidence before treating
it as stopped. Reconnect and reselect the investigation to inspect its history
without rerunning it.

For Python setup and development checks, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Troubleshooting and limits

- **Service unavailable / Connect fails:** confirm `tessera serve` is still running
  and the endpoint uses the same port. If 8765 is occupied, stop only a process
  you own or choose a free port in both the serve command and Connect dialog.
- **Credential rejected:** reselect the owner-controlled header for this service
  through the native picker. Do not paste a token, use a provider key, or loosen
  permissions. Authentication errors do not authorize a new grant.
- **Initialization refuses:** use a different, new state directory only if you
  intend to create a separate local service. Never rerun `init` over existing
  research state.
- **Desktop install rejects Node:** the declared requirement is Node 22.12.0 or
  newer. Check `node --version`; launching Electron on an older host does not
  validate the npm installation prerequisite.
- **Kubernetes namespace warning:** Kubernetes setup is optional for this local
  quickstart. The warning does not install or configure a control plane.
- **Stale save or offline screen:** reconnect and review the selected project and
  current records before retrying a user action; do not assume a save or dispatch
  occurred from a quiet/stale screen.

Earlier evaluation inspected an operator-authorized CPU result. The current UI
gallery instead uses controlled test records with a real isolated local backend:
no experiment execution or model calls. Selectors/save destinations are simulated;
the captures do not establish native dialogs or scientific validity.
Execution, study-ledger JSON and reproduction-bundle records are distinct exports.

Production identity, genuine agent reasoning, independent-host operation and
scientific acceptance remain separate. See [capabilities and limits](implemented-capabilities.md)
and [recovery](recovery.md) before operating valuable state.
