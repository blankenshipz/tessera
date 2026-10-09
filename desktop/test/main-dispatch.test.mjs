import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { test } from 'node:test';
import { ControlConnection } from '../connection.mjs';
import { validateSender } from '../ipc-policy.mjs';
import contract from '../ipc-contract.cjs';
import { validateProtectedRequest } from '../protected-secrets.mjs';

const json = value => new Response(JSON.stringify(value));
const body = { kind: 'direction', scope: { type: 'research_program', study_id: 'study_1' } };

test('admin bridge is typed, sender/context fenced, rejects value fields before transport', async () => {
  const f = await fixture(() => json({ records: [], provider_enabled: false }));
  await f.bridge.adminWorkspace('one', 'project_1', { offset: 0, limit: 100 });
  const create = { expected_revision: 0, spec: { kind: 'secret', backend: 'fake', purpose: 'salad_management', provider: 'salad', audience: 'server_proxy', expires_at: '2030-01-01T00:00:00Z' } };
  await f.bridge.adminCreate('one', 'project_1', create, 'admin-key');
  assert.equal(f.calls[0].url, 'http://127.0.0.1:8765/admin/projects/project_1/records?offset=0&limit=100');
  assert.equal(f.calls[1].headers['Idempotency-Key'], 'admin-key');
  assert.deepEqual(JSON.parse(f.calls[1].body), create);
  await assert.rejects(f.bridge.adminCreate('one', 'project_1', { ...create, spec: { ...create.spec, value: 'PRIVATE_SENTINEL' } }, 'forbidden'), /Invalid administration metadata/);
  await assert.rejects(f.bridge.adminCommand('one', 'project_1', 'record_1', { expected_revision: 1, action: 'secret.read' }, 'forbidden'), /Invalid administration metadata/);
  await assert.rejects(f.bridge.adminCreate('stale', 'project_1', create, 'key'), /another context/);
  await assert.rejects(f.bridge.adminWorkspace('one', '../other'), /identifier/);
  f.connection.disconnect();
  await assert.rejects(f.bridge.adminWorkspace('one', 'project_1'), /another context/);
  assert.equal(f.calls.length, 2);
  assert.doesNotMatch(JSON.stringify(f.calls), /PRIVATE_SENTINEL/);
  assert.equal(f.bridge.secretRead, undefined);
  assert.equal(f.bridge.secretImport, undefined);
});

test('admin old-generation responses are discarded and remote APIs are exposed in preload', async () => {
  let finish;
  const f = await fixture(() => new Promise(resolve => { finish = resolve; }));
  const pending = f.bridge.adminWorkspace('one', 'project_1');
  await f.connect('two');
  finish(json({ records: [{ id: 'old-context' }] }));
  await assert.rejects(pending, /Connection changed/);
  for (const method of ['remoteExecutions', 'remoteExecution', 'remoteExecutionCommand']) assert.equal(typeof f.bridge[method], 'function');
  assert.equal(f.calls.length, 1);
});

test('autonomy bridge is scoped read-only and has no effect operation', async () => {
  const f = await fixture(() => json({ sessions: [] }));
  assert.deepEqual(await f.bridge.autonomySessions('one', 'task_1'), { sessions: [] });
  assert.equal(f.calls[0].url, 'http://127.0.0.1:8765/tasks/task_1/autonomy-sessions');
  assert.equal(f.calls[0].method, 'GET');
  await assert.rejects(f.bridge.autonomySessions('wrong', 'task_1'), /another context/);
  await assert.rejects(f.bridge.autonomySessions('one', '../host'), /identifier/);
  assert.equal(f.bridge.startAutonomyAction, undefined);
  assert.equal(f.calls.length, 1);
});

async function fixture(respond = () => json({ id: 'review_1' }), nativeHelper = async () => ({ state: 'active', version: 1, providerEnabled: false })) {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: 'org' });
    calls.push({ url, ...options });
    return respond(url, options);
  });
  const connect = id => connection.connect({ id, name: id, endpoint: 'http://127.0.0.1:8765', token: 'x'.repeat(43) });
  await connect('one');
  const handlers = new Map();
  const frame = { url: 'tessera://app/index.html' };
  const contents = { mainFrame: frame, setWindowOpenHandler() {}, on() {}, once() {} };
  let event = { sender: contents, senderFrame: frame };
  let ready;
  // Execute the real entrypoint and registration loop, replacing only Electron
  // and native boundaries. No copied handler table or surrogate dispatcher.
  const source = readFileSync(new URL('../main.mjs', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replaceAll('import.meta.url', JSON.stringify(new URL('../main.mjs', import.meta.url).href));
  vm.runInNewContext(source, {
    dirname, fileURLToPath, contract, validateSender, validateProtectedRequest, runProtectedHelper: nativeHelper,
    ControlConnection: class { constructor() { return connection; } },
    NamespaceLifecycle: class {},
    app: { enableSandbox() {}, whenReady: () => ({ then: callback => { ready = Promise.resolve().then(callback); } }), on() {} },
    protocol: { registerSchemesAsPrivileged() {}, handle() {} },
    session: { defaultSession: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {} } },
    ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
    BrowserWindow: class { webContents = contents; on() {} loadURL() {} },
    join: (...parts) => parts.join('/'), process: { env: {} },
  }, { filename: 'main.mjs' });
  await ready;
  let bridge;
  vm.runInNewContext(readFileSync(new URL('../preload.cjs', import.meta.url), 'utf8'), {
    require: name => {
      assert.equal(name, 'electron');
      return {
        contextBridge: { exposeInMainWorld: (_name, value) => { bridge = value; } },
        ipcRenderer: { invoke: (name, ...args) => handlers.get(name)(event, ...args) },
      };
    },
  }, { filename: 'preload.cjs' });
  return { bridge, calls, connection, connect, setEvent: value => { event = value; } };
}

test('protected enrollment uses actual sender/context dispatcher and cancels on project switch', async () => {
  const request = { project_id: 'project_1', record_id: 'admin_' + 'a'.repeat(32), expected_revision: 2, enrollment_id: 'b'.repeat(32), operation: 'enroll' };
  let release, signal;
  const f = await fixture(() => json({ revision: 2, enrollment_id: request.enrollment_id, spec: { backend: 'os_keychain' } }),
    options => { signal = options.signal; return new Promise(resolve => { release = resolve; }); });
  await assert.rejects(f.bridge.protectedSecret('one', request), /context changed/);
  await f.bridge.secretContext('one', 'project_1');
  await assert.rejects(f.bridge.protectedSecret('stale', request), /another context/);
  await assert.rejects(f.bridge.protectedSecret('one', { ...request, value: 'PRIVATE_SENTINEL' }));
  const pending = f.bridge.protectedSecret('one', request);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(f.bridge.protectedSecret('one', request), /already in progress/);
  await f.bridge.secretContext('one', 'project_2');
  assert.equal(signal.aborted, true);
  release({ state: 'active' }); await assert.rejects(pending, /Connection changed/);
  assert.doesNotMatch(JSON.stringify(f.calls), /PRIVATE_SENTINEL/);
  f.setEvent({ sender: {}, senderFrame: { url: 'tessera://app/index.html' } });
  await assert.rejects(f.bridge.protectedSecret('one', request), /Untrusted/);
});

test('real preload/main dispatcher strips only connection ID for programContext and createDecision', async () => {
  const f = await fixture(url => json(url.includes('program-context') ? { study_id: 'study_1' } : { id: 'review_1' }));
  assert.deepEqual(await f.bridge.programContext('one', 'study_1'), { study_id: 'study_1' });
  assert.deepEqual(await f.bridge.createDecision('one', body, 'review-command'), { id: 'review_1' });
  assert.equal(f.calls[0].url, 'http://127.0.0.1:8765/interaction/studies/study_1/program-context');
  assert.equal(f.calls[0].method, 'GET');
  assert.equal(f.calls[1].url, 'http://127.0.0.1:8765/interaction/decisions');
  assert.equal(f.calls[1].method, 'POST');
  assert.deepEqual(JSON.parse(f.calls[1].body), body);
  assert.equal(f.calls[1].headers['Idempotency-Key'], 'review-command');
  await assert.rejects(f.bridge.createDecision('one', body), /explicit command identifier/);
  assert.equal(f.calls.length, 2);
});

test('dispatcher rejects wrong, missing, stale and disconnected connection IDs before transport', async () => {
  const f = await fixture();
  for (const id of ['wrong', undefined]) {
    await assert.rejects(f.bridge.programContext(id, 'study_1'), /another context/);
    await assert.rejects(f.bridge.createDecision(id, body, 'key'), /another context/);
  }
  await f.connect('two');
  await assert.rejects(f.bridge.programContext('one', 'study_1'), /another context/);
  await assert.rejects(f.bridge.createDecision('one', body, 'key'), /another context/);
  f.connection.disconnect();
  await assert.rejects(f.bridge.programContext('two', 'study_1'), /another context/);
  await assert.rejects(f.bridge.createDecision('two', body, 'key'), /another context/);
  assert.equal(f.calls.length, 0);
});

for (const method of ['programContext', 'createDecision']) {
  test(`${method} response from an old generation cannot cross the dispatcher`, async () => {
    let finish;
    const f = await fixture(() => new Promise(resolve => { finish = resolve; }));
    const args = method === 'programContext' ? ['study_1'] : [body, 'key'];
    const pending = f.bridge[method]('one', ...args);
    // Even reusing the ID cannot let an in-flight old generation return data.
    await f.connect('one');
    finish(json({ id: 'stale' }));
    await assert.rejects(pending, /Connection changed/);
    assert.equal(f.calls.length, 1);
    assert.equal(f.connection.status().health, 'online');
  });
}

for (const failure of ['lost response', 'invalid JSON']) {
  test(`unknown decision outcome (${failure}) is not retried; explicit retry preserves body/key`, async () => {
    let failed = false;
    const f = await fixture(() => {
      if (failed) return json({ id: 'review_1' });
      failed = true;
      if (failure === 'lost response') throw new Error('private transport details');
      return new Response('not JSON');
    });
    await assert.rejects(f.bridge.createDecision('one', body, 'same-key'), /Command outcome is unknown/);
    assert.equal(f.calls.length, 1);
    assert.deepEqual(await f.bridge.createDecision('one', body, 'same-key'), { id: 'review_1' });
    assert.equal(f.calls[0].body, f.calls[1].body);
    assert.equal(f.calls[0].headers['Idempotency-Key'], f.calls[1].headers['Idempotency-Key']);
  });
}

test('real dispatcher retains sender validation and request size limit', async () => {
  const f = await fixture();
  await assert.rejects(f.bridge.createDecision('one', { text: 'x'.repeat(128 * 1024) }, 'key'), /too large/);
  f.setEvent({ sender: {}, senderFrame: { url: 'tessera://app/index.html' } });
  await assert.rejects(f.bridge.programContext('one', 'study_1'), /Untrusted desktop message/);
  await assert.rejects(f.bridge.createDecision('one', body, 'key'), /Untrusted desktop message/);
  assert.equal(f.calls.length, 0);
});
