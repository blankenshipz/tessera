import { app, BrowserWindow, dialog, ipcMain, net, protocol, session } from 'electron';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ControlConnection } from './connection.mjs';
import { validateSender } from './ipc-policy.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const assets = new Map([
  ['/index.html', 'index.html'], ['/style.css', 'style.css'], ['/renderer.mjs', 'renderer.mjs'],
]);
const connection = new ControlConnection();
let window;
let connecting = false;
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

await app.whenReady();
protocol.handle('tessera', request => {
  const url = new URL(request.url);
  const asset = url.host === 'app' ? assets.get(url.pathname) : undefined;
  if (!asset || request.method !== 'GET') return new Response('Not found', { status: 404 });
  return net.fetch(pathToFileURL(join(root, asset)).href);
});
session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
session.defaultSession.setPermissionCheckHandler(() => false);

const handlers = {
  status: () => connection.status(), connect,
  disconnect: () => connection.disconnect(),
  projects: page => connection.projects(page), tasks: (id, page) => connection.tasks(id, page),
  threads: (id, page) => connection.threads(id, page), messages: (id, page) => connection.messages(id, page),
  decisions: (id, page) => connection.decisions(id, page),
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
  window.on('closed', () => { connection.disconnect(); window = null; });
  window.loadURL('tessera://app/index.html');
}
createWindow();
app.on('activate', () => { if (!window) createWindow(); });
app.on('window-all-closed', () => { connection.disconnect(); if (process.platform !== 'darwin') app.quit(); });
// Closing the desktop never stops the separately running control plane or jobs.
