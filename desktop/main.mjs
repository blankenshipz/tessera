import { app, BrowserWindow, dialog, ipcMain, net, protocol, session } from 'electron';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ControlConnection } from './connection.mjs';
import { MAX_CREDENTIAL_HEADER_BYTES, parseCredentialHeader, parseControlTokenFile } from './control-credential.mjs';
import { runControlIssuance, nativeIssuanceDialogs } from './control-issuance.mjs';
import { NamespaceLifecycle } from './astra-namespace.mjs';
import { validateSender } from './ipc-policy.mjs';
import { saveStudyExport } from './study-export.mjs';
import { saveReleasedArtifact } from './artifact-save.mjs';
import contract from './ipc-contract.cjs';
import { runProtectedHelper, validateProtectedRequest } from './protected-secrets.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const assets = new Map([
  ['/index.html', 'index.html'], ['/style.css', 'style.css'], ['/renderer.mjs', 'renderer.mjs'],
  ['/appearance.js', 'appearance.js'],
]);
const connection = new ControlConnection();
const namespaceLifecycle = new NamespaceLifecycle({ picker: (parent, options) => dialog.showOpenDialog(parent, options) });
let window;
let connecting = false;
let selectedStudy = null;
let exporting = false;
let retrievingArtifact = false;
let secretContext = null;
let secretBusy = false;

function reportSmoke(type, details = {}) {
  const smokeToken = process.env.TESSERA_ELECTRON_SMOKE_TOKEN;
  if (smokeToken && process.stdout?.writable) {
    process.stdout.write(`TESSERA_ELECTRON_SMOKE ${JSON.stringify({ type, smokeToken, ...details })}\n`);
  }
}

async function reportSmokeWindow(candidate) {
  if (!process.env.TESSERA_ELECTRON_SMOKE_TOKEN) return;
  try {
    const renderer = await candidate.webContents.executeJavaScript(`(async () => ({
      href: location.href,
      readyState: document.readyState,
      bridge: typeof window.tessera === 'object' && typeof window.tessera.status === 'function',
       harnessContextBridge: typeof window.tessera?.harnessSessionBinding === 'function' &&
         typeof window.tessera?.enqueueHarnessDelivery === 'function' &&
         typeof window.tessera?.cancelHarnessDelivery === 'function',
       remoteWorkflowBridge: typeof window.tessera?.remoteWorkflows === 'function' &&
         typeof window.tessera?.remoteWorkflow === 'function' &&
         typeof window.tessera?.executionGrant === 'function' &&
         typeof window.tessera?.revokeExecutionGrant === 'function',
      connectionButton: Boolean(document.querySelector('#connection-open')),
      nodeRequire: typeof window.require,
      visibilityState: document.visibilityState,
      statusIsNull: await window.tessera.status() === null,
    }))()`, true);
    const preferences = candidate.webContents.getLastWebPreferences();
    reportSmoke('tessera-electron-window', {
      renderer,
      isolation: {
        sandbox: preferences.sandbox,
        contextIsolation: preferences.contextIsolation,
        nodeIntegration: preferences.nodeIntegration,
      },
      window: {
        visible: candidate.isVisible(), minimized: candidate.isMinimized(), bounds: candidate.getBounds(),
        nativeHandleBytes: candidate.getNativeWindowHandle().byteLength,
      },
    });
  } catch (error) {
    reportSmoke('tessera-electron-renderer-error', { message: `Smoke inspection failed: ${error.message}` });
  }
}

protocol.registerSchemesAsPrivileged([{ scheme: 'tessera', privileges: {
  standard: true, secure: true, supportFetchAPI: false,
} }]);
app.enableSandbox();

async function connect(settings) {
  if (connecting) throw new Error('Connection selection is already in progress.');
  if (!settings || typeof settings !== 'object') throw new Error('Connection settings are required.');
  connecting = true;
  // Disconnect first: cancellation or failed enrollment cannot leave stale data
  // authorized under the previous selected connection.
  connection.disconnect();
  try {
    const selection = await dialog.showOpenDialog(window, {
      title: 'Select a private control-plane credential file',
      properties: ['openFile'], filters: [{ name: 'Control credential', extensions: ['header', 'control-token'] }],
    });
    if (selection.canceled || selection.filePaths.length !== 1) return null;
    if (!constants.O_NOFOLLOW || !constants.O_NONBLOCK) {
      throw new Error('Safe credential-file selection is not supported on this platform yet.');
    }
    const file = await open(selection.filePaths[0], constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    let header;
    const buffer = Buffer.alloc(MAX_CREDENTIAL_HEADER_BYTES + 1);
    try {
      const info = await file.stat();
      if (!info.isFile() || info.size > MAX_CREDENTIAL_HEADER_BYTES || (info.mode & 0o077) || (process.getuid && info.uid !== process.getuid())) throw new Error('Select a private, owner-readable regular credential header file (chmod 600).');
      const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
      if (bytesRead > MAX_CREDENTIAL_HEADER_BYTES) throw new Error('Credential header is too large.');
      header = buffer.subarray(0, bytesRead).toString('utf8');
    } finally { buffer.fill(0); await file.close(); }
    const token = selection.filePaths[0].endsWith('.control-token') ? parseControlTokenFile(header) : parseCredentialHeader(header);
    // Session-only custody initially. No plaintext persistent credential fallback.
    return await connection.connect({ id: connection.commandId(), name: settings.name,
      endpoint: settings.endpoint, token });
  } finally { connecting = false; }
}

// Do not await readiness at module scope. Electron waits for its package entry
// module to finish before completing startup, so that cycle deadlocks `electron .`.
app.whenReady().then(() => {
protocol.handle('tessera', request => {
  const url = new URL(request.url);
  const asset = url.host === 'app' ? assets.get(url.pathname) : undefined;
  if (!asset || request.method !== 'GET') return new Response('Not found', { status: 404 });
  return net.fetch(pathToFileURL(join(root, asset)).href);
});
session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
session.defaultSession.setPermissionCheckHandler(() => false);

let issuanceBusy = false;
const handlers = {
  issueOperatorControlCredential: async (projectId, mode, minutes) => {
    if (issuanceBusy) throw new Error('Credential issuance is already in progress.');
    if (!['new', 'resume'].includes(mode) || !Number.isInteger(minutes) || minutes < 1 || minutes > 1440) throw new Error('Invalid credential issuance selection.');
    issuanceBusy = true;
    try {
      const context = secretContext;
      const current = () => {
        if (!context || secretContext !== context || context.connectionId !== connection.status()?.id || context.projectId !== projectId) throw new Error('Selected project changed.');
      };
      current();
      const issuer = await connection.prepareControlIssuance(projectId, current);
      return await runControlIssuance({ issuer, mode, minutes, ...nativeIssuanceDialogs(dialog, window) });
    } finally { issuanceBusy = false; }
  },
  status: () => connection.status(), connect: async settings => { selectedStudy = null; await namespaceLifecycle.clear(); return connect(settings); },
  disconnect: async () => { selectedStudy = null; await namespaceLifecycle.clear(); return connection.disconnect(); },
  autonomySessions: taskId => connection.autonomySessions(taskId),
  adminWorkspace: (projectId, page) => connection.adminWorkspace(projectId, page),
  adminCreate: (projectId, body, key) => connection.adminCreate(projectId, body, key),
  adminCommand: (projectId, recordId, body, key) => connection.adminCommand(projectId, recordId, body, key),
  adminAudit: (projectId, after) => connection.adminAudit(projectId, after),
  secretContext: projectId => {
    if (projectId !== null && (typeof projectId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(projectId))) throw new Error('Invalid project context.');
    connection.cancelProtectedSecrets();
    secretContext = { connectionId: connection.status()?.id, projectId };
  },
  protectedSecret: async request => {
    validateProtectedRequest(request);
    if (secretBusy) throw new Error('A protected secret operation is already in progress.');
    const context = secretContext;
    const current = () => {
      if (!context || secretContext !== context || context.connectionId !== connection.status()?.id || context.projectId !== request.project_id) throw new Error('Secret enrollment context changed.');
    };
    current(); secretBusy = true;
    try {
      return await connection.protectedSecret(request, options => runProtectedHelper({ ...options,
        python: process.env.TESSERA_SECRET_PYTHON, stateDir: process.env.TESSERA_SECRET_STATE_DIR }), current);
    } finally { secretBusy = false; }
  },
  remoteExecutions: taskId => connection.remoteExecutions(taskId),
  remoteExecution: routeId => connection.remoteExecution(routeId),
  remoteExecutionCommand: (routeId, body, key) => connection.remoteExecutionCommand(routeId, body, key),
  projects: page => connection.projects(page), tasks: (id, page) => connection.tasks(id, page), workers: (id, page) => connection.workers(id, page),
   taskPlan: (taskId, digest) => connection.taskPlan(taskId, digest), reviseTaskPlan: (taskId, body, key) => connection.reviseTaskPlan(taskId, body, key),
  recovery: () => connection.recovery(),
  threads: (id, page) => connection.threads(id, page), messages: (id, page) => connection.messages(id, page),
  harnessSessionBinding: id => connection.harnessSessionBinding(id),
  enqueueHarnessDelivery: (body, key) => connection.enqueueHarnessDelivery(body, key),
  cancelHarnessDelivery: (id, body, key) => connection.cancelHarnessDelivery(id, body, key),
  astraConversation: (id, page) => connection.astraConversation(id, page),
  astraProposals: (id, page) => connection.astraProposals(id, page),
  coordinatorConsumerStatus: id => connection.coordinatorConsumerStatus(id),
  requestCoordinatorTurn: (id, body, key) => connection.requestCoordinatorTurn(id, body, key),
  explainStatusTurns: (id, page) => connection.explainStatusTurns(id, page),
  requestExplainStatusTurn: (id, body, key) => connection.requestExplainStatusTurn(id, body, key),
   registerProposalDesign: (id, body, key) => connection.registerProposalDesign(id, body, key),
   decisions: (id, page) => connection.decisions(id, page),
    programContext: id => connection.programContext(id),
    experiments: (id, page) => connection.experiments(id, page), remoteWorkflows: (id, page) => connection.remoteWorkflows(id, page), remoteWorkflow: id => connection.remoteWorkflow(id), executionGrant: id => connection.executionGrant(id), revokeExecutionGrant: (id, body, key) => connection.revokeExecutionGrant(id, body, key), recoveryReconciliations: id => connection.recoveryReconciliations(id), reconcileLostWorkerOperation: (id, body, key) => connection.reconcileLostWorkerOperation(id, body, key), experiment: id => connection.experiment(id),
   studies: (id, page) => connection.studies(id, page), study: id => connection.study(id),
    artifacts: (id, page) => connection.artifacts(id, page), artifactManifests: (id, page) => connection.artifactManifests(id, page),
    recordArtifactCustodyRelease: (id, body, key) => connection.recordArtifactCustodyRelease(id, body, key),
    artifactDispositions: (id, page) => connection.artifactDispositions(id, page),
    recordArtifactDisposition: (id, body, key) => connection.recordArtifactDisposition(id, body, key),
    releasedArtifactManifest: id => connection.releasedArtifactManifest(id), artifactPreview: (manifestId, artifactId, maxBytes) => connection.artifactPreview(manifestId, artifactId, maxBytes),
    exportReproductionBundle: (id, body, key) => connection.exportReproductionBundle(id, body, key), reproductionBundle: id => connection.reproductionBundle(id),
  createStudy: (id, body, key) => connection.createStudy(id, body, key),
  registerVariant: (id, body, key) => connection.registerVariant(id, body, key),
  recordVariant: (id, body, key) => connection.recordVariant(id, body, key),
  closeStudy: (id, body, key) => connection.closeStudy(id, body, key),
  createSource: (id, body, key) => connection.createSource(id, body, key),
  assessSource: (id, body, key) => connection.assessSource(id, body, key),
  correctSource: (id, body, key) => connection.correctSource(id, body, key),
  createFinding: (id, body, key) => connection.createFinding(id, body, key),
  acceptFinding: (id, body, key) => connection.acceptFinding(id, body, key),
  reaffirmFinding: (id, body, key) => connection.reaffirmFinding(id, body, key),
  retractFinding: (id, body, key) => connection.retractFinding(id, body, key),
   reviseFinding: (id, body, key) => connection.reviseFinding(id, body, key),
   createExperiment: (id, body, key) => connection.createExperiment(id, body, key), reviseExperiment: (id, body, key) => connection.reviseExperiment(id, body, key),
   admitExperimentAttempt: (id, body, key) => connection.admitExperimentAttempt(id, body, key), dispatchAdmittedAttempt: (id, body, key) => connection.dispatchAdmittedAttempt(id, body, key),
   resolveAdmittedAttempt: (id, body, key) => connection.resolveAdmittedAttempt(id, body, key), linkAttemptArtifacts: (id, body, key) => connection.linkAttemptArtifacts(id, body, key),
   recordMeasurement: (id, body, key) => connection.recordMeasurement(id, body, key), createConclusion: (id, body, key) => connection.createConclusion(id, body, key),
   updateConclusion: (id, body, key) => connection.updateConclusion(id, body, key), reviewConclusion: (id, body, key) => connection.reviewConclusion(id, body, key), amendConclusion: (id, body, key) => connection.amendConclusion(id, body, key),
  studySelection: (studyId, selection, projectId) => {
    if (!Number.isSafeInteger(selection) || selection < 0) throw new Error('Invalid study selection.');
    selectedStudy = studyId === null ? null : { id: connection.status()?.id, studyId, selection, projectId };
  },
   saveStudyExport: async (studyId, selection) => {
    if (exporting) throw new Error('A study export is already in progress.');
    exporting = true;
    try {
      return await saveStudyExport({ dialog, window, connection, id: connection.status()?.id,
        studyId, selection, projectId: selectedStudy?.projectId,
        current: (id, study, version) => selectedStudy?.id === id &&
          selectedStudy.studyId === study && selectedStudy.selection === version });
     } finally { exporting = false; }
   },
   saveReleasedArtifact: async (manifestId, artifactId) => {
     if (retrievingArtifact) throw new Error('An artifact retrieval is already in progress.');
     retrievingArtifact = true;
     try {
       return await saveReleasedArtifact({ dialog, window, connection, manifestId, artifactId });
     } finally { retrievingArtifact = false; }
   },
  revisions: (id, page) => connection.revisions(id, page), labbook: (id, after) => connection.labbook(id, after),
  createProject: (body, key) => connection.createProject(body, key),
  createTask: (id, body, key) => connection.createTask(id, body, key),
  createThread: (body, key) => connection.createThread(body, key),
  sendMessage: (body, key) => connection.sendMessage(body, key),
   resolveDecision: (body, key) => connection.resolveDecision(body, key),
    createDecision: (body, key) => connection.createDecision(body, key),
   approve: (id, body, key) => connection.approve(id, body, key), commandId: () => connection.commandId(),
   namespaceSelectKubeconfig: () => namespaceLifecycle.selectKubeconfig(window),
   namespaceInspect: request => namespaceLifecycle.inspect(request),
   namespacePlanUseExisting: request => namespaceLifecycle.planUseExisting(request),
   namespacePlanCreate: request => namespaceLifecycle.planCreate(request),
   namespaceCreate: request => namespaceLifecycle.create(request),
   namespaceStatus: request => namespaceLifecycle.status(request),
   namespaceReconcile: request => namespaceLifecycle.reconcile(request),
   namespaceClear: () => namespaceLifecycle.clear(),
 };
 const connectionFree = new Set(['status', 'connect', 'disconnect', 'commandId',
   'namespaceSelectKubeconfig', 'namespaceInspect', 'namespacePlanUseExisting', 'namespacePlanCreate',
   'namespaceCreate', 'namespaceStatus', 'namespaceReconcile', 'namespaceClear']);
 if (Object.keys(handlers).some(method => !contract.rendererMethods.includes(method)) ||
     contract.rendererMethods.some(method => !(method in handlers))) {
   throw new Error('Desktop IPC contract and handlers differ.');
 }
for (const [method, handler] of Object.entries(handlers)) {
  ipcMain.handle(`tessera:${method}`, async (event, ...args) => {
    validateSender(event, window);
    if (JSON.stringify(args).length > 128 * 1024) throw new Error('Desktop request is too large.');
    if (!connectionFree.has(method)) {
      const expectedConnectionId = args.shift();
      connection.assertActive(expectedConnectionId);
    }
    return handler(...args);
  });
}

function createWindow() {
  window = new BrowserWindow({ width: 1280, height: 850, minWidth: 900, minHeight: 620,
    title: 'Tessera', backgroundColor: '#f4f2ed',
    // The opt-in smoke exercises the ordinary entrypoint but keeps its temporary
    // window off screen. Production windows remain visible.
    show: !process.env.TESSERA_ELECTRON_SMOKE_HIDDEN,
    webPreferences: { preload: join(root, 'preload.cjs'), nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false,
      allowRunningInsecureContent: false },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.on('console-message', details => {
    if (details.level === 'error') reportSmoke('tessera-electron-renderer-error', {
      message: details.message, lineNumber: details.lineNumber, sourceId: details.sourceId,
    });
  });
  window.webContents.on('render-process-gone', (_event, details) => {
    reportSmoke('tessera-electron-renderer-error', { message: `Renderer exited: ${details.reason}` });
  });
  window.webContents.on('preload-error', (_event, _path, error) => {
    reportSmoke('tessera-electron-renderer-error', { message: `Preload failed: ${error.message}` });
  });
  window.webContents.once('did-finish-load', () => { void reportSmokeWindow(window); });
  window.on('closed', () => { connection.disconnect(); void namespaceLifecycle.clear(); selectedStudy = null; window = null; });
  window.loadURL('tessera://app/index.html');
}
createWindow();
app.on('activate', () => { if (!window) createWindow(); });
 app.on('window-all-closed', () => { connection.disconnect(); void namespaceLifecycle.clear(); if (process.platform !== 'darwin') app.quit(); });
// Closing the desktop never stops the separately running control plane or jobs.
});
