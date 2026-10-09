import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runProtectedHelper, validateProtectedRequest } from '../protected-secrets.mjs';
import { ControlConnection } from '../connection.mjs';
import { validateSender } from '../ipc-policy.mjs';

const request = { project_id: 'p1', record_id: 'admin_' + 'a'.repeat(32),
  expected_revision: 2, enrollment_id: 'b'.repeat(32), operation: 'enroll' };
const token = 'synthetic-session-only-' + 'x'.repeat(43);
const options = { python: '/fixture/python', stateDir: '/fixture/control', platform: 'darwin', token, request };
function fixture() {
  const child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.killed = false;
  child.kill = () => { child.killed = true; };
  let invocation;
  const spawnImpl = (...args) => { invocation = args; return child; };
  const controller = new AbortController();
  const start = (changes = {}) => runProtectedHelper({ ...options, spawnImpl, signal: controller.signal, current: () => {}, ...changes });
  const send = message => child.stdout.write(JSON.stringify(message) + '\n');
  return { child, controller, start, send, invocation: () => invocation };
}

test('protected native protocol never sends a secret through renderer or process args/env', async () => {
  const f = fixture(); const input = [];
  f.child.stdin.on('data', chunk => input.push(chunk.toString()));
  const result = f.start();
  f.send({ phase: 'confirm' });
  f.send({ phase: 'result', store: 'macos_keychain', version: 1, state: 'active', cleanup_pending: false });
  f.child.emit('close', 0);
  assert.deepEqual(await result, { version: 1, state: 'active', cleanupPending: false,
    store: 'macos_keychain', available: undefined, providerEnabled: false });
  assert.equal(JSON.stringify(f.invocation()).includes(token), false);
  assert.equal(JSON.parse(input[0]).token, token); // Private inherited auth channel only.
  assert.equal(input[1], '{"commit":true}\n');
});

test('stale project during native prompt never confirms store write', async () => {
  const f = fixture(); let stale = false; let input = '';
  f.child.stdin.on('data', chunk => { input += chunk.toString(); });
  const result = f.start({ current: () => { if (stale) throw Error('stale'); } });
  stale = true; f.send({ phase: 'confirm' });
  await assert.rejects(result, /unavailable or uncertain/);
  assert.equal(input.includes('"commit"'), false);
  assert.equal(f.child.killed, true);
});

test('disconnect kills bounded helper and discards delayed result', async () => {
  const f = fixture(); const result = f.start(); f.controller.abort();
  await assert.rejects(result, /unavailable or uncertain/);
  assert.equal(f.child.killed, true);
});

for (const message of [
  { phase: 'error', reason: 'synthetic-plaintext-not-for-renderer' },
  { phase: 'confirm', value: 'synthetic-plaintext-not-for-renderer' },
  { phase: 'result', store: 'macos_keychain', version: 1, state: 'active', value: 'synthetic-plaintext-not-for-renderer' },
  { phase: 'result', store: 'plaintext_file', version: 1, state: 'active' },
]) test(`reject untrusted helper output: ${Object.keys(message).join(',')}`, async () => {
  const f = fixture(); const result = f.start();
  if (message.phase === 'result') f.send({ phase: 'confirm' });
  f.send(message);
  await assert.rejects(result, error => !error.message.includes('synthetic-plaintext') && /uncertain/.test(error.message));
});

test('replayed confirms, unconfirmed results and oversized helper output fail closed', async () => {
  for (const effect of [f => { f.send({ phase: 'confirm' }); f.send({ phase: 'confirm' }); },
    f => f.send({ phase: 'result', store: 'macos_keychain', version: 1, state: 'active' }),
    f => f.child.stdout.write('x'.repeat(4097))]) {
    const f = fixture(); const result = f.start(); effect(f); await assert.rejects(result);
  }
});

test('unavailable platforms/configuration and input values never spawn a fallback', () => {
  const f = fixture();
  for (const changes of [{ platform: 'win32' }, { platform: 'linux' }, { python: null }, { stateDir: 'relative' },
    { request: { ...request, value: 'not-allowed' } }]) assert.throws(() => f.start(changes));
  assert.equal(f.invocation(), undefined);
  for (const change of [{ project_id: '../../p' }, { record_id: 'raw-key' }, { expected_revision: true },
    { enrollment_id: 'replay' }, { operation: 'read' }, { operation: 'resolve' }, { secret_fd: 3 }]) {
    assert.throws(() => validateProtectedRequest({ ...request, ...change }));
  }
});

const response = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
async function connection(endpoint = 'http://127.0.0.1:8765', changes = {}) {
  const c = new ControlConnection(async url => response(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org', role: 'human' } :
    { revision: 2, enrollment_id: request.enrollment_id, spec: { backend: 'os_keychain' }, ...changes }));
  await c.connect({ id: 'connection', name: 'Fixture', endpoint, token });
  return c;
}

test('connection fences remote topology, stale API metadata and disconnect', async () => {
  for (const c of [await connection('https://remote.test'), await connection(undefined, { enrollment_id: 'c'.repeat(32) })]) {
    let called = false;
    await assert.rejects(c.protectedSecret(request, () => { called = true; }, () => {}));
    assert.equal(called, false);
  }
  const c = await connection(); let finish, signal;
  const pending = c.protectedSecret(request, args => { signal = args.signal; return new Promise(resolve => { finish = resolve; }); }, () => {});
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  c.disconnect(); assert.equal(signal.aborted, true); finish({ state: 'active' });
  await assert.rejects(pending, /Connection changed/);
});

test('project cancellation aborts native operation without disconnecting unrelated connection', async () => {
  const c = await connection(); let finish, signal;
  const pending = c.protectedSecret(request, args => { signal = args.signal; return new Promise(resolve => { finish = resolve; }); }, () => {});
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  c.cancelProtectedSecrets(); assert.equal(signal.aborted, true); finish({ state: 'active' });
  await assert.rejects(pending); assert.equal(c.status().id, 'connection');
});

test('same sender validation applies and no generic value IPC or renderer storage exists', () => {
  const frame = { url: 'tessera://app/index.html' }, contents = { mainFrame: null }; contents.mainFrame = frame;
  const window = { webContents: contents }, event = { sender: contents, senderFrame: frame };
  validateSender(event, window);
  for (const invalid of [{ ...event, sender: {} }, { ...event, senderFrame: { url: frame.url } },
    { ...event, senderFrame: { url: 'https://evil.test' } }]) assert.throws(() => validateSender(invalid, window));
  const main = readFileSync(new URL('../main.mjs', import.meta.url), 'utf8');
  assert.match(main, /validateSender\(event, window\)/);
  assert.match(main, /validateProtectedRequest\(request\)/);
  const renderer = readFileSync(new URL('../renderer.mjs', import.meta.url), 'utf8');
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const preload = readFileSync(new URL('../preload.cjs', import.meta.url), 'utf8');
  assert.doesNotMatch(renderer, /localStorage|sessionStorage|secretValue|plaintext/);
  assert.doesNotMatch(html, /<input[^>]+(?:name=["'](?:token|secret|password)|type=["']password)/i);
  assert.doesNotMatch(preload, /secretRead|secretGet|resolveSecret/);
});

test('recognizable provider credentials cannot enter Connect or be sent to an endpoint', async () => {
  let calls = 0; const c = new ControlConnection(() => { calls++; });
  for (const prefix of ['salad_', 'sld_', 'sk-', 'ai_gateway_']) {
    await assert.rejects(c.connect({ id: 'c', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', token: prefix + 'x'.repeat(48) }));
  }
  assert.equal(calls, 0);
});
