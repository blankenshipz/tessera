import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ControlConnection, normalizeEndpoint } from '../connection.mjs';

const tokenA = 'a'.repeat(43);
const tokenB = 'b'.repeat(43);
const config = (id = 'one', token = tokenA) => ({ id, token, name: id, endpoint: `http://127.0.0.1:${id === 'one' ? 8765 : 8766}` });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

test('connection metadata never returns bearer credentials', async () => {
  const connection = new ControlConnection(async () => json({ principal_id: 'human', org_id: 'org', role: 'human' }));
  const status = await connection.connect(config());
  assert.equal(status.organizationId, 'org');
  assert.equal(status.principalId, 'human');
  assert.ok(!JSON.stringify(status).includes(tokenA));
  connection.disconnect();
  assert.equal(connection.status(), null);
});

test('a late response from an old connection cannot become current data', async () => {
  let resolveOld;
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: url.includes('8765') ? 'A' : 'B' });
    if (url.includes('8765')) return new Promise(resolve => { resolveOld = resolve; });
    return json({ projects: [{ id: 'project_B' }] });
  });
  await connection.connect(config());
  const stale = connection.projects();
  const rejected = assert.rejects(stale, /Connection changed/);
  await connection.connect(config('two', tokenB));
  resolveOld(json({ projects: [{ id: 'project_A' }] }));
  await rejected;
  assert.equal(connection.status().organizationId, 'B');
  assert.deepEqual(await connection.projects(), { projects: [{ id: 'project_B' }] });
  for (const call of calls) {
    assert.equal(call.options.headers.Authorization, `Bearer ${call.url.includes('8765') ? tokenA : tokenB}`);
    assert.equal(call.options.redirect, 'error');
  }
});

test('mutations are not automatically retried or replayed after a connection switch', async () => {
  const commands = [];
  const connection = new ControlConnection(async (url, options) => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: 'org' });
    commands.push({ url, options });
    throw new Error('lost reply');
  });
  await connection.connect(config());
  await assert.rejects(connection.createProject({ name: 'Study' }, 'command-1'), /outcome is unknown/);
  await connection.connect(config('two', tokenB));
  assert.equal(commands.length, 1);
  assert.equal(commands[0].options.headers['Idempotency-Key'], 'command-1');
});

test('TLS, route scope, and authorization failures fail closed', async () => {
  for (const endpoint of ['http://remote.example', 'https://user:pass@remote.example', 'file:///tmp', 'https://host/path']) {
    assert.throws(() => normalizeEndpoint(endpoint));
  }
  assert.equal(normalizeEndpoint('https://trusted.example'), 'https://trusted.example');
  const connection = new ControlConnection(async url => url.endsWith('/me') ?
    json({ principal_id: 'human', org_id: 'org' }) : json({ detail: tokenA }, 401));
  await connection.connect(config());
  assert.throws(() => connection.tasks('../credentials'));
  assert.throws(() => connection.createProject({ name: 'Study' }));
  await assert.rejects(connection.projects(), error => !error.message.includes(tokenA) && /401/.test(error.message));
  assert.equal(connection.status(), null);
});

test('streaming response size is bounded even without content-length', async () => {
  const connection = new ControlConnection(async url => url.endsWith('/me') ?
    json({ principal_id: 'human', org_id: 'org' }) : new Response('x'.repeat(4 * 1024 * 1024 + 1)));
  await connection.connect(config());
  await assert.rejects(connection.projects(), /too large/);
});

test('interaction routes carry bounded pages, pending status, and explicit idempotency keys', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ threads: [], messages: [], decisions: [] });
  });
  await connection.connect(config());
  await connection.threads('project_1', { offset: 12, limit: 100 });
  await connection.messages('thread_1', { offset: 2, limit: 50 });
  await connection.decisions('project_1', { offset: 0, limit: 100 });
  await connection.createThread({ project_id: 'project_1', title: 'Question' }, 'thread-command');
  await connection.sendMessage({ thread_id: 'thread_1', intent: 'question', body: 'Why?' }, 'message-command');
  const resolution = { decision_id: 'decision_1', expected_revision: 2, decision: 'reject', rationale: 'Revise the scope.' };
  await connection.resolveDecision(resolution, 'resolution-command');
  assert.match(calls[1].url, /\/interaction\/projects\/project_1\/threads\?offset=12&limit=100$/);
  assert.match(calls[2].url, /\/interaction\/threads\/thread_1\/messages\?offset=2&limit=50$/);
  assert.match(calls[3].url, /\/interaction\/projects\/project_1\/decisions\?offset=0&limit=100&status=pending$/);
  assert.equal(calls[4].options.headers['Idempotency-Key'], 'thread-command');
  assert.equal(calls[5].options.headers['Idempotency-Key'], 'message-command');
  assert.match(calls[6].url, /\/interaction\/decisions\/resolve$/);
  assert.equal(calls[6].options.method, 'POST');
  assert.equal(calls[6].options.headers['Idempotency-Key'], 'resolution-command');
  assert.deepEqual(JSON.parse(calls[6].options.body), resolution);
});

test('interaction identifiers and API page bounds fail closed', async () => {
  const connection = new ControlConnection(async () => json({ principal_id: 'human', org_id: 'org' }));
  await connection.connect(config());
  assert.throws(() => connection.threads('../project'));
  assert.throws(() => connection.messages('thread_1', { offset: 100001 }));
  assert.throws(() => connection.decisions('project_1', { limit: 101 }));
  assert.throws(() => connection.createThread({ project_id: 'project_1', title: 'x' }));
  assert.throws(() => connection.resolveDecision({ decision_id: 'decision_1', decision: 'approve' }));
});

test('research ledger routes use bounded read-only study requests', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ studies: [] });
  });
  await connection.connect(config());
  await connection.studies('project_1', { offset: 100, limit: 200 });
  await connection.study('study_1');
  assert.match(calls[1].url, /\/projects\/project_1\/studies\?offset=100&limit=200$/);
  assert.match(calls[2].url, /\/studies\/study_1$/);
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[2].options.method, 'GET');
  assert.equal(calls[1].options.body, undefined);
  assert.equal(calls[2].options.headers['Idempotency-Key'], undefined);
  assert.throws(() => connection.studies('../project'));
  assert.throws(() => connection.studies('project_1', { limit: 201 }));
  assert.throws(() => connection.study('../study'));
});

test('study authoring routes are scoped, typed POST commands', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ id: 'recorded' });
  });
  await connection.connect(config());
  const study = { purpose: 'Purpose', protocol: 'Protocol', mode: 'exploratory' };
  const variant = { name: 'Variant', method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: {}, replication_label: 'unavailable' };
  const outcome = { expected_revision: 1, status: 'completed', reason: null, provenance: 'manual', outcome: {} };
  const close = { expected_revision: 2 };
  await connection.createStudy('project_1', study, 'study-command');
  await connection.registerVariant('study_1', variant, 'variant-command');
  await connection.recordVariant('variant_1', outcome, 'outcome-command');
  await connection.closeStudy('study_1', close, 'close-command');
  assert.match(calls[1].url, /\/projects\/project_1\/studies$/);
  assert.match(calls[2].url, /\/studies\/study_1\/variants$/);
  assert.match(calls[3].url, /\/variants\/variant_1\/record$/);
  assert.match(calls[4].url, /\/studies\/study_1\/close$/);
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'study-command');
  assert.deepEqual(JSON.parse(calls[2].options.body), variant);
  assert.deepEqual(JSON.parse(calls[3].options.body), outcome);
  assert.deepEqual(JSON.parse(calls[4].options.body), close);
  assert.throws(() => connection.createStudy('../project', study, 'key'));
  assert.throws(() => connection.registerVariant('study_1', variant));
  assert.throws(() => connection.recordVariant('../variant', outcome, 'key'));
  assert.throws(() => connection.closeStudy('study_1', close));
});

test('source and finding routes preserve typed POST bodies and reject unsafe IDs', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ id: 'recorded' });
  });
  await connection.connect(config());
  const source = { uri: 'https://example.test/paper', quoted_passage: 'Quoted passage', content_digest: 'c'.repeat(64), locator: 'p. 2' };
  const assessment = { expected_revision: 1, verdict: 'supported', reason: 'The passage supports the claim.' };
  const correction = { expected_revision: 2, uri: 'https://example.test/correction', quoted_passage: 'Replacement passage', content_digest: 'd'.repeat(64), reason: 'The original passage was superseded.', locator: null };
  const finding = { statement: 'The result is associated with the cited source.', source_ids: ['source_1'], variant_ids: ['variant_1'] };
  const acceptance = { expected_revision: 1 };
  const reaffirmation = { expected_revision: 3, reason: 'Reviewed again' };
  const retraction = { expected_revision: 4, reason: 'The source was withdrawn.' };
  await connection.createSource('study_1', source, 'source-command');
  await connection.assessSource('source_1', assessment, 'assessment-command');
  await connection.correctSource('source_1', correction, 'correction-command');
  await connection.createFinding('study_1', finding, 'finding-command');
  await connection.acceptFinding('finding_1', acceptance, 'acceptance-command');
  await connection.reaffirmFinding('finding_1', reaffirmation, 'reaffirm-command');
  await connection.retractFinding('finding_1', retraction, 'retract-command');
  assert.match(calls[6].url, /\/findings\/finding_1\/reaffirm$/);
  assert.deepEqual(JSON.parse(calls[6].options.body), reaffirmation);
  assert.match(calls[7].url, /\/findings\/finding_1\/retract$/);
  assert.deepEqual(JSON.parse(calls[7].options.body), retraction);
  assert.throws(() => connection.reaffirmFinding('../finding', reaffirmation, 'key'));
  assert.throws(() => connection.retractFinding('../finding', retraction, 'key'));
  assert.match(calls[1].url, /\/studies\/study_1\/sources$/);
  assert.match(calls[2].url, /\/sources\/source_1\/assess$/);
  assert.match(calls[3].url, /\/sources\/source_1\/correct$/);
  assert.match(calls[4].url, /\/studies\/study_1\/findings$/);
  assert.match(calls[5].url, /\/findings\/finding_1\/accept$/);
  for (const [index, key] of [[1, 'source-command'], [2, 'assessment-command'], [3, 'correction-command'], [4, 'finding-command'], [5, 'acceptance-command'], [6, 'reaffirm-command'], [7, 'retract-command']]) {
    assert.equal(calls[index].options.method, 'POST');
    assert.equal(calls[index].options.headers['Idempotency-Key'], key);
  }
  assert.deepEqual(JSON.parse(calls[1].options.body), source);
  assert.deepEqual(JSON.parse(calls[2].options.body), assessment);
  assert.deepEqual(JSON.parse(calls[3].options.body), correction);
  assert.deepEqual(JSON.parse(calls[4].options.body), finding);
  assert.deepEqual(JSON.parse(calls[5].options.body), acceptance);
  assert.deepEqual(JSON.parse(calls[7].options.body), retraction);
  assert.throws(() => connection.createSource('../study', source, 'key'));
  assert.throws(() => connection.assessSource('../source', assessment, 'key'));
  assert.throws(() => connection.correctSource('source_1', correction));
  assert.throws(() => connection.createFinding('../study', finding, 'key'));
  assert.throws(() => connection.acceptFinding('../finding', acceptance, 'key'));
});

test('an interaction read cannot cross a connection switch', async () => {
  let resolveOld;
  const connection = new ControlConnection(async url => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: url.includes('8765') ? 'A' : 'B' });
    if (url.includes('8765')) return new Promise(resolve => { resolveOld = resolve; });
    return json({ threads: [{ id: 'thread_B' }] });
  });
  await connection.connect(config());
  const stale = connection.threads('project_1');
  const rejected = assert.rejects(stale, /Connection changed/);
  await connection.connect(config('two', tokenB));
  resolveOld(json({ threads: [{ id: 'thread_A' }] }));
  await rejected;
  assert.deepEqual(await connection.threads('project_1'), { threads: [{ id: 'thread_B' }] });
});

test('IPC callers must name the active connection before dispatch', async () => {
  const connection = new ControlConnection(async () => json({ principal_id: 'human', org_id: 'org' }));
  await connection.connect(config());
  connection.assertActive('one');
  await connection.connect(config('two', tokenB));
  assert.throws(() => connection.assertActive('one'), /another context/);
  assert.throws(() => connection.assertActive(undefined), /another context/);
  connection.assertActive('two');
});

test('recovery status is an authenticated read on the selected control connection', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } :
      { state: 'held_restore', recovery_hold: true });
  });
  await connection.connect(config());
  assert.deepEqual(await connection.recovery(), { state: 'held_restore', recovery_hold: true });
  assert.match(calls[1].url, /\/recovery$/);
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[1].options.body, undefined);
  assert.equal(calls[1].options.headers.Authorization, `Bearer ${tokenA}`);
});
