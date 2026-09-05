'use strict';

const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');

const { app } = require('electron');

const timeoutMs = 15_000;
const userDataDir = mkdtempSync(join(tmpdir(), 'tessera-electron-smoke-'));
let timeout;
let window;
const rendererErrors = [];
let loadFailure;
let loadFinished;
const load = new Promise((resolve, reject) => {
  loadFinished = resolve;
  loadFailure = reject;
});

function observe(candidate) {
  window = candidate;
  candidate.webContents.on('did-finish-load', () => loadFinished());
  candidate.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    loadFailure(new Error(`did-fail-load ${errorCode}: ${errorDescription} (${validatedURL})`));
  });
  candidate.webContents.on('render-process-gone', (_event, details) => {
    loadFailure(new Error(`render-process-gone ${details.reason}`));
  });
  candidate.webContents.on('console-message', details => {
    if (details.level === 'error') rendererErrors.push(details.message);
  });
  candidate.webContents.on('preload-error', (_event, _path, error) => loadFailure(error));
}

app.on('browser-window-created', (_event, candidate) => observe(candidate));

async function main() {
  if (app.commandLine.hasSwitch('no-sandbox')) throw new Error('Smoke requires the application sandbox.');
  app.setPath('userData', userDataDir);
  await import('../main.mjs');
  await app.whenReady();
  if (!window) throw new Error('Electron did not create an application window.');
  const preferences = window.webContents.getLastWebPreferences();
  if (!preferences.sandbox || !preferences.contextIsolation || preferences.nodeIntegration) {
    throw new Error('Application isolation settings are missing.');
  }
  await Promise.race([
    load,
    new Promise((_, reject) => setTimeout(() => reject(new Error('Timed out waiting for renderer load.')), timeoutMs)),
  ]);
  const state = await window.webContents.executeJavaScript(`({
    href: location.href,
    readyState: document.readyState,
    bridge: typeof window.tessera === 'object' && typeof window.tessera.status === 'function',
    connectionButton: Boolean(document.querySelector('#connection-open')),
    nodeRequire: typeof window.require,
  })`, true);
  if (state.href !== 'tessera://app/index.html') throw new Error(`Unexpected location: ${state.href}`);
  if (state.readyState !== 'complete') throw new Error(`Unexpected readyState: ${state.readyState}`);
  if (!state.bridge) throw new Error('Renderer bridge is missing or malformed.');
  if (!state.connectionButton) throw new Error('Connection button was not rendered.');
  if (state.nodeRequire !== 'undefined') throw new Error(`Renderer exposes require: ${state.nodeRequire}`);
  if (rendererErrors.length) throw new Error(`Renderer console errors: ${rendererErrors.join('; ')}`);
  console.log(JSON.stringify({ ...state, rendererErrors }));
}

(async () => {
  try {
    await Promise.race([
      main(),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('Electron smoke timed out.')), timeoutMs); }),
    ]);
  } catch (error) {
    console.error(error.stack || error);
    process.exitCode = 1;
  } finally {
    clearTimeout(timeout);
    rmSync(userDataDir, { recursive: true, force: true });
    app.exit(process.exitCode || 0);
  }
})();
