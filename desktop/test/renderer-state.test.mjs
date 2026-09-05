import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

// Exercise the actual renderer without Electron. Deferred replies reproduce races.
class Element {
  constructor() { this.children = []; this.listeners = {}; this.dataset = {}; this.fields = {}; this.hidden = false; this.disabled = false; this.textContent = ''; this.value = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute() {}
  removeAttribute() {}
  querySelector() { return this.submit ??= new Element(); }
  reset() { this.fields = {}; }
  close() {}
  showModal() {}
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
function fixture(overrides = {}) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const api = { status: async () => ({ id: 'connection' }), commandId: async () => 'command',
    tasks: async () => ({ tasks: [] }), labbook: async () => ({ events: [] }),
    threads: async () => ({ threads: [] }), messages: async () => ({ messages: [] }),
    decisions: async () => ({ decisions: [] }), ...overrides };
  const context = vm.createContext({ window: { tessera: api },
    document: { getElementById: get, createElement: () => new Element(), querySelectorAll: () => [] },
    FormData: class { constructor(form) { this.fields = { ...form.fields }; } get(name) { return this.fields[name]; } },
  });
  const source = readFileSync(new URL('../renderer.mjs', import.meta.url), 'utf8');
  vm.runInContext(source + '\nglobalThis.hooks = { selectProject, selectThread, loadThreads, loadMessages, loadDecisions, clearContext, state: () => ({ activeProject, activeThread }) };', context);
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
const pendingDecision = (id = 'd1', projectId = 'p1') => ({
  id, project_id: projectId, task_id: 't1', kind: 'spending', scope: { budget_cents: 100, access: 'public-data' },
  expires_at: '2030-01-01T00:00:00Z', expected_task_revision: 3, reviewer_principal_id: 'human', revision: 4,
});

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
