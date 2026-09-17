import { app, BrowserWindow, dialog, ipcMain, net, protocol, session } from 'electron';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ControlConnection } from './connection.mjs';
import { validateSender } from './ipc-policy.mjs';
import { saveStudyExport } from './study-export.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const assets = new Map([
  ['/index.html', 'index.html'], ['/style.css', 'style.css'], ['/renderer.mjs', 'renderer.mjs'],
]);
const connection = new ControlConnection();
let window;
let connecting = false;
let selectedStudy = null;
let exporting = false;

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
      title: 'Select the private control-plane credential header',
      properties: ['openFile'], filters: [{ name: 'Credential header', extensions: ['header'] }],
    });
    if (selection.canceled || selection.filePaths.length !== 1) return null;
    if (!constants.O_NOFOLLOW || !constants.O_NONBLOCK) {
      throw new Error('Safe credential-file selection is not supported on this platform yet.');
    }
    const file = await open(selection.filePaths[0], constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    let header;
    try {
      const info = await file.stat();
      if (!info.isFile() || info.size > 1024) throw new Error('Invalid credential header file.');
      const buffer = Buffer.alloc(1025);
      const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
      if (bytesRead > 1024) throw new Error('Credential header is too large.');
      header = buffer.subarray(0, bytesRead).toString('utf8');
    } finally { await file.close(); }
    const match = /^Authorization: Bearer ([A-Za-z0-9_-]{32,256})\r?\n?$/.exec(header);
    if (!match) throw new Error('Select a valid credential header.');
    // Session-only custody initially. No plaintext persistent credential fallback.
    return await connection.connect({ id: connection.commandId(), name: settings.name,
      endpoint: settings.endpoint, token: match[1] });
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

const handlers = {
  status: () => connection.status(), connect: async settings => { selectedStudy = null; return connect(settings); },
  disconnect: () => { selectedStudy = null; return connection.disconnect(); },
  projects: page => connection.projects(page), tasks: (id, page) => connection.tasks(id, page), workers: (id, page) => connection.workers(id, page),
  taskPlan: (taskId, digest) => connection.taskPlan(taskId, digest),
  recovery: () => connection.recovery(),
  threads: (id, page) => connection.threads(id, page), messages: (id, page) => connection.messages(id, page),
  harnessSessionBinding: id => connection.harnessSessionBinding(id),
  enqueueHarnessDelivery: (body, key) => connection.enqueueHarnessDelivery(body, key),
  cancelHarnessDelivery: (id, body, key) => connection.cancelHarnessDelivery(id, body, key),
  astraConversation: (id, page) => connection.astraConversation(id, page),
  astraProposals: (id, page) => connection.astraProposals(id, page),
  requestCoordinatorTurn: (id, body, key) => connection.requestCoordinatorTurn(id, body, key),
  explainStatusTurns: (id, page) => connection.explainStatusTurns(id, page),
  requestExplainStatusTurn: (id, body, key) => connection.requestExplainStatusTurn(id, body, key),
  registerProposalDesign: (id, body, key) => connection.registerProposalDesign(id, body, key),
  decisions: (id, page) => connection.decisions(id, page),
   experiments: (id, page) => connection.experiments(id, page), remoteWorkflows: (id, page) => connection.remoteWorkflows(id, page), remoteWorkflow: id => connection.remoteWorkflow(id), executionGrant: id => connection.executionGrant(id), revokeExecutionGrant: (id, body, key) => connection.revokeExecutionGrant(id, body, key), experiment: id => connection.experiment(id),
  studies: (id, page) => connection.studies(id, page), study: id => connection.study(id),
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
  revisions: (id, page) => connection.revisions(id, page), labbook: (id, after) => connection.labbook(id, after),
  createProject: (body, key) => connection.createProject(body, key),
  createTask: (id, body, key) => connection.createTask(id, body, key),
  createThread: (body, key) => connection.createThread(body, key),
  sendMessage: (body, key) => connection.sendMessage(body, key),
  resolveDecision: (body, key) => connection.resolveDecision(body, key),
  approve: (id, body, key) => connection.approve(id, body, key), commandId: () => connection.commandId(),
};
for (const [method, handler] of Object.entries(handlers)) {
  ipcMain.handle(`tessera:${method}`, async (event, ...args) => {
    validateSender(event, window);
    if (JSON.stringify(args).length > 128 * 1024) throw new Error('Desktop request is too large.');
    if (!['status', 'connect', 'disconnect', 'commandId'].includes(method)) {
      const expectedConnectionId = args.shift();
      connection.assertActive(expectedConnectionId);
    }
    return handler(...args);
  });
}

function createWindow() {
  window = new BrowserWindow({ width: 1280, height: 850, minWidth: 900, minHeight: 620,
    title: 'Tessera', backgroundColor: '#f4f2ed',
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
  window.on('closed', () => { connection.disconnect(); selectedStudy = null; window = null; });
  window.loadURL('tessera://app/index.html');
}
createWindow();
app.on('activate', () => { if (!window) createWindow(); });
app.on('window-all-closed', () => { connection.disconnect(); if (process.platform !== 'darwin') app.quit(); });
// Closing the desktop never stops the separately running control plane or jobs.
});
