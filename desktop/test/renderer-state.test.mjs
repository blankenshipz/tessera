import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

// Exercise the actual renderer without Electron. Deferred replies reproduce races.
class Element {
  constructor(tagName = '') { this.tagName = tagName; this.children = []; this.listeners = {}; this.dataset = {}; this.fields = {}; this.hidden = false; this.disabled = false; this.textContent = ''; this.value = ''; this.isConnected = true; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  contains(node) { return this === node || this.children.some(child => child.contains?.(node)); }
  focus() { this.onFocus?.(); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { (this.attributes ??= {})[name] = value; }
  removeAttribute(name) { delete this.attributes?.[name]; }
  querySelector() { return this.submit ??= new Element(); }
  reset() { this.fields = {}; }
  close() { this.open = false; this.listeners.close?.(); }
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
  const dialogIds = [...readFileSync(new URL('../index.html', import.meta.url), 'utf8').matchAll(/<dialog id="([^"]+)"/g)].map(match => match[1]);
  const dom = { activeElement: null };
  const focusable = node => Object.assign(node, { onFocus: () => { dom.activeElement = node; } });
  const get = id => { if (!nodes.has(id)) nodes.set(id, focusable(Object.assign(new Element(), { id }))); return nodes.get(id); };
  const api = { status: async () => ({ id: 'connection', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health: 'online' }), commandId: async () => 'command',
    tasks: async () => ({ tasks: [] }), workers: async () => ({ workers: [] }), taskPlan: async () => null, labbook: async () => ({ events: [] }),
    threads: async () => ({ threads: [] }), messages: async () => ({ messages: [] }),
    harnessSessionBinding: async () => null, enqueueHarnessDelivery: async () => ({ id: 'message' }), cancelHarnessDelivery: async () => ({}),
    astraConversation: async () => ({ conversation: null, messages: [] }), astraProposals: async () => ({ proposals: [] }),
    coordinatorConsumerStatus: async () => ({ availability: 'available', health: 'healthy', freshness: 'fresh', credential_status: 'active', credential_expires_at: null, heartbeat_at: '2026-01-01T00:00:00Z', last_outcome: 'idle', observed_consumers: 1, provider_disabled: true, provider_enabled: false }),
    explainStatusTurns: async () => ({ turns: [] }), requestCoordinatorTurn: async () => ({}), requestExplainStatusTurn: async () => ({}),
    registerProposalDesign: async () => ({ execution_authorized: false }),
    namespaceSelectKubeconfig: async () => ({ state: 'selected', reference: '1'.repeat(8) + '-1111-4111-8111-111111111111', name: 'kubeconfig' }),
    namespaceInspect: async () => ({ state: 'absent', uid: null, resourceVersion: null, target: { kubeconfigReferenceSha256: 'a'.repeat(64), context: 'team/admin@prod', server: 'https://cluster.test', caSha256: 'b'.repeat(64), clusterUid: 'cluster-uid', namespace: 'astra-system' } }),
    namespacePlanUseExisting: async () => ({ planId: 'plan-existing', mode: 'use-existing', digest: 'c'.repeat(64), expiresAt: '2030-01-01T00:00:00Z' }),
    namespacePlanCreate: async () => ({ planId: 'plan-create', mode: 'create-dedicated', digest: 'c'.repeat(64), expiresAt: '2030-01-01T00:00:00Z' }),
    namespaceCreate: async () => ({ state: 'namespace_created_control_plane_not_installed', uid: 'namespace-uid', resourceVersion: '9' }),
    namespaceReconcile: async () => ({ state: 'namespace_created_control_plane_not_installed', uid: 'namespace-uid', resourceVersion: '9' }),
    namespaceClear: async () => {},
    studySelection: async () => {},
    recovery: async () => ({ state: 'active', recovery_hold: false }), artifacts: async () => ({ artifacts: [] }),
    decisions: async () => ({ decisions: [] }), programContext: async (_connection, studyId) => ({ study_id: studyId, project_id: 'p1', basis_digest: 'a'.repeat(64), basis: { sources: [], variants: [], findings: [] }, reviews: [], condition_catalog: { outcome_0: ['direct_property_language'], outcome_1: ['unified_mixture_schema'] }, execution_granted: false, scientific_validity: 'not_assessed' }), experiments: async () => ({ experiments: [] }), remoteWorkflows: async () => ({ remote_workflows: [] }), remoteWorkflow: async () => null, executionGrant: async () => ({ id: 'grant_1', revision: 1, status: 'active' }), revokeExecutionGrant: async () => ({}), studies: async () => ({ studies: [] }),
    createStudy: async () => ({ id: 's-created', purpose: 'Created', protocol: 'Protocol', mode: 'exploratory', status: 'open', revision: 1, project_id: 'p1' }),
    registerVariant: async () => ({ id: 'v-created' }),
    study: async () => ({ completeness: 'complete_in_scope_study_ledger', study: {}, variants: [], sources: [], findings: [], corrections: [], events: [] }), ...overrides };
  const context = vm.createContext({ window: { tessera: api }, atob: globalThis.atob, TextDecoder, Uint8Array,
    document: Object.assign(dom, { getElementById: get, createElement: tag => focusable(new Element(tag)), querySelector: selector => selector === '.skip-link' ? get('skip-link') : null, querySelectorAll: selector => {
      if (selector === 'dialog') return dialogIds.map(get);
      if (selector === 'dialog[open]') return dialogIds.map(get).filter(dialog => dialog.open);
      if (selector === '[data-workspace-view]') return ['experiments', 'plans', 'ledger', 'conversation', 'operations'].map(view => { const panel = get('panel-'+view); panel.dataset.workspaceView = view; return panel; });
      if (selector === '#conclusion-evidence input') return findAll(get('conclusion-evidence'), node => node.tagName === 'input');
      return selector.split(',').flatMap(part => {
        const match = /^#([a-z-]+) button$/.exec(part.trim());
        return match ? findAll(get(match[1]), node => node.tagName === 'button') : [];
      });
    } }),
    FormData: class { constructor(form) { this.fields = { ...form.fields }; } get(name) { return this.fields[name]; } },
  });
  const source = readFileSync(new URL('../renderer.mjs', import.meta.url), 'utf8');
  vm.runInContext(source + '\napplyRecoveryStatus({state: \'active\', recovery_hold: false}); globalThis.hooks = { applyRecoveryStatus, applyConnectionHealth, guarded, command, selectProject, selectThread, loadThreads, loadMessages, loadDecisions, loadExperiments, selectExperiment, loadRemoteWorkflows, loadStudies, selectStudy, loadEvents, loadWorkbench, renderWorkbenchEntries, clearContext, clearNamespaceSetup, namespaceWork, selectNamespaceKubeconfig, inspectNamespace, planNamespace, confirmNamespace, reconcileNamespace, state: () => ({ activeProject, activeThread, activeExperiment, activeStudy, activeHarnessBinding, namespaceContext, coordinatorConversationRevision, selectedCoordinatorContext: [...selectedCoordinatorContext] }) };', context);
  return { ...context.hooks, get, api, activeElement: () => dom.activeElement, submit: id => { const target = get(id); target.listeners.submit({ target, preventDefault() {} }); } };
}
const thread = id => ({ id, title: id });
const project = id => ({ id, name: id });

test('keyboard skip link prevents URL navigation and moves workspace focus', () => {
  const ui = fixture(); let prevented = false;
  ui.get('skip-link').listeners.click({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(ui.activeElement().id, 'main-content');
});

test('403 feedback stays visible inside the open artifact dialog while authorization is cleared', async () => {
  const ui = fixture({ status: async () => null }); await flush();
  const dialog = ui.get('artifact-dialog'); const input = ui.get('artifact-id-input'); dialog.append(input); input.focus(); dialog.showModal();
  await ui.guarded(async () => { throw new Error('Control request failed (403). Reconnect with an authorized identity.'); });
  const region = find(dialog, node => node.className === 'dialog-notice');
  assert.equal(dialog.open, true);
  assert.equal(region.hidden, false);
  assert.equal(region.attributes.role, 'alert');
  assert.equal(region.attributes['aria-live'], 'assertive');
  assert.match(region.textContent, /\(403\)/);
  assert.equal(ui.get('notice').textContent, region.textContent);
  assert.equal(ui.activeElement(), region);
  assert.equal(ui.get('project-new').disabled, true);
  assert.equal(ui.state().activeProject, null);
});

test('dialog load clears stale errors and restores focus; close clears scoped feedback', async () => {
  const ui = fixture(); await flush();
  const dialog = ui.get('artifact-dialog'); const input = ui.get('artifact-id-input'); dialog.append(input); dialog.showModal(); input.focus();
  await ui.guarded(async () => { throw new Error('Control request failed (404).'); });
  const region = find(dialog, node => node.className === 'dialog-notice');
  assert.equal(ui.activeElement(), region);
  await ui.guarded(async () => {});
  assert.equal(region.hidden, true); assert.equal(region.textContent, ''); assert.equal(ui.activeElement(), input);
  await ui.guarded(async () => { throw new Error('Control request failed (404).'); });
  dialog.close();
  assert.equal(region.hidden, true); assert.equal(region.textContent, '');
  assert.match(ui.get('notice').textContent, /404/, 'global feedback remains outside the closed dialog');
});

test('late background errors do not attach to a dialog opened by unrelated work', async () => {
  const ui = fixture(); await flush(); const pending = deferred();
  const result = ui.guarded(() => pending.promise);
  const dialog = ui.get('artifact-dialog'); dialog.showModal();
  pending.reject(new Error('Control request failed (404).')); await result;
  assert.equal(find(dialog, node => node.className === 'dialog-notice'), null);
  assert.match(ui.get('notice').textContent, /404/);
});

test('Conversation context guidance names the separate views and return path', async () => {
  const ui = fixture(); await ui.selectProject(project('p1'));
  const text = JSON.stringify(ui.get('coordinator-context'));
  assert.match(text, /Experiments or Plans & decisions/);
  assert.match(text, /Include experiment in coordinator context/);
  assert.match(text, /then return to Conversation/);
  assert.doesNotMatch(text, /experiment below/);
});

test('workspace defaults to Experiments, switches panels without commands, and preserves view on refresh', async () => {
  const ui = fixture();
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('view-experiments').attributes['aria-current'], 'page');
  for (const view of ['plans', 'ledger', 'conversation', 'operations', 'experiments']) {
    ui.get('view-'+view).listeners.click();
    assert.equal(ui.get('workspace-view-title').textContent, {plans:'Plans & decisions', ledger:'Research ledger', conversation:'Conversation', operations:'Operations', experiments:'Experiments'}[view]);
    for (const other of ['plans', 'ledger', 'conversation', 'operations', 'experiments']) assert.equal(ui.get('panel-'+other).hidden, other !== view);
  }
  ui.get('view-ledger').listeners.click();
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('view-ledger').attributes['aria-current'], 'page');
  await ui.selectProject(project('p2'));
  assert.equal(ui.get('view-experiments').attributes['aria-current'], 'page');
});

test('refresh restores selected experiment through a fresh read, without repeating the current design in history', async () => {
  const data = experimentLedger(); let reads = 0;
  const ui = fixture({ experiment: async () => { reads++; return data; } });
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
  await ui.selectProject(project('p1'));
  assert.equal(reads, 2);
  assert.equal(ui.state().activeExperiment.id, 'e1');
  assert.equal(ui.get('experiment-title').textContent, data.experiment.goal);
  const detail = ui.get('experiment-detail');
  assert.equal(findAll(detail, node => node.textContent === 'Current design revision 2').length, 1);
  assert.equal(findAll(detail, node => node.textContent === 'Design revision 2').length, 0);
  assert.ok(find(detail, node => node.textContent === 'Historical design revisions (1)'));
  const provenance = find(detail, node => node.tagName === 'details' && node.children[0]?.textContent === 'Identifiers & provenance');
  assert.ok(provenance);
  assert.equal(provenance.open, undefined);
  const history = find(detail, node => node.tagName === 'details' && /^Semantic state history/.test(node.children[0]?.textContent));
  assert.equal(history.open, true, 'unknown history stays expanded');
});

test('pending Connect ignores duplicate submissions without invalidating the first result', async () => {
  const pending = deferred(); const calls = []; const recoveries = [];
  const ui = fixture({ status: async () => null,
    connect: async settings => { calls.push(settings); return pending.promise; },
    recovery: async id => { recoveries.push(id); return { state: 'active', recovery_hold: false }; },
    projects: async () => ({ projects: [] }) });
  await flush();
  ui.get('connection-form').fields = { name: 'Local', endpoint: 'http://127.0.0.1:60310' };
  ui.submit('connection-form'); ui.submit('connection-form'); await flush();
  assert.equal(calls.length, 1);
  assert.equal(ui.get('connection-form').querySelector().disabled, true);
  pending.resolve({ id: 'accepted', name: 'Local', endpoint: calls[0].endpoint, organizationId: 'org', health: 'online' });
  await flush();
  assert.deepEqual(recoveries, ['accepted']);
  assert.equal(ui.get('disconnect').hidden, false);
  assert.equal(ui.get('connection-form').querySelector().disabled, false);
  assert.equal(ui.get('welcome-project-new').hidden, false);
});

for (const outcome of ['cancelled', 'failed']) {
  test(`Connect restores retained inputs and allows retry after ${outcome}`, async () => {
    const pending = deferred(); let calls = 0;
    const ui = fixture({ status: async () => null, projects: async () => ({ projects: [] }),
      connect: async () => { calls++; return calls === 1 ? pending.promise : { id: 'retry', name: 'Local', endpoint: 'http://127.0.0.1:60310', organizationId: 'org', health: 'online' }; } });
    await flush();
    const fields = { name: 'Local', endpoint: 'http://127.0.0.1:60310' };
    ui.get('connection-form').fields = fields;
    ui.submit('connection-form'); ui.submit('connection-form'); await flush();
    if (outcome === 'failed') pending.reject(new Error('Connection failed.')); else pending.resolve(null);
    await flush();
    assert.equal(calls, 1);
    assert.equal(ui.get('connection-form').querySelector().disabled, false);
    assert.deepEqual(ui.get('connection-form').fields, fields);
    ui.submit('connection-form'); await flush();
    assert.equal(calls, 2);
    assert.equal(ui.get('welcome-project-new').hidden, false);
  });
}

async function artifactSaveFixture(saveReleasedArtifact) {
  const ui = fixture({ saveReleasedArtifact,
    releasedArtifactManifest: async () => ({ manifest: { id: 'manifest_1', project_id: 'p1' } }),
    artifactPreview: async () => ({ artifact_id: 'artifact_1', relative_path: 'result.txt', sha256: 'a'.repeat(64),
      size: 1, preview_base64: 'eA==', preview_bytes: 1, truncated: false, status: 'quarantined', classification: 'unclassified' }) });
  await ui.selectProject(project('p1'));
  ui.get('artifact-review').listeners.click(); await flush();
  ui.get('artifact-form').fields = { manifest_id: 'manifest_1', artifact_id: 'artifact_1' };
  ui.submit('artifact-form'); await flush();
  return ui;
}

for (const change of ['project', 'disconnect']) {
  for (const outcome of ['saved', 'cancelled', 'stale', 'rejected']) {
    test(`artifact save ${outcome} stays quiet after ${change} changes`, async () => {
      const pending = deferred(); let calls = 0;
      const ui = await artifactSaveFixture(async () => { calls++; return pending.promise; });
      ui.get('artifact-download').listeners.click(); ui.get('artifact-download').listeners.click(); await flush();
      assert.equal(calls, 1);
      assert.equal(ui.get('artifact-download').disabled, true);
      if (change === 'project') await ui.selectProject(project('p2')); else ui.clearContext();
      const notice = ui.get('notice').textContent;
      if (outcome === 'rejected') pending.reject(new Error('Old request failed.'));
      else pending.resolve({ status: outcome, sha256: 'b'.repeat(64), basename: 'old-result.txt' });
      await flush();
      assert.equal(ui.get('notice').textContent, notice);
      assert.equal(ui.get('artifact-download').disabled, true);
    });
  }
}

for (const outcome of ['saved', 'cancelled', 'stale', 'rejected']) {
  test(`artifact save ${outcome} restores eligibility after offline completion and reconnect`, async () => {
    const pending = deferred(); let calls = 0;
    const ui = await artifactSaveFixture(async () => { calls++; return pending.promise; });
    let health = 'online';
    const state = () => ({ id: 'connection', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health });
    ui.api.status = async () => state();
    ui.get('artifact-download').listeners.click(); await flush();
    health = 'offline'; ui.applyConnectionHealth(state());
    assert.equal(ui.get('artifact-download').disabled, true);
    if (outcome === 'rejected') pending.reject(new Error('Current request failed.'));
    else pending.resolve({ status: outcome, sha256: 'a'.repeat(64), basename: 'result.txt' });
    await flush();
    assert.equal(ui.get('artifact-download').disabled, true, 'offline overlay must remain');
    health = 'online'; ui.applyConnectionHealth(state());
    assert.equal(ui.get('artifact-download').disabled, false, 'completed busy state must not survive reconnect');
    ui.get('artifact-download').listeners.click(); await flush();
    assert.equal(calls, 2, 'current preview can be saved again without reload');
  });
}

test('artifact reconnect cannot enable a still-pending save', async () => {
  const pending = deferred(); let calls = 0;
  const ui = await artifactSaveFixture(async () => { calls++; return pending.promise; });
  ui.get('artifact-download').listeners.click(); await flush();
  const state = { id: 'connection', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org' };
  ui.applyConnectionHealth({ ...state, health: 'offline' });
  ui.applyConnectionHealth({ ...state, health: 'online' });
  assert.equal(ui.get('artifact-download').disabled, true);
  ui.get('artifact-download').listeners.click(); await flush();
  assert.equal(calls, 1);
  pending.resolve({ status: 'cancelled' }); await flush();
  assert.equal(ui.get('artifact-download').disabled, false);
});

test('offline artifact completion cannot restore eligibility for a different project', async () => {
  const pending = deferred();
  const ui = await artifactSaveFixture(async () => pending.promise);
  ui.get('artifact-download').listeners.click(); await flush();
  const state = { id: 'connection', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org' };
  ui.applyConnectionHealth({ ...state, health: 'offline' });
  await ui.selectProject(project('p2'));
  pending.resolve({ status: 'saved', sha256: 'a'.repeat(64), basename: 'old-result.txt' }); await flush();
  ui.applyConnectionHealth({ ...state, health: 'online' });
  assert.equal(ui.get('artifact-download').disabled, true);
  assert.doesNotMatch(ui.get('notice').textContent, /Saved exact released/);
});

for (const outcome of ['saved', 'cancelled', 'stale', 'mismatch', 'rejected']) {
  test(`current artifact save handles ${outcome} and restores its button`, async () => {
    const pending = deferred(); let calls = 0;
    const ui = await artifactSaveFixture(async () => { calls++; return pending.promise; });
    const notice = ui.get('notice').textContent;
    ui.get('artifact-download').listeners.click(); ui.get('artifact-download').listeners.click(); await flush();
    assert.equal(calls, 1);
    if (outcome === 'rejected') pending.reject(new Error('Current request failed.'));
    else pending.resolve({ status: outcome === 'mismatch' ? 'saved' : outcome,
      sha256: (outcome === 'mismatch' ? 'b' : 'a').repeat(64), basename: 'result.txt' });
    await flush();
    assert.equal(ui.get('artifact-download').disabled, false);
    if (outcome === 'saved') assert.match(ui.get('notice').textContent, /Saved exact released artifact bytes: result.txt/);
    if (outcome === 'cancelled') assert.match(ui.get('notice').textContent, /Artifact retrieval cancelled/);
    if (outcome === 'stale') assert.equal(ui.get('notice').textContent, notice);
    if (outcome === 'mismatch') assert.match(ui.get('notice').textContent, /integrity check failed/);
    if (outcome === 'rejected') assert.match(ui.get('notice').textContent, /Current request failed/);
  });
}

test('disconnected onboarding gives local commands and treats namespace setup as optional', async () => {
  const ui = fixture({ status: async () => null }); await flush();
  assert.equal(ui.get('local-control-start').hidden, false);
  assert.match(ui.get('journey-next').textContent, /init-once and serve.*owner.header.*optional/);
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /tessera init --state-dir &quot;|tessera init --state-dir "/);
  assert.match(html, /tessera serve --state-dir "\$HOME\/\.local\/state\/tessera-alpha" --port 8765/);
  assert.match(html, /On later launches, run only serve/);
  assert.match(html, /id="journey-next"[^>]*>Next: Follow the init-once and serve/);
  assert.match(html, /Optional advanced setup: Kubernetes namespace/);
  assert.match(html, /does not start a backend automatically or grant execution authority/);
});

async function adminFixture(overrides = {}) {
  const identity = { id: 'connection', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', principalId: 'human', role: 'human', health: 'online' };
  const ui = fixture({ connect: async () => identity, status: async () => identity,
    projects: async () => ({ projects: [] }),
    adminWorkspace: async () => ({ records: [], next_offset: null, held: false, root_admin: true, control_status: 'ready', observed_at: '2026-09-23T00:00:00Z' }),
    adminAudit: async () => ({ events: [] }), ...overrides });
  ui.get('connection-form').fields = { name: 'Fixture', endpoint: identity.endpoint };
  ui.submit('connection-form'); await flush();
  await ui.selectProject(project('p1'));
  ui.get('admin-open').listeners.click(); await flush();
  return ui;
}

test('admin secret form previews only fake metadata and requires explicit confirmation', async () => {
  const calls = [];
  const ui = await adminFixture({ adminCreate: async (...args) => { calls.push(args); return {}; } });
  ui.get('admin-secrets').listeners.click(); await flush();
  ui.get('admin-secret-form').fields = { purpose: 'salad_management', minutes: '15' };
  ui.submit('admin-secret-form'); await flush();
  assert.equal(calls.length, 0);
  assert.equal(ui.get('admin-preview').hidden, false);
  const preview = JSON.parse(ui.get('admin-preview-body').textContent);
  assert.equal(preview.spec.backend, 'fake');
  assert.equal(preview.spec.audience, 'server_proxy');
  assert.equal(preview.spec.provider, 'salad');
  assert.equal('value' in preview.spec, false);
  ui.get('admin-confirm').listeners.click(); await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'connection'); assert.equal(calls[0][1], 'p1');
  assert.equal(calls[0][3], 'command');
  assert.equal(ui.get('admin-preview').hidden, true);
  assert.equal(ui.get('admin-preview-body').textContent, '');
  assert.match(ui.get('admin-status').textContent, /provider DISABLED/);
});

test('protected enrollment renderer passes metadata only, no secret field, and discards stale confirmation', async () => {
  const reply = deferred(), calls = [], contexts = [];
  const record = { id: 'admin_' + 'a'.repeat(32), project_id: 'p1', revision: 2, version: 1,
    enrollment_id: 'b'.repeat(32), spec: { kind: 'secret', backend: 'os_keychain', provider: 'salad', purpose: 'salad_management', audience: 'server_proxy' },
    state: 'approved', proposed_by: 'different_human', health: 'unknown', versions: [] };
  const ui = await adminFixture({ secretContext: async (...args) => contexts.push(args),
    adminWorkspace: async () => ({ records: [record], next_offset: null, root_admin: true, held: false }),
    protectedSecret: (...args) => { calls.push(args); return reply.promise; } });
  ui.get('admin-secrets').listeners.click(); await flush();
  find(ui.get('admin-records'), n => n.textContent === 'Enroll secret — native hidden prompt').listeners.click(); await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(Object.keys(calls[0][1]).sort(), ['enrollment_id', 'expected_revision', 'operation', 'project_id', 'record_id']);
  assert.equal(calls[0][1].operation, 'enroll');
  await ui.selectProject(project('p2'));
  reply.resolve({ state: 'active', version: 1, providerEnabled: false }); await flush();
  assert.equal(ui.get('admin-records').children.length, 0);
  assert.doesNotMatch(ui.get('admin-status').textContent, /Protected operation: active/);
  assert.deepEqual(contexts.at(-1), ['connection', 'p2']);
  ui.clearContext(); await flush();
  assert.deepEqual(contexts.at(-1), ['connection', null]);
});

test('protected proposal renderer binds purpose and provider without capturing input value', async () => {
  const ui = await adminFixture();
  ui.get('admin-secret-form').fields = { backend: 'os_keychain', purpose: 'ai_gateway', minutes: '15' };
  ui.submit('admin-secret-form'); await flush();
  const preview = JSON.parse(ui.get('admin-preview-body').textContent);
  assert.equal(preview.spec.backend, 'os_keychain');
  assert.equal(preview.spec.purpose, 'ai_gateway'); assert.equal(preview.spec.provider, 'ai_gateway');
  assert.equal(preview.spec.audience, 'gateway');
  assert.deepEqual(Object.keys(preview.spec).sort(), ['audience', 'backend', 'expires_at', 'kind', 'provider', 'purpose']);
});

test('admin approval is exact-revision, no self-review and readiness stays blocked', async () => {
  const calls = [];
  const record = { id: 'admin_1', project_id: 'p1', spec: { kind: 'configuration', component: 'execution_target', provider: 'salad' }, state: 'proposed', proposed_by: 'agent', revision: 3, version: 0, health: 'unknown', verification: 'unknown' };
  const ui = await adminFixture({ adminWorkspace: async () => ({ records: [record], next_offset: null, root_admin: true, held: false }), adminCommand: async (...args) => { calls.push(args); record.state = 'approved'; record.revision++; return record; } });
  ui.get('admin-targets').listeners.click(); await flush();
  assert.equal(calls.length, 0);
  find(ui.get('admin-records'), n => n.textContent === 'Review approval').listeners.click(); await flush();
  assert.equal(calls.length, 0);
  ui.get('admin-confirm').listeners.click(); await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3])), { expected_revision: 3, action: 'approve' });
  assert.equal(find(ui.get('admin-records'), n => n.textContent === 'Enable live — unavailable').disabled, true);
  assert.ok(find(ui.get('admin-records'), n => n.textContent.includes('verification unknown')));
  record.state = 'proposed'; record.proposed_by = 'human';
  ui.get('admin-refresh').listeners.click(); await flush();
  assert.equal(find(ui.get('admin-records'), n => n.textContent === 'Review approval'), null);
});

test('admin pending key, stale reads, disconnect and held recovery cannot dispatch', async () => {
  const calls = [], key = deferred(), read = deferred();
  const ui = await adminFixture({ commandId: () => key.promise, adminCreate: async (...args) => calls.push(args) });
  ui.get('admin-secret-form').fields = { purpose: 'ai_gateway', minutes: '15' };
  ui.submit('admin-secret-form'); await flush();
  ui.get('admin-confirm').listeners.click(); await flush();
  await ui.selectProject(project('p2'));
  key.resolve('key'); await flush();
  assert.equal(calls.length, 0); assert.equal(ui.get('admin-preview-body').textContent, '');
  ui.api.adminWorkspace = () => read.promise;
  ui.get('admin-open').listeners.click(); await flush();
  ui.clearContext();
  read.resolve({ records: [{ id: 'wrong-context' }] }); await flush();
  assert.equal(ui.get('admin-records').children.length, 0);
  assert.equal(ui.get('admin-workspace').hidden, true);
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  ui.get('admin-secret-form').fields = { purpose: 'ai_gateway', minutes: '15' };
  ui.submit('admin-secret-form'); await flush();
  assert.equal(calls.length, 0);
  assert.equal(ui.get('admin-preview').hidden, true);
});

test('admin uncertain submission is not replayed and clears the preview', async () => {
  let calls = 0;
  const ui = await adminFixture({ adminCreate: async () => { calls++; throw new Error('Command outcome is unknown.'); } });
  ui.get('admin-secret-form').fields = { purpose: 'monitor', minutes: '15' };
  ui.submit('admin-secret-form'); await flush();
  ui.get('admin-confirm').listeners.click(); await flush();
  ui.get('admin-confirm').listeners.click(); await flush();
  assert.equal(calls, 1);
  assert.equal(ui.get('admin-preview-body').textContent, '');
  assert.match(ui.get('admin-status').textContent, /Refresh history/);
});

test('null-spec draft stays visible and editable without inventing an execution specification', async () => {
  const task = { id: 'task_1', project_id: 'p1', plan_digest: 'a'.repeat(64), revision: 1, status: 'draft' };
  const plan = { ...task, task_id: task.id, title: 'Draft question', purpose: 'Purpose', budget_cents: 0, max_duration_seconds: 120, execution_spec: null };
  const calls = [];
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan, reviseTaskPlan: async (...args) => { calls.push(args); return {}; } });
  ui.applyConnectionHealth(await ui.api.status());
  await ui.selectProject(project('p1')); await flush();
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('Not yet supplied')));
  const revise = find(ui.get('tasks'), n => n.textContent === 'Revise this proposal');
  assert.ok(revise); revise.listeners.click(); await flush();
  assert.equal(ui.get('task-revise-dialog').open, true);
  const form = ui.get('task-revise-form');
  assert.equal(form.fields.input_snapshot_digest, ''); assert.equal(form.fields.argv, '[]');
  assert.equal(form.fields.max_output_bytes, 1);
  form.fields = { ...form.fields, argv: '["fixture"]', input_snapshot_digest: 'b'.repeat(64), output_paths: '["result.json"]' };
  ui.submit('task-revise-form'); await flush();
  assert.equal(calls.length, 1); assert.equal(calls[0][2].expected_revision, 1);
  assert.equal(calls[0][2].execution_spec.network_enabled, false);
  assert.match(ui.get('notice').textContent, /no admission, dispatch, or run/);
});

test('approved task revision 3 retains immutable plan revision 2; wrong digest or revision fails closed', async () => {
  const task = { id: 'task_1', project_id: 'p1', plan_digest: 'a'.repeat(64), revision: 3, status: 'approved' };
  const plan = { ...task, revision: 2, task_id: task.id, title: 'Reviewed plan', purpose: 'Purpose', execution_spec: {
    argv: ['fixture'], pinned_skill_digests: [], output_paths: [], network_enabled: false,
  } };
  const revisions = [];
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan, reviseTaskPlan: async (...args) => { revisions.push(args); return {}; } });
  ui.applyConnectionHealth(await ui.api.status());
  await ui.selectProject(project('p1')); await flush();
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('task revision 3 · plan revision 2')));
  assert.equal(find(ui.get('tasks'), n => n.textContent === 'Plan approved').disabled, true);
  find(ui.get('tasks'), n => n.textContent === 'Include plan in coordinator context').listeners.click(); await flush();
  assert.equal(ui.state().selectedCoordinatorContext[0].expected_revision, 3);
  find(ui.get('tasks'), n => n.textContent === 'Revise this proposal').listeners.click(); await flush();
  ui.submit('task-revise-form'); await flush();
  assert.equal(revisions.length, 1);
  assert.equal(revisions[0][2].expected_revision, 3);
  for (const invalid of [{ ...plan, revision: 3 }, { ...plan, plan_digest: 'b'.repeat(64) }]) {
    ui.api.taskPlan = async () => invalid;
    await ui.selectProject(project('p1')); await flush();
    assert.equal(find(ui.get('tasks'), n => n.textContent === 'Plan approved'), null);
    assert.ok(find(ui.get('tasks'), n => n.textContent.includes('Approval is unavailable')));
  }
});

test('health ticks preserve approved task3/plan2 authority fences and recover eligible edit controls', async () => {
  const task = { id: 'task_1', project_id: 'p1', plan_digest: 'a'.repeat(64), revision: 3, status: 'approved' };
  const plan = { ...task, revision: 2, task_id: task.id, title: 'Reviewed plan', purpose: 'Purpose', execution_spec: null };
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan });
  const online = await ui.api.status(); ui.applyConnectionHealth(online);
  await ui.selectProject(project('p1')); await flush();
  const approved = find(ui.get('tasks'), n => n.textContent === 'Plan approved');
  const revise = find(ui.get('tasks'), n => n.textContent === 'Revise this proposal');
  assert.equal(approved.disabled, true); assert.equal(revise.disabled, false);
  ui.applyConnectionHealth(online);
  assert.equal(approved.disabled, true); assert.equal(revise.disabled, false);
  ui.applyConnectionHealth({ ...online, health: 'offline' });
  assert.equal(approved.disabled, true); assert.equal(revise.disabled, true);
  ui.applyConnectionHealth(online);
  assert.equal(approved.disabled, true); assert.equal(revise.disabled, false);
  revise.listeners.click(); await flush();
  assert.equal(ui.get('task-revise-dialog').open, true);
  assert.equal(ui.get('task-revise-form').fields.input_snapshot_digest, '');
  assert.equal(approved.disabled, true);
});

test('health availability cannot loosen expired, read-only, ineligible or busy action states', async () => {
  const ui = fixture(); const online = await ui.api.status(); ui.applyConnectionHealth(online);
  await ui.selectProject(project('p1')); await flush();
  const controls = [];
  for (const id of ['tasks', 'decisions', 'events', 'admin-records', 'remote-workflows', 'study-detail']) {
    const ineligible = new Element('button'); ineligible.disabled = true; ineligible.textContent = 'Read-only or expired';
    const eligible = new Element('button'); eligible.disabled = false; eligible.textContent = 'Eligible';
    ui.get(id).append(ineligible, eligible); controls.push({ ineligible, eligible });
  }
  const busy = ui.get('task-form').querySelector('button[type="submit"]'); busy.disabled = true;
  ui.applyConnectionHealth(online);
  for (const { ineligible, eligible } of controls) { assert.equal(ineligible.disabled, true); assert.equal(eligible.disabled, false); }
  assert.equal(busy.disabled, true);
  ui.applyConnectionHealth({ ...online, health: 'offline' });
  for (const { ineligible, eligible } of controls) { assert.equal(ineligible.disabled, true); assert.equal(eligible.disabled, true); }
  ui.applyConnectionHealth(online);
  for (const { ineligible, eligible } of controls) { assert.equal(ineligible.disabled, true); assert.equal(eligible.disabled, false); }
  assert.equal(busy.disabled, true);
});

test('verified coordinator availability recovers eligibility without releasing an in-flight composer', async () => {
  const pending = deferred(); let calls = 0;
  const ui = fixture({ requestCoordinatorTurn: async () => { calls++; return pending.promise; } });
  const online = await ui.api.status(); ui.applyConnectionHealth(online);
  await ui.selectProject(project('p1')); await flush();
  const form = ui.get('coordinator-form'); const submit = form.querySelector('button[type="submit"]');
  assert.equal(submit.disabled, false);
  form.fields = { mode: 'Discuss', prompt: 'Keep this proposal-only.' };
  ui.submit('coordinator-form'); await flush();
  assert.equal(calls, 1); assert.equal(submit.disabled, true);
  ui.applyConnectionHealth(online); ui.submit('coordinator-form'); await flush();
  assert.equal(submit.disabled, true); assert.equal(calls, 1);
  pending.resolve({}); await flush();
  assert.equal(submit.disabled, false);
});

test('offline control snapshots cannot enable actions in a changed context', async () => {
  const task = { id: 'task_1', project_id: 'p1', plan_digest: 'a'.repeat(64), revision: 1, status: 'draft' };
  const plan = { ...task, task_id: task.id, title: 'Draft', purpose: 'Purpose', execution_spec: null };
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan });
  const online = await ui.api.status(); ui.applyConnectionHealth(online);
  await ui.selectProject(project('p1')); await flush();
  const approve = find(ui.get('tasks'), n => n.textContent === 'Approve this plan');
  assert.equal(approve.disabled, false);
  ui.applyConnectionHealth({ ...online, health: 'offline' });
  await ui.selectProject(project('p1')); await flush();
  ui.applyConnectionHealth(online);
  assert.equal(find(ui.get('tasks'), n => n.textContent === 'Approve this plan').disabled, true);
  await ui.selectProject(project('p1')); await flush();
  assert.equal(find(ui.get('tasks'), n => n.textContent === 'Approve this plan').disabled, false);
});

test('workflow guide tracks disconnected, connected, project and held contexts truthfully', async () => {
  const ui = fixture();
  ui.clearContext();
  assert.match(ui.get('journey-next').textContent, /init-once and serve.*Connect with/);
  await connectFixture(ui);
  assert.match(ui.get('journey-next').textContent, /Select an existing project/);
  const selection = ui.selectProject(project('p1'));
  assert.match(ui.get('journey-next').textContent, /admission and dispatch are separate/);
  await selection; await flush();
  assert.match(ui.get('journey-next').textContent, /admission and dispatch are separate/);
  ui.applyRecoveryStatus({ state: 'held_restore', recovery_hold: true });
  assert.match(ui.get('journey-next').textContent, /recovery hold/);
});

test('autonomy task projection shows unknown and receipts as text, discards stale reads', async () => {
  const task = { id: 'task_1', project_id: 'p1', plan_digest: 'a'.repeat(64), revision: 1, status: 'draft' };
  const plan = { ...task, task_id: task.id, title: 'Task', purpose: 'Purpose', execution_spec: {
    argv: ['fixture'], pinned_skill_digests: [], output_paths: [], network_enabled: false,
  } };
  const response = { sessions: [{ id: 'autonomy_1', task_id: task.id, project_id: 'p1', org_id: 'org',
    revision: 4, plan_digest: 'b'.repeat(64), state: 'unknown', pending_approval: false,
    execution_enabled: false, provider_enabled: false, live_activation: 'blocked', actions: [],
    blocked_reasons: ['unknown'], checkpoint: { question: '<script>never execute</script>' } }] };
  const pending = deferred(); let calls = 0;
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan,
    autonomySessions: async () => ++calls === 1 ? response : pending.promise });
  ui.applyConnectionHealth(await ui.api.status());
  await ui.selectProject(project('p1')); await flush();
  const button = find(ui.get('tasks'), n => n.textContent === 'Inspect autonomy sessions');
  assert.ok(button);
  await button.listeners.click(); await flush();
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('unknown · revision 4')));
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('<script>never execute</script>')));
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('billing unknown')));
  const late = button.listeners.click();
  await ui.selectProject(project('p2')); await flush();
  pending.resolve(response); await late;
  assert.equal(find(ui.get('tasks'), n => n.textContent.includes('unknown · revision 4')), null);
});
test('remote execution panel reviews exact target, reports pending cancellation and fences stale reads', async () => {
  const task = { id: 'task_1', project_id: 'p1', plan_digest: 'a'.repeat(64), revision: 1, status: 'draft' };
  const plan = { ...task, task_id: task.id, title: 'Remote task', purpose: 'Fixture', execution_spec: {
    argv: ['fixture'], pinned_skill_digests: [], output_paths: [], network_enabled: false,
  } };
  const route = { id: 'route_1', task_id: task.id, project_id: 'p1', org_id: 'org', revision: 1,
    proposal_digest: 'b'.repeat(64), proposal: { selection: { target: 'salad_container' } },
    provider_enabled: false, live_activation: 'blocked', state: 'proposed', desired: 'hold',
    provider_status: 'not_queried', cancellation: 'not_requested', go_no_go: 'no_go_live_activation_blocked',
    receipts: [{ log: '<script>not executable</script>' }], cost_cents: null };
  const calls = []; const pending = deferred(); let stale = false;
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan,
    remoteExecutions: async () => stale ? pending.promise : { routes: [route] },
    remoteExecutionCommand: async (...args) => { calls.push(args); route.revision++; route.state = 'reviewed'; return route; } });
  ui.applyConnectionHealth(await ui.api.status());
  await ui.selectProject(project('p1')); await flush();
  const inspect = find(ui.get('tasks'), n => n.textContent === 'Inspect / reconnect remote execution');
  await inspect.listeners.click(); await flush();
  assert.equal(calls.length, 0);
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('salad_container · proposed')));
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('<script>not executable</script>')));
  const approve = find(ui.get('tasks'), n => n.textContent === 'Review and approve exact remote proposal');
  assert.equal(approve.disabled, false);
  approve.listeners.click(); await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], 'route_1');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][2])), { expected_revision: 1, proposal_digest: 'b'.repeat(64), command: 'approve' });
  route.state = 'unknown'; route.cancellation = 'pending'; route.desired = 'stop'; route.provider_status = 'unknown';
  await inspect.listeners.click(); await flush();
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('cancellation pending')));
  assert.ok(find(ui.get('tasks'), n => n.textContent.includes('cost: unknown')));
  stale = true;
  const late = inspect.listeners.click();
  await ui.selectProject(project('p2')); await flush();
  pending.resolve({ routes: [route] }); await late;
  assert.equal(find(ui.get('tasks'), n => n.textContent.includes('cancellation pending')), null);
  assert.equal(calls.length, 1);
});
const remoteWorkflow = (state = 'unknown') => ({
  operation_id: 'a'.repeat(32), org_id: 'org', project_id: 'p1', task_id: 'task_1', grant_id: 'grant_1',
  lease_id: 'lease_1', worker_principal_id: 'worker_1', epoch: 2, experiment_attempt_id: 'attempt_1',
  experiment_id: 'experiment_1', experiment_design_revision: 1, experiment_variant_id: 'variant_1', state,
  operation_desired_state: 'admitted',
  last_heartbeat_at: null, last_checkpoint_sequence: 0, last_checkpoint_cursor: null, last_checkpoint_digest: null,
  last_reconnected_at: null, control_outage_at: null, last_observed_at: '2026-01-01T00:00:00Z',
  last_observed_state: state === 'prepared' ? null : state, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  reconciliation_required: state !== 'exited', operational_only: true,
  reconciliation: state === 'prepared' ? null : { observed_state: state, observed_at: '2026-01-01T00:00:00Z',
    termination_confirmed: state === 'exited', cursor: 'observation:synthetic', sources: [], source_order: ['worker'],
    contradiction_state: state === 'exited' ? 'none' : 'unconfirmed_current_observation' },
});
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

test('research program inbox explains review-only scope and requires a human rationale', async () => {
  const calls = [];
  const decision = { ...pendingDecision(), task_id: null, kind: 'direction', scope: {
    type: 'research_program', outcome: 'outcome_1', action: 'go_no_go', recommendation: 'no_go',
    study_id: 's1', basis_digest: 'a'.repeat(64), supersedes_id: 'prior-review', reason: 'Unknown execution',
  } };
  const ui = fixture({ decisions: async () => ({ decisions: [decision] }),
    resolveDecision: async (...args) => { calls.push(args); return {}; } });
  await ui.selectProject(project('p1')); await flush();
  const panel = ui.get('decisions');
  assert.ok(find(panel, node => node.textContent.includes('outcome_1 · go_no_go · no_go')));
  assert.ok(find(panel, node => node.textContent.includes('not a byte release')));
  assert.ok(find(panel, node => node.textContent.includes('not reversed by an agent suggestion')));
  const rationale = find(panel, node => node.tagName === 'textarea');
  const approve = find(panel, node => node.textContent === 'Approve');
  assert.equal(rationale.placeholder, 'Required human rationale');
  approve.listeners.click(); await flush();
  assert.equal(calls.length, 0);
  assert.equal(approve.disabled, false);
  rationale.value = 'Stop while termination remains unknown.';
  approve.listeners.click(); await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].rationale, rationale.value);
});
test('program review authors a complete Outcome 0 plan from current records and keeps proposals, human decisions, and stale evidence distinct', async () => {
  const calls = []; const source = { id: 'source_1', verdict: 'supported', uri: 'https://example.test/source' };
  const variant = { id: 'variant_1', status: 'registered', name: 'Declared arm' };
  const current = studyId => ({ study_id: studyId, project_id: 'p1', basis_digest: 'a'.repeat(64), basis: { sources: [source], variants: [variant], findings: [] },
    reviews: [{ id: 'old_1', requester_principal_id: 'agent_1', status: 'approved', resolved_by_principal_id: 'human_1', rationale: 'Earlier human review.', basis_current: false, is_proposal_head: true, is_effective_review: false,
      scope: { outcome: 'outcome_0', action: 'plan', basis_digest: 'b'.repeat(64), reason: 'Earlier proposal', supersedes_id: null } }],
    condition_catalog: { outcome_0: ['direct_property_language'], outcome_1: ['unified_mixture_schema'] }, execution_granted: false, scientific_validity: 'not_assessed' });
  const ui = fixture({ study: async () => ledger('s1', [variant]), programContext: async (_connection, studyId) => current(studyId),
    createDecision: async (...args) => { calls.push(args); return { id: 'new_review' }; } });
  await ui.selectProject(project('p1')); await ui.selectStudy({ id: 's1', project_id: 'p1', status: 'open' }); await flush();
  const history = JSON.stringify(ui.get('program-review-detail'));
  assert.match(history, /agent proposals/); assert.match(history, /Human decisions/); assert.match(history, /STALE EVIDENCE/);
  ui.get('program-review').listeners.click(); await flush();
  const form = ui.get('program-review-form');
  form.fields = { outcome: 'outcome_0', action: 'plan', reason: 'Prespecify the bounded review.', objective: 'Measure a declared property.', hypothesis: 'The declared arm changes the property.', success_gate: 'A prespecified threshold.', failure_gate: 'A null or inconclusive result.', stop_gate: 'Stop at the declared limit.', target_domain: 'Declared domain.', provenance_policy: 'Retain source and method provenance.', budget_cents: '0', max_wall_seconds: '60', max_gpu_hours: '0', target: 'local_cpu', resources: 'One bounded CPU.', dataset_recommendation: 'Declared data only.', representation_recommendation: 'Declared representation.', split_recommendation: 'Hold out before review.', metric_recommendation: 'Record calibration.', uncertainty: 'Unknown external validity.', missing_prerequisites: '' };
  for (const input of [...findAll(ui.get('program-data-sources'), node => node.dataset?.programChoiceId === 'source_1'), ...findAll(ui.get('program-conditions'), node => node.dataset?.programChoiceId === 'variant_1')]) input.checked = true;
  const rationale = find(ui.get('program-conditions'), node => node.placeholder === 'Required condition rationale'); rationale.value = 'Selected as the declared test arm.';
  ui.submit('program-review-form'); await flush();
  assert.equal(calls.length, 1);
  const body = calls[0][1]; assert.equal(body.kind, 'direction'); assert.equal(body.scope.supersedes_id, 'old_1');
  assert.deepEqual(JSON.parse(JSON.stringify(body.scope.brief.data_source_ids)), ['source_1']);
  assert.deepEqual(JSON.parse(JSON.stringify(body.scope.conditions.direct_property_language.variant_ids)), ['variant_1']);
  assert.equal(body.scope.basis_digest, 'a'.repeat(64));
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
  const admittedOperationId = '9'.repeat(32);
  const oldAttempt = { id: 'attempt-old-failed', variant_id: first.variants[0].id, design_revision: 1,
    provenance_kind: 'admitted_operation', operation_id: operationId, execution_plan_digest: 'a'.repeat(64), package_digest: '5'.repeat(64),
    operation_desired_state: 'admitted', operation_observed_state: 'exited', termination_status: 'confirmed', human_observation: null,
    outcome: 'failed', reason: 'The old bounded operation failed.', scientific_validity: 'not_assessed' };
  const humanAttempt = { id: 'attempt-current-human', variant_id: second.variants[0].id, design_revision: 2,
    provenance_kind: 'human_reported_observation', operation_id: null, execution_plan_digest: null, package_digest: null,
    operation_desired_state: null, operation_observed_state: 'not_observed', termination_status: 'not_applicable',
    human_observation: 'A human recorded the current baseline observation.', outcome: 'inconclusive', reason: 'Human observation remains unresolved.', scientific_validity: 'not_assessed' };
  const admittedAttempt = { id: 'attempt-admitted-unknown', experiment_id: id, variant_id: second.variants[1].id,
    design_revision: 2, task_id: 'task_1', plan_digest: 'a'.repeat(64), grant_id: 'grant_1', operation_id: admittedOperationId,
    package_digest: '0'.repeat(64), state: 'unknown', outcome: null, reason: null, revision: 4,
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:03Z', created_by_principal_id: 'human', resolved_by_principal_id: null,
    operational_state: 'unknown', operational_observed_state: 'unknown', operational_termination_confirmed: false,
    operational_observation_cursor: 'observation:fixture-4', operational_observed_at: '2026-01-01T00:00:03Z', human_outcome: null, scientific_validity: 'not_assessed', state_history: [
      { attempt_id: 'attempt-admitted-unknown', sequence: 1, state: 'admitted', observed_state: null, reason: null, operation_id: null, recorded_at: '2026-01-01T00:00:00Z', actor_principal_id: 'human' },
      { attempt_id: 'attempt-admitted-unknown', sequence: 2, state: 'preparing', observed_state: null, reason: null, operation_id: admittedOperationId, recorded_at: '2026-01-01T00:00:01Z', actor_principal_id: 'worker' },
      { attempt_id: 'attempt-admitted-unknown', sequence: 3, state: 'running', observed_state: 'running', reason: null, operation_id: admittedOperationId, recorded_at: '2026-01-01T00:00:02Z', actor_principal_id: 'worker' },
      { attempt_id: 'attempt-admitted-unknown', sequence: 4, state: 'unknown', observed_state: 'unknown', reason: null, operation_id: admittedOperationId, recorded_at: '2026-01-01T00:00:03Z', actor_principal_id: 'worker' },
    ] };
  const artifacts = [
    { id: 'artifact-legacy', project_id: projectId, relative_path: 'results/metric.json', sha256: '6'.repeat(64), status: 'quarantined', classification: 'unclassified', retention_state: 'legacy_unplaced', custody_state: 'legacy_unplaced' },
    { id: 'artifact-degraded', project_id: projectId, relative_path: 'results/degraded.json', sha256: '7'.repeat(64), status: 'quarantined', classification: 'unclassified', retention_state: 'degraded', custody_state: 'degraded' },
    { id: 'artifact-deleted', project_id: projectId, relative_path: 'results/deleted.json', sha256: '8'.repeat(64), status: 'quarantined', classification: 'unclassified', retention_state: 'deleted', custody_state: 'deleted' },
  ];
  const measurement = { id: 'measurement-1', attempt_id: oldAttempt.id, name: 'duration', value: { seconds: 1 }, artifact_id: artifacts[0].id };
  return { completeness: 'complete_in_scope_goal_to_decision_chain',
    experiment: { id, project_id: projectId, goal: second.goal, success_criterion: second.success_criterion, current_design_revision: 2, design: second },
    designs: [first, second], variants: [...first.variants, ...second.variants], attempts: [oldAttempt, humanAttempt], admitted_attempts: [admittedAttempt],
    // The operation has since entered cancellation and an unknown observation;
    // this must not rewrite the attempt's recorded outcome or terminal observation.
    operations: [{ operation_id: operationId, project_id: projectId, desired_state: 'cancel_requested', observed_state: 'unknown' },
      { operation_id: admittedOperationId, project_id: projectId, task_id: 'task_1', grant_id: 'grant_1', package_digest: '0'.repeat(64),
        experiment_attempt_id: admittedAttempt.id, experiment_id: id, experiment_design_revision: 2, experiment_variant_id: second.variants[1].id,
        desired_state: 'admitted', observed_state: 'unknown' }],
    artifact_links: artifacts.map(artifact => ({ attempt_id: oldAttempt.id, artifact_id: artifact.id })), artifacts, measurements: [measurement],
    conclusions: [{ id: 'conclusion-1', experiment_id: id, design_revision: 1, design_snapshot: first, status: 'reviewed', revision: 2, lineage_revision: 3, statement: 'The old result needs human review.',
      evidence: [{ attempt_id: oldAttempt.id, artifact_id: null, measurement_id: measurement.id }], scientific_validity: 'not_assessed', lineage: [{ id: 'amendment-1', conclusion_id: 'conclusion-1', experiment_id: id, relation: 'supersedes', reason: 'A human narrowed the interpretation after review.', expected_revision: 2, expected_design_revision: 1, prior_lineage_revision: 2, lineage_revision: 3, created_at: '2026-01-02T00:00:00Z', actor_principal_id: 'human', scientific_validity: 'not_assessed' }] },
    { id: 'conclusion-legacy-unbound', experiment_id: id, design_revision: null, design_snapshot: null, status: 'draft', revision: 1, lineage_revision: 1, statement: 'Legacy conclusion needs human review.',
      evidence: [{ attempt_id: oldAttempt.id, artifact_id: artifacts[1].id, measurement_id: null }], scientific_validity: 'not_assessed', lineage: [] }],
    conclusion_amendments: [{ id: 'amendment-1', conclusion_id: 'conclusion-1', experiment_id: id, relation: 'supersedes', reason: 'A human narrowed the interpretation after review.', expected_revision: 2, expected_design_revision: 1, prior_lineage_revision: 2, lineage_revision: 3, created_at: '2026-01-02T00:00:00Z', actor_principal_id: 'human', scientific_validity: 'not_assessed' }],
  };
};

async function connectFixture(ui, projects = []) {
  ui.api.connect = async () => ({ id: 'connection', name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health: 'online' });
  ui.api.recovery = async () => ({ state: 'active', recovery_hold: false });
  ui.api.projects = async () => ({ projects });
  ui.get('connection-form').fields = { name: 'Fixture', endpoint: 'http://127.0.0.1:8765' };
  ui.submit('connection-form');
  await flush();
}

test('onboarding keeps disconnected and connected-without-project states distinct', async () => {
  const ui = fixture();
  ui.clearContext();
  assert.equal(ui.get('project-new').disabled, true);
  assert.equal(ui.get('workspace').hidden, true);
  assert.equal(ui.state().activeProject, null);

  await connectFixture(ui);
  assert.match(ui.get('connection-status').textContent, /Fixture[\s\S]*online/);
  assert.equal(ui.get('project-new').disabled, false);
  assert.equal(ui.get('workspace').hidden, true);
  assert.equal(ui.get('projects').children.length, 0);
  assert.match(ui.get('welcome-heading').textContent, /Connected\. Start your first project/);
  assert.equal(ui.get('welcome-project-new').hidden, false);
  assert.equal(ui.get('welcome-project-new').disabled, false);
  ui.get('welcome-project-new').listeners.click();
  assert.equal(ui.get('project-dialog').open, true);
});

test('namespace onboarding exposes only target facts, makes existing use a zero-write confirmation, and never claims installation', async () => {
  const calls = [];
  const inspected = { state: 'present', uid: 'existing-uid', resourceVersion: '4', target: { kubeconfigReferenceSha256: 'a'.repeat(64), context: 'team/admin@prod', server: 'https://cluster.test', caSha256: 'b'.repeat(64), clusterUid: 'cluster-uid', namespace: 'astra-system' } };
  const ui = fixture({ namespaceInspect: async request => { calls.push(['inspect', request]); return inspected; },
    namespacePlanUseExisting: async request => { calls.push(['plan', request]); return { planId: 'existing-plan', mode: 'use-existing', digest: 'c'.repeat(64), expiresAt: '2030-01-01T00:00:00Z' }; } });
  await ui.selectNamespaceKubeconfig();
  ui.get('namespace-form').fields = { context: 'team/admin@prod', namespace: 'astra-system' };
  await ui.inspectNamespace();
  assert.equal(ui.get('namespace-use-existing').disabled, false);
  assert.match(JSON.stringify(ui.get('namespace-detail')), /zero Kubernetes writes/);
  assert.doesNotMatch(JSON.stringify(ui.get('namespace-detail')), /private kubeconfig bytes|\/Users\//);
  await ui.planNamespace('use-existing');
  assert.equal(ui.get('namespace-confirm-dialog').open, true);
  await ui.confirmNamespace();
  assert.equal(calls.length, 2);
  assert.match(ui.get('notice').textContent, /Connected to namespace; control plane not installed/);
  assert.match(JSON.stringify(ui.get('namespace-detail')), /Connected to namespace; control plane not installed/);
});

test('namespace dedicated plan displays digest and expiry, creates once, and reconciles unknown results without replay', async () => {
  const creates = []; const reconciles = [];
  const ui = fixture({ namespaceCreate: async request => { creates.push(request); return { state: 'reconciliation_required' }; },
    namespaceReconcile: async request => { reconciles.push(request); return { state: 'reconciliation_required', uid: null, resourceVersion: null }; } });
  await ui.selectNamespaceKubeconfig();
  ui.get('namespace-form').fields = { context: 'team/admin@prod', namespace: 'astra-system' };
  await ui.inspectNamespace(); await ui.planNamespace('create-dedicated');
  assert.match(JSON.stringify(ui.get('namespace-confirm-detail')), new RegExp('c'.repeat(64)));
  assert.match(JSON.stringify(ui.get('namespace-confirm-detail')), /2030-01-01/);
  const first = ui.confirmNamespace(); const duplicate = ui.confirmNamespace(); await Promise.all([first, duplicate]);
  assert.equal(creates.length, 1);
  assert.match(ui.get('notice').textContent, /unknown.*Reconcile read-only/i);
  await ui.reconcileNamespace();
  assert.equal(reconciles.length, 1);
  assert.match(ui.get('notice').textContent, /did not replay create/);
});

test('namespace cancellation, conflicts, expiry, and stale context fence old UI and do not replay', async () => {
  const late = deferred(); let clears = 0; let creates = 0;
  const ui = fixture({ namespaceSelectKubeconfig: async () => ({ state: 'cancelled' }), namespaceClear: async () => { clears++; },
    namespaceInspect: async () => late.promise, namespaceCreate: async () => { creates++; throw new Error('Namespace expired: namespace plan approval has expired'); } });
  await ui.selectNamespaceKubeconfig();
  assert.match(ui.get('notice').textContent, /cancelled/);
  ui.api.namespaceSelectKubeconfig = async () => ({ state: 'selected', reference: '2'.repeat(8) + '-2222-4222-8222-222222222222', name: 'selected-config' });
  await ui.selectNamespaceKubeconfig();
  ui.get('namespace-form').fields = { context: 'team/admin@prod', namespace: 'astra-system' };
  const pending = ui.inspectNamespace(); await flush();
  ui.clearContext(); late.resolve({ state: 'terminating', uid: 'old', resourceVersion: '1', target: { kubeconfigReferenceSha256: 'a'.repeat(64), context: 'team/admin@prod', server: 'https://cluster.test', caSha256: 'b'.repeat(64), clusterUid: 'cluster', namespace: 'astra-system' } });
  await pending;
  assert.equal(ui.state().namespaceContext, null);
  assert.equal(ui.get('namespace-use-existing').disabled, true);
  assert.ok(clears >= 1);
  assert.equal(creates, 0);
});

test('namespace forbidden, unavailable, terminating conflict, and expired-plan messages remain fenced and truthful', async () => {
  const terminating = { state: 'terminating', uid: 'terminating-uid', resourceVersion: '7', target: { kubeconfigReferenceSha256: 'a'.repeat(64), context: 'team/admin@prod', server: 'https://cluster.test', caSha256: 'b'.repeat(64), clusterUid: 'cluster', namespace: 'astra-system' } };
  const ui = fixture({ namespaceInspect: async () => terminating });
  await ui.selectNamespaceKubeconfig(); ui.get('namespace-form').fields = { context: 'team/admin@prod', namespace: 'astra-system' };
  await ui.inspectNamespace();
  assert.equal(ui.get('namespace-use-existing').disabled, true); assert.equal(ui.get('namespace-create-plan').disabled, true);
  assert.match(JSON.stringify(ui.get('namespace-detail')), /terminating/);
  await ui.namespaceWork(async () => { throw new Error('Namespace forbidden: Kubernetes RBAC denied'); });
  assert.match(ui.get('notice').textContent, /forbidden.*no write was retried/i);
  await ui.namespaceWork(async () => { throw new Error('Namespace command is unavailable.'); });
  assert.match(ui.get('notice').textContent, /Reconcile read-only/);
  await ui.namespaceWork(async () => { throw new Error('Namespace expired: namespace plan approval has expired'); });
  assert.match(ui.get('notice').textContent, /expired.*fresh plan/i);
});

test('failed connection keeps endpoint inputs visible for correction and retry', async () => {
  const ui = fixture({ connect: async () => { throw new Error('Control plane is unavailable or not ready (503).'); }, status: async () => null });
  ui.get('connection-dialog').showModal();
  ui.get('connection-form').fields = { name: 'Existing team endpoint', endpoint: 'https://control.example' };
  ui.submit('connection-form'); await flush();
  assert.equal(ui.get('connection-dialog').open, true);
  assert.deepEqual(ui.get('connection-form').fields, { name: 'Existing team endpoint', endpoint: 'https://control.example' });
  assert.match(ui.get('notice').textContent, /not ready \(503\)/);
});

test('project onboarding creates, selects, and fences duplicate and stale submissions', async () => {
  const pending = deferred(); const created = project('created'); const calls = []; const visible = [];
  const ui = fixture({ commandId: async () => 'create-project-command',
    createProject: async (_connection, body, key) => { calls.push({ body, key }); return pending.promise; },
    projects: async () => ({ projects: visible }),
  });
  await connectFixture(ui, visible);
  ui.get('project-new').listeners.click();
  ui.get('project-form').fields = { name: 'Created project' };
  ui.submit('project-form'); ui.submit('project-form'); await flush();
  assert.equal(calls.length, 1);
  visible.push(created); pending.resolve(created); await flush();
  assert.equal(JSON.stringify(calls[0]), JSON.stringify({ body: { name: 'Created project' }, key: 'create-project-command' }));
  assert.equal(ui.state().activeProject.id, 'created');
  assert.equal(ui.get('project-title').textContent, 'created');
  assert.equal(ui.get('workspace').hidden, false);
  assert.equal(ui.get('project-dialog').open, false);

  const late = deferred();
  ui.api.createProject = async () => late.promise;
  ui.get('project-new').listeners.click();
  ui.get('project-form').fields = { name: 'Created in old context' };
  ui.submit('project-form'); await flush();
  await ui.selectProject(project('B'));
  late.resolve(project('late-A')); await flush();
  assert.equal(ui.state().activeProject.id, 'B');
  assert.equal(ui.get('project-title').textContent, 'B');
  assert.equal(ui.get('project-form').querySelector('button[type="submit"]').disabled, false);
});

test('project creation preserves a failed draft, while authorization loss clears context', async () => {
  const rejected = fixture({ createProject: async () => { throw new Error('Control request failed (409).'); } });
  await connectFixture(rejected);
  rejected.get('project-new').listeners.click();
  rejected.get('project-form').fields = { name: 'Stale project' };
  rejected.submit('project-form'); await flush();
  assert.equal(rejected.get('project-dialog').open, true);
  assert.deepEqual(rejected.get('project-form').fields, { name: 'Stale project' });
  assert.equal(rejected.get('project-form').querySelector('button[type="submit"]').disabled, false);
  assert.match(rejected.get('notice').textContent, /409/);

  const unauthorized = fixture({ createProject: async () => { throw new Error('Control request failed (401).'); }, status: async () => null });
  await connectFixture(unauthorized);
  unauthorized.get('project-new').listeners.click();
  unauthorized.get('project-form').fields = { name: 'Unauthorized project' };
  unauthorized.submit('project-form'); await flush();
  assert.equal(unauthorized.get('connection-status').textContent, 'Not connected');
  assert.equal(unauthorized.get('project-new').disabled, true);
  assert.equal(unauthorized.get('project-dialog').open, false);
});

test('populated workspace keeps local coordinator, remote operations, and human outcomes separate', async () => {
  const data = experimentLedger();
  const ui = fixture({
    astraConversation: async () => ({ conversation: { project_id: 'p1', revision: 1 }, messages: [
      { id: 'local-reply', project_id: 'p1', sender_kind: 'coordinator', body: 'Local coordinator recap.', created_at: '2026-01-01T00:00:00Z' },
    ] }),
    remoteWorkflows: async () => ({ remote_workflows: [{ ...remoteWorkflow('running'), reconciliation_required: false }] }),
    experiment: async () => data,
  });
  await ui.selectProject(project('p1')); await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
  const workbench = JSON.stringify(ui.get('events'));
  const remote = JSON.stringify(ui.get('remote-workflows'));
  const experiment = JSON.stringify(ui.get('experiment-detail'));
  assert.match(workbench, /Deterministic local coordinator reply/);
  assert.match(workbench, /Provider disabled\. This reply has no approval, grant, dispatch, tool, shell, or skill-loading authority/);
  assert.match(remote, /A remote run observation is recorded; it is not a result, evidence, or conclusion/);
  assert.doesNotMatch(remote, /Human-recorded outcome/);
  assert.match(experiment, /Human-reported observation: A human recorded the current baseline observation/);
  assert.match(experiment, /Attempt-recorded outcome is not scientific success/);
});

test('worker discovery renders only the safe project projection and labels stale workers fenced', async () => {
  const calls = [];
  const worker = {
    id: 'worker_1', project_id: 'p1',
    approved_metadata: { build_identifier: 'approved-supervisor-v1', build_digest: 'a'.repeat(64), region: 'local' },
    lifecycle: { state: 'ready', revision: 3, credential_status: 'active' },
    freshness: { status: 'stale', last_seen_at: '2026-01-01T00:00:00Z', heartbeat_max_age_seconds: 60 },
    capabilities: { status: 'not_probed', probed: false },
    trust_zone: { status: 'declared_unverified', trust_class: 'test' },
    lease_admission: { registration_is_not_scheduling: true, status: 'fenced', fence_reason: 'heartbeat_stale' },
  };
  const ui = fixture({ workers: async (_connection, projectId, page) => {
    calls.push({ projectId, page }); return { workers: [worker] };
  } });
  await ui.selectProject(project('p1'));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].projectId, 'p1');
  assert.equal(calls[0].page.offset, 0);
  assert.equal(calls[0].page.limit, 100);
  const rendered = JSON.stringify(ui.get('workers'));
  assert.match(rendered, /Approved build: approved-supervisor-v1/);
  assert.match(rendered, /Freshness: stale/);
  assert.match(rendered, /New lease admission is fenced \(heartbeat_stale\)/);
  assert.match(rendered, /Registration is metadata only; it does not schedule, dispatch, or authorize work/);
  for (const forbidden of ['kubeconfig', 'endpoint', 'secret', 'worker_principal_id', 'credential_id']) {
    assert.ok(!rendered.includes(forbidden));
  }
});

test('remote workflow projection keeps admission, unknown state, reconciliation, and conclusions distinct', async () => {
  const calls = [];
  const record = remoteWorkflow('unknown');
  const stops = [];
  const ui = fixture({ remoteWorkflows: async (_connection, projectId, page) => {
    calls.push({ projectId, page }); return { remote_workflows: [record] };
  }, remoteWorkflow: async (_connection, operationId) => {
    assert.equal(operationId, record.operation_id); return record;
  }, executionGrant: async () => ({ id: record.grant_id, project_id: 'p1', revision: 4, status: 'active' }),
  revokeExecutionGrant: async (_connection, grantId, body, key) => { stops.push({ grantId, body, key }); return {}; } });
  await ui.selectProject(project('p1'));
  assert.equal(JSON.stringify(calls), JSON.stringify([{ projectId: 'p1', page: { offset: 0, limit: 100 } }]));
  const rendered = JSON.stringify(ui.get('remote-workflows'));
  assert.match(rendered, /Execution admission\/package intent is recorded|Remote state is unknown/);
  assert.match(rendered, /Reconciliation required/);
  assert.match(rendered, /Do not treat a stop request, reconnect, or displayed exit as confirmed termination/);
  assert.match(rendered, /Freshness: heartbeat not recorded · latest observation unknown at 2026-01-01T00:00:00Z/);
  assert.match(rendered, /Latest checkpoint: none recorded/);
  assert.ok(!rendered.includes('scientific success'));
  const refresh = find(ui.get('remote-workflows'), node => node.textContent === 'Refresh reconciled operation');
  refresh.listeners.click(); await flush();
  assert.equal(calls.length, 2);
  assert.match(ui.get('notice').textContent, /No run, dispatch, stop, evidence, or conclusion was replayed/);
  const stop = find(ui.get('remote-workflows'), node => node.textContent === 'Request stop');
  stop.listeners.click(); await flush();
  assert.equal(stops.length, 1);
  assert.equal(JSON.stringify(stops[0].body), JSON.stringify({ expected_revision: 4 }));
  assert.match(ui.get('notice').textContent, /does not confirm termination/);
});

test('remote workflow labels requested stop, confirmed cancellation, and unknown as separate states', async () => {
  const requested = remoteWorkflow('unknown');
  requested.operation_desired_state = 'cancel_requested';
  const confirmed = { ...remoteWorkflow('exited'), operation_id: 'b'.repeat(32), operation_desired_state: 'cancel_requested', reconciliation_required: false,
    reconciliation: { observed_state: 'exited', observed_at: '2026-01-01T00:00:03Z', termination_confirmed: true,
      cursor: 'observation:synthetic-confirmed', sources: ['worker'], source_order: ['worker'], contradiction_state: 'none' } };
  const ui = fixture({ remoteWorkflows: async () => ({ remote_workflows: [requested, confirmed] }) });
  await ui.selectProject(project('p1'));
  const rendered = JSON.stringify(ui.get('remote-workflows'));
  assert.match(rendered, /Stop requested in control\. This is not confirmed termination/);
  assert.match(rendered, /Remote state is unknown; execution, cost exposure, and termination require reconciliation/);
  assert.match(rendered, /Requested stop has confirmed cancellation from attributed operational evidence only/);
  assert.match(rendered, /Reconciled observation: exited · cancellation confirmed/);
  assert.doesNotMatch(rendered, /stopped|successful/i);
});

test('remote workflow pagination deduplicates live rows and never revokes a cross-project grant', async () => {
  const record = remoteWorkflow('unknown'); const calls = []; const stops = [];
  const ui = fixture({ remoteWorkflows: async (_connection, projectId, page) => {
    calls.push({ projectId, page });
    return page.offset === 0 ? { remote_workflows: Array.from({ length: 100 }, (_, index) => ({ ...record, operation_id: index === 0 ? record.operation_id : index.toString(16).padStart(32, '0') })) } :
      { remote_workflows: [{ ...record }, { ...record, operation_id: 'f'.repeat(32) }] };
  }, executionGrant: async () => ({ id: record.grant_id, project_id: 'p2', revision: 4, status: 'active' }),
  revokeExecutionGrant: async () => { stops.push(true); return {}; } });
  await ui.selectProject(project('p1'));
  await ui.loadRemoteWorkflows();
  assert.equal(calls[1].page.offset, 100);
  assert.equal(findAll(ui.get('remote-workflows'), node => node.dataset?.remoteWorkflowOperationId === record.operation_id).length, 1);
  const stop = find(ui.get('remote-workflows'), node => node.textContent === 'Request stop');
  stop.listeners.click(); await flush();
  assert.equal(stops.length, 0);
  assert.match(ui.get('notice').textContent, /current execution grant cannot be used/);
});

test('late remote-workflow reads cannot replace a newer project projection', async () => {
  const late = deferred(); const old = remoteWorkflow('unknown'); const current = { ...remoteWorkflow('running'), project_id: 'p2', operation_id: 'b'.repeat(32) };
  const ui = fixture({ remoteWorkflows: async (_connection, projectId) => projectId === 'p1' ? late.promise : { remote_workflows: [current] } });
  const stale = ui.selectProject(project('p1'));
  await flush();
  await ui.selectProject(project('p2'));
  late.resolve({ remote_workflows: [old] });
  await stale;
  assert.equal(ui.state().activeProject.id, 'p2');
  const rendered = JSON.stringify(ui.get('remote-workflows'));
  assert.match(rendered, new RegExp(current.operation_id));
  assert.doesNotMatch(rendered, new RegExp(old.operation_id));
});

test('consumer availability labels revoked credentials and blocks new coordinator turns', async () => {
  const ui = fixture({ coordinatorConsumerStatus: async () => ({ availability: 'unavailable', health: 'revoked', freshness: 'fresh', credential_status: 'revoked', credential_expires_at: '2030-01-01T00:00:00Z', heartbeat_at: '2026-01-01T00:00:00Z', last_outcome: 'idle', observed_consumers: 1, provider_disabled: true, provider_enabled: false }) });
  await ui.selectProject(project('p1'));
  assert.match(ui.get('coordinator-health').textContent, /credential revoked/);
  assert.equal(ui.get('coordinator-form').querySelector('button[type="submit"]').disabled, true);
});

test('human disposition reviews exact metadata before retention and never implicitly releases custody', async () => {
  const calls = []; const manifest = { id: 'manifest_1', project_id: 'p1', manifest_digest: 'a'.repeat(64), status: 'quarantined', classification: 'unclassified', late: false,
    artifacts: [{ id: 'artifact_1', relative_path: 'result.txt', size: 36, sha256: 'b'.repeat(64) }] };
  const ui = fixture({ status: async () => ({ id: 'connection', role: 'human', health: 'online' }), artifactManifests: async () => ({ artifact_manifests: [manifest] }),
    artifactDispositions: async () => ({ dispositions: [] }), recordArtifactDisposition: async (...args) => { calls.push(args); return {}; }, recordArtifactCustodyRelease: async () => { throw new Error('Implicit custody release forbidden'); } });
  ui.applyConnectionHealth(await ui.api.status()); await ui.selectProject(project('p1')); await flush();
  ui.get('artifact-disposition').listeners.click(); await flush();
  const form = ui.get('artifact-disposition-form');
  assert.equal(form.querySelector('button[type="submit"]').disabled, true);
  ui.applyConnectionHealth(await ui.api.status()); assert.equal(form.querySelector('button[type="submit"]').disabled, true);
  form.fields = { manifest_id: manifest.id };
  ui.get('artifact-disposition-load').listeners.click(); await flush();
  assert.match(JSON.stringify(ui.get('artifact-disposition-detail')), /36 bytes.*sha256/);
  assert.equal(form.querySelector('button[type="submit"]').disabled, false);
  form.fields.rationale = 'Human review of operational toy output, not scientific acceptance.';
  ui.submit('artifact-disposition-form'); await flush();
  assert.equal(calls.length, 1); assert.equal(calls[0][1], manifest.id);
  assert.equal(calls[0][2].disposition, 'retain_quarantined'); assert.equal(calls[0][2].expected_revision, 0);
  assert.match(ui.get('notice').textContent, /separately review custody release/);
});

test('human disposition refuses unloaded, tampered, held, offline and changed-project review bases', async () => {
  let writes = 0; const manifest = { id: 'manifest_1', project_id: 'p1', manifest_digest: 'a'.repeat(64), status: 'quarantined', artifacts: [], classification: 'unclassified', late: false };
  const ui = fixture({ status: async () => ({ id: 'connection', role: 'human', health: 'online' }), artifactManifests: async () => ({ artifact_manifests: [manifest] }), artifactDispositions: async () => ({ dispositions: [] }), recordArtifactDisposition: async () => { writes++; } });
  ui.applyConnectionHealth(await ui.api.status()); await ui.selectProject(project('p1')); await flush();
  ui.get('artifact-disposition').listeners.click(); await flush();
  const form = ui.get('artifact-disposition-form'); form.fields = { manifest_id: manifest.id, rationale: 'Review.' };
  ui.submit('artifact-disposition-form'); await flush(); assert.equal(writes, 0);
  ui.get('artifact-disposition-load').listeners.click(); await flush();
  form.fields.expected_manifest_digest = 'c'.repeat(64); ui.submit('artifact-disposition-form'); await flush(); assert.equal(writes, 0);
  form.fields.expected_manifest_digest = manifest.manifest_digest;
  ui.get('artifact-disposition-dialog').close(); ui.submit('artifact-disposition-form'); await flush(); assert.equal(writes, 0);
  ui.get('artifact-disposition-dialog').showModal();
  ui.applyRecoveryStatus({state:'held_restore',recovery_hold:true}); ui.submit('artifact-disposition-form'); await flush(); assert.equal(writes, 0);
  ui.applyRecoveryStatus({state:'active',recovery_hold:false});
  ui.api.status = async () => ({id:'connection',role:'human',health:'offline'}); ui.applyConnectionHealth(await ui.api.status()); ui.submit('artifact-disposition-form'); await flush(); assert.equal(writes, 0);
  ui.api.status = async () => ({id:'connection',role:'human',health:'online'}); await ui.selectProject(project('p2')); ui.submit('artifact-disposition-form'); await flush(); assert.equal(writes, 0);
});

test('human disposition cannot load a foreign-project manifest or enable an ineligible principal', async () => {
  const ui = fixture({status:async()=>({id:'connection',role:'human',health:'online'}), artifactManifests:async()=>({artifact_manifests:[{id:'manifest_1',project_id:'p2',manifest_digest:'a'.repeat(64),status:'quarantined',artifacts:[]}]}),artifactDispositions:async()=>({dispositions:[]})});
  ui.applyConnectionHealth(await ui.api.status()); await ui.selectProject(project('p1')); await flush();
  ui.get('artifact-disposition').listeners.click(); await flush();
  ui.get('artifact-disposition-form').fields={manifest_id:'manifest_1'};
  ui.get('artifact-disposition-load').listeners.click(); await flush();
  assert.equal(ui.get('artifact-disposition-form').querySelector('button[type="submit"]').disabled,true);
  assert.match(ui.get('notice').textContent,/No eligible exact quarantine manifest/);
  ui.api.status=async()=>({id:'connection',role:'agent',health:'online'});
  ui.applyConnectionHealth(await ui.api.status());
  assert.equal(ui.get('artifact-disposition').disabled,true);
  ui.get('artifact-disposition').listeners.click(); await flush();
  assert.match(ui.get('notice').textContent,/online human project/);
});

test('desktop keeps admission, dispatch, evidence-linked conclusion, and human review as distinct revision-fenced commands', async () => {
  const calls = []; const ledger = experimentLedger('e1');
  for (const variant of ledger.variants) variant.revision = 1;
  for (const design of ledger.designs) for (const variant of design.variants) variant.revision = 1;
  const experiment = { id: 'e1', project_id: 'p1', goal: 'Current goal.', success_criterion: 'Current criterion.', current_design_revision: 2 };
  const ui = fixture({ experiment: async () => ledger,
    admitExperimentAttempt: async (_connection, variantId, body, key) => { calls.push({ type: 'admit', variantId, body, key }); return {}; },
    dispatchAdmittedAttempt: async (_connection, attemptId, body, key) => { calls.push({ type: 'dispatch', attemptId, body, key }); return {}; },
    linkAttemptArtifacts: async (_connection, attemptId, body, key) => { calls.push({ type: 'link', attemptId, body, key }); return {}; },
    createConclusion: async (_connection, experimentId, body, key) => { calls.push({ type: 'conclusion', experimentId, body, key }); return {}; },
  });
  await ui.selectProject(project('p1')); await ui.selectExperiment(experiment);
  const admission = find(ui.get('experiment-detail'), node => node.textContent === 'Review separate execution admission');
  assert.ok(admission); admission.listeners.click(); await flush();
  ui.get('admission-form').fields = { grant_id: 'grant_1' }; ui.submit('admission-form'); await flush();
  assert.ok(calls.length, ui.get('notice').textContent); assert.equal(calls[0].type, 'admit');
  assert.equal(calls[0].body.expected_design_revision, 2);
  assert.equal(calls[0].body.expected_variant_revision, 1);
  assert.equal(calls[0].body.grant_id, 'grant_1');
  const dispatch = find(ui.get('experiment-detail'), node => node.textContent === 'Review Kubernetes dispatch');
  assert.ok(dispatch); dispatch.listeners.click(); await flush();
  ui.get('dispatch-form').fields = { target_digest: 'f'.repeat(64) }; ui.submit('dispatch-form'); await flush();
  assert.equal(calls[1].type, 'dispatch'); assert.equal(calls[1].attemptId, 'attempt-admitted-unknown');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body)), {
    expected_revision: 4, target_digest: 'f'.repeat(64), profile_version: 'development-minikube-opencode-v1',
  });
  const link = find(ui.get('experiment-detail'), node => node.textContent === 'Link retained artifact evidence');
  assert.ok(link); link.listeners.click(); await flush();
  ui.get('attempt-artifacts-form').fields = { artifact_ids: '["artifact-legacy"]' }; ui.submit('attempt-artifacts-form'); await flush();
  assert.equal(calls[2].type, 'link'); assert.equal(calls[2].body.expected_revision, 4);
  const author = ui.get('conclusion-new'); author.listeners.click(); await flush();
  const evidence = findAll(ui.get('conclusion-evidence'), node => node.dataset?.conclusionArtifactId);
  assert.ok(evidence.length); evidence[0].checked = true;
  ui.get('conclusion-form').fields = { statement: 'A bounded human interpretation.' }; ui.submit('conclusion-form'); await flush();
  assert.ok(calls.length > 3, ui.get('notice').textContent); assert.equal(calls[3].type, 'conclusion');
  assert.equal(calls[3].body.design_revision, 2);
  assert.equal(JSON.stringify(calls[3].body.evidence), JSON.stringify([{ attempt_id: 'attempt-old-failed', artifact_ids: ['artifact-legacy'], measurement_ids: [] }]));
  assert.match(ui.get('notice').textContent, /distinct from execution and scientific validity/);
});

test('disconnect and reconnect re-read fake workflow effects without replaying dispatch or stop', async () => {
  const workflow = remoteWorkflow('unknown'); const projectRecord = project('p1');
  let workflowReads = 0; let mutations = 0;
  const ui = fixture({ disconnect: async () => {}, remoteWorkflows: async (_connection, projectId) => {
    assert.equal(projectId, 'p1'); workflowReads++; return { remote_workflows: [workflow] };
  }, dispatchAdmittedAttempt: async () => { mutations++; }, revokeExecutionGrant: async () => { mutations++; } });
  await connectFixture(ui, [projectRecord]);
  ui.get('projects').children[0].listeners.click(); await flush();
  const firstTimeline = JSON.stringify(ui.get('remote-workflows'));
  ui.get('disconnect').listeners.click(); await flush();
  assert.equal(ui.state().activeProject, null);
  await connectFixture(ui, [projectRecord]);
  ui.get('projects').children[0].listeners.click(); await flush();
  assert.equal(workflowReads, 2);
  assert.equal(JSON.stringify(ui.get('remote-workflows')), firstTimeline);
  assert.equal(mutations, 0);
});

test('a pending dispatch command is fenced by project and connection changes', async () => {
  const projectKey = deferred(); const connectionKey = deferred(); const keys = [projectKey, connectionKey]; const ledger = experimentLedger(); let dispatches = 0;
  const ui = fixture({ commandId: () => keys.shift().promise, experiment: async () => ledger,
    dispatchAdmittedAttempt: async () => { dispatches++; } });
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: ledger.experiment.goal, current_design_revision: 2 });
  find(ui.get('experiment-detail'), node => node.textContent === 'Review Kubernetes dispatch').listeners.click(); await flush();
  ui.get('dispatch-form').fields = { target_digest: 'f'.repeat(64) }; ui.submit('dispatch-form'); await flush();
  await ui.selectProject(project('p2'));
  projectKey.resolve('project-dispatch-command'); await flush();
  assert.equal(dispatches, 0);
  assert.equal(ui.state().activeProject.id, 'p2');
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: ledger.experiment.goal, current_design_revision: 2 });
  find(ui.get('experiment-detail'), node => node.textContent === 'Review Kubernetes dispatch').listeners.click(); await flush();
  ui.get('dispatch-form').fields = { target_digest: 'e'.repeat(64) }; ui.submit('dispatch-form'); await flush();
  ui.clearContext();
  connectionKey.resolve('connection-dispatch-command'); await flush();
  assert.equal(dispatches, 0);
  assert.equal(ui.state().activeProject, null);
});

test('conclusion drafting, review, and amendment use separate revision-fenced human commands', async () => {
  const data = experimentLedger('e1');
  data.conclusions[0].lineage = [];
  data.conclusions[0].lineage_revision = data.conclusions[0].revision;
  data.conclusion_amendments = [];
  data.conclusions[1].design_revision = 2;
  data.conclusions[1].design_snapshot = data.designs[1];
  const calls = [];
  const ui = fixture({ experiment: async () => data,
    updateConclusion: async (_connection, id, body, key) => { calls.push({ type: 'update', id, body, key }); return {}; },
    reviewConclusion: async (_connection, id, body, key) => { calls.push({ type: 'review', id, body, key }); return {}; },
    amendConclusion: async (_connection, id, body, key) => { calls.push({ type: 'amend', id, body, key }); return {}; },
  });
  await ui.selectProject(project('p1')); await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
  find(ui.get('experiment-detail'), node => node.textContent === 'Revise draft conclusion').listeners.click(); await flush();
  const choice = find(ui.get('conclusion-evidence'), node => node.dataset?.conclusionArtifactId === 'artifact-legacy');
  choice.checked = true;
  ui.get('conclusion-form').fields = { statement: 'A narrowed human interpretation.' };
  ui.submit('conclusion-form'); await flush();
  find(ui.get('experiment-detail'), node => node.textContent === 'Review conclusion').listeners.click(); await flush();
  find(ui.get('experiment-detail'), node => node.textContent === 'Amend reviewed conclusion').listeners.click(); await flush();
  ui.get('conclusion-amendment-form').fields = { relation: 'retracts', reason: 'The human review narrowed the claim.' };
  ui.submit('conclusion-amendment-form'); await flush();
  assert.deepEqual(calls.map(call => call.type), ['update', 'review', 'amend']);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), { statement: 'A narrowed human interpretation.', evidence: [
    { attempt_id: 'attempt-old-failed', artifact_ids: ['artifact-legacy', 'artifact-degraded'], measurement_ids: [] },
  ], design_revision: 2, expected_revision: 1 });
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body)), { expected_revision: 1, design_revision: 2 });
  assert.deepEqual(JSON.parse(JSON.stringify(calls[2].body)), { expected_revision: 2, expected_design_revision: 1,
    relation: 'retracts', reason: 'The human review narrowed the claim.' });
  assert.match(ui.get('notice').textContent, /reviewed conclusion and evidence remain preserved/);
});

test('artifact inventory paginates without duplicate cards and disables its read control offline', async () => {
  const calls = [];
  const artifact = index => ({ id: `artifact_${index}`, project_id: 'p1', relative_path: `results/${index}.txt`,
    sha256: index.toString(16).padStart(64, '0'), size: index, status: 'quarantined', classification: 'unclassified' });
  const first = Array.from({ length: 100 }, (_, index) => artifact(index));
  const ui = fixture({ artifacts: async (_connection, projectId, page) => {
    calls.push({ projectId, page });
    return { artifacts: page.offset === 0 ? first : [first[0], artifact(100)] };
  } });
  await ui.selectProject(project('p1'));
  assert.equal(ui.get('artifacts').children.length, 100);
  assert.equal(ui.get('artifacts-more').hidden, false);
  ui.applyConnectionHealth({ name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health: 'offline' });
  assert.equal(ui.get('artifacts-more').disabled, true);
  ui.applyConnectionHealth({ name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health: 'online' });
  ui.get('artifacts-more').listeners.click(); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    { projectId: 'p1', page: { offset: 0, limit: 100 } },
    { projectId: 'p1', page: { offset: 100, limit: 100 } },
  ]);
  assert.equal(ui.get('artifacts').children.length, 101);
  assert.equal(ui.get('artifacts-more').hidden, true);
  assert.match(JSON.stringify(ui.get('artifacts')), /Artifact metadata is not evidence acceptance/);
});

test('released artifact previews require the exact current manifest and render untrusted content as text', async () => {
  const digest = 'a'.repeat(64); const calls = [];
  const ui = fixture({
    releasedArtifactManifest: async (_connection, manifestId) => ({ manifest: { id: manifestId, project_id: 'p1' } }),
    artifactPreview: async (_connection, manifestId, artifactId, maxBytes) => {
      calls.push({ manifestId, artifactId, maxBytes });
      return { artifact_id: artifactId, relative_path: '<img src=x onerror=alert(1)>.txt', sha256: digest,
        size: 22, preview_base64: 'PHNjcmlwdD5pbmVydDwvc2NyaXB0Pg==', preview_bytes: 22, truncated: false,
        status: 'quarantined', classification: 'unclassified' };
    },
  });
  await ui.selectProject(project('p1'));
  ui.get('artifact-review').listeners.click(); await flush();
  ui.get('artifact-form').fields = { manifest_id: 'manifest_1', artifact_id: 'artifact_1' };
  ui.submit('artifact-form'); await flush();
  assert.deepEqual(calls, [{ manifestId: 'manifest_1', artifactId: 'artifact_1', maxBytes: 4096 }]);
  assert.match(JSON.stringify(ui.get('artifact-review-detail')), /<img src=x onerror=alert\(1\)>\.txt/);
  assert.match(JSON.stringify(ui.get('artifact-review-detail')), /<script>inert<\/script>/);
  assert.equal(findAll(ui.get('artifact-review-detail'), node => ['img', 'script'].includes(node.tagName)).length, 0);
  assert.equal(ui.get('artifact-download').disabled, false);
  await ui.selectProject(project('p2'));
  ui.get('artifact-download').listeners.click(); await flush();
  assert.match(ui.get('notice').textContent, /Load a current released artifact preview first/);

  const mismatched = fixture({
    releasedArtifactManifest: async () => ({ manifest: { id: 'other_manifest', project_id: 'p1' } }),
    artifactPreview: async () => ({ artifact_id: 'artifact_1', relative_path: 'result.txt', sha256: digest,
      size: 1, preview_base64: 'eA==', preview_bytes: 1, truncated: false, status: 'quarantined', classification: 'unclassified' }),
  });
  await mismatched.selectProject(project('p1'));
  mismatched.get('artifact-review').listeners.click(); await flush();
  mismatched.get('artifact-form').fields = { manifest_id: 'manifest_1', artifact_id: 'artifact_1' };
  mismatched.submit('artifact-form'); await flush();
  assert.equal(mismatched.get('artifact-download').disabled, true);
  assert.match(mismatched.get('notice').textContent, /released quarantined artifact preview/);
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

test('coordinator messages, proposals, and status turns paginate independently beyond 100 records', async () => {
  const calls = { messages: [], proposals: [], turns: [] };
  const ui = fixture({
    astraConversation: async (_connection, _project, page) => {
      calls.messages.push(page); return { conversation: { revision: 3 }, messages: Array.from({ length: page.offset ? 1 : 100 }, (_, index) => ({
        id: `message-${page.offset + index}`, project_id: 'p1', sender_kind: 'human', body: `Message ${page.offset + index}`,
      })) };
    },
    astraProposals: async (_connection, _project, page) => {
      calls.proposals.push(page); return { proposals: Array.from({ length: page.offset < 200 ? 100 : 1 }, (_, index) => ({
        id: `proposal-${page.offset + index}`, project_id: 'p1', proposal_type: 'human_review_needed', body: 'Review it.', recap: 'No authority.', status: [],
        provider_disabled: true, authority_effect: 'none', execution_authorized: false, design: null, context_digest: 'e'.repeat(64),
      })) };
    },
    explainStatusTurns: async (_connection, _project, page) => {
      calls.turns.push(page); return { turns: Array.from({ length: page.offset ? 1 : 100 }, (_, index) => ({
        turn_id: `turn-${page.offset + index}`, project_id: 'p1', experiment_id: 'e1', design_revision: 1, state: 'completed',
      })) };
    },
  });
  await ui.selectProject(project('p1'));
  await ui.loadEvents();
  await ui.loadEvents();
  assert.deepEqual(calls.messages.map(page => page.offset), [0, 100]);
  assert.deepEqual(calls.proposals.map(page => page.offset), [0, 100, 200]);
  assert.deepEqual(calls.turns.map(page => page.offset), [0, 100]);
  assert.equal(findAll(ui.get('events'), node => node.dataset?.workbenchType === 'scientist-message').length, 101);
  assert.equal(findAll(ui.get('events'), node => node.dataset?.workbenchType === 'coordinator-proposal').length, 201);
  assert.equal(findAll(ui.get('events'), node => node.dataset?.workbenchType === 'status-turn').length, 101);
  assert.equal(ui.get('events-more').hidden, true);
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

test('proposal revision uses the verified immutable plan revision and does not become approval or execution', async () => {
  const calls = []; const digest = 'a'.repeat(64);
  const task = { id: 'task-1', status: 'draft', revision: 3, plan_digest: digest };
  const plan = { task_id: 'task-1', project_id: 'p1', revision: 3, plan_digest: digest, title: 'Original proposal', purpose: 'Bounded purpose', budget_cents: 50, max_duration_seconds: 120,
    execution_spec: { argv: ['python', '-m', 'fixture'], input_snapshot_digest: 'b'.repeat(64), pinned_skill_digests: [], output_paths: ['results/out.json'], profile: 'local-cpu', network_enabled: false, max_output_bytes: 4096 } };
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan,
    reviseTaskPlan: async (_connection, taskId, body, key) => { calls.push({ taskId, body, key }); return {}; } });
  await ui.selectProject(project('p1')); await flush();
  const revise = find(ui.get('tasks'), node => node.textContent === 'Revise this proposal'); assert.ok(revise);
  revise.listeners.click(); await flush();
  ui.get('task-revise-form').fields = { title: 'Revised proposal', purpose: 'Narrower bounded purpose', budget_cents: '50', max_duration_seconds: '120', argv: '["python","-m","fixture"]', input_snapshot_digest: 'b'.repeat(64), pinned_skill_digests: '[]', output_paths: '["results/out.json"]', profile: 'local-cpu', max_output_bytes: '4096' };
  ui.submit('task-revise-form'); await flush();
  assert.equal(calls.length, 1); assert.equal(calls[0].taskId, 'task-1'); assert.equal(calls[0].body.expected_revision, 3);
  assert.equal(calls[0].body.execution_spec.network_enabled, false);
  assert.match(ui.get('notice').textContent, /prior approval basis is invalidated/);
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

const harnessBinding = (id = 'binding_1', projectId = 'p1', taskId = 'task_1') => ({
  id, organization_id: 'org', project_id: projectId, task_id: taskId, operation_id: 'a'.repeat(32),
  admitted_attempt_id: 'attempt_1', attempt_revision: 3, grant_id: 'grant_1', lease_id: 'lease_1',
  worker_principal_id: 'worker_1', epoch: 7, harness_instance_id: 'harness_1', upstream_session_id: 'session_1',
  adapter_profile: 'opencode-v1.18.30-context-only', provider_disabled: true,
});

test('server-derived harness binding is displayed and context enqueue carries no routing authority inputs', async () => {
  const calls = [];
  const ui = fixture({ harnessSessionBinding: async (_connection, id) => harnessBinding(id),
    enqueueHarnessDelivery: async (_connection, body, key) => { calls.push({ body, key }); return {}; } });
  await ui.selectProject(project('p1'));
  await ui.selectThread({ ...thread('t1'), task_id: 'task_1' });
  ui.get('binding-form').fields = { harness_session_binding_id: 'binding_1' };
  ui.submit('binding-form'); await flush();
  assert.match(JSON.stringify(ui.get('binding-detail')), /Admitted attempt: attempt_1 · attempt revision 3/);
  assert.match(JSON.stringify(ui.get('binding-detail')), /Skills metadata: not loaded/);
  ui.get('context-form').fields = { context_revision: '4', intent: 'direction', body: 'Use the selected records.' };
  ui.submit('context-form'); await flush();
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({ thread_id: 't1', harness_session_binding_id: 'binding_1',
    context_revision: 4, intent: 'direction', body: 'Use the selected records.' }));
  assert.deepEqual(Object.keys(calls[0].body).sort(), ['body', 'context_revision', 'harness_session_binding_id', 'intent', 'thread_id']);
});

test('missing or stale harness bindings never become a send target across thread switches', async () => {
  const late = deferred();
  const ui = fixture({ harnessSessionBinding: async (_connection, id) => id === 'late' ? late.promise : null });
  await ui.selectProject(project('p1'));
  await ui.selectThread({ ...thread('t1'), task_id: 'task_1' });
  ui.get('binding-form').fields = { harness_session_binding_id: 'late' };
  ui.submit('binding-form'); await flush();
  await ui.selectThread({ ...thread('t2'), task_id: 'task_1' });
  late.resolve(harnessBinding('late')); await flush();
  assert.equal(ui.state().activeHarnessBinding, null);
  assert.match(ui.get('binding-detail').textContent, /No server-derived harness binding loaded/);
  ui.get('binding-form').fields = { harness_session_binding_id: 'missing' };
  ui.submit('binding-form'); await flush();
  assert.equal(ui.state().activeHarnessBinding, null);
  assert.match(ui.get('binding-detail').textContent, /missing, stale, or does not belong/);
});

test('uncertain harness context retries retain their command key and are never automatic', async () => {
  const calls = [];
  const ui = fixture({ harnessSessionBinding: async () => harnessBinding(),
    enqueueHarnessDelivery: async (_connection, body, key) => { calls.push({ body, key }); throw new Error('lost reply'); } });
  await ui.selectProject(project('p1'));
  await ui.selectThread({ ...thread('t1'), task_id: 'task_1' });
  ui.get('binding-form').fields = { harness_session_binding_id: 'binding_1' }; ui.submit('binding-form'); await flush();
  ui.get('context-form').fields = { context_revision: '2', intent: 'question', body: 'Is this context visible?' };
  ui.submit('context-form'); await flush();
  assert.equal(calls.length, 1);
  assert.match(ui.get('notice').textContent, /lost reply/);
  ui.submit('context-form'); await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].key, calls[1].key);
});

test('legacy and context delivery labels do not imply an answer, execution, or acceptance', async () => {
  const ui = fixture({ messages: async () => ({ messages: [
    { intent: 'question', sender_principal_id: 'human', body: 'Old record', created_at: '2026-01-01T00:00:00Z', delivery_legacy: true, delivery: null },
    { intent: 'direction', sender_principal_id: 'human', body: 'New context', created_at: '2026-01-01T00:00:00Z', delivery: { delivery_id: 'delivery_1', state: 'acknowledged', revision: 2, context_digest: 'a'.repeat(64) } },
  ] }) });
  await ui.selectProject(project('p1')); await ui.selectThread(thread('t1'));
  const rendered = JSON.stringify(ui.get('messages'));
  assert.match(rendered, /Legacy conversation record/);
  assert.match(rendered, /Context acknowledged by the harness — not an answer, execution, or acceptance/);
});

test('offline health marks displayed records stale and disables record and context actions', async () => {
  const ui = fixture();
  await ui.selectProject(project('p1')); await ui.selectThread(thread('t1'));
  ui.applyConnectionHealth({ name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health: 'offline' });
  assert.equal(ui.get('message-form').querySelector('button[type="submit"]').disabled, true);
  assert.equal(ui.get('context-form').querySelector('button[type="submit"]').disabled, true);
  assert.equal(ui.get('coordinator-form').querySelector('button[type="submit"]').disabled, true);
  assert.match(ui.get('connection-status').textContent, /offline — displayed data may be stale/);
  await assert.rejects(ui.command('offline', {}, async () => {}), /offline/);
  await ui.guarded(async () => {});
  assert.equal(ui.get('message-form').querySelector('button[type="submit"]').disabled, false);
  assert.equal(ui.get('coordinator-form').querySelector('button[type="submit"]').disabled, false);
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
  assert.ok(find(detail, node => node.textContent === 'Current design revision 2'));
  assert.ok(find(detail, node => /Current unattempted intervention/.test(node.textContent)));
  assert.ok(find(detail, node => node.textContent === 'No attempts recorded for this registered variant.'));
  assert.ok(find(detail, node => /Registered execution reference: task task_1/.test(node.textContent)));
  assert.ok(find(detail, node => /Human-reported observation: A human recorded/.test(node.textContent)));
  assert.ok(find(detail, node => node.textContent === 'Admitted-attempt operational state: unknown'));
  assert.ok(find(detail, node => /Operation link: 99999999999999999999999999999999/.test(node.textContent)));
  assert.ok(find(detail, node => /State unknown is unresolved; external execution and exposure remain visible/.test(node.textContent)));
  assert.ok(find(detail, node => /Latest admitted-attempt observation: unknown.*observed 2026-01-01T00:00:03Z.*cursor observation:fixture-4/.test(node.textContent)));
  assert.ok(find(detail, node => /#4: unknown · observed unknown · actor worker/.test(node.textContent)));
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
  assert.ok(find(detail, node => /Conclusion lineage revision 3: supersedes · reason: A human narrowed the interpretation after review. · actor: human/.test(node.textContent)));
  for (const field of ['Quarantine status: quarantined · classification: unclassified', 'Retention state: legacy_unplaced', 'Custody state: legacy_unplaced']) assert.ok(find(detail, node => node.textContent === field));
  for (const warning of ['degraded', 'deleted', 'legacy-unplaced']) assert.ok(find(detail, node => new RegExp(`Warning: .*${warning}`).test(node.textContent)));
  const text = JSON.stringify(detail);
  assert.match(text, /Operation provenance is operational evidence only; it does not establish scientific success\./);
  assert.match(text, /Attempt-recorded outcome is not scientific success\. Scientific validity: not assessed\./);
  assert.match(text, /Human review records an interpretation; it does not establish scientific success\./);
  assert.match(text, /Admission, operational state history, human outcome, and an operation link are records only; they do not establish scientific success\./);
  assert.equal(findAll(detail, node => node.tagName === 'a').length, 0);
});

test('experiment switches clear projection cards immediately and only render the selected ledger revision', async () => {
  const first = experimentLedger('e1'); const second = experimentLedger('e2'); const delayed = deferred();
  const ui = fixture({ experiment: async (_connection, id) => id === 'e1' ? first : delayed.promise });
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: first.experiment.goal, current_design_revision: 2 });
  assert.ok(find(ui.get('events'), node => /Experiment e1 · loaded design revision 2/.test(node.textContent)));
  const switching = ui.selectExperiment({ id: 'e2', goal: second.experiment.goal, current_design_revision: 2 });
  assert.equal(find(ui.get('events'), node => node.dataset?.workbenchType === 'conclusion-card'), null);
  assert.equal(find(ui.get('events'), node => node.dataset?.workbenchType === 'evidence-link'), null);
  delayed.resolve(second); await switching;
  assert.ok(find(ui.get('events'), node => /Experiment e2 · loaded design revision 2/.test(node.textContent)));
  assert.equal(find(ui.get('events'), node => /Experiment e1 · loaded design revision 2/.test(node.textContent)), null);
});

test('Labbook includes the selected experiment evidence and conclusion projection cards', async () => {
  const data = experimentLedger('e1');
  const ui = fixture({ experiment: async () => data, labbook: async () => ({ events: [] }) });
  await ui.selectProject(project('p1'));
  await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
  ui.get('labbook-view').listeners.click();
  assert.ok(find(ui.get('events'), node => node.dataset?.workbenchType === 'conclusion-card'));
  assert.ok(find(ui.get('events'), node => node.dataset?.workbenchType === 'evidence-link'));
  assert.ok(find(ui.get('events'), node => /Experiment e1 · loaded design revision 2/.test(node.textContent)));
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

test('admitted outcomes, late observations, migrated labels, evidence and retractions remain visible together', async () => {
  for (const [outcome, operationalState, late] of [
    ['unknown', 'preparing', false], ['unknown', 'running', true],
    ['failed', 'legacy_unverified', false], ['succeeded', 'interrupted', false],
    ['cancelled', 'interrupted', false],
  ]) {
    const data = experimentLedger();
    const attempt = data.admitted_attempts[0];
    Object.assign(attempt, { state: outcome, outcome, reason: 'Human bookkeeping only.',
      resolved_by_principal_id: 'human', operational_state: operationalState,
      operational_observed_state: operationalState === 'legacy_unverified' ? 'legacy_unverified' :
        operationalState === 'interrupted' ? 'exited' : late ? 'running' : 'not_observed',
      operational_termination_confirmed: operationalState === 'legacy_unverified' ? null : operationalState === 'interrupted',
      operational_observation_cursor: operationalState === 'legacy_unverified' ? null : 'observation:fixture-latest',
      operational_observed_at: operationalState === 'legacy_unverified' ? null : '2026-01-02T00:00:00Z',
      human_outcome: { outcome, reason: 'Human bookkeeping only.', expected_revision: 4,
        decided_at: '2026-01-02T00:00:00Z', decided_by_principal_id: 'human' } });
    attempt.state_history = attempt.state_history.slice(0, 2);
    const appendState = (state, observed, actor, reason = null) => attempt.state_history.push({
      attempt_id: attempt.id, sequence: attempt.state_history.length + 1, state,
      observed_state: observed, actor_principal_id: actor, reason, operation_id: attempt.operation_id,
      recorded_at: '2026-01-02T00:00:00Z',
    });
    if (operationalState === 'interrupted') appendState('interrupted', 'exited', 'worker');
    appendState(outcome, null, 'human', attempt.reason);
    if (late) appendState('running', 'running', 'worker');
    attempt.revision = attempt.state_history.length + 1;
    data.artifact_links.push({ attempt_id: attempt.id, artifact_id: data.artifacts[0].id });
    data.measurements.push({ id: 'measurement-admitted', attempt_id: attempt.id, name: 'admitted metric',
      value: { synthetic: true }, artifact_id: data.artifacts[0].id });
    data.conclusions[0].evidence = [{ attempt_id: attempt.id, artifact_id: null,
      measurement_id: 'measurement-admitted' }, { attempt_id: attempt.id,
      artifact_id: data.artifacts[0].id, measurement_id: null }];
    data.conclusions[0].design_revision = 2;
    data.conclusions[0].design_snapshot = data.designs[1];
    for (const amendment of [data.conclusions[0].lineage[0], data.conclusion_amendments[0]]) {
      amendment.expected_design_revision = 2;
      amendment.relation = 'retracts';
    }
    const ui = fixture({ experiment: async () => data });
    await ui.selectProject(project('p1'));
    await ui.selectExperiment({ id: 'e1', goal: data.experiment.goal, current_design_revision: 2 });
    const detail = ui.get('experiment-detail');
    const card = find(detail, node => node.dataset.experimentAdmittedAttemptId === attempt.id);
    assert.ok(card);
    assert.match(JSON.stringify(card), new RegExp(`Human-recorded outcome: ${outcome}`));
    assert.match(JSON.stringify(card), new RegExp(`Admitted-attempt operational state: ${operationalState}`));
    if (operationalState === 'interrupted') {
      assert.match(JSON.stringify(card), /Interrupted is a control operational label, not proof of workload failure/);
      assert.match(JSON.stringify(card), /inspect the attributed termination observation, original Job\/provider state and retained-output timing separately/);
      assert.doesNotMatch(JSON.stringify(card), /State interrupted is unresolved/);
    }
    assert.match(JSON.stringify(card), /Linked measurements: measurement-admitted/);
    assert.match(JSON.stringify(card), /Linked artifacts: artifact-legacy/);
    assert.match(JSON.stringify(detail), /Conclusion lineage revision 3: retracts/);
    if (operationalState === 'legacy_unverified') assert.match(JSON.stringify(card), /Legacy operational detail was not reconstructed/);
  }
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
    ledger => { ledger.admitted_attempts[0].state_history.pop(); },
    ledger => { ledger.admitted_attempts[0].operational_state = 'running'; },
    ledger => { ledger.admitted_attempts[0].operational_termination_confirmed = 1; },
    ledger => { ledger.admitted_attempts[0].human_outcome = { outcome: 'succeeded' }; },
    ledger => { ledger.operations[1].experiment_attempt_id = 'other-attempt'; },
    ledger => { ledger.conclusions[0].lineage[0].actor_principal_id = ''; },
    ledger => { ledger.conclusion_amendments = []; },
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

test('message delivery exposes session uncertainty and stale/offline reads without treating legacy chat as delivered', async () => {
  const ui = fixture({ messages: async () => ({ messages: [
    { id: 'legacy', intent: 'question', sender_principal_id: 'human', body: 'Historical chat', created_at: '2026-01-01T00:00:00Z', delivery_legacy: true },
    { id: 'delivery', intent: 'direction', sender_principal_id: 'human', body: 'Pinned context', created_at: '2026-01-01T00:00:01Z', delivery: { state: 'uncertain', harness_instance_id: 'harness-1', upstream_session_id: 'session-1', error_code: 'upstream_uncertain' } },
  ] }) });
  await ui.selectProject(project('p1')); await ui.selectThread(thread('t1'));
  assert.ok(find(ui.get('messages'), node => /Legacy conversation record/.test(node.textContent)));
  assert.ok(find(ui.get('messages'), node => /raw harness and session identifiers are not shown/.test(node.textContent)));
  assert.ok(find(ui.get('messages'), node => /will not be sent again automatically/.test(node.textContent)));
  ui.api.messages = async () => { throw new Error('offline'); };
  await assert.rejects(ui.loadMessages(undefined, undefined, true), /offline/);
  assert.match(ui.get('messages').children[0].textContent, /stale because the control plane is offline/);
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
    ui.applyConnectionHealth({ name: 'Fixture', endpoint: 'http://127.0.0.1:8765', organizationId: 'org', health: 'online' });
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

test('coordinator workbench gives each returned record a typed label without self-approval', async () => {
  const ui = fixture({
    astraConversation: async () => ({ conversation: { project_id: 'p1', revision: 2 }, messages: [
      { id: 'message-human', project_id: 'p1', sender_kind: 'human', body: '[Discuss]\nWhat should we inspect?', created_at: '2026-01-01T00:00:00Z' },
      { id: 'message-coordinator', project_id: 'p1', sender_kind: 'coordinator', body: 'Status: provider disabled', created_at: '2026-01-01T00:00:01Z' },
    ] }),
    astraProposals: async () => ({ proposals: [{ id: 'proposal-1', project_id: 'p1', conversation_revision: 2, turn_id: 'turn-1', proposal_type: 'human_review_needed',
      body: 'Ask a human to review the plan.', recap: 'No provider activity.', status: ['provider disabled'],
      provider_disabled: true, authority_effect: 'none', execution_authorized: false, design: null,
      context_digest: 'e'.repeat(64), registration: null, created_at: '2026-01-01T00:00:02Z' }] }),
    labbook: async () => ({ events: [
      { cursor: 1, event_type: 'decision_resolved', resource_revision: 2, created_at: '2026-01-01T00:00:03Z', payload: { decision: 'approved' } },
      { cursor: 2, event_type: 'execution.observation_reported', resource_revision: 2, created_at: '2026-01-01T00:00:04Z', payload: {} },
    ] }),
  });
  await ui.selectProject(project('p1'));
  const entries = ui.get('events'); const rendered = JSON.stringify(entries);
  for (const label of ['Scientist message', 'Deterministic local coordinator reply', 'Coordinator proposal', 'Human decision receipt', 'Execution milestone']) {
    assert.match(rendered, new RegExp(label));
  }
  assert.match(rendered, /Authority effect: none\. Execution is not authorized/);
  assert.equal(find(entries, node => node.tagName === 'button' && /Approve|Authorize|Dispatch/.test(node.textContent)), null);
  ui.get('labbook-view').listeners.click();
  assert.equal(find(entries, node => /Scientist message/.test(node.textContent)), null);
  ui.get('conversation-view').listeners.click();
  assert.ok(find(entries, node => /Scientist message/.test(node.textContent)));
});

test('coordinator design card requires an explicit human registration action without execution authority', async () => {
  const calls = [];
  const design = { goal: 'Compare the recorded baseline.', success_criterion: 'A human compares measurements.',
    protocol: 'Register one baseline and one intervention.', variants: [
      { kind: 'baseline', name: 'baseline', method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: {}, execution_task_id: null, execution_plan_digest: null },
      { kind: 'intervention', name: 'intervention', method_digest: 'c'.repeat(64), input_digest: 'd'.repeat(64), metadata: {}, execution_task_id: null, execution_plan_digest: null },
    ] };
  const ui = fixture({ astraConversation: async () => ({ conversation: { revision: 2 }, messages: [] }),
    astraProposals: async () => ({ proposals: [{ id: 'proposal-1', project_id: 'p1', proposal_type: 'human_review_needed',
      body: 'A human may register this candidate.', recap: 'No authority was created.', status: ['Candidate ready.'],
      provider_disabled: true, authority_effect: 'none', execution_authorized: false, design,
      context_digest: 'e'.repeat(64), registration: null, created_at: '2026-01-01T00:00:00Z' }] }),
    registerProposalDesign: async (_connection, proposalId, body, key) => {
      calls.push({ proposalId, body, key }); return { execution_authorized: false };
    } });
  await ui.selectProject(project('p1'));
  const preview = find(ui.get('events'), node => node.tagName === 'article' &&
    node.children.some(child => child.textContent === 'Complete design preview'));
  assert.ok(preview);
  const previewText = JSON.stringify(preview);
  for (const expected of ['Goal: Compare the recorded baseline.', 'Success criterion: A human compares measurements.',
    'Protocol: Register one baseline and one intervention.', 'baseline variant: baseline', 'intervention variant: intervention',
    `Method digest: ${'a'.repeat(64)}`, `Input digest: ${'d'.repeat(64)}`, 'Metadata: {}',
    'No registered execution reference.', `Registration context digest: ${'e'.repeat(64)}`]) assert.match(previewText, new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  const button = find(ui.get('events'), node => node.textContent === 'Register design — execution not authorized');
  assert.ok(button);
  button.listeners.click(); await flush();
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({ expected_context_digest: 'e'.repeat(64) }));
  assert.equal(calls[0].proposalId, 'proposal-1');
  assert.match(ui.get('notice').textContent, /Design registered\. Execution is not authorized/);
  assert.equal(find(ui.get('events'), node => /approve and run/i.test(node.textContent)), null);
});

test('coordinator composer carries only explicit current context and reports stale revisions without approval', async () => {
  const calls = [];
  const ui = fixture({ astraConversation: async () => ({ conversation: { project_id: 'p1', revision: 3 }, messages: [] }),
    tasks: async () => ({ tasks: [{ id: 'task_1', status: 'draft', revision: 4, plan_digest: 'a'.repeat(64) }] }),
    taskPlan: async () => ({ task_id: 'task_1', project_id: 'p1', revision: 4, plan_digest: 'a'.repeat(64), title: 'Selected plan', purpose: 'Explicit context',
      execution_spec: { argv: ['python'], input_snapshot_digest: 'b'.repeat(64), pinned_skill_digests: [], output_paths: [], profile: 'local-cpu', network_enabled: false, max_output_bytes: 1 } }),
    requestCoordinatorTurn: async (_connection, projectId, body, key) => { calls.push({ projectId, body, key }); throw new Error('Control request failed (409).'); } });
  await ui.selectProject(project('p1')); await flush();
  find(ui.get('tasks'), node => node.textContent === 'Include plan in coordinator context').listeners.click();
  ui.get('coordinator-form').fields = { mode: 'Draft experiment', prompt: 'Compare the registered variants.' };
  ui.submit('coordinator-form'); await flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.expected_conversation_revision, 3);
  assert.equal(calls[0].body.prompt, '[Draft experiment]\nCompare the registered variants.');
  assert.equal(JSON.stringify(calls[0].body.selected_context), JSON.stringify([{ kind: 'task', record_id: 'task_1', expected_revision: 4 }]));
  assert.match(ui.get('notice').textContent, /selected coordinator context or conversation revision is stale/);
  assert.equal(find(ui.get('events'), node => node.tagName === 'button' && /Approve/.test(node.textContent)), null);
});

test('researcher prompt, proposal registration, and plan approval remain separate from execution', async () => {
  const calls = []; let proposalVisible = false; let registered = false;
  const design = { goal: 'Compare the bounded candidates.', success_criterion: 'A human reviews the records.',
    protocol: 'Register a baseline and intervention before separate execution review.', variants: [
      { kind: 'baseline', name: 'baseline', method_digest: 'a'.repeat(64), input_digest: 'b'.repeat(64), metadata: {}, execution_task_id: null, execution_plan_digest: null },
      { kind: 'intervention', name: 'intervention', method_digest: 'c'.repeat(64), input_digest: 'd'.repeat(64), metadata: {}, execution_task_id: null, execution_plan_digest: null },
    ] };
  const proposal = () => ({ id: 'proposal_1', project_id: 'p1', proposal_type: 'human_review_needed',
    body: 'A human may register this candidate.', recap: 'No authority was created.', status: ['Candidate ready.'],
    provider_disabled: true, authority_effect: 'none', execution_authorized: false, design,
    context_digest: 'e'.repeat(64), registration: registered ? { experiment_id: 'experiment_1', design_revision: 1 } : null,
    created_at: '2026-01-01T00:00:00Z' });
  const task = { id: 'task_1', status: 'draft', revision: 4, plan_digest: 'f'.repeat(64) };
  const plan = { task_id: task.id, project_id: 'p1', revision: 4, plan_digest: task.plan_digest,
    title: 'Reviewed plan', purpose: 'A separate human plan decision.', budget_cents: 0, max_duration_seconds: 60,
    execution_spec: { argv: ['python', '-c', 'pass'], input_snapshot_digest: 'a'.repeat(64),
      pinned_skill_digests: [], output_paths: [], profile: 'local-cpu', network_enabled: false, max_output_bytes: 1 } };
  const ui = fixture({ tasks: async () => ({ tasks: [task] }), taskPlan: async () => plan,
    astraConversation: async () => ({ conversation: { project_id: 'p1', revision: 2 }, messages: [] }),
    astraProposals: async () => ({ proposals: proposalVisible ? [proposal()] : [] }),
    requestCoordinatorTurn: async (_connection, projectId, body, key) => {
      calls.push({ type: 'prompt', projectId, body, key }); proposalVisible = true; return {};
    },
    registerProposalDesign: async (_connection, proposalId, body, key) => {
      calls.push({ type: 'registration', proposalId, body, key }); registered = true; return { execution_authorized: false };
    },
    approve: async (_connection, taskId, body, key) => {
      calls.push({ type: 'approval', taskId, body, key }); return {};
    } });
  await ui.selectProject(project('p1')); await flush();
  ui.get('coordinator-form').fields = { mode: 'Draft experiment', prompt: 'Compare the candidates.' };
  ui.submit('coordinator-form'); await flush();
  const register = find(ui.get('events'), node => node.textContent === 'Register design — execution not authorized');
  assert.ok(register);
  register.listeners.click(); await flush();
  find(ui.get('tasks'), node => node.textContent === 'Approve this plan').listeners.click(); await flush();

  assert.deepEqual(calls.map(call => call.type), ['prompt', 'registration', 'approval']);
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({
    expected_conversation_revision: 2, prompt: '[Draft experiment]\nCompare the candidates.', selected_context: [],
  }));
  assert.equal(JSON.stringify(calls[1].body), JSON.stringify({ expected_context_digest: 'e'.repeat(64) }));
  assert.equal(JSON.stringify(calls[2].body), JSON.stringify({ expected_revision: 4, expected_plan_digest: 'f'.repeat(64) }));
  assert.match(JSON.stringify(ui.get('events')), /Execution remains unauthorized/);
  assert.match(ui.get('notice').textContent, /Plan approval recorded\. No execution was started/);
});
