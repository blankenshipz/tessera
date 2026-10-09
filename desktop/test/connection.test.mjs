import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ControlConnection, normalizeEndpoint } from '../connection.mjs';

const tokenA = 'a'.repeat(43);
const tokenB = 'b'.repeat(43);
const config = (id = 'one', token = tokenA) => ({ id, token, name: id, endpoint: `http://127.0.0.1:${id === 'one' ? 8765 : 8766}` });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

test('API maximum token authenticates and rejected lengths never reach transport', async () => {
  let calls = 0;
  const connection = new ControlConnection(async () => { calls++; return json({ principal_id: 'human', org_id: 'org' }); });
  await connection.connect(config('one', 'a'.repeat(4096)));
  assert.equal(calls, 1);
  for (const length of [32, 42, 4097]) await assert.rejects(connection.connect(config('one', 'a'.repeat(length))), /credentials/);
  assert.equal(calls, 1);
});

test('safe status errors explain next actions without exposing response bodies', async () => {
  for (const [status, hint] of [[401, /Reconnect/], [403, /scope/], [409, /revision and history/], [422, /form fields/], [503, /readiness and schema/]]) {
    const connection = new ControlConnection(async url => url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ detail: 'PRIVATE_SERVER_SENTINEL' }, status));
    await connection.connect(config());
    await assert.rejects(connection.projects(), error => {
      assert.match(error.message, hint); assert.doesNotMatch(error.message, /PRIVATE_SERVER_SENTINEL/); return true;
    });
  }
});

test('connection metadata never returns bearer credentials', async () => {
  const connection = new ControlConnection(async () => json({ principal_id: 'human', org_id: 'org', role: 'human' }));
  const status = await connection.connect(config());
  assert.equal(status.organizationId, 'org');
  assert.equal(status.principalId, 'human');
  assert.equal(status.health, 'online');
  assert.ok(!JSON.stringify(status).includes(tokenA));
  connection.disconnect();
  assert.equal(connection.status(), null);
});

test('harness-context routes use only a server-derived binding and an explicit command key', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { id: 'binding_1' });
  });
  await connection.connect(config());
  const enqueue = { thread_id: 'thread_1', harness_session_binding_id: 'binding_1',
    context_revision: 4, intent: 'direction', body: 'Use this as context only.' };
  await connection.harnessSessionBinding('binding_1');
  await connection.enqueueHarnessDelivery(enqueue, 'context-command');
  await connection.cancelHarnessDelivery('delivery_1', { expected_revision: 2 }, 'cancel-command');
  assert.match(calls[1].url, /\/interaction\/harness-session-bindings\/binding_1$/);
  assert.match(calls[2].url, /\/interaction\/deliveries$/);
  assert.deepEqual(JSON.parse(calls[2].options.body), enqueue);
  assert.equal(calls[2].options.headers['Idempotency-Key'], 'context-command');
  assert.match(calls[3].url, /\/interaction\/deliveries\/delivery_1\/cancel$/);
  assert.deepEqual(JSON.parse(calls[3].options.body), { expected_revision: 2 });
  assert.equal(calls[3].options.headers['Idempotency-Key'], 'cancel-command');
  assert.throws(() => connection.harnessSessionBinding('../binding'));
  assert.throws(() => connection.cancelHarnessDelivery('../delivery', { expected_revision: 2 }, 'key'));
});

test('program-review context and proposal commands use only the typed interaction contract', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { study_id: 'study_1' });
  });
  await connection.connect(config());
  const body = { project_id: 'project_1', task_id: null, kind: 'direction', scope: { type: 'research_program' }, expires_at: '2030-01-01T00:00:00Z', priority: 50 };
  await connection.programContext('study_1');
  await connection.createDecision(body, 'program-review-command');
  assert.match(calls[1].url, /\/interaction\/studies\/study_1\/program-context$/);
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[1].options.headers['Idempotency-Key'], undefined);
  assert.match(calls[2].url, /\/interaction\/decisions$/);
  assert.deepEqual(JSON.parse(calls[2].options.body), body);
  assert.equal(calls[2].options.headers['Idempotency-Key'], 'program-review-command');
  assert.throws(() => connection.programContext('../study'));
});

test('worker discovery is a bounded project-scoped read with no command key', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { workers: [] });
  });
  await connection.connect(config());
  await connection.workers('project_1', { offset: 100, limit: 200 });
  assert.match(calls[1].url, /\/projects\/project_1\/workers\?offset=100&limit=200$/);
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[1].options.body, undefined);
  assert.equal(calls[1].options.headers['Idempotency-Key'], undefined);
  assert.throws(() => connection.workers('../project'));
  assert.throws(() => connection.workers('project_1', { limit: 201 }));
});

test('an unavailable request marks the connection offline without replaying a command', async () => {
  let commands = 0;
  const connection = new ControlConnection(async (url) => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: 'org' });
    commands++;
    throw new Error('network lost');
  });
  await connection.connect(config());
  await assert.rejects(connection.enqueueHarnessDelivery({ thread_id: 'thread_1', harness_session_binding_id: 'binding_1',
    context_revision: 1, intent: 'question', body: 'Context' }, 'context-command'), /outcome is unknown/);
  assert.equal(connection.status().health, 'offline');
  assert.equal(commands, 1);
});

test('a readiness or backend-incompatibility 503 never remains online', async () => {
  const connection = new ControlConnection(async url =>
    url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ detail: 'not ready' }, 503));
  await connection.connect(config());
  await assert.rejects(connection.projects(), /unavailable or not ready \(503\)/);
  assert.equal(connection.status().health, 'offline');
});

test('a timed-out command aborts once, reports an unknown outcome, and never retries itself', async () => {
  let aborts = 0; let commands = 0;
  const connection = new ControlConnection(async (url, options) => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: 'org' });
    commands++;
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => {
      aborts++; reject(new Error('aborted by bounded timeout'));
    }, { once: true }));
  }, 5);
  await connection.connect(config());
  await assert.rejects(connection.createProject({ name: 'Timed out' }, 'timeout-command'), /outcome is unknown/);
  assert.equal(commands, 1);
  assert.equal(aborts, 1);
  assert.equal(connection.status().health, 'offline');
});

test('only the freshest request in an active connection generation can change health', async () => {
  let resolveOlder;
  const connection = new ControlConnection(async url => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: 'org' });
    if (url.includes('/projects?')) return new Promise(resolve => { resolveOlder = resolve; });
    throw new Error('newer request lost its network');
  });
  await connection.connect(config());
  const older = connection.projects();
  await assert.rejects(connection.tasks('project_1'), /Control plane unavailable/);
  resolveOlder(json({ projects: [] }));
  await older;
  assert.equal(connection.status().health, 'offline');
});

test('a late request failure cannot mark a newer successful request offline', async () => {
  let rejectOlder;
  const connection = new ControlConnection(async url => {
    if (url.endsWith('/me')) return json({ principal_id: 'human', org_id: 'org' });
    if (url.includes('/projects?')) return new Promise((_resolve, reject) => { rejectOlder = reject; });
    return json({ tasks: [] });
  });
  await connection.connect(config());
  const older = connection.projects();
  await connection.tasks('project_1');
  rejectOlder(new Error('older request lost its network'));
  await assert.rejects(older, /Control plane unavailable/);
  assert.equal(connection.status().health, 'online');
});

test('desktop reconnect reads the same durable remote-workflow timeline without replaying work', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ remote_workflows: [{ operation_id: 'a'.repeat(32), state: 'unknown', reconciliation_required: true }] });
  });
  await connection.connect(config());
  await connection.remoteWorkflows('project_1', { offset: 0, limit: 100 });
  connection.disconnect(); // Desktop exit/disconnect only drops its client context.
  await connection.connect(config());
  const timeline = await connection.remoteWorkflows('project_1', { offset: 0, limit: 100 });
  assert.equal(timeline.remote_workflows[0].reconciliation_required, true);
  assert.equal(calls.filter(call => /remote-workflows/.test(call.url)).length, 2);
  assert.ok(calls.filter(call => /remote-workflows/.test(call.url)).every(call => call.options.method === 'GET'));
});

test('remote-workflow detail is an authenticated read fenced to an operation identity', async () => {
  const calls = [];
  const operationId = 'a'.repeat(32);
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ operation_id: operationId });
  });
  await connection.connect(config());
  await connection.remoteWorkflow(operationId);
  assert.match(calls[1].url, new RegExp(`/remote-workflows/${operationId}$`));
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[1].options.headers['Idempotency-Key'], undefined);
  assert.throws(() => connection.remoteWorkflow('../operation'));
  assert.throws(() => connection.remoteWorkflow('A'.repeat(32)));
});

test('remote workflow stop request reads the current grant revision and remains an explicit command', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ id: 'grant_1' });
  });
  await connection.connect(config());
  await connection.executionGrant('grant_1');
  await connection.revokeExecutionGrant('grant_1', { expected_revision: 4 }, 'stop-command');
  assert.match(calls[1].url, /\/execution-grants\/grant_1$/);
  assert.equal(calls[1].options.method, 'GET');
  assert.match(calls[2].url, /\/execution-grants\/grant_1\/revoke$/);
  assert.equal(calls[2].options.headers['Idempotency-Key'], 'stop-command');
  assert.deepEqual(JSON.parse(calls[2].options.body), { expected_revision: 4 });
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

test('provider-disabled coordinator routes use bounded reads and an explicit human turn command', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ messages: [], proposals: [] });
  });
  await connection.connect(config());
  const body = { expected_conversation_revision: 2, prompt: '[Discuss]\nReview the recorded context.',
    selected_context: [{ kind: 'task', record_id: 'task_1', expected_revision: 4 }] };
  await connection.astraConversation('project_1', { offset: 0, limit: 100 });
  await connection.astraProposals('project_1', { offset: 10, limit: 50 });
  await connection.explainStatusTurns('project_1', { offset: 0, limit: 100 });
  await connection.requestCoordinatorTurn('project_1', body, 'coordinator-command');
  assert.match(calls[1].url, /\/astra\/projects\/project_1\/conversation\?offset=0&limit=100$/);
  assert.match(calls[2].url, /\/astra\/projects\/project_1\/proposals\?offset=10&limit=50$/);
  assert.match(calls[3].url, /\/astra\/projects\/project_1\/explain-status-turns\?offset=0&limit=100$/);
  assert.match(calls[4].url, /\/astra\/projects\/project_1\/turns$/);
  assert.equal(calls[4].options.headers['Idempotency-Key'], 'coordinator-command');
  assert.deepEqual(JSON.parse(calls[4].options.body), body);
  assert.throws(() => connection.astraConversation('../project'));
  assert.throws(() => connection.requestCoordinatorTurn('../project', body, 'key'));
});

test('human disposition facade is a closed retain-quarantined command, never an endpoint proxy', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => { calls.push({url,options}); return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org', role: 'human' } : { dispositions: [] }); });
  await connection.connect(config());
  const body = { expected_manifest_digest: 'a'.repeat(64), expected_revision: 0, disposition: 'retain_quarantined', rationale: 'Human operational output review only.' };
  await connection.artifactDispositions('manifest_1', { offset: 0, limit: 100 });
  await connection.recordArtifactDisposition('manifest_1', body, 'human-review');
  assert.match(calls.at(-1).url, /\/artifact-manifests\/manifest_1\/dispositions$/);
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), body);
  const count = calls.length;
  for (const patch of [{ disposition: 'reject' }, { disposition: 'accept' }, { action: 'release' }, { expected_revision: -1 }, { expected_manifest_digest: 'not-a-digest' }, { rationale: '' }, { rationale: 'x'.repeat(8001) }]) assert.throws(() => connection.recordArtifactDisposition('manifest_1', { ...body, ...patch }, 'key'));
  assert.throws(() => connection.recordArtifactDisposition('../other', body, 'key'));
  assert.equal(calls.length, count);
  assert.throws(() => connection.assertActive('wrong-connection'), /another context/);
  connection.disconnect();
  assert.throws(() => connection.recordArtifactDisposition('manifest_1', body, 'key'), /connection/i);
  const agent = new ControlConnection(async () => json({principal_id:'agent',org_id:'org',role:'agent'}));
  await agent.connect(config());
  assert.throws(() => agent.recordArtifactDisposition('manifest_1', body, 'key'), /Human artifact disposition/);
});

test('consumer status, custody, bundle, and recovery routes stay narrow and explicit', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { id: 'recorded' });
  });
  await connection.connect(config());
  const custody = { expected_manifest_digest: 'a'.repeat(64), expected_revision: 2, action: 'release', rationale: 'Review bounded output.' };
  const bundle = { max_bytes: 4096 };
  const recovery = { expected_grant_revision: 3, expected_recovery_revision: 2, operation_id: 'b'.repeat(32), request_cancellation: true };
  await connection.coordinatorConsumerStatus('project_1');
  await connection.recordArtifactCustodyRelease('manifest_1', custody, 'custody-command');
  await connection.exportReproductionBundle('manifest_1', bundle, 'bundle-command');
  await connection.reproductionBundle('bundle_1');
  await connection.recoveryReconciliations('grant_1');
  await connection.reconcileLostWorkerOperation('grant_1', recovery, 'recovery-command');
  assert.match(calls[1].url, /\/astra\/projects\/project_1\/consumer-status$/);
  assert.match(calls[2].url, /\/artifact-manifests\/manifest_1\/custody-releases$/);
  assert.deepEqual(JSON.parse(calls[2].options.body), custody);
  assert.match(calls[3].url, /\/artifact-manifests\/manifest_1\/reproduction-bundles$/);
  assert.match(calls[4].url, /\/reproduction-bundles\/bundle_1$/);
  assert.match(calls[5].url, /\/execution-grants\/grant_1\/recovery-reconciliations$/);
  assert.match(calls[6].url, /\/execution-grants\/grant_1\/recovery-reconciliations$/);
  for (const index of [2, 3, 6]) assert.ok(calls[index].options.headers['Idempotency-Key']);
  for (const index of [1, 4, 5]) assert.equal(calls[index].options.headers['Idempotency-Key'], undefined);
  assert.throws(() => connection.coordinatorConsumerStatus('../project'));
  assert.throws(() => connection.reconcileLostWorkerOperation('../grant', recovery, 'key'));
});

test('human design registration is a separate coordinator command, never an approval or run command', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ execution_authorized: false });
  });
  await connection.connect(config());
  const body = { expected_context_digest: 'a'.repeat(64) };
  await connection.registerProposalDesign('proposal_1', body, 'register-design-command');
  assert.match(calls[1].url, /\/astra\/proposals\/proposal_1\/register-design$/);
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'register-design-command');
  assert.deepEqual(JSON.parse(calls[1].options.body), body);
  assert.throws(() => connection.registerProposalDesign('../proposal', body, 'key'));
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

test('goal-to-decision routes are scoped authenticated reads without command keys', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/me') ? json({ principal_id: 'human', org_id: 'org' }) : json({ experiments: [] });
  });
  await connection.connect(config());
  await connection.experiments('project_1', { offset: 100, limit: 200 });
  await connection.experiment('experiment_1');
  assert.match(calls[1].url, /\/projects\/project_1\/experiments\?offset=100&limit=200$/);
  assert.match(calls[2].url, /\/experiments\/experiment_1$/);
  for (const call of calls.slice(1)) {
    assert.equal(call.options.method, 'GET');
    assert.equal(call.options.body, undefined);
    assert.equal(call.options.headers['Idempotency-Key'], undefined);
  }
  assert.throws(() => connection.experiments('../project'));
  assert.throws(() => connection.experiment('../experiment'));
  assert.throws(() => connection.experiments('project_1', { limit: 201 }));
});

test('researcher journey routes preserve separate design, admission, dispatch, evidence, and conclusion authority', async () => {
  const calls = [];
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { id: 'recorded' });
  });
  await connection.connect(config());
  await connection.reviseTaskPlan('task_1', { expected_revision: 2, title: 'Revised proposal', purpose: 'Bounded', budget_cents: 0, max_duration_seconds: 60,
    execution_spec: { argv: ['python'], input_snapshot_digest: 'b'.repeat(64), pinned_skill_digests: [], output_paths: [], profile: 'local-cpu', network_enabled: false, max_output_bytes: 0 } }, 'proposal-revision-command');
  const design = { expected_revision: 2, goal: 'Goal', success_criterion: 'Criterion', protocol: 'Protocol', variants: [] };
  const admission = { expected_variant_revision: 1, expected_design_revision: 2, grant_id: 'grant_1' };
  const dispatch = { expected_revision: 3, target_digest: 'a'.repeat(64), profile_version: 'development-minikube-opencode-v1' };
  const evidence = { expected_revision: 4, artifact_ids: ['artifact_1'] };
  const conclusion = { statement: 'Human interpretation.', design_revision: 2, evidence: [{ attempt_id: 'attempt_1', artifact_ids: ['artifact_1'], measurement_ids: [] }] };
  await connection.reviseExperiment('experiment_1', design, 'design-command');
  await connection.admitExperimentAttempt('variant_1', admission, 'admit-command');
  await connection.dispatchAdmittedAttempt('attempt_1', dispatch, 'dispatch-command');
  await connection.linkAttemptArtifacts('attempt_1', evidence, 'evidence-command');
  await connection.createConclusion('experiment_1', conclusion, 'conclusion-command');
  await connection.updateConclusion('conclusion_1', { ...conclusion, expected_revision: 1 }, 'update-command');
  await connection.reviewConclusion('conclusion_1', { expected_revision: 2, design_revision: 2 }, 'review-command');
  await connection.amendConclusion('conclusion_1', { expected_revision: 3, expected_design_revision: 2, relation: 'retracts', reason: 'Corrected evidence.' }, 'amend-command');
  await connection.artifacts('project_1', { offset: 0, limit: 100 });
  await connection.artifactPreview('manifest_1', 'artifact_1');
  assert.match(calls[1].url, /\/tasks\/task_1\/plan$/); assert.equal(calls[1].options.method, 'PUT');
  assert.match(calls[2].url, /\/experiments\/experiment_1\/designs$/);
  assert.match(calls[3].url, /\/experiment-variants\/variant_1\/admitted-attempts$/);
  assert.match(calls[4].url, /\/experiment-admitted-attempts\/attempt_1\/dispatch$/);
  assert.match(calls[5].url, /\/experiment-attempts\/attempt_1\/artifacts$/);
  assert.match(calls[6].url, /\/experiments\/experiment_1\/conclusions$/);
  assert.equal(calls[7].options.method, 'PUT');
  assert.match(calls[8].url, /\/conclusions\/conclusion_1\/review$/);
  assert.match(calls[9].url, /\/conclusions\/conclusion_1\/amendments$/);
  assert.match(calls[10].url, /\/projects\/project_1\/artifacts\?offset=0&limit=100$/);
  assert.match(calls[11].url, /\/artifact-manifests\/manifest_1\/artifacts\/artifact_1\/preview\?max_bytes=4096$/);
  for (const call of calls.slice(1, 10)) assert.ok(call.options.headers['Idempotency-Key']);
  assert.equal(calls[10].options.headers['Idempotency-Key'], undefined);
  assert.throws(() => connection.artifactPreview('manifest_1', 'artifact_1', 0));
  assert.throws(() => connection.reviseExperiment('../experiment', design, 'key'));
});

test('immutable task-plan detail is a digest-addressed authenticated read', async () => {
  const calls = [];
  const digest = 'a'.repeat(64);
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return json(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { task_id: 'task_1', plan_digest: digest });
  });
  await connection.connect(config());
  await connection.taskPlan('task_1', digest);
  assert.match(calls[1].url, new RegExp(`/tasks/task_1/plans/${digest}$`));
  assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[1].options.headers['Idempotency-Key'], undefined);
  assert.throws(() => connection.taskPlan('../task', digest));
  assert.throws(() => connection.taskPlan('task_1', 'not-a-digest'));
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
  const revision = { expected_revision: 5, kind: 'narrow', reason: 'Narrowed scope.', statement: 'The narrower result holds.', source_ids: ['source_1'], variant_ids: ['variant_1'] };
  await connection.createSource('study_1', source, 'source-command');
  await connection.assessSource('source_1', assessment, 'assessment-command');
  await connection.correctSource('source_1', correction, 'correction-command');
  await connection.createFinding('study_1', finding, 'finding-command');
  await connection.acceptFinding('finding_1', acceptance, 'acceptance-command');
  await connection.reaffirmFinding('finding_1', reaffirmation, 'reaffirm-command');
  await connection.retractFinding('finding_1', retraction, 'retract-command');
  await connection.reviseFinding('finding_1', revision, 'revise-command');
  assert.match(calls[6].url, /\/findings\/finding_1\/reaffirm$/);
  assert.deepEqual(JSON.parse(calls[6].options.body), reaffirmation);
  assert.match(calls[7].url, /\/findings\/finding_1\/retract$/);
  assert.deepEqual(JSON.parse(calls[7].options.body), retraction);
  assert.match(calls[8].url, /\/findings\/finding_1\/revise$/);
  assert.deepEqual(JSON.parse(calls[8].options.body), revision);
  assert.throws(() => connection.reaffirmFinding('../finding', reaffirmation, 'key'));
  assert.throws(() => connection.retractFinding('../finding', retraction, 'key'));
  assert.throws(() => connection.reviseFinding('../finding', revision, 'key'));
  assert.match(calls[1].url, /\/studies\/study_1\/sources$/);
  assert.match(calls[2].url, /\/sources\/source_1\/assess$/);
  assert.match(calls[3].url, /\/sources\/source_1\/correct$/);
  assert.match(calls[4].url, /\/studies\/study_1\/findings$/);
  assert.match(calls[5].url, /\/findings\/finding_1\/accept$/);
  for (const [index, key] of [[1, 'source-command'], [2, 'assessment-command'], [3, 'correction-command'], [4, 'finding-command'], [5, 'acceptance-command'], [6, 'reaffirm-command'], [7, 'retract-command'], [8, 'revise-command']]) {
    assert.equal(calls[index].options.method, 'POST');
    assert.equal(calls[index].options.headers['Idempotency-Key'], key);
  }
  assert.deepEqual(JSON.parse(calls[1].options.body), source);
  assert.deepEqual(JSON.parse(calls[2].options.body), assessment);
  assert.deepEqual(JSON.parse(calls[3].options.body), correction);
  assert.deepEqual(JSON.parse(calls[4].options.body), finding);
  assert.deepEqual(JSON.parse(calls[5].options.body), acceptance);
  assert.deepEqual(JSON.parse(calls[7].options.body), retraction);
  assert.deepEqual(JSON.parse(calls[8].options.body), revision);
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
