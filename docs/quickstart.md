# Tessera local macOS alpha quickstart

This is one developer-alpha path: start a local control service, open the desktop,
create a project and experiment design, then inspect results from a separately
authorized baseline. The app does not itself issue execution grants or establish
production readiness. The evidence below used the actual renderer and authenticated
local backend in a test simulator with platform interactions explicitly simulated;
that is distinct from the native macOS picker/save journey.

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

In **Your research workflow**, choose **Connect**, use the endpoint
`http://127.0.0.1:8765`, and select `.tessera/owner.header` with the native file
picker. Confirm the displayed account/organization. The app does not start or
initialize the service for you. For an existing service, use only the endpoint
and private credential file supplied by its operator.

## Follow the supported screen flow

1. **Connect** to the running local service; the disconnected guide remains visible
   if it is unavailable.
2. Choose **New project** (or **Create your first project**), enter a project name,
   and choose **Create project**; otherwise select an existing project. Then in
   **Goal-to-decision experiments**, choose **New experiment**, enter its goal,
   success criterion, protocol and baseline/intervention details with real
   method/input digests, then choose **Record design**. This records a design
   only—not approval, a grant, a dispatch, or a result.
3. **Execution is a separate operator handoff.** The exact task revision, plan,
   input snapshot, grant, configured target and worker must be authorized and
   prepared through the control API/operator workflow before admission and dispatch.
   The desktop controls do not create those authorities or start a job. Follow the
   bounded [desktop execution procedure](../desktop/EXECUTION.md); do not dispatch
   without its prerequisites and an operator-issued grant.
4. Once a run exists, select the experiment to inspect its measurements, attempts
   and artifact inventory. For quarantined output, choose **Review quarantine
   retention** → **Load exact output metadata**, inspect it, then submit the human
    decision with **Record human retain-quarantined review**. Retrieval separately
    requires **Record custody release** → select **Release researcher retrieval**
    → **Record custody action**. Neither decision classifies data or accepts
    scientific results.
5. For an already released output, choose **Input and artifact review** → **Inspect
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
  newer. Check `node --version`; this checkout's earlier 22.2.0 host was below
  that requirement and does not validate the newcomer install step.
- **Kubernetes namespace warning:** Kubernetes setup is optional for this local
  quickstart. The warning does not install or configure a control plane.
- **Stale save or offline screen:** reconnect and review the selected project and
  current records before retrying a user action; do not assume a save or dispatch
  occurred from a quiet/stale screen.

**What the bounded journey established:** the current renderer used the real local
backend for project/design/study-ledger operations and, in a separate simulator
session, displayed results from one successful, operator-authorized Docker Desktop
Kubernetes CPU baseline. The 1,393-byte result measured test MSE 131.0623 and MAE
9.8565 over 1,667 rows; it was marked late, quarantined and unclassified. The
simulator read retention/custody records already written by an authorized operator
through the API; it did not submit another retention review or
custody release, perform a personal UI review, or change classification. It
previewed and retrieved the exact released bytes through a simulated destination,
created one reproduction-bundle record, then reconnected and restored the same
records. The study-ledger export is a distinct 2,875-byte ledger,
not the execution result. These measurements do not establish scientific validity.

These connected-flow checks used a test-only platform shim; the file-picker/save
destination was simulated, not a native macOS chooser. The earlier real Electron
capture showed the disconnected screen. This session did not verify a fresh native
Connect/picker or OS save-dialog journey. Host Node 22.2.0 is below the declared
22.12.0 minimum, so the newcomer npm install prerequisite remains unverified.
Production-owner, genuine remote reasoning, independent-host/device and scientific
acceptance remain separate; see the [release evidence](release-evidence-intent9.md)
and [implementation journal](remote-agent-implementation-journal.md).
