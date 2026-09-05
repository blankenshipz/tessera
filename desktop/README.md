# Desktop workspace

This desktop slice keeps credentials and HTTP requests in Electron's main process.
The sandboxed renderer has a small operation-specific IPC bridge, a restrictive
CSP, no Node access, no remote navigation, and no external-content execution.
Each data request names its connection; switching connections cancels in-flight
reads and rejects stale replies. Mutation retries are explicit and stay with
their original command and connection.

The initial interface covers connections, projects, record-only plans, approval
and labbook history. It does not start experiments or claim a complete research
workflow. Credential files are selected through a native dialog and held only in
main-process memory; saved encrypted sessions and organization login are later
work. Closing the desktop does not stop the backend.

Run the dependency-free boundary tests with `npm test --prefix desktop`.
Electron 44.2.0 and its dependencies are pinned in `package-lock.json`.
Use Node 22.12 or newer, then `npm ci --prefix desktop` and
`npm start --prefix desktop` against the current control plane. The boundary tests
are dependency-free; Electron installation is required to launch the interface.

The lockfile was generated and audited without reported vulnerabilities. Full
installation currently fails fetching a dependency tarball due to a registry DNS
error in this session. Electron launch, visual QA, signed packaging and desktop
E2E tests have **not** been validated.

Sources: [Electron security](https://www.electronjs.org/docs/latest/tutorial/security),
[stable releases](https://releases.electronjs.org/),
[credential storage](https://www.electronjs.org/docs/latest/api/safe-storage).
