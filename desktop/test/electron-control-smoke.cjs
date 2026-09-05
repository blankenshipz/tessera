'use strict';
// Opt-in test entry point. Never loaded by the production application.
const { mkdtempSync, readFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { app, dialog } = require('electron');
const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const userDataDir = mkdtempSync(join(tmpdir(), 'tessera-electron-control-'));
let window;
let timer;
const errors = [];
let loaded;
let failed;
const load = new Promise((resolve, reject) => { loaded = resolve; failed = reject; });
app.on('browser-window-created', (_event, candidate) => {
  window = candidate;
  candidate.webContents.on('did-finish-load', loaded);
  candidate.webContents.on('did-fail-load', () => failed(new Error('Renderer load failed.')));
  candidate.webContents.on('render-process-gone', () => failed(new Error('Renderer exited.')));
  candidate.webContents.on('preload-error', (_event, _path, error) => failed(error));
  candidate.webContents.on('console-message', details => {
    if (details.level === 'error') errors.push(details.message);
  });
});
// Only the native picker is substituted; the main process still reads and
// validates the temporary header, and no bearer token enters renderer memory.
dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [fixture.header] });
async function main() {
  if (app.commandLine.hasSwitch('no-sandbox')) throw new Error('Sandbox is required.');
  app.setPath('userData', userDataDir);
  await import('../main.mjs');
  await load;
  const preferences = window.webContents.getLastWebPreferences();
  if (!preferences.sandbox || !preferences.contextIsolation || preferences.nodeIntegration) {
    throw new Error('Application isolation settings are missing.');
  }
  const result = await window.webContents.executeJavaScript(`(async () => {
    const expected = ${JSON.stringify({ endpoint: fixture.endpoint, projectId: fixture.projectId, decisionId: fixture.decisionId })};
    const waitFor = async predicate => {
      const deadline = Date.now() + 10000;
      while (!predicate()) {
        if (Date.now() > deadline) throw new Error('UI condition timed out: ' + document.querySelector('#notice').textContent);
        await new Promise(resolve => setTimeout(resolve, 25));
      }
    };
    document.querySelector('#connection-open').click();
    const form = document.querySelector('#connection-form');
    form.elements.name.value = 'Synthetic fixture';
    form.elements.endpoint.value = expected.endpoint;
    form.requestSubmit();
    await waitFor(() => document.querySelector('#projects button'));
    const project = [...document.querySelectorAll('#projects button')].find(button => button.dataset.projectId === expected.projectId);
    if (!project) throw new Error('Expected synthetic project is missing.');
    project.click();
    await waitFor(() => document.querySelector('#decisions textarea'));
    const card = [...document.querySelectorAll('#decisions article')].find(node => node.textContent.includes(expected.decisionId));
    if (!card) throw new Error('Expected synthetic decision is missing.');
    card.querySelector('textarea').value = 'Synthetic UI acceptance fixture.';
    [...card.querySelectorAll('button')].find(button => button.textContent === 'Approve').click();
    await waitFor(() => document.querySelector('#decisions').textContent.includes('No pending decisions.'));
    if (typeof window.require !== 'undefined') throw new Error('Node is exposed to renderer.');
    return { href: location.href, decisionRemoved: true };
  })()`, true);
  if (result.href !== 'tessera://app/index.html' || errors.length) throw new Error('Unexpected renderer state.');
  console.log(JSON.stringify(result));
}
(async () => {
  let status = 0;
  try {
    await Promise.race([main(), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Desktop control smoke timed out.')), 25000);
    })]);
  } catch (error) {
    console.error(error.stack || error); status = 1;
  } finally {
    clearTimeout(timer);
    rmSync(userDataDir, { recursive: true, force: true });
    app.exit(status);
  }
})();
