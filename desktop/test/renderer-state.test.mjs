import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

// Exercise the actual renderer without Electron. Deferred replies reproduce races.
class Element {
  constructor() { this.children = []; this.listeners = {}; this.dataset = {}; this.fields = {}; this.hidden = false; this.disabled = false; this.textContent = ''; }
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
