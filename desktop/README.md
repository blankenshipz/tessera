# Desktop workspace

When the connected control service reports a held recovery state, the desktop shows
recovered history in read-only mode. It cannot clear the hold or resume mutations.
The recovery banner does not claim that the original control service or a provider
job was stopped; see [the recovery runbook](../docs/recovery.md).

This desktop slice keeps credentials and HTTP requests in Electron's main process.
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

The panel lists a project's studies and renders
only a control-plane `complete_in_scope_study_ledger` envelope: every returned
variant, source, finding, correction, and ledger event. Recorded outcomes remain
manual or imported assertions where identified and have no execution receipt in
this slice. Source URIs are displayed as text; the renderer does not open them,
fetch external content, or accept findings. If the server cannot
produce its bounded complete ledger, the desktop shows an error rather than a
partial view labeled complete.

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
env -u ELECTRON_RUN_AS_NODE desktop/node_modules/.bin/electron desktop/test/electron-smoke.cjs
```

Run this from the repository root in a desktop-capable environment. The smoke
uses temporary application state, checks the real application URL and renderer
bridge, rejects disabled sandbox settings, and reports renderer errors. It does
not connect to a control plane or establish visual, signed-package, or full
desktop E2E acceptance.

`.venv/bin/python scripts/check_desktop_control.py` additionally passes a live
synthetic decision through the actual sandboxed renderer, IPC and loopback control
API. It uses temporary control and application state, with a test-only native
file-picker stub selecting the fixture credential. The fixture verifies human
attribution and absence of execution grants or leases. It also displays all 80
synthetic registered variants and source-correction history. It also creates a new
study and registers two variants through the UI, records failed/abandoned
outcomes, closes the accounted study, and checks the stored attribution and
metadata, then verifies that
browsing left the study ledger unchanged. It saves JSON on both the active and
held restored branches and compares each file with the complete control ledger. This exposed and corrected
the desktop's identity-field mismatch with the real `/me` response. It remains a
platform fixture, not a research investigation or visual acceptance test.

Renderer tests execute the application code with synthetic DOM nodes and deferred
API replies. They verify pagination, draft clearing on context switches, delayed
thread selection, overlapping refreshes, and retry keys after uncertain message
or decision-resolution responses. They also verify that stale decision actions do
not cross a project switch. They do not establish visual or Electron runtime
acceptance. Unsent message drafts are cleared when switching thread, project, or
connection.

Sources: [Electron security](https://www.electronjs.org/docs/latest/tutorial/security),
[stable releases](https://releases.electronjs.org/),
[credential storage](https://www.electronjs.org/docs/latest/api/safe-storage).
