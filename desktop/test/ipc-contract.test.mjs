import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import contract from '../ipc-contract.cjs';

test('namespace IPC is operation-specific and cannot expose credential or shell custody', () => {
  const namespace = contract.rendererMethods.filter(method => method.startsWith('namespace'));
  assert.deepEqual(namespace, ['namespaceSelectKubeconfig', 'namespaceInspect', 'namespacePlanUseExisting', 'namespacePlanCreate', 'namespaceCreate', 'namespaceStatus', 'namespaceReconcile', 'namespaceClear']);
  for (const forbidden of ['shell', 'argv', 'kubectl', 'apply', 'patch', 'delete', 'secret', 'readFile', 'writeFile', 'filesystem']) {
    assert.ok(!contract.rendererMethods.includes(forbidden));
  }
  const preload = readFileSync(new URL('../preload.cjs', import.meta.url), 'utf8');
  assert.doesNotMatch(preload, /require\(['"]\.\//);
  assert.doesNotMatch(preload, /ipcRenderer\.(send|on)|contextBridge\.exposeInMainWorld\([^,]+,\s*ipcRenderer/);
});

test('experiment export is one read-only operation, listed identically in preload and contract', () => {
  const preload = readFileSync(new URL('../preload.cjs', import.meta.url), 'utf8');
  const listed = (source) => source.match(/'saveStudyExport', 'saveExperimentExport', 'createStudy'/) !== null;
  assert.ok(listed(preload) && listed(readFileSync(new URL('../ipc-contract.cjs', import.meta.url), 'utf8')));
  assert.equal(contract.rendererMethods.filter(method => method === 'saveExperimentExport').length, 1);
  const names = new Set(contract.rendererMethods);
  const preloadNames = [...preload.matchAll(/'([A-Za-z]+)'/g)].map(match => match[1]).filter(name => names.has(name));
  assert.deepEqual([...new Set(preloadNames)].sort(), [...names].sort());
});
