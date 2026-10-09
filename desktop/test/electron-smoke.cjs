'use strict';

const { execFileSync, spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');

const electron = require('electron');

const timeoutMs = 15_000;
const userDataDir = mkdtempSync(join(tmpdir(), 'tessera-electron-smoke-'));
const packageDir = mkdtempSync(join(tmpdir(), 'tessera-electron-package-'));
let timeout;
let child;
let output = '';
const rendererErrors = [];
const smokePrefix = 'TESSERA_ELECTRON_SMOKE ';

function waitForChildExit(milliseconds) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise(resolve => {
    const timer = setTimeout(() => { child.removeListener('exit', exited); resolve(false); }, milliseconds);
    const exited = () => { clearTimeout(timer); resolve(true); };
    child.once('exit', exited);
  });
}

async function stopChild() {
  if (!child) return null;
  if (child.exitCode === null && child.signalCode === null) {
    child.kill('SIGTERM');
    if (!await waitForChildExit(3_000)) {
      child.kill('SIGKILL');
      if (!await waitForChildExit(3_000)) throw new Error('Electron child did not exit after SIGKILL.');
    }
  }
  return { exitCode: child.exitCode, signalCode: child.signalCode };
}

async function main() {
  const { ELECTRON_RUN_AS_NODE: _ignored, ...environment } = process.env;
  const smokeToken = randomBytes(32).toString('hex');
  environment.TESSERA_ELECTRON_SMOKE_TOKEN = smokeToken;
  environment.TESSERA_ELECTRON_SMOKE_HIDDEN = '1';
  const source = join(__dirname, '..');
  const packed = JSON.parse(execFileSync('npm', ['pack', '--json', '--pack-destination', packageDir], {
    cwd: source, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  }));
  if (!Array.isArray(packed) || packed.length !== 1 || !packed[0]?.filename) {
    throw new Error('npm pack did not produce one desktop package.');
  }
  execFileSync('tar', ['-xzf', join(packageDir, packed[0].filename), '-C', packageDir], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const packagedApp = join(packageDir, 'package');
  child = spawn(electron, [packagedApp, `--user-data-dir=${userDataDir}`], {
    cwd: packagedApp, env: environment, stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stderr.on('data', chunk => { output = (output + chunk).slice(-16_384); });
  const windowObservation = new Promise((resolve, reject) => {
    let stdout = '';
    child.stdout.on('data', chunk => {
      stdout += chunk;
      const lines = stdout.split('\n');
      stdout = lines.pop();
      for (const line of lines) {
        if (!line.startsWith(smokePrefix)) continue;
        let message;
        try { message = JSON.parse(line.slice(smokePrefix.length)); } catch { continue; }
        if (message?.smokeToken !== smokeToken) continue;
        if (message.type === 'tessera-electron-renderer-error') {
          rendererErrors.push(message);
          reject(new Error(`Renderer error: ${message.message}. Output:\n${output}`));
        }
        if (message.type === 'tessera-electron-window') resolve(message);
      }
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => reject(new Error(
      `Electron exited before reporting its window (${code ?? signal}). Output:\n${output}`,
    )));
  });
  const observation = await windowObservation;
  const { renderer: state, isolation, window: nativeWindow } = observation;
  if (state.href !== 'tessera://app/index.html') throw new Error(`Unexpected location: ${state.href}`);
  if (state.readyState !== 'complete') throw new Error(`Unexpected readyState: ${state.readyState}`);
  if (!state.bridge) throw new Error('Renderer bridge is missing or malformed.');
  if (!state.harnessContextBridge) throw new Error('Harness-context IPC bridge is missing or malformed.');
  if (!state.remoteWorkflowBridge) throw new Error('Remote-workflow IPC bridge is missing or malformed.');
  if (!state.statusIsNull) throw new Error('Unexpected renderer IPC status response.');
  if (!state.connectionButton) throw new Error('Connection button was not rendered.');
  if (state.nodeRequire !== 'undefined') throw new Error(`Renderer exposes require: ${state.nodeRequire}`);
  if (state.visibilityState !== 'visible') throw new Error(`Unexpected renderer visibility: ${state.visibilityState}`);
  if (!isolation.sandbox || !isolation.contextIsolation || isolation.nodeIntegration) {
    throw new Error(`Application isolation settings are missing: ${JSON.stringify(isolation)}`);
  }
  if (rendererErrors.length) throw new Error(`Renderer errors: ${rendererErrors.map(error => error.message).join('; ')}`);
  if (nativeWindow.visible || nativeWindow.minimized || nativeWindow.nativeHandleBytes <= 0 ||
      nativeWindow.bounds.width <= 0 || nativeWindow.bounds.height <= 0) {
    throw new Error(`Hidden native application window is invalid: ${JSON.stringify(nativeWindow)}`);
  }
  const teardown = await stopChild();
  if (!teardown || (teardown.exitCode === null && teardown.signalCode === null)) {
    throw new Error('Electron child teardown was not confirmed.');
  }
  return { ...state, isolation, rendererErrors, window: nativeWindow, teardown };
}

(async () => {
  try {
    const result = await Promise.race([
      main(),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('Electron smoke timed out.')), timeoutMs); }),
    ]);
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(error.stack || error);
    process.exitCode = 1;
  } finally {
    clearTimeout(timeout);
    await stopChild();
    rmSync(userDataDir, { recursive: true, force: true });
    rmSync(packageDir, { recursive: true, force: true });
  }
})();
