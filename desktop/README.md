# Desktop workspace

This desktop slice keeps credentials and HTTP requests in Electron's main process.
The sandboxed renderer has a small operation-specific IPC bridge, a restrictive
CSP, no Node access, no remote navigation, and no external-content execution.
Each data request names its connection; switching connections cancels in-flight
reads and rejects stale replies. Mutation retries are explicit and stay with
their original command and connection.

The initial interface covers connections, projects, record-only plans, approval,
labbook history, attributed interaction threads/messages, and a read-only pending
decision inbox. It does not start experiments, resolve decisions, or claim a
complete research workflow. Credential files are selected through a native dialog and held only in
main-process memory; saved encrypted sessions and organization login are later
work. Closing the desktop does not stop the backend.

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

Renderer tests execute the application code with synthetic DOM nodes and deferred
API replies. They verify pagination, draft clearing on context switches, delayed
thread selection, overlapping refreshes, and retry keys after uncertain message
responses. They do not establish visual or Electron runtime acceptance. Unsent
message drafts are cleared when switching thread, project, or connection.

Sources: [Electron security](https://www.electronjs.org/docs/latest/tutorial/security),
[stable releases](https://releases.electronjs.org/),
[credential storage](https://www.electronjs.org/docs/latest/api/safe-storage).
