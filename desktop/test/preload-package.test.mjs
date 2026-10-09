import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import contract from '../ipc-contract.cjs';

const desktop = fileURLToPath(new URL('..', import.meta.url));

test('the packed desktop ships a self-contained sandbox preload without Electron GUI', () => {
  const manifest = JSON.parse(readFileSync(join(desktop, 'package.json'), 'utf8'));
  assert.ok(manifest.files.includes('*.cjs'), 'runtime CJS dependencies must be packaged');
  const packed = JSON.parse(execFileSync('npm', ['pack', '--dry-run', '--json'], {
    cwd: desktop, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  }));
  assert.ok(packed[0]?.files?.some(file => file.path === 'preload.cjs'),
    'the packed application must include its preload entrypoint');

  const stage = mkdtempSync(join(tmpdir(), 'tessera-preload-package-'));
  try {
    // Sandboxed Electron only exposes its limited preload require. Stage no
    // local contract module so this executes the same self-contained path.
    cpSync(join(desktop, 'preload.cjs'), join(stage, 'preload.cjs'));
    mkdirSync(join(stage, 'node_modules', 'electron'), { recursive: true });
    writeFileSync(join(stage, 'node_modules', 'electron', 'index.js'), `
      let bridge;
      module.exports = {
        contextBridge: { exposeInMainWorld: (_name, value) => { bridge = value; } },
        ipcRenderer: { invoke: () => null },
        bridge: () => bridge,
      };
    `);
    const output = execFileSync(process.execPath, ['-e', `
      const electron = require('electron');
      require('./preload.cjs');
      const bridge = electron.bridge();
      if (typeof bridge.status !== 'function' || typeof bridge.namespaceClear !== 'function') process.exit(1);
      process.stdout.write(JSON.stringify(Object.keys(bridge).sort()));
    `], { cwd: stage, encoding: 'utf8' });
    assert.deepEqual(JSON.parse(output), [...contract.rendererMethods].sort(),
      'the staged preload must expose exactly the main-process typed bridge');
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
});
