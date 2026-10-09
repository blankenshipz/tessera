'use strict';
// Opt-in, real visible native window. No backend, credentials or picker substitute.
const { mkdirSync, mkdtempSync, rmSync, writeFileSync } = require('node:fs');
const { join, resolve } = require('node:path');
const { app } = require('electron');
const output = resolve(process.argv[2] || '/private/tmp/opencode/tessera-desktop-screens');
mkdirSync(output, { recursive: true, mode: 0o700 });
const profile = mkdtempSync(join(output, 'profile-'));
app.setPath('userData', profile);
let window;
let timer;
let resolveLoad, rejectLoad;
const loaded = new Promise((yes, no) => { resolveLoad = yes; rejectLoad = no; });
app.on('browser-window-created', (_event, candidate) => {
  window = candidate;
  candidate.webContents.once('did-finish-load', resolveLoad);
  candidate.webContents.once('did-fail-load', () => rejectLoad(new Error('Renderer load failed.')));
  candidate.webContents.once('preload-error', (_event, _path, error) => rejectLoad(error));
});
async function capture(name) {
  await window.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
  const image = await window.webContents.capturePage();
  writeFileSync(join(output, name), image.toPNG(), { mode: 0o600 });
}
async function main() {
  await import('../main.mjs');
  await loaded;
  if (!window.isVisible()) throw new Error('Native window is not visible.');
  await window.webContents.executeJavaScript(`document.querySelector('#journey-heading').parentElement.querySelector('details').open = true`);
  await capture('01-disconnected-workflow.png');
  const disconnected = await window.webContents.executeJavaScript(`({
    heading: document.querySelector('#journey-heading').textContent,
    next: document.querySelector('#journey-next').textContent,
    steps: [...document.querySelector('#journey-heading').parentElement.querySelectorAll('li')].map(n => n.textContent),
    credentialInRenderer: Object.keys(window.tessera).some(key => /token|password/i.test(key))
  })`);
  await window.webContents.executeJavaScript(`document.querySelector('#connection-open').click()`);
  await capture('02-connect-dialog.png');
  const connect = await window.webContents.executeJavaScript(`({
    open: document.querySelector('#connection-dialog').open,
    endpoint: document.querySelector('#connection-form').elements.endpoint.value,
    action: document.querySelector('#connection-form button[type=submit]').textContent
  })`);
  if (!connect.open || disconnected.steps.length !== 7 || disconnected.credentialInRenderer) throw new Error('Unexpected screen state.');
  const result = { visibleNativeWindow: window.isVisible(), disconnected, connect,
    unverified: ['native credential picker', 'authenticated connect', 'create/import', 'propose/approve', 'real service execution', 'review/export', 'reconnect'] };
  writeFileSync(join(output, 'screen-state.json'), JSON.stringify(result, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(result));
}
Promise.race([main(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Native screen check timed out.')), 15000); })])
  .catch(error => { console.error(error.message); process.exitCode = 1; })
  .finally(() => { clearTimeout(timer); if (window && !window.isDestroyed()) window.destroy(); rmSync(profile, { recursive: true, force: true }); app.exit(process.exitCode || 0); });
