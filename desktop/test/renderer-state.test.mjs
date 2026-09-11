import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

// Exercise the actual renderer without Electron. Deferred replies reproduce races.
class Element {
  constructor(tagName = '') { this.tagName = tagName; this.children = []; this.listeners = {}; this.dataset = {}; this.fields = {}; this.hidden = false; this.disabled = false; this.textContent = ''; this.value = ''; this.isConnected = true; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute() {}
  removeAttribute() {}
  querySelector() { return this.submit ??= new Element(); }
  reset() { this.fields = {}; }
  close() { this.open = false; }
  showModal() { this.open = true; }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
function fixture(overrides = {}) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, Object.assign(new Element(), { id })); return nodes.get(id); };
  const api = { status: async () => ({ id: 'connection' }), commandId: async () => 'command',
    tasks: async () => ({ tasks: [] }), taskPlan: async () => null, labbook: async () => ({ events: [] }),
    threads: async () => ({ threads: [] }), messages: async () => ({ messages: [] }),
    studySelection: async () => {},
    recovery: async () => ({ state: 'active', recovery_hold: false }),
    decisions: async () => ({ decisions: [] }), experiments: async () => ({ experiments: [] }), studies: async () => ({ studies: [] }),
    createStudy: async () => ({ id: 's-created', purpose: 'Created', protocol: 'Protocol', mode: 'exploratory', status: 'open', revision: 1, project_id: 'p1' }),
    registerVariant: async () => ({ id: 'v-created' }),
    study: async () => ({ completeness: 'complete_in_scope_study_ledger', study: {}, variants: [], sources: [], findings: [], corrections: [], events: [] }), ...overrides };
  const context = vm.createContext({ window: { tessera: api },
    document: { getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => [] },
    FormData: class { constructor(form) { this.fields = { ...form.fields }; } get(name) { return this.fields[name]; } },
  });
  const source = readFileSync(new URL('../renderer.mjs', import.meta.url), 'utf8');
  vm.runInContext(source + '\napplyRecoveryStatus({state: \'active\', recovery_hold: false}); globalThis.hooks = { applyRecoveryStatus, command, selectProject, selectThread, loadThreads, loadMessages, loadDecisions, loadExperiments, selectExperiment, loadStudies, selectStudy, loadEvents, clearContext, state: () => ({ activeProject, activeThread, activeExperiment, activeStudy }) };', context);
  return { ...context.hooks, get, api, submit: id => { const target = get(id); target.listeners.submit({ target, preventDefault() {} }); } };
}
const thread = id => ({ id, title: id });
const project = id => ({ id, name: id });
function find(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children ?? []) {
    const match = find(child, predicate);
    if (match) return match;
  }
  return null;
}
function findAll(node, predicate, result = []) {
  if (predicate(node)) result.push(node);
  for (const child of node.children ?? []) findAll(child, predicate, result);
  return result;
}
const pendingDecision = (id = 'd1', projectId = 'p1') => ({
  id, project_id: projectId, task_id: 't1', kind: 'spending', scope: { budget_cents: 100, access: 'public-data' },
  expires_at: '2030-01-01T00:00:00Z', expected_task_revision: 3, reviewer_principal_id: 'human', revision: 4,
});
const ledger = (studyId = 's1', variants = []) => ({
  completeness: 'complete_in_scope_study_ledger',
  study: { id: studyId, project_id: 'p1', purpose: `Purpose ${studyId}`, protocol: 'Protocol', mode: 'exploratory', status: 'open', revision: 1 },
  variants, sources: [], findings: [], corrections: [], events: [],
});
const experimentLedger = (id = 'e1', projectId = 'p1') => {
  const variant = (revision, kind, name, method, input) => ({
    id: `${id}-${revision}-${kind}`, experiment_id: id, design_revision: revision, kind, name,
    method_digest: method.repeat(64), input_digest: input.repeat(64), metadata: { revision, kind },
    execution_task_id: 'task_1', execution_plan_digest: 'a'.repeat(64),
  });
  // v27 only copied goal and criterion to the current design. Earlier records are
  // intentionally unknown rather than reconstructed during migration.
  const first = { experiment_id: id, revision: 1, goal: null, success_criterion: null,
    protocol: 'Original bounded protocol.', variants: [variant(1, 'baseline', 'Original baseline', 'b', 'c'), variant(1, 'intervention', 'Original intervention', 'd', 'e')] };
  const second = { experiment_id: id, revision: 2, goal: 'Current goal.', success_criterion: 'Current criterion.',
    protocol: 'Current bounded protocol.', variants: [variant(2, 'baseline', 'Current baseline', 'f', '1'), variant(2, 'intervention', 'Current unattempted intervention', '2', '3')] };
  const operationId = '4'.repeat(32);
  const oldAttempt = { id: 'attempt-old-failed', variant_id: first.variants[0].id, design_revision: 1,
    provenance_kind: 'admitted_operation', operation_id: operationId, execution_plan_digest: 'a'.repeat(64), package_digest: '5'.repeat(64),
    operation_desired_state: 'admitted', operation_observed_state: 'exited', termination_status: 'confirmed', human_observation: null,
    outcome: 'failed', reason: 'The old bounded operation failed.', scientific_validity: 'not_assessed' };
  const humanAttempt = { id: 'attempt-current-human', variant_id: second.variants[0].id, design_revision: 2,
    provenance_kind: 'human_reported_observation', operation_id: null, execution_plan_digest: null, package_digest: null,
    operation_desired_state: null, operation_observed_state: 'not_observed', termination_status: 'not_applicable',
    human_observation: 'A human recorded the current baseline observation.', outcome: 'inconclusive', reason: 'Human observation remains unresolved.', scientific_validity: 'not_assessed' };
  const artifacts = [
    { id: 'artifact-legacy', project_id: projectId, relative_path: 'results/metric.json', sha256: '6'.repeat(64), status: 'quarantined', classification: 'unclassified', retention_state: 'legacy_unplaced', custody_state: 'legacy_unplaced' },
    { id: 'artifact-degraded', project_id: projectId, relative_path: 'results/degraded.json', sha256: '7'.repeat(64), status: 'quarantined', classification: 'unclassified', retention_state: 'degraded', custody_state: 'degraded' },
    { id: 'artifact-deleted', project_id: projectId, relative_path: 'results/deleted.json', sha256: '8'.repeat(64), status: 'quarantined', classification: 'unclassified', retention_state: 'deleted', custody_state: 'deleted' },
  ];
  const measurement = { id: 'measurement-1', attempt_id: oldAttempt.id, name: 'duration', value: { seconds: 1 }, artifact_id: artifacts[0].id };
  return { completeness: 'complete_in_scope_goal_to_decision_chain',
    experiment: { id, project_id: projectId, goal: second.goal, success_criterion: second.success_criterion, current_design_revision: 2, design: second },
    designs: [first, second], variants: [...first.variants, ...second.variants], attempts: [oldAttempt, humanAttempt],
    // The operation has since entered cancellation and an unknown observation;
    // this must not rewrite the attempt's recorded outcome or terminal observation.
    operations: [{ operation_id: operationId, project_id: projectId, desired_state: 'cancel_requested', observed_state: 'unknown' }],
    artifact_links: artifacts.map(artifact => ({ attempt_id: oldAttempt.id, artifact_id: artifact.id })), artifacts, measurements: [measurement],
    conclusions: [{ id: 'conclusion-1', experiment_id: id, design_revision: 1, design_snapshot: first, status: 'reviewed', statement: 'The old result needs human review.',
      evidence: [{ attempt_id: oldAttempt.id, artifact_id: null, measurement_id: measurement.id }], scientific_validity: 'not_assessed' },
    { id: 'conclusion-legacy-unbound', experiment_id: id, design_revision: null, design_snapshot: null, status: 'draft', statement: 'Legacy conclusion needs human review.',
      evidence: [{ attempt_id: oldAttempt.id, artifact_id: artifacts[1].id, measurement_id: null }], scientific_validity: 'not_assessed' }],
  };
};

test('renderer keeps earlier pages, removes loading markers, and deduplicates concurrent More reads', async () => {
  const next = deferred(); let calls = 0;
  const ui = fixture({ threads: async (_id, _project, page) => {
    calls++;
    return page.offset === 0 ? { threads: Array.from({ length: 100 }, (_, i) => thread(`t${i}`)) } : next.promise;
  } });
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('threads').children.length, 100);
  const first = ui.loadThreads();
  await ui.loadThreads();
  assert.equal(calls, 2);
  next.resolve({ threads: [thread('last')] }); await first;
  assert.equal(ui.get('threads').children.length, 101);
  assert.equal(ui.get('threads-more').hidden, true);
  assert.equal(ui.get('decisions').children[0].textContent, 'No pending decisions.');
});

test('approval displays only the digest-verified immutable specification and binds that revision', async () => {
  const calls = []; const digest = 'd'.repeat(64);
  const task = { id: 'task-1', status: 'draft', revision: 3, plan_digest: digest,
    title: 'Untrusted list title', purpose: 'Untrusted list purpose', budget_cents: 1, max_duration_seconds: 1 };
  const plan = { task_id: 'task-1', project_id: 'p1', revision: 3, plan_digest: digest,
    title: 'Verified package', purpose: 'Verified purpose', budget_cents: 120, max_duration_seconds: 300,
    execution_spec: { argv: ['python', '-m', 'fixture'], input_snapshot_digest: 'a'.repeat(64),
      pinned_skill_digests: ['b'.repeat(64)], output_paths: ['results/out.txt'], profile: 'local-cpu',
      network_enabled: false, max_output_bytes: 4096 } };
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async (...args) => {
    calls.push(args); return plan;
  }, approve: async (_connection, _taskId, body) => { calls.push(body); return {}; } });
  await ui.selectProject(project('p1')); await flush();
  const card = ui.get('tasks').children[0];
  assert.ok(find(card, node => node.textContent === 'Command / package identity: python -m fixture'));
  assert.ok(find(card, node => node.textContent === `Verified immutable plan digest: ${digest}`));
  assert.equal(find(card, node => node.textContent === 'Untrusted list title'), null);
  find(card, node => node.textContent === 'Approve this plan').listeners.click(); await flush();
  assert.deepEqual(calls[0].slice(1), ['task-1', digest]);
  assert.equal(JSON.stringify(calls[1]), JSON.stringify({ expected_revision: 3, expected_plan_digest: digest }));
});

test('approval stays unavailable when plan detail does not match the requested digest', async () => {
  const task = { id: 'task-1', status: 'draft', revision: 3, plan_digest: 'd'.repeat(64) };
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => ({
    task_id: 'task-1', project_id: 'p1', revision: 3, plan_digest: 'e'.repeat(64), execution_spec: {},
  }) });
  await ui.selectProject(project('p1')); await flush();
  assert.match(ui.get('tasks').children[0].children[0].textContent, /Approval is unavailable/);
});

test('renderer ignores thread and message replies from a previous context and clears drafts', async () => {
  const old = deferred(); const oldMessages = deferred();
  const ui = fixture({ threads: async (_id, id) => id === 'p1' ? old.promise : { threads: [thread('new')] },
    messages: async (_id, id) => id === 't1' ? oldMessages.promise : { messages: [] } });
  const first = ui.selectProject(project('p1'));
  await ui.selectProject(project('p2'));
  old.resolve({ threads: [thread('old')] }); await first;
  assert.equal(ui.get('threads').children[0].textContent, 'new');
  const firstThread = ui.selectThread(thread('t1'));
  ui.get('message-form').fields.body = 'Private draft for t1';
  await ui.selectThread(thread('t2'));
  assert.equal(ui.get('message-form').fields.body, undefined);
  oldMessages.resolve({ messages: [{ intent: 'question', sender_principal_id: 'old', body: 'stale' }] });
  await firstThread;
  assert.equal(ui.get('messages').children[0].textContent, 'No messages yet.');
  ui.get('message-form').fields.body = 'Another draft'; ui.clearContext();
  assert.equal(ui.get('message-form').fields.body, undefined);
});

test('project and connection context changes close and clear task and thread drafts', async () => {
  const ui = fixture();
  await ui.selectProject(project('p1'));
  for (const id of ['task', 'thread']) {
    ui.get(`${id}-dialog`).showModal();
    ui.get(`${id}-form`).fields = { title: `Draft for p1 ${id}` };
  }
  await ui.selectProject(project('p2'));
  for (const id of ['task', 'thread']) {
    assert.equal(ui.get(`${id}-dialog`).open, false);
    assert.deepEqual(ui.get(`${id}-form`).fields, {});
    assert.equal(ui.get(`${id}-form`).querySelector('button[type="submit"]').disabled, false);
  }

  ui.get('task-dialog').showModal();
  ui.get('task-form').fields = { title: 'Draft before disconnect' };
  ui.get('thread-dialog').showModal();
  ui.get('thread-form').fields = { title: 'Thread before disconnect' };
  ui.get('connection-dialog').showModal();
  ui.get('connection-form').fields = { name: 'Old identity', endpoint: 'https://old.example' };
  ui.clearContext();
  for (const id of ['task', 'thread', 'connection']) {
    assert.equal(ui.get(`${id}-dialog`).open, false);
    assert.deepEqual(ui.get(`${id}-form`).fields, {});
  }
});

test('pending task and thread drafts cannot dispatch after a project switch', async () => {
  const key = deferred(); let tasks = 0; let threads = 0;
  const ui = fixture({ commandId: () => key.promise,
    createTask: async () => { tasks++; }, createThread: async () => { threads++; } });
  await ui.selectProject(project('p1'));
  ui.get('task-form').fields = { title: 'Old plan', purpose: 'Old purpose', budget: '0', duration: '120' };
  ui.get('thread-form').fields = { title: 'Old question' };
  ui.submit('task-form'); ui.submit('thread-form'); await flush();
  await ui.selectProject(project('p2'));
  key.resolve('old-command'); await flush();
  assert.equal(tasks, 0);
  assert.equal(threads, 0);
});

test('cancellation telemetry never renders unresolved termination as stopped or successful', async () => {
  const ui = fixture({ labbook: async () => ({ events: [
    { cursor: 1, event_type: 'execution.grant_cancel_requested', resource_revision: 2, created_at: '2026-01-01T00:00:00Z', payload: { cancellation: { state: 'requested' } } },
    { cursor: 2, event_type: 'execution.observation_reported', resource_revision: 2, created_at: '2026-01-01T00:00:01Z', payload: { cancellation: { state: 'unresolved' } } },
    { cursor: 3, event_type: 'execution.observation_reported', resource_revision: 2, created_at: '2026-01-01T00:00:02Z', payload: { cancellation: { state: 'confirmed_termination' } } },
  ] }) });
  await ui.selectProject(project('p1'));
  const labels = ui.get('events').children.map(entry => entry.children[0].textContent);
  assert.deepEqual(labels, [
    'Cancellation requested — termination remains unresolved',
    'Cancellation unresolved or unknown — exposure retained',
    'Cancellation termination confirmed — operational observation only',
  ]);
  assert.ok(labels.every(label => !/stopped|successful/i.test(label)));
});

test('experiment projection renders migrated unknown context, attempt history, later operation uncertainty, and artifact custody without scientific success', async () => {
  const data = experimentLedger();
  const ui = fixture({ experiments: async () => ({ experiments: [{ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 }] }),
    experiment: async () => data });
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
  const detail = ui.get('experiment-detail');
  for (const outcome of ['failed', 'inconclusive']) assert.ok(find(detail, node => node.textContent === `Attempt-recorded outcome: ${outcome}`));
  assert.ok(find(detail, node => node.textContent === 'Design revision 1'));
  assert.ok(find(detail, node => node.textContent === 'Design revision 2'));
  assert.ok(find(detail, node => /Current unattempted intervention/.test(node.textContent)));
  assert.ok(find(detail, node => node.textContent === 'No attempts recorded for this registered variant.'));
  assert.ok(find(detail, node => /Registered execution reference: task task_1/.test(node.textContent)));
  assert.ok(find(detail, node => /Human-reported observation: A human recorded/.test(node.textContent)));
  assert.ok(find(detail, node => node.textContent === 'Goal: unknown (not recorded)'));
  assert.ok(find(detail, node => /Operation state recorded with attempt:.*desired admitted.*observed exited/.test(node.textContent)));
  assert.ok(find(detail, node => node.textContent === 'Latest desired operation state: cancel_requested'));
  assert.ok(find(detail, node => node.textContent === 'Latest observed operation state: unknown'));
  assert.ok(find(detail, node => /Cancellation has been requested/.test(node.textContent)));
  assert.ok(find(detail, node => /external state and exposure remain unresolved/.test(node.textContent)));
  assert.ok(find(detail, node => node.textContent === 'Linked artifacts: artifact-legacy, artifact-degraded, artifact-deleted'));
  assert.ok(find(detail, node => node.textContent === 'Linked measurements: measurement-1'));
  assert.ok(find(detail, node => /Reviewed human conclusion/.test(node.textContent)));
  assert.ok(find(detail, node => /Design snapshot: unknown \(legacy conclusion was not bound/.test(node.textContent)));
  for (const field of ['Quarantine status: quarantined · classification: unclassified', 'Retention state: legacy_unplaced', 'Custody state: legacy_unplaced']) assert.ok(find(detail, node => node.textContent === field));
  for (const warning of ['degraded', 'deleted', 'legacy-unplaced']) assert.ok(find(detail, node => new RegExp(`Warning: .*${warning}`).test(node.textContent)));
  const text = JSON.stringify(detail);
  assert.match(text, /Operation provenance is operational evidence only; it does not establish scientific success\./);
  assert.match(text, /Attempt-recorded outcome is not scientific success\. Scientific validity: not assessed\./);
  assert.match(text, /Human review records an interpretation; it does not establish scientific success\./);
  assert.equal(findAll(detail, node => node.tagName === 'a').length, 0);
});

test('experiment projection retains explicitly unverified legacy operation provenance without inventing a package or termination', async () => {
  const data = experimentLedger();
  Object.assign(data.attempts[0], { provenance_kind: 'legacy_operation_unverified', execution_plan_digest: null,
    package_digest: null, operation_desired_state: null, operation_observed_state: 'legacy_unverified', termination_status: 'legacy_unknown' });
  const ui = fixture({ experiments: async () => ({ experiments: [{ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 }] }),
    experiment: async () => data });
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
  assert.ok(find(ui.get('experiment-detail'), node => /Legacy operation provenance is unverified/.test(node.textContent)));
  assert.ok(find(ui.get('experiment-detail'), node => /desired not reconstructed/.test(node.textContent)));
});

test('experiment projection has accessible loading and empty states and fences stale project, revision, digest, and cross-links', async () => {
  const late = deferred();
  const ui = fixture({ experiments: async (_connection, projectId) => projectId === 'p1' ?
    { experiments: [{ id: 'e1', goal: 'Old goal', current_design_revision: 2 }] } : { experiments: [] },
  experiment: async (_connection, id) => id === 'e1' ? late.promise : experimentLedger('e2', 'p2') });
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('experiments').children[0].textContent, 'Old goal · design revision 2');
  const old = ui.selectExperiment({ id: 'e1', goal: 'Old goal', current_design_revision: 2 });
  await ui.selectProject(project('p2'));
  late.resolve(experimentLedger('e1', 'p1')); await old;
  assert.equal(ui.state().activeProject.id, 'p2');
  assert.equal(ui.get('experiment-title').textContent, 'Select an experiment');
  assert.equal(ui.get('experiments').children[0].textContent, 'No goal-to-decision experiments recorded.');

  await ui.selectProject(project('p1'));
  const inconsistent = experimentLedger();
  inconsistent.experiment.current_design_revision = 1;
  inconsistent.experiment.design.revision = 1;
  ui.api.experiment = async () => inconsistent;
  await assert.rejects(ui.selectExperiment({ id: 'e1', goal: 'Old goal', current_design_revision: 2 }), /complete current experiment ledger/);
  assert.match(ui.get('experiment-detail').children[0].textContent, /Partial, stale, or digest-inconsistent/);
  ui.api.experiment = async () => {
    const malformed = experimentLedger(); malformed.experiment.design.variants[0].method_digest = 'stale-digest'; return malformed;
  };
  await assert.rejects(ui.selectExperiment({ id: 'e1', goal: 'Old goal', current_design_revision: 2 }), /complete current experiment ledger/);
  for (const corrupt of [
    ledger => { ledger.designs = []; },
    ledger => { ledger.designs[0].goal = ''; },
    ledger => { ledger.variants.push({ ...ledger.variants[0] }); },
    ledger => { ledger.operations.push({ ...ledger.operations[0] }); },
    ledger => { ledger.attempts[0].variant_id = 'missing-variant'; },
    ledger => { ledger.attempts[0].design_revision = 2; },
    ledger => { ledger.artifacts[0].sha256 = 'invalid-digest'; },
    ledger => { ledger.artifact_links.push({ ...ledger.artifact_links[0] }); },
    ledger => { ledger.measurements[0].artifact_id = 'missing-artifact'; },
    ledger => { ledger.conclusions[0].design_revision = 99; },
    ledger => { ledger.conclusions[0].design_snapshot = null; },
    ledger => { ledger.conclusions[1].design_snapshot = ledger.designs[0]; },
    ledger => { ledger.conclusions[0].evidence[0].attempt_id = 'missing-attempt'; },
    ledger => { ledger.conclusions[0].evidence.push({ ...ledger.conclusions[0].evidence[0] }); },
  ]) {
    ui.api.experiment = async () => { const malformed = experimentLedger(); corrupt(malformed); return malformed; };
    await assert.rejects(ui.selectExperiment({ id: 'e1', goal: 'Old goal', current_design_revision: 2 }), /complete current experiment ledger/);
  }
});

test('switching thread while a command key is pending prevents message dispatch', async () => {
  const key = deferred(); let sent = 0;
  const ui = fixture({ commandId: () => key.promise, sendMessage: async () => { sent++; } });
  await ui.selectProject(project('p1')); await ui.selectThread(thread('t1'));
  ui.get('message-form').fields = { intent: 'question', body: 'For t1' };
  ui.submit('message-form');
  await ui.selectThread(thread('t2')); key.resolve('command'); await flush();
  assert.equal(sent, 0);
});

test('creating a thread cannot select it in a project chosen during the subsequent reload', async () => {
  const reload = deferred(); let reads = 0;
  const ui = fixture({ createThread: async () => thread('created'), threads: async (_id, id) => {
    if (id === 'p1' && ++reads === 2) return reload.promise;
    return { threads: [] };
  } });
  await ui.selectProject(project('p1'));
  ui.get('thread-form').fields = { title: 'Created' }; ui.submit('thread-form'); await flush();
  await ui.selectProject(project('p2'));
  reload.resolve({ threads: [thread('created')] }); await flush();
  assert.equal(ui.state().activeProject.id, 'p2');
  assert.equal(ui.state().activeThread, null);
});

test('a reset supersedes an in-flight page and errors replace loading placeholders', async () => {
  const pending = deferred(); let calls = 0;
  const ui = fixture({ threads: async () => {
    if (++calls === 1) return pending.promise;
    return { threads: [thread('fresh')] };
  } });
  const first = ui.selectProject(project('p1'));
  await ui.loadThreads(true); pending.resolve({ threads: [thread('stale')] }); await first;
  assert.equal(ui.get('threads').children[0].textContent, 'fresh');
  ui.api.threads = async () => { throw new Error('synthetic failure'); };
  await assert.rejects(ui.loadThreads(true), /synthetic failure/);
  assert.match(ui.get('threads').children[0].textContent, /Could not load/);
});

test('study creation sends typed metadata, refreshes, and opens the complete ledger', async () => {
  const calls = [];
  const ui = fixture({
    createStudy: async (_connection, _id, body, key) => { calls.push({ body, key }); return { id: 's-created', purpose: 'Created', protocol: 'Protocol', mode: 'exploratory', status: 'open', revision: 1, project_id: 'p1' }; },
    study: async (_connection, id) => ledger(id),
  });
  await ui.selectProject(project('p1'));
  ui.get('study-form').fields = { purpose: 'Purpose', protocol: 'Protocol', mode: 'exploratory' };
  ui.submit('study-form'); await flush();
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({ purpose: 'Purpose', protocol: 'Protocol', mode: 'exploratory' }));
  assert.equal(calls[0].key, 'command');
  assert.equal(ui.state().activeStudy.id, 's-created');
  assert.ok(find(ui.get('study-detail'), node => node.textContent === 'Completeness: complete in-scope study ledger.'));
});

test('variant registration preserves the selected study fence and refreshes its ledger', async () => {
  const calls = [];
  const ui = fixture({ registerVariant: async (_connection, _id, body, key) => { calls.push({ body, key }); return { id: 'v-created' }; }, study: async (_connection, id) => ledger(id) });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1', project_id: 'p1', purpose: 'Study', status: 'open' });
  ui.get('variant-form').fields = { name: 'Variant', method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: '{"source":"fixture"}', replication_label: 'unavailable' };
  ui.submit('variant-form'); await flush();
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({ name: 'Variant', method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: { source: 'fixture' }, replication_label: 'unavailable' }));
  assert.equal(calls[0].key, 'command');
  assert.equal(ui.state().activeStudy.id, 's1');
  assert.equal(ui.get('variant-form').querySelector('button[type="submit"]').disabled, false);
  ui.get('variant-form').fields = { name: 'Variant two', method_digest: 'c'.repeat(64), input_digest: 'd'.repeat(64), metadata: '{}', replication_label: 'rerun' };
  ui.submit('variant-form'); await flush();
  assert.equal(calls.length, 2);
});

test('a stale study creation cannot close or disable a form in the new project', async () => {
  const pending = deferred(); let created = 0;
  const ui = fixture({ createStudy: async () => { created++; return pending.promise; } });
  await ui.selectProject(project('p1'));
  ui.get('study-form').fields = { purpose: 'Old', protocol: 'Old protocol', mode: 'exploratory' };
  ui.submit('study-form'); await flush();
  await ui.selectProject(project('p2'));
  pending.resolve({ id: 's-old', purpose: 'Old', protocol: 'Old protocol', mode: 'exploratory', status: 'open', revision: 1, project_id: 'p1' });
  await flush();
  assert.equal(created, 1);
  assert.equal(ui.state().activeProject.id, 'p2');
  assert.equal(ui.get('study-form').querySelector('button[type="submit"]').disabled, false);
});

test('held recovery disables study and variant authoring and does not call APIs', async () => {
  let created = 0; let registered = 0;
  const ui = fixture({ createStudy: async () => { created++; }, registerVariant: async () => { registered++; } });
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('study-new').disabled, true);
  ui.get('study-form').fields = { purpose: 'x', protocol: 'y', mode: 'exploratory' }; ui.submit('study-form');
  await flush();
  assert.equal(created, 0); assert.equal(registered, 0);
});

test('uncertain study creation keeps the same command key for retry', async () => {
  let call = 0; let generated = 0; const keys = [];
  const ui = fixture({ commandId: async () => `command-${++generated}`, createStudy: async (_connection, _id, _body, key) => { keys.push(key); if (++call === 1) throw new Error('Outcome unknown'); return { id: 's-created', purpose: 'Created', protocol: 'Protocol', mode: 'exploratory', status: 'open', revision: 1, project_id: 'p1' }; } });
  await ui.selectProject(project('p1'));
  ui.get('study-form').fields = { purpose: 'Purpose', protocol: 'Protocol', mode: 'exploratory' };
  ui.submit('study-form'); await flush();
  ui.submit('study-form'); await flush();
  assert.deepEqual(keys, ['command-1', 'command-1']);
  assert.equal(generated, 1);
});

test('message submission blocks duplicate clicks and keeps its key after an uncertain response', async () => {
  const pending = deferred(); let generated = 0; const keys = [];
  const ui = fixture({ commandId: async () => `command-${++generated}`,
    sendMessage: async (_id, _body, key) => { keys.push(key); if (keys.length === 1) return pending.promise; return {}; } });
  await ui.selectProject(project('p1')); await ui.selectThread(thread('t1'));
  ui.get('message-form').fields = { intent: 'question', body: 'Retain until acknowledged' };
  ui.submit('message-form'); ui.submit('message-form'); await flush();
  assert.equal(keys.length, 1);
  pending.reject(new Error('Outcome unknown')); await flush();
  assert.equal(ui.get('message-form').fields.body, 'Retain until acknowledged');
  ui.submit('message-form'); await flush();
  assert.deepEqual(keys, ['command-1', 'command-1']);
  assert.equal(ui.get('message-form').fields.body, undefined);
});

test('decision resolution sends an explicit revision-bound intent and does not approve on load', async () => {
  const calls = [];
  const ui = fixture({ decisions: async () => ({ decisions: [pendingDecision()] }),
    resolveDecision: async (_id, body, key) => { calls.push({ body, key }); return { status: 'approved' }; } });
  await ui.selectProject(project('p1'));
  assert.equal(calls.length, 0);
  const card = ui.get('decisions').children[0];
  const rationale = find(card, node => node.placeholder === 'Optional rationale');
  rationale.value = 'Reviewed within the stated limit';
  find(card, node => node.textContent === 'Approve').listeners.click(); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([{ body: { decision_id: 'd1', expected_revision: 4, decision: 'approve', rationale: 'Reviewed within the stated limit' }, key: 'command' }]));
  assert.match(ui.get('notice').textContent, /intent record; no grant or execution started/);
});

test('decision rejection sends the displayed revision and remains intent only', async () => {
  const calls = [];
  const ui = fixture({ decisions: async () => ({ decisions: [pendingDecision()] }),
    resolveDecision: async (_id, body, key) => { calls.push({ body, key }); return { status: 'rejected' }; } });
  await ui.selectProject(project('p1'));
  const card = ui.get('decisions').children[0];
  find(card, node => node.placeholder === 'Optional rationale').value = 'The disclosed access is not acceptable.';
  find(card, node => node.textContent === 'Reject').listeners.click(); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([{ body: {
    decision_id: 'd1', expected_revision: 4, decision: 'reject', rationale: 'The disclosed access is not acceptable.',
  }, key: 'command' }]));
  assert.match(ui.get('notice').textContent, /Decision rejection recorded\. This is an intent record/);
});

test('decision retry preserves its idempotency key after an uncertain result', async () => {
  const keys = []; const bodies = []; let attempts = 0;
  const ui = fixture({ commandId: async () => `command-${keys.length + 1}`,
    decisions: async () => ({ decisions: [pendingDecision()] }),
    resolveDecision: async (_id, body, key) => { bodies.push(body); keys.push(key); if (++attempts === 1) throw new Error('Outcome unknown'); return {}; } });
  await ui.selectProject(project('p1'));
  const card = ui.get('decisions').children[0];
  const approve = find(card, node => node.textContent === 'Approve');
  approve.listeners.click(); await flush();
  assert.match(ui.get('notice').textContent, /outcome is unknown/);
  await ui.selectProject(project('p1'));
  const refreshed = ui.get('decisions').children[0];
  find(refreshed, node => node.placeholder === 'Retry will use the original rationale').value = 'Cannot change an uncertain request';
  find(refreshed, node => node.textContent === 'Approve').listeners.click(); await flush();
  assert.deepEqual(keys, ['command-1', 'command-1']);
  assert.equal(JSON.stringify(bodies), JSON.stringify([
    { decision_id: 'd1', expected_revision: 4, decision: 'approve' },
    { decision_id: 'd1', expected_revision: 4, decision: 'approve' },
  ]));
});

test('a confirmed decision is retained when its inbox refresh fails', async () => {
  let reads = 0;
  const ui = fixture({ decisions: async () => {
    if (++reads === 1) return { decisions: [pendingDecision()] };
    throw new Error('inbox unavailable');
  }, resolveDecision: async () => ({ status: 'approved' }) });
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click(); await flush();
  assert.match(ui.get('notice').textContent, /Decision recorded\. The inbox could not refresh/);
});

test('an old completion cannot clear a retry created after reselecting the same project', async () => {
  const first = deferred(); const second = deferred(); const keys = []; let calls = 0;
  const ui = fixture({ commandId: async () => 'command-1',
    decisions: async () => ({ decisions: [pendingDecision()] }),
    resolveDecision: async (_id, _body, key) => {
      keys.push(key); calls++;
      if (calls === 1) return first.promise;
      if (calls === 2) return second.promise;
      return {};
    } });
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click(); await flush();
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click(); await flush();
  first.resolve({ status: 'approved' }); await flush();
  second.reject(new Error('Outcome unknown')); await flush();
  assert.match(ui.get('notice').textContent, /outcome is unknown/);
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click(); await flush();
  assert.deepEqual(keys, ['command-1', 'command-1', 'command-1']);
});

test('a stale resolution failure does not announce into a different project', async () => {
  const response = deferred();
  const ui = fixture({ decisions: async (_id, projectId) => ({ decisions: projectId === 'p1' ? [pendingDecision()] : [] }),
    resolveDecision: async () => response.promise });
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click(); await flush();
  await ui.selectProject(project('p2'));
  response.reject(new Error('Control request failed (409).')); await flush();
  assert.equal(ui.get('notice').textContent, '');
});

test('a pending decision cannot resolve after its project switches', async () => {
  const key = deferred(); let resolutions = 0;
  const ui = fixture({ commandId: () => key.promise,
    decisions: async (_id, projectId) => ({ decisions: projectId === 'p1' ? [pendingDecision()] : [] }),
    resolveDecision: async () => { resolutions++; } });
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Reject').listeners.click();
  await ui.selectProject(project('p2')); key.resolve('command'); await flush();
  assert.equal(resolutions, 0);
  assert.equal(ui.state().activeProject.id, 'p2');
});

test('study pagination retains earlier rows and the ledger keeps every returned variant', async () => {
  const variants = Array.from({ length: 80 }, (_, index) => ({
    id: `v${index}`, name: `Variant ${index}`, method_digest: 'm'.repeat(64), input_digest: 'i'.repeat(64),
    status: index === 1 ? 'failed' : index === 2 ? 'excluded' : index === 3 ? 'abandoned' : index === 4 ? 'registered' : 'completed',
    replication_label: 'unavailable', reason: index === 1 ? 'Synthetic failure' : index === 2 ? 'Synthetic exclusion' : null,
    outcome: index === 0 ? { score: 1 } : null, outcome_provenance: index === 0 ? 'manual' : null,
    execution_receipt: null, independence_status: 'unavailable', metadata: { candidate: index },
  }));
  const studies = Array.from({ length: 101 }, (_, index) => ({ id: `s${index}`, purpose: `Study ${index}`, status: 'open' }));
  const ui = fixture({ studies: async (_id, _project, page) => ({ studies: studies.slice(page.offset, page.offset + page.limit) }),
    study: async (_connection, id) => ledger(id, variants) });
  await ui.selectProject(project('p1'));
  await ui.loadStudies();
  assert.equal(ui.get('studies').children.length, 101);
  await ui.selectStudy(studies[0]);
  const cards = findAll(ui.get('study-detail'), node => node.dataset?.variantId);
  assert.equal(cards.length, 80);
  assert.ok(cards.some(card => card.children.some(child => child.textContent === 'failed · unavailable')));
  assert.ok(cards.some(card => card.children.some(child => child.textContent === 'excluded · unavailable')));
  assert.ok(cards.some(card => card.children.some(child => /manual \(assertion; no execution receipt\)/.test(child.textContent))));
  assert.ok(cards.every(card => card.children.some(child => /Execution receipt: none recorded/.test(child.textContent))));
});

test('study ledger renders source corrections and linked needs-review findings as text only', async () => {
  const value = ledger();
  value.sources = [{ id: 'source-old', uri: 'https://untrusted.example/source', quoted_passage: '<script>untrusted</script>', content_digest: 'd'.repeat(64), locator: 'p. 1', verdict: 'unsupported', assessment_reason: 'Corrected', supersedes_source_id: null, superseded_at: '2026-01-01T00:00:00Z' }];
  value.findings = [{ id: 'finding-1', statement: 'Requires review', status: 'needs_review', source_ids: ['source-old'], variant_ids: ['v1'] }];
  value.corrections = [{ id: 'correction-1', original_source_id: 'source-old', replacement_source_id: 'source-new', reason: 'Passage was superseded', actor_principal_id: 'human', created_at: '2026-01-01T00:00:00Z' }];
  const ui = fixture({ study: async () => value });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1', purpose: 'Study', status: 'open' });
  assert.match(find(ui.get('study-detail'), node => /Recorded URI: https:\/\/untrusted/.test(node.textContent)).textContent, /untrusted/);
  assert.match(find(ui.get('study-detail'), node => node.textContent === 'Needs review. Consult the linked sources and ledger history.').textContent, /Needs review/);
  assert.match(find(ui.get('study-detail'), node => /Original source: source-old/.test(node.textContent)).textContent, /source-old/);
  assert.equal(findAll(ui.get('study-detail'), node => node.tagName === 'a').length, 0);
});

test('late study reads cannot replace a newer study or project view', async () => {
  const first = deferred();
  const ui = fixture({ study: async (_connection, id) => id === 's1' ? first.promise : ledger('s2') });
  await ui.selectProject(project('p1'));
  const staleStudy = ui.selectStudy({ id: 's1', purpose: 'First', status: 'open' });
  await ui.selectStudy({ id: 's2', purpose: 'Second', status: 'open' });
  first.resolve(ledger('s1')); await staleStudy;
  assert.equal(ui.state().activeStudy.id, 's2');
  assert.match(ui.get('study-title').textContent, /s2/);
  const projectRead = deferred(); ui.api.study = async () => projectRead.promise;
  const staleProject = ui.selectStudy({ id: 's2', purpose: 'Second', status: 'open' });
  await ui.selectProject(project('p2')); projectRead.resolve(ledger('s2')); await staleProject;
  assert.equal(ui.state().activeProject.id, 'p2');
  assert.equal(ui.get('study-title').textContent, 'Select a study');
});

test('an incomplete or over-limit ledger response is not presented as complete', async () => {
  let registered = 0;
  const ui = fixture({ study: async () => { throw new Error('Control request failed (409).'); }, registerVariant: async () => { registered++; } });
  await ui.selectProject(project('p1'));
  await assert.rejects(ui.selectStudy({ id: 's1', purpose: 'Study', status: 'open' }), /409/);
  assert.match(ui.get('study-detail').children[0].textContent, /Partial records are not shown as complete/);
  ui.get('variant-form').fields = { name: 'Variant', method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: '{}', replication_label: 'unavailable' };
  ui.submit('variant-form'); await flush();
  assert.equal(registered, 0);
  for (const value of [
    { ...ledger(), completeness: 'partial' },
    ledger('another-study'),
    { ...ledger(), study: { ...ledger().study, project_id: 'another-project' } },
  ]) {
    ui.api.study = async () => value;
    await assert.rejects(ui.selectStudy({ id: 's1' }), /complete study ledger/);
    assert.match(ui.get('study-detail').children[0].textContent, /Partial records are not shown as complete/);
  }
});

test('refresh while a decision command ID is pending shares one ID allocation', async () => {
  const allocation = deferred(); let allocations = 0; const keys = [];
  const ui = fixture({ commandId: () => { allocations++; return allocation.promise; },
    decisions: async () => ({ decisions: [pendingDecision()] }),
    resolveDecision: async (_id, _body, key) => { keys.push(key); throw new Error('Outcome unknown'); } });
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click();
  await ui.selectProject(project('p1'));
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click();
  assert.equal(allocations, 1);
  allocation.resolve('shared-command'); await flush();
  assert.deepEqual(keys, ['shared-command']);
  find(ui.get('decisions').children[0], node => node.textContent === 'Approve').listeners.click(); await flush();
  assert.deepEqual(keys, ['shared-command', 'shared-command']);
});

test('held recovery displays history but blocks desktop commands and decision actions', async () => {
  let commands = 0;
  const ui = fixture({ decisions: async () => ({ decisions: [pendingDecision()] }),
    resolveDecision: async () => { commands++; } });
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('recovery-banner').hidden, false);
  assert.match(ui.get('recovery-banner').textContent, /original control plane.*may still be active/);
  for (const id of ['project-new', 'task-new', 'thread-new']) assert.equal(ui.get(id).disabled, true);
  for (const choice of ['Approve', 'Reject']) {
    const button = find(ui.get('decisions'), node => node.textContent === choice);
    assert.equal(button.disabled, true);
    button.listeners.click();
  }
  await assert.rejects(ui.command('synthetic', {}, async () => { commands++; }), /read-only/);
  await flush();
  assert.equal(commands, 0);
});

test('missing or inconsistent recovery metadata fails closed for commands', async () => {
  for (const value of [null, {}, { state: 'active', recovery_hold: true }]) {
    const ui = fixture();
    assert.throws(() => ui.applyRecoveryStatus(value), /supported recovery state/);
    await assert.rejects(ui.command('synthetic', {}, async () => {}), /read-only/);
  }
});


test('study export uses displayed context, survives held mode, and blocks duplicate clicks', async () => {
  const pending = deferred(); const calls = []; const selections = [];
  const ui = fixture({ study: async (_connection, id) => ledger(id),
    studySelection: async (...args) => selections.push(args),
    saveStudyExport: async (...args) => { calls.push(args); return pending.promise; } });
  await ui.selectProject(project('p1'));
  await ui.selectStudy({ id: 's1' });
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  const button = ui.get('study-export');
  button.listeners.click(); button.listeners.click(); await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], 's1');
  assert.equal(calls[0][2], selections.at(-1)[2]);
  assert.equal(selections.at(-1)[3], 'p1');
  pending.resolve({ status: 'saved', basename: 'study.json' }); await flush();
  assert.equal(ui.get('notice').textContent, 'Saved study ledger JSON: study.json');
  assert.equal(button.disabled, false);
});

test('project changes invalidate exports and late failures cannot announce into a new context', async () => {
  const pending = deferred(); const selections = [];
  const ui = fixture({ study: async (_connection, id) => ledger(id),
    studySelection: async (...args) => selections.push(args),
    saveStudyExport: async () => pending.promise });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  ui.get('study-export').listeners.click(); await flush();
  await ui.selectProject(project('p2'));
  assert.equal(selections.at(-1)[1], null);
  assert.equal(ui.get('study-export').hidden, true);
  pending.reject(new Error('Old export failed')); await flush();
  assert.equal(ui.get('notice').textContent, '');
});


test('variant authoring rejects closed studies and closes drafts on project changes', async () => {
  let calls = 0;
  const ui = fixture({ study: async (_connection, id) => ({ ...ledger(id), study: { ...ledger(id).study, status: 'closed' } }),
    registerVariant: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1', status: 'open' });
  assert.equal(ui.get('variant-new').hidden, true);
  ui.get('variant-form').fields = { name: 'x', metadata: '{}' };
  ui.submit('variant-form'); await flush();
  assert.equal(calls, 0);
  assert.match(ui.get('notice').textContent, /open study/);
  ui.get('study-dialog').showModal(); ui.get('variant-dialog').showModal();
  await ui.selectProject(project('p2'));
  assert.equal(ui.get('study-dialog').open, false);
  assert.equal(ui.get('variant-dialog').open, false);
});

test('variant submit blocks duplicates and a late failure cannot affect another study', async () => {
  const pending = deferred(); let calls = 0;
  const ui = fixture({ study: async (_connection, id) => ledger(id),
    registerVariant: async () => { calls++; return pending.promise; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  ui.get('variant-form').fields = { name: 'x', metadata: '{}' };
  ui.submit('variant-form'); ui.submit('variant-form'); await flush();
  assert.equal(calls, 1);
  await ui.selectStudy({ id: 's2' });
  pending.reject(new Error('Old failure')); await flush();
  assert.equal(ui.get('notice').textContent, '');
  assert.equal(ui.get('variant-form').querySelector('button[type="submit"]').disabled, false);
});

const registeredVariant = (id = 'v1') => ({ id, study_id: 's1', name: id, revision: 3,
  status: 'registered', replication_label: 'unavailable', independence_status: 'unavailable',
  method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: {},
  reason: null, outcome: null, outcome_provenance: null, execution_receipt: null });

test('outcomes bind the displayed revision and study closure retains every accounted variant', async () => {
  const calls = []; let data = ledger('s1', [registeredVariant()]);
  const ui = fixture({ study: async () => data,
    recordVariant: async (_connection, id, body) => {
      calls.push({ id, body }); data = { ...data, variants: [{ ...data.variants[0], ...body,
        outcome_provenance: body.provenance, revision: 4 }] }; return data.variants[0];
    },
    closeStudy: async (_connection, id, body) => {
      calls.push({ id, body }); data = { ...data, study: { ...data.study, status: 'closed', revision: 2 } }; return data.study;
    } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  assert.equal(ui.get('study-close').disabled, true);
  ui.get('study-close').listeners.click(); await flush();
  assert.equal(calls.length, 0);
  find(ui.get('study-detail'), node => node.dataset?.recordVariantId === 'v1').listeners.click(); await flush();
  ui.get('outcome-form').fields = { status: 'completed', reason: '', provenance: 'manual', outcome: '{"score":0.7}' };
  ui.submit('outcome-form'); await flush();
  assert.equal(JSON.stringify(calls[0]), JSON.stringify({ id: 'v1', body: {
    expected_revision: 3, status: 'completed', reason: null, outcome: { score: 0.7 }, provenance: 'manual' } }));
  assert.equal(ui.get('study-close').disabled, false);
  assert.ok(find(ui.get('study-detail'), node => node.textContent?.includes('Outcome provenance: manual')));
  ui.get('study-close').listeners.click(); await flush(); ui.submit('study-close-form'); await flush();
  assert.equal(JSON.stringify(calls[1]), JSON.stringify({ id: 's1', body: { expected_revision: 1 } }));
  assert.equal(ui.get('study-close').hidden, true);
  assert.equal(findAll(ui.get('study-detail'), node => node.dataset?.variantId).length, 1);
});

test('outcome forms reject missing exclusion reasons, nonobject JSON, and held authority', async () => {
  let calls = 0;
  const ui = fixture({ study: async () => ledger('s1', [registeredVariant()]), recordVariant: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.recordVariantId === 'v1').listeners.click(); await flush();
  ui.get('outcome-form').fields = { status: 'excluded', reason: '', provenance: 'manual', outcome: '{}' };
  ui.submit('outcome-form'); await flush(); assert.match(ui.get('notice').textContent, /requires a reason/);
  ui.get('outcome-form').fields = { status: 'failed', reason: 'Fixture', provenance: 'manual', outcome: '[]' };
  ui.submit('outcome-form'); await flush(); assert.match(ui.get('notice').textContent, /JSON object/);
  ui.get('outcome-form').fields.outcome = '{}';
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  ui.submit('outcome-form'); await flush();
  assert.equal(calls, 0);
});

test('outcome retry preserves its key, blocks duplicates, and suppresses stale failures', async () => {
  const pending = deferred(); let generated = 0; const keys = [];
  const ui = fixture({ commandId: async () => `outcome-${++generated}`,
    study: async (_connection, id) => ledger(id, [registeredVariant()]),
    recordVariant: async (_connection, _variant, _body, key) => { keys.push(key); if (keys.length === 1) throw new Error('Unknown outcome'); return pending.promise; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.recordVariantId === 'v1').listeners.click(); await flush();
  ui.get('outcome-form').fields = { status: 'abandoned', reason: 'Fixture', provenance: 'manual', outcome: '' };
  ui.submit('outcome-form'); await flush();
  ui.submit('outcome-form'); ui.submit('outcome-form'); await flush();
  assert.deepEqual(keys, ['outcome-1', 'outcome-1']);
  await ui.selectStudy({ id: 's2' });
  pending.reject(new Error('Old failure')); await flush();
  assert.equal(ui.get('notice').textContent, '');
  assert.equal(ui.get('outcome-dialog').open, false);
});

test('changing study while a close command ID is pending prevents dispatch', async () => {
  const key = deferred(); let calls = 0;
  const ui = fixture({ commandId: async () => key.promise,
    study: async (_connection, id) => ledger(id), closeStudy: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  ui.get('study-close').listeners.click(); await flush(); ui.submit('study-close-form'); await flush();
  await ui.selectStudy({ id: 's2' }); key.resolve('old-key'); await flush();
  assert.equal(calls, 0); assert.equal(ui.get('study-close-dialog').open, false);
});

const evidenceSource = (overrides = {}) => ({ id: 'source1', study_id: 's1', revision: 7,
  uri: 'urn:fixture:source', quoted_passage: 'An exact <script>passage</script>', content_digest: 'c'.repeat(64),
  verdict: 'supported', superseded_at: null, ...overrides });
const evidenceFinding = { id: 'finding1', study_id: 's1', revision: 4, status: 'candidate',
  statement: 'A recorded assertion', source_ids: ['source1'], variant_ids: ['v1'] };
const evidenceLedger = () => ({ ...ledger('s1', [registeredVariant()]),
  sources: [evidenceSource()], findings: [evidenceFinding] });

test('acceptance UI requires current supported citations and closed confirmatory accounting', async () => {
  const fixtures = [
    { verdict: 'unsupported', mode: 'exploratory', status: 'open', allowed: false },
    { verdict: 'supported', mode: 'confirmatory', status: 'open', allowed: false },
    { verdict: 'supported', mode: 'confirmatory', status: 'closed', allowed: true },
    { verdict: 'supported', mode: 'exploratory', status: 'open', superseded_at: '2026-01-01', allowed: false },
  ];
  for (const entry of fixtures) {
    const data = evidenceLedger(); data.sources[0] = evidenceSource(entry);
    data.study = { ...data.study, mode: entry.mode, status: entry.status };
    data.variants[0].status = 'failed';
    let accepted = 0;
    const ui = fixture({ study: async () => data, acceptFinding: async () => { accepted++; } });
    await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
    const button = find(ui.get('study-detail'), node => node.dataset?.acceptFindingId);
    assert.equal(button.disabled, !entry.allowed);
    if (!entry.allowed) { button.listeners.click(); await flush(); assert.equal(accepted, 0); }
  }
});

test('finding review displays cited passage as text, requires explicit submit, and clears old context', async () => {
  const calls = []; const data = evidenceLedger();
  const ui = fixture({ study: async () => data, acceptFinding: async (_connection, id, body) => { calls.push({ id, body }); return {}; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.acceptFindingId).listeners.click(); await flush();
  assert.equal(calls.length, 0);
  assert.ok(find(ui.get('finding-review'), node => node.textContent === 'An exact <script>passage</script>'));
  ui.submit('finding-accept-form'); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([{ id: 'finding1', body: { expected_revision: 4 } }]));
  assert.equal(ui.get('finding-review').children.length, 0);
  await ui.selectProject(project('p2'));
  assert.equal(ui.get('finding-accept-dialog').open, false);
});

test('finding proposals use only explicit selected source and variant records', async () => {
  const calls = [];
  const ui = fixture({ study: async () => evidenceLedger(), createFinding: async (_connection, id, body) => { calls.push({ id, body }); return {}; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  ui.get('finding-new').listeners.click(); await flush();
  ui.get('finding-form').fields = { statement: 'Candidate' };
  ui.submit('finding-form'); await flush(); assert.equal(calls.length, 0);
  find(ui.get('finding-sources'), node => node.dataset?.sourceChoiceId).checked = true;
  find(ui.get('finding-variants'), node => node.dataset?.variantChoiceId).checked = true;
  ui.submit('finding-form'); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([{ id: 's1', body: {
    statement: 'Candidate', source_ids: ['source1'], variant_ids: ['v1'] } }]));
});

test('source corrections bind the displayed revision and cannot cross a pending context switch', async () => {
  const key = deferred(); let calls = 0;
  const ui = fixture({ commandId: async () => key.promise, study: async () => evidenceLedger(), correctSource: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.correctionSourceId).listeners.click(); await flush();
  ui.get('correction-form').fields = { uri: 'urn:replacement', quoted_passage: 'Replacement', content_digest: 'd'.repeat(64), reason: '' };
  ui.submit('correction-form'); await flush(); assert.equal(calls, 0);
  assert.match(ui.get('notice').textContent, /requires a reason/);
  ui.get('correction-form').fields.reason = 'Corrected'; ui.submit('correction-form'); await flush();
  await ui.selectProject(project('p2')); key.resolve('stale-key'); await flush();
  assert.equal(calls, 0); assert.equal(ui.get('correction-dialog').open, false);
  assert.equal(ui.get('correction-original').children.length, 0);
});

test('reaffirmation requires an explicit reason, retains retry key and displayed revision', async () => {
  const data = evidenceLedger(); data.findings = [{ ...evidenceFinding, status: 'needs_review' }];
  const calls = []; let generated = 0;
  const ui = fixture({ study: async () => data, commandId: async () => `review-${++generated}`,
    reaffirmFinding: async (_connection, id, body, key) => {
      calls.push({ id, body, key }); if (calls.length === 1) throw new Error('Unknown response'); return {};
    } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.reaffirmFindingId).listeners.click(); await flush();
  assert.equal(ui.get('finding-reaffirmation').hidden, false);
  assert.equal(ui.get('finding-reaffirmation-reason').required, true);
  assert.equal(calls.length, 0);
  ui.submit('finding-accept-form'); await flush(); assert.equal(calls.length, 0);
  assert.match(ui.get('notice').textContent, /requires a reason/);
  ui.get('finding-accept-form').fields = { reason: '  Reviewed unchanged evidence  ' };
  ui.submit('finding-accept-form'); await flush(); ui.submit('finding-accept-form'); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([1, 2].map(() => ({ id: 'finding1',
    body: { expected_revision: 4, reason: 'Reviewed unchanged evidence' }, key: 'review-1' }))));
});

test('reaffirmation rejects superseded citations and held or stale authority', async () => {
  const data = evidenceLedger(); data.findings = [{ ...evidenceFinding, status: 'needs_review' }];
  data.sources[0].superseded_at = '2026-01-01';
  data.sources.push(evidenceSource({ id: 'replacement' }));
  const key = deferred(); let calls = 0;
  const ui = fixture({ study: async () => data, commandId: async () => key.promise,
    reaffirmFinding: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  assert.equal(find(ui.get('study-detail'), node => node.dataset?.reaffirmFindingId).disabled, true);
  data.sources[0].superseded_at = null; await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.reaffirmFindingId).listeners.click(); await flush();
  ui.get('finding-accept-form').fields = { reason: 'Reviewed' };
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  ui.submit('finding-accept-form'); await flush(); assert.equal(calls, 0);
  ui.applyRecoveryStatus({ state: 'active', recovery_hold: false });
  await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.reaffirmFindingId).listeners.click(); await flush();
  ui.get('finding-accept-form').fields = { reason: 'Reviewed' };
  ui.submit('finding-accept-form'); await flush();
  await ui.selectProject(project('p2')); key.resolve('stale-key'); await flush();
  assert.equal(calls, 0);
});

test('retraction requires a reason, binds the displayed revision, and retains its retry key', async () => {
  const data = evidenceLedger();
  data.sources[0].verdict = 'unsupported';
  const calls = []; let generated = 0;
  const ui = fixture({ study: async () => data, commandId: async () => `retract-${++generated}`,
    retractFinding: async (_connection, id, body, key) => {
      calls.push({ id, body, key }); if (calls.length === 1) throw new Error('Unknown response'); return {};
    } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  const button = find(ui.get('study-detail'), node => node.dataset?.retractFindingId);
  assert.equal(button.textContent, 'Review retraction');
  button.listeners.click(); await flush();
  assert.equal(ui.get('finding-retract-form').querySelector('button[type="submit"]').disabled, false);
  assert.ok(find(ui.get('finding-retract-review'), node => node.textContent === 'Source IDs: source1'));
  ui.submit('finding-retract-form'); await flush();
  assert.equal(calls.length, 0);
  assert.match(ui.get('notice').textContent, /requires a reason/);
  ui.get('finding-retract-form').fields = { reason: '  Source withdrawn  ' };
  ui.submit('finding-retract-form'); await flush(); ui.submit('finding-retract-form'); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([1, 2].map(() => ({ id: 'finding1',
    body: { expected_revision: 4, reason: 'Source withdrawn' }, key: 'retract-1' }))));
});

test('retraction is blocked by held or stale authority and retracted findings have no action', async () => {
  const data = evidenceLedger(); let calls = 0; const key = deferred();
  const ui = fixture({ study: async () => data, commandId: async () => key.promise,
    retractFinding: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.retractFindingId).listeners.click(); await flush();
  ui.get('finding-retract-form').fields = { reason: 'Withdrawn' };
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  ui.submit('finding-retract-form'); await flush(); assert.equal(calls, 0);
  ui.applyRecoveryStatus({ state: 'active', recovery_hold: false });
  await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.retractFindingId).listeners.click(); await flush();
  ui.get('finding-retract-form').fields = { reason: 'Withdrawn after review' };
  ui.submit('finding-retract-form'); await flush();
  await ui.selectProject(project('p2')); key.resolve('stale-retract-key'); await flush();
  assert.equal(calls, 0);
  data.findings = [{ ...evidenceFinding, status: 'retracted' }];
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  assert.equal(find(ui.get('study-detail'), node => node.dataset?.retractFindingId), null);
  assert.equal(find(ui.get('study-detail'), node => node.dataset?.acceptFindingId), null);
  assert.equal(find(ui.get('study-detail'), node => node.dataset?.reaffirmFindingId), null);
});

test('finding revision sends explicit selected records, permits closed studies, and retains retry key', async () => {
  const data = evidenceLedger(); data.study.status = 'closed';
  const calls = []; let generated = 0;
  const ui = fixture({ study: async () => data, commandId: async () => `revise-${++generated}`,
    reviseFinding: async (_connection, id, body, key) => {
      calls.push({ id, body, key }); if (calls.length === 1) throw new Error('Unknown response'); return {};
    } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  const button = find(ui.get('study-detail'), node => node.dataset?.reviseFindingId);
  assert.equal(button.textContent, 'Review revision'); button.listeners.click(); await flush();
  assert.ok(find(ui.get('finding-revise-original'), node => node.textContent === evidenceFinding.statement));
  assert.equal(!!find(ui.get('finding-revise-sources'), node => node.dataset?.revisedSourceChoiceId).checked, false);
  assert.equal(!!find(ui.get('finding-revise-variants'), node => node.dataset?.revisedVariantChoiceId).checked, false);
  ui.get('finding-revise-form').fields = { kind: 'narrow', statement: '  Narrowed result  ', reason: '  Scope clarified  ' };
  ui.submit('finding-revise-form'); await flush(); assert.equal(calls.length, 0);
  assert.match(ui.get('notice').textContent, /between 1 and 200/);
  find(ui.get('finding-revise-sources'), node => node.dataset?.revisedSourceChoiceId).checked = true;
  find(ui.get('finding-revise-variants'), node => node.dataset?.revisedVariantChoiceId).checked = true;
  ui.submit('finding-revise-form'); await flush(); ui.submit('finding-revise-form'); await flush();
  assert.equal(JSON.stringify(calls), JSON.stringify([1, 2].map(() => ({ id: 'finding1',
    body: { expected_revision: 4, kind: 'narrow', reason: 'Scope clarified', statement: 'Narrowed result', source_ids: ['source1'], variant_ids: ['v1'] }, key: 'revise-1' }))));
});

test('finding revision is blocked by held or stale authority and superseded findings have no actions', async () => {
  const data = evidenceLedger(); const key = deferred(); let calls = 0;
  const ui = fixture({ study: async () => data, commandId: async () => key.promise,
    reviseFinding: async () => { calls++; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.reviseFindingId).listeners.click(); await flush();
  ui.get('finding-revise-form').fields = { kind: 'supersede', statement: 'Replacement', reason: 'Updated' };
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  ui.submit('finding-revise-form'); await flush(); assert.equal(calls, 0);
  ui.applyRecoveryStatus({ state: 'active', recovery_hold: false }); await ui.selectStudy({ id: 's1' });
  find(ui.get('study-detail'), node => node.dataset?.reviseFindingId).listeners.click(); await flush();
  find(ui.get('finding-revise-sources'), node => node.dataset?.revisedSourceChoiceId).checked = true;
  find(ui.get('finding-revise-variants'), node => node.dataset?.revisedVariantChoiceId).checked = true;
  ui.get('finding-revise-form').fields = { kind: 'supersede', statement: 'Replacement', reason: 'Updated' };
  ui.submit('finding-revise-form'); await flush(); await ui.selectProject(project('p2')); key.resolve('stale-revise-key'); await flush();
  assert.equal(calls, 0);
  data.findings = [{ ...evidenceFinding, status: 'superseded', supersedes_finding_id: 'older', revision_kind: 'supersede', revision_reason: 'Updated' }];
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1' });
  assert.ok(find(ui.get('study-detail'), node => node.textContent === 'Supersedes finding: older'));
  assert.ok(find(ui.get('study-detail'), node => node.textContent === 'Revision kind: supersede'));
  assert.ok(find(ui.get('study-detail'), node => node.textContent === 'Revision reason: Updated'));
  assert.equal(find(ui.get('study-detail'), node => node.dataset?.reviseFindingId), null);
});
