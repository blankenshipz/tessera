import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, readFile, chmod, symlink, stat, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createIssuanceBundle, submitIssuanceBundle, runControlIssuance } from '../control-issuance.mjs';
import { parseControlTokenFile, parseCredentialHeader } from '../control-credential.mjs';
import { ControlConnection } from '../connection.mjs';
import { inspect } from 'node:util';

const json = body => new Response(JSON.stringify(body));
const root = { principal_id: 'root', credential_id: 'root-credential', org_id: 'org', role: 'human',
  kind: 'local_owner', parent_id: null, audience: 'control', revoked_at: null };
const config = { id: 'connection', name: 'isolated', endpoint: 'http://127.0.0.1:8765', token: 'x'.repeat(43) };

for (const mode of ['submit', 'resume']) {
  test(`malformed private intent ${mode} emits only fixed error and never POSTs`, async t => {
    const f = await fixture(t);
    await createIssuanceBundle(f.path, f.issuer, 60);
    // Synthetic text only. Actual private data is never used for this reproduction.
    const sentinel = 'SENT009';
    await writeFile(join(f.path, 'intent.json'), sentinel + '! not JSON');
    let confirmationCalls = 0;
    const action = mode === 'submit' ? () => submitIssuanceBundle(f.path, f.issuer) :
      () => runControlIssuance({ issuer: f.issuer, mode: 'resume', minutes: 60,
        choose: async () => f.path, confirm: async () => { confirmationCalls++; return true; } });
    const error = await action().then(() => assert.fail('Malformed intent was accepted.'), error => error);
    assert.equal(f.requests.filter(r => r.url.endsWith('/credentials')).length, 0);
    assert.equal(confirmationCalls, 0);
    // Cover renderer message/IPC error serialization and native logging/inspection surfaces.
    const exposed = [String(error), error.message, error.stack, JSON.stringify(error),
      inspect(error, { showHidden: true, depth: null }),
      ...Object.getOwnPropertyNames(error).map(key => inspect(error[key], { depth: null }))].join('\n');
    assert.equal(exposed.includes(sentinel), false, 'Private excerpt escaped the native error boundary.');
    assert.equal(error.message, 'Private issuance bundle unavailable; preserve it and inspect before retrying.');
    assert.equal(Object.hasOwn(error, 'cause'), false);
  });
}

test('intent reader preserves non-parse filesystem error codes without submitting', async t => {
  const f = await fixture(t);
  await createIssuanceBundle(f.path, f.issuer, 60);
  await rm(join(f.path, 'intent.json'));
  await assert.rejects(submitIssuanceBundle(f.path, f.issuer), error => error.code === 'ENOENT');
  assert.equal(f.requests.filter(r => r.url.endsWith('/credentials')).length, 0);
});
async function fixture(t, handler) {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'tessera-issuance-test-')));
  t.after(() => rm(home, { recursive: true, force: true }));
  const requests = [];
  const connection = new ControlConnection(async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith('/me')) return json(root);
    const body = JSON.parse(options.body);
    if (handler) await handler(body);
    return json({ ...root, role: 'agent', kind: 'agent', parent_id: root.credential_id,
      credential_id: 'child', principal_id: 'operator', project_id: body.project_id,
      expires_at: body.expires_at, token: 'MUST_NOT_CROSS_IPC' });
  });
  await connection.connect(config);
  return { path: join(home, 'bundle'), connection, requests, issuer: await connection.prepareControlIssuance('project') };
}

test('native private bundle and exact uncertain retry never regenerate token/body/key', async t => {
  let fail = true;
  const f = await fixture(t, () => { if (fail) { fail = false; throw new Error('PRIVATE_ERROR'); } });
  await createIssuanceBundle(f.path, f.issuer, 60);
  assert.equal((await stat(f.path)).mode & 0o777, 0o700);
  assert.equal((await stat(join(f.path, 'operator.control-token'))).mode & 0o777, 0o600);
  assert.equal((await stat(join(f.path, 'intent.json'))).mode & 0o777, 0o600);
  const token = parseControlTokenFile(await readFile(join(f.path, 'operator.control-token'), 'utf8'));
  assert.equal(token.length, 43);
  const intent = JSON.parse(await readFile(join(f.path, 'intent.json'), 'utf8'));
  assert.ok(!JSON.stringify(intent).includes(token));
  await assert.rejects(submitIssuanceBundle(f.path, f.issuer), /outcome is unknown/);
  const result = await submitIssuanceBundle(f.path, f.issuer);
  const posts = f.requests.filter(r => r.url.endsWith('/credentials'));
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.body, posts[1].options.body);
  assert.equal(posts[0].options.headers['Idempotency-Key'], posts[1].options.headers['Idempotency-Key']);
  assert.deepEqual(Object.keys(JSON.parse(posts[0].options.body)).sort(), ['audience','expires_at','kind','project_id','token']);
  assert.ok(!JSON.stringify(result).includes(token));
  assert.ok(!JSON.stringify(result).includes('MUST_NOT_CROSS_IPC'));
  await assert.rejects(createIssuanceBundle(f.path, f.issuer, 60), /EEXIST/);
});

test('switch, endpoint/identity mismatch, unsafe files and expiry prevent submission', async t => {
  const f = await fixture(t);
  await createIssuanceBundle(f.path, f.issuer, 1);
  await chmod(join(f.path, 'operator.control-token'), 0o644);
  await assert.rejects(submitIssuanceBundle(f.path, f.issuer), /bundle unavailable/);
  await chmod(join(f.path, 'operator.control-token'), 0o600);
  await assert.rejects(submitIssuanceBundle(f.path, { ...f.issuer, binding: { ...f.issuer.binding, origin: 'https://other.invalid' } }), /bundle unavailable/);
  const original = await readFile(join(f.path, 'operator.control-token'));
  await rm(join(f.path, 'operator.control-token'));
  await symlink(join(f.path, 'intent.json'), join(f.path, 'operator.control-token'));
  await assert.rejects(submitIssuanceBundle(f.path, f.issuer));
  await rm(join(f.path, 'operator.control-token'));
  await writeFile(join(f.path, 'operator.control-token'), original, { mode: 0o600 });
  const intentPath = join(f.path, 'intent.json');
  const intent = JSON.parse(await readFile(intentPath, 'utf8'));
  intent.expires_at = '2000-01-01T00:00:00Z';
  await writeFile(intentPath, JSON.stringify(intent));
  await assert.rejects(submitIssuanceBundle(f.path, f.issuer), /bounded/);
  f.connection.disconnect();
  await assert.rejects(submitIssuanceBundle(f.path, f.issuer), /Connection changed/);
  assert.equal(f.requests.filter(r => r.url.endsWith('/credentials')).length, 0);
});

test('refreshed human_session/agent/child-root/revoked identity cannot issue', async () => {
  for (const change of [{ kind: 'human_session' }, { role: 'agent' }, { parent_id: 'parent' }, { revoked_at: 'revoked' }]) {
    let calls = 0;
    const c = new ControlConnection(async () => json(++calls === 1 ? root : { ...root, ...change }));
    await c.connect(config);
    await assert.rejects(c.prepareControlIssuance('project'), /root local-owner/);
    assert.equal(calls, 2);
  }
});

test('raw token mode is explicit, provider keys and headers are rejected', () => {
  const token = 't'.repeat(43);
  assert.equal(parseControlTokenFile(token + '\n'), token);
  assert.equal(parseCredentialHeader('Authorization: Bearer ' + token), token);
  for (const value of ['sk_' + token, 'salad' + token, 'Authorization: Bearer ' + token, token + '\n\n', ' ' + token]) assert.throws(() => parseControlTokenFile(value));
});

test('native choose/confirm cancellation and context switch never POST', async t => {
  for (const phase of ['choose', 'confirm', 'switch']) {
    const f = await fixture(t);
    const result = runControlIssuance({ issuer: f.issuer, mode: 'new', minutes: 60,
      choose: async () => phase === 'choose' ? null : f.path,
      confirm: async () => { if (phase === 'switch') f.connection.disconnect(); return phase !== 'confirm'; } });
    if (phase === 'switch') await assert.rejects(result, /Connection changed/);
    else assert.deepEqual(await result, { canceled: true });
    assert.equal(f.requests.filter(r => r.url.endsWith('/credentials')).length, 0);
    await assert.rejects(stat(f.path), /ENOENT/);
  }
});

test('selected project epoch changing in picker, confirmation or refreshed me prevents POST', async t => {
  for (const phase of ['picker', 'confirm', 'refresh']) {
    const f = await fixture(t);
    let epoch = 1;
    const guard = () => { if (epoch !== 1) throw new Error('Selected project changed.'); };
    const issuer = await f.connection.prepareControlIssuance('project', guard);
    // Switch exactly while the async identity refresh for submit is outstanding.
    if (phase === 'refresh') {
      let me = 0;
      const c = new ControlConnection(async url => {
        assert.ok(url.endsWith('/me'));
        if (++me === 3) epoch = 2;
        return json(root);
      });
      await c.connect(config);
      const refreshed = await c.prepareControlIssuance('project', guard);
      await createIssuanceBundle(f.path, refreshed, 60);
      await assert.rejects(submitIssuanceBundle(f.path, refreshed), /Selected project changed/);
      assert.equal(me, 3);
    } else {
      await assert.rejects(runControlIssuance({ issuer, mode: 'new', minutes: 60,
        choose: async () => { if (phase === 'picker') epoch = 2; return f.path; },
        confirm: async () => { epoch = 2; return true; } }), /Selected project changed/);
      await assert.rejects(stat(f.path), /ENOENT/);
    }
    assert.equal(f.requests.filter(r => r.url.endsWith('/credentials')).length, 0);
  }
});

test('committed request followed by context switch retains exact bundle and safe retry', async t => {
  let epoch = 1;
  const f = await fixture(t, () => { epoch = 2; });
  const guard = () => { if (epoch !== 1) throw new Error('Selected project changed.'); };
  const issuer = await f.connection.prepareControlIssuance('project', guard);
  await createIssuanceBundle(f.path, issuer, 60);
  const intent = await readFile(join(f.path, 'intent.json'), 'utf8');
  const token = await readFile(join(f.path, 'operator.control-token'), 'utf8');
  await assert.rejects(submitIssuanceBundle(f.path, issuer), /Selected project changed/);
  assert.equal(f.requests.filter(r => r.url.endsWith('/credentials')).length, 1);
  assert.equal(await readFile(join(f.path, 'intent.json'), 'utf8'), intent);
  assert.equal(await readFile(join(f.path, 'operator.control-token'), 'utf8'), token);
  // A fresh capability after explicit return to the SAME identity/project can replay.
  const returned = await f.connection.prepareControlIssuance('project');
  await submitIssuanceBundle(f.path, returned);
  const posts = f.requests.filter(r => r.url.endsWith('/credentials'));
  assert.equal(posts[0].options.body, posts[1].options.body);
  assert.equal(posts[0].options.headers['Idempotency-Key'], posts[1].options.headers['Idempotency-Key']);
});
