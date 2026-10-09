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
