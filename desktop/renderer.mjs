function interactionReadIsCurrent({
  requestedEpoch, currentEpoch, requestedThreadEpoch, currentThreadEpoch,
  requestedProject, currentProject, requestedThread, currentThread,
}) {
  return requestedEpoch === currentEpoch && requestedProject === currentProject &&
    (requestedThreadEpoch === undefined || requestedThreadEpoch === currentThreadEpoch) &&
    (requestedThread === undefined || requestedThread === currentThread);
}

const api = window.tessera;
const $ = id => document.getElementById(id);
let epoch = 0;
let connectionId = null;
let recoveryHeld = true;
let activeProject = null;
let activeExperiment = null;
let activeLedger = null;
let outcomeContext = null;
let closeContext = null;
const evidenceContexts = new Map();
let projectOffset = 0;
let taskOffset = 0;
let eventCursor = 0;
let threadOffset = 0;
let decisionOffset = 0;
let studyOffset = 0;
let experimentOffset = 0;
let messageOffset = 0;
let activeThread = null;
let threadEpoch = 0;
let activeStudy = null;
let studyEpoch = 0;
const pendingKeys = new Map();
const pendingDecisionResolutions = new Map();
const loading = new Set();
const readVersion = { threads: 0, messages: 0, decisions: 0, experiments: 0, experiment: 0, studies: 0, study: 0 };

function notice(message = '') { $('notice').textContent = message; }
function applyRecoveryStatus(status) {
  recoveryHeld = true;
  if (!status || typeof status.recovery_hold !== 'boolean' ||
      status.state !== (status.recovery_hold ? 'held_restore' : 'active')) {
    throw new Error('The control plane did not return a supported recovery state.');
  }
  recoveryHeld = status.recovery_hold;
  $('recovery-banner').hidden = !recoveryHeld;
  $('recovery-banner').textContent = recoveryHeld ?
    'Restored history — read-only. Changes and execution are blocked. The original control plane and its jobs may still be active; activity after the backup is unknown.' : '';
  for (const id of ['project-new', 'task-new', 'thread-new', 'study-new', 'variant-new', 'study-close', 'source-new', 'finding-new']) $(id).disabled = recoveryHeld;
  for (const id of ['project-form', 'task-form', 'thread-form', 'study-form', 'variant-form', 'outcome-form', 'study-close-form', 'source-form', 'assessment-form', 'correction-form', 'finding-form', 'finding-accept-form', 'finding-retract-form', 'finding-revise-form']) $(id).querySelector('button[type="submit"]').disabled = recoveryHeld;
  $('message-form').querySelector('button[type="submit"]').disabled = recoveryHeld;
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function resetAuthoringForms() {
  outcomeContext = closeContext = null; evidenceContexts.clear();
  for (const id of ['assessment-source', 'correction-original', 'finding-review', 'finding-sources', 'finding-variants', 'finding-retract-review', 'finding-revise-original', 'finding-revise-sources', 'finding-revise-variants']) {
    $(id).replaceChildren();
  }
  for (const id of ['project-form', 'task-form', 'thread-form', 'study-form', 'variant-form', 'outcome-form', 'study-close-form', 'source-form', 'assessment-form', 'correction-form', 'finding-form', 'finding-accept-form', 'finding-retract-form', 'finding-revise-form']) {
    const form = $(id); form.reset(); $(id.replace('form', 'dialog')).close();
    form.querySelector('button[type="submit"]').disabled = recoveryHeld;
  }
}
function resetConnectionForm() {
  const form = $('connection-form'); form.reset(); $('connection-dialog').close();
}
function clearContext() {
  epoch++;
  connectionId = null;
  recoveryHeld = true;
  $('recovery-banner').hidden = true;
  activeProject = null;
  projectOffset = taskOffset = eventCursor = threadOffset = decisionOffset = experimentOffset = studyOffset = messageOffset = 0;
  activeThread = null; threadEpoch++; activeStudy = null; activeLedger = null; studyEpoch++;
  pendingKeys.clear();
  pendingDecisionResolutions.clear();
  activeExperiment = null;
  for (const name of ['projects', 'tasks', 'events', 'threads', 'messages', 'decisions', 'experiments', 'experiment-detail', 'studies', 'study-detail']) $(name).replaceChildren();
  $('project-title').textContent = 'Start with a question.';
  $('welcome').hidden = false;
  $('workspace').hidden = true;
  $('project-new').disabled = $('refresh').disabled = true;
  $('disconnect').hidden = true;
  for (const name of ['projects-more', 'tasks-more', 'events-more', 'threads-more', 'messages-more', 'decisions-more', 'studies-more']) $(name).hidden = true;
  $('experiments-more').hidden = true;
  $('experiment-title').textContent = 'Select an experiment';
  $('study-title').textContent = 'Select a study'; $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true;
  resetAuthoringForms();
  resetConnectionForm();
  $('study-export').hidden = true;
  $('message-form').hidden = true; $('message-form').reset(); $('thread-title').textContent = 'Select a thread';
  loading.clear();
}
async function guarded(work) {
  notice();
  try { await work(); } catch (error) {
    notice(error.message || 'The request could not be completed.');
    if (api && !(await api.status())) {
      clearContext();
      $('connection-status').textContent = 'Not connected';
    }
  }
}
async function keyFor(scope, body) {
  const fingerprint = JSON.stringify([scope, body]);
  if (!pendingKeys.has(fingerprint)) pendingKeys.set(fingerprint, api.commandId());
  const pending = pendingKeys.get(fingerprint);
  try { return await pending; } catch (error) {
    if (pendingKeys.get(fingerprint) === pending) pendingKeys.delete(fingerprint);
    throw error;
  }
}
async function command(scope, body, execute, valid = () => true) {
  if (recoveryHeld) throw new Error('Restored or unverified control state is read-only.');
  const id = connectionId;
  const version = epoch;
  const fingerprint = JSON.stringify([scope, body]);
  const key = await keyFor(scope, body);
  if (version !== epoch || id !== connectionId || !valid()) throw new Error('Connection or project changed.');
  const result = await execute(id, key);
  if (version === epoch && id === connectionId && valid()) pendingKeys.delete(fingerprint);
  return result;
}
async function loadProjects(reset = false) {
  const version = epoch;
  if (reset) { projectOffset = 0; $('projects').replaceChildren(); }
  const response = await api.projects(connectionId, { offset: projectOffset, limit: 100 });
  if (version !== epoch) return;
  for (const project of response.projects) {
    const button = element('button', project.name);
    button.dataset.projectId = project.id;
    button.addEventListener('click', () => guarded(() => selectProject(project)));
    $('projects').append(button);
  }
  projectOffset += response.projects.length;
  $('projects-more').hidden = response.projects.length < 100;
}
async function selectProject(project) {
  epoch++;
  if (activeProject?.id !== project.id) pendingDecisionResolutions.clear();
  activeProject = project;
  taskOffset = eventCursor = threadOffset = decisionOffset = experimentOffset = studyOffset = messageOffset = 0;
  activeThread = null; threadEpoch++; activeStudy = null; activeLedger = null; studyEpoch++;
  const selectionUpdate = api.studySelection(connectionId, null, studyEpoch, null);
  activeExperiment = null;
  for (const name of ['threads-more', 'messages-more', 'decisions-more', 'experiments-more', 'studies-more']) $(name).hidden = true;
  $('tasks').replaceChildren(); $('events').replaceChildren(); $('threads').replaceChildren(); $('messages').replaceChildren(); $('decisions').replaceChildren(); $('experiments').replaceChildren(); $('experiment-detail').replaceChildren(); $('studies').replaceChildren(); $('study-detail').replaceChildren();
  $('message-form').hidden = true; $('message-form').reset(); $('thread-title').textContent = 'Select a thread';
  $('study-title').textContent = 'Select a study';
  $('experiment-title').textContent = 'Select an experiment';
  $('study-export').hidden = true;
  $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true;
  resetAuthoringForms();
  $('project-title').textContent = project.name;
  $('welcome').hidden = true; $('workspace').hidden = false;
  for (const button of $('projects').children) {
    if (button.dataset.projectId === project.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  await Promise.all([selectionUpdate, loadTasks(), loadEvents(), loadThreads(true), loadDecisions(true), loadExperiments(true), loadStudies(true)]);
}
async function loadTasks() {
  const version = epoch;
  const project = activeProject;
  const response = await api.tasks(connectionId, project.id, { offset: taskOffset, limit: 100 });
  if (version !== epoch) return;
  for (const task of response.tasks) {
    const card = element('article', undefined, 'card');
    card.append(element('p', 'Loading immutable execution specification…', 'muted'));
    $('tasks').append(card);
    void loadTaskPlan(card, task, version, project);
  }
  taskOffset += response.tasks.length;
  $('tasks-more').hidden = response.tasks.length < 100;
  if (!taskOffset) $('tasks').append(element('p', 'No research plans yet. Create a draft to begin.', 'muted'));
}
function validTaskPlan(plan, task, project) {
  return plan && plan.task_id === task.id && plan.project_id === project.id &&
    plan.plan_digest === task.plan_digest && plan.revision === task.revision &&
    plan.execution_spec && typeof plan.execution_spec === 'object' &&
    Array.isArray(plan.execution_spec.argv) && Array.isArray(plan.execution_spec.pinned_skill_digests) &&
    Array.isArray(plan.execution_spec.output_paths);
}
function specificationLines(plan) {
  const spec = plan.execution_spec;
  return [
    ['Command / package identity', spec.argv.join(' ')],
    ['Input snapshot', spec.input_snapshot_digest],
    ['Pinned skills', spec.pinned_skill_digests.join(', ') || 'None'],
    ['Execution destination', spec.profile],
    ['Network restriction', spec.network_enabled === false ? 'Network disabled' : 'Not approved'],
    ['Declared outputs', spec.output_paths.join(', ') || 'No output paths'],
    ['Output restriction', `At most ${spec.max_output_bytes} bytes`],
    ['Budget / duration', `${plan.budget_cents} cents / ${plan.max_duration_seconds} seconds`],
  ];
}
async function loadTaskPlan(card, task, version, project) {
  try {
    const plan = await api.taskPlan(connectionId, task.id, task.plan_digest);
    if (version !== epoch || project !== activeProject || !card.isConnected) return;
    if (!validTaskPlan(plan, task, project)) throw new Error('The control plane returned an unverified plan.');
    card.replaceChildren(element('span', `${task.status} · plan revision ${plan.revision}`, 'tag'),
      element('h3', plan.title), element('p', plan.purpose));
    for (const [label, value] of specificationLines(plan)) {
      card.append(element('p', `${label}: ${value}`));
    }
    card.append(element('p', `Verified immutable plan digest: ${plan.plan_digest}`, 'muted'));
    const button = element('button', task.status === 'approved' ? 'Plan approved' : 'Approve this plan');
    button.disabled = recoveryHeld || task.status !== 'draft';
    button.addEventListener('click', () => guarded(async () => {
      button.disabled = true;
      const body = { expected_revision: plan.revision, expected_plan_digest: plan.plan_digest };
      try {
        if (version !== epoch || project !== activeProject) return;
        await command(`approve:${task.id}`, body, (id, key) => api.approve(id, task.id, body, key));
        if (version === epoch && project === activeProject) { await selectProject(project); notice('Plan approval recorded. No execution was started.'); }
      } finally { if (button.isConnected) button.disabled = recoveryHeld || task.status !== 'draft'; }
    }));
    card.append(button);
  } catch (error) {
    if (version === epoch && project === activeProject && card.isConnected) {
      card.replaceChildren(element('p', 'Could not verify the immutable execution specification. Approval is unavailable; use Refresh to retry.', 'muted'));
    }
  }
}
async function loadThreads(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) readVersion.threads++;
  const requestVersion = readVersion.threads;
  const loadingKey = `threads:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('threads-more').disabled = true;
  if (reset) { threadOffset = 0; $('threads').replaceChildren(element('p', 'Loading threads…', 'muted')); }
  try {
    const response = await api.threads(connectionId, project.id, { offset: threadOffset, limit: 100 });
    if (!interactionReadIsCurrent({ requestedEpoch: version, currentEpoch: epoch,
      requestedProject: project, currentProject: activeProject })) return;
    if (requestVersion !== readVersion.threads) return;
    if (reset) $('threads').replaceChildren();
    for (const thread of response.threads) {
      const button = element('button', thread.title); button.dataset.threadId = thread.id;
      button.addEventListener('click', () => guarded(() => selectThread(thread))); $('threads').append(button);
    }
    threadOffset += response.threads.length; $('threads-more').hidden = response.threads.length < 100 || threadOffset > 100000;
    if (!threadOffset) $('threads').append(element('p', 'No threads yet.', 'muted'));
  } catch (error) {
    if (version === epoch && requestVersion === readVersion.threads) {
      if (reset) $('threads').replaceChildren(element('p', 'Could not load threads. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey); if (version === epoch && requestVersion === readVersion.threads) $('threads-more').disabled = false;
  }
}
async function selectThread(thread) {
  const version = epoch; const selection = ++threadEpoch;
  activeThread = thread; messageOffset = 0; $('messages').replaceChildren();
  $('messages-more').hidden = true;
  $('thread-title').textContent = thread.title; $('message-form').reset(); $('message-form').hidden = false;
  for (const button of $('threads').children) {
    if (button.dataset.threadId === thread.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  await loadMessages(version, selection, true);
}
async function loadMessages(version = epoch, selection = threadEpoch, reset = false) {
  const thread = activeThread; if (!thread) return;
  if (reset) readVersion.messages++;
  const requestVersion = readVersion.messages;
  const loadingKey = `messages:${version}:${selection}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('messages-more').disabled = true;
  if (reset) { messageOffset = 0; $('messages').replaceChildren(element('p', 'Loading messages…', 'muted')); }
  try {
    const response = await api.messages(connectionId, thread.id, { offset: messageOffset, limit: 100 });
    if (!interactionReadIsCurrent({ requestedEpoch: version, currentEpoch: epoch,
      requestedThreadEpoch: selection, currentThreadEpoch: threadEpoch,
      requestedProject: activeProject, currentProject: activeProject,
      requestedThread: thread, currentThread: activeThread })) return;
    if (requestVersion !== readVersion.messages) return;
    if (reset) $('messages').replaceChildren();
    for (const message of response.messages) {
      const row = element('article', undefined, 'message');
      row.append(element('span', `${message.intent} · ${message.sender_principal_id}`, 'tag'));
      row.append(element('p', message.body)); row.append(element('time', new Date(message.created_at).toLocaleString(), 'muted'));
      $('messages').append(row);
    }
    messageOffset += response.messages.length; $('messages-more').hidden = response.messages.length < 100 || messageOffset > 100000;
    if (!messageOffset) $('messages').append(element('p', 'No messages yet.', 'muted'));
  } catch (error) {
    if (version === epoch && requestVersion === readVersion.messages && selection === threadEpoch) {
      if (reset) $('messages').replaceChildren(element('p', 'Could not load messages. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey); if (version === epoch && requestVersion === readVersion.messages && selection === threadEpoch) $('messages-more').disabled = false;
  }
}
async function loadDecisions(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) readVersion.decisions++;
  const requestVersion = readVersion.decisions;
  const loadingKey = `decisions:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('decisions-more').disabled = true;
  if (reset) { decisionOffset = 0; $('decisions').replaceChildren(element('p', 'Loading decisions…', 'muted')); }
  try {
    const response = await api.decisions(connectionId, project.id, { offset: decisionOffset, limit: 100 });
    if (!interactionReadIsCurrent({ requestedEpoch: version, currentEpoch: epoch,
      requestedProject: project, currentProject: activeProject })) return;
    if (requestVersion !== readVersion.decisions) return;
    if (reset) $('decisions').replaceChildren();
    for (const decision of response.decisions) {
      const card = element('article', undefined, 'card');
      card.append(element('span', `${decision.kind} · pending`, 'tag'));
      card.append(element('h3', `Decision ${decision.id}`));
      card.append(element('p', `Scope: ${JSON.stringify(decision.scope)}`));
      card.append(element('p', `Reviewer: ${decision.reviewer_principal_id} · expires ${decision.expires_at}`, 'muted'));
      card.append(element('p', `Task: ${decision.task_id ?? 'Project-level'} · task basis revision ${decision.expected_task_revision ?? 'none'}`, 'muted'));
      card.append(element('p', `Decision revision ${decision.revision}`, 'muted'));
      card.append(element('p', 'Safe default: leave this request pending. Silence does not approve it.', 'muted'));
      card.append(element('p', 'A recorded decision is intent only. It does not issue a grant or start execution.', 'muted'));
      const rationale = document.createElement('textarea');
      rationale.maxLength = 8000; rationale.rows = 3;
      rationale.setAttribute('aria-label', `Rationale for decision ${decision.id}`);
      rationale.placeholder = 'Optional rationale';
      const retry = pendingDecisionResolutions.get(decision.id);
      if (retry?.projectId === project.id && retry.revision === decision.revision) {
        rationale.value = retry.body.rationale ?? '';
        rationale.disabled = true;
        rationale.placeholder = 'Retry will use the original rationale';
      }
      const actions = element('div', undefined, 'decision-actions');
      const approve = element('button', 'Approve'); approve.type = 'button';
      const reject = element('button', 'Reject'); reject.type = 'button';
      approve.disabled = reject.disabled = recoveryHeld;
      if (retry?.projectId === project.id && retry.revision === decision.revision) {
        (retry.body.decision === 'approve' ? reject : approve).disabled = true;
      }
      const resolve = choice => guarded(async () => {
        const action = choice === 'approve' ? approve : reject;
        if (action.disabled) return;
        approve.disabled = reject.disabled = true;
        const decisionProject = project;
        const existing = pendingDecisionResolutions.get(decision.id);
        const text = String(rationale.value ?? '').trim();
        const retryRecord = existing?.projectId === decisionProject.id && existing.revision === decision.revision ? existing : {
          projectId: decisionProject.id,
          revision: decision.revision,
          body: { decision_id: decision.id, expected_revision: decision.revision, decision: choice,
            ...(text ? { rationale: text } : {}) },
        };
        const body = retryRecord.body;
        pendingDecisionResolutions.set(decision.id, retryRecord);
        try {
          await command(`decision:${decision.id}`, body,
            (id, key) => api.resolveDecision(id, body, key),
            () => version === epoch && decisionProject === activeProject && decision.project_id === decisionProject.id);
          if (version === epoch && decisionProject === activeProject &&
              pendingDecisionResolutions.get(decision.id) === retryRecord) {
            pendingDecisionResolutions.delete(decision.id);
          }
          if (version === epoch && decisionProject === activeProject) {
            try {
              await loadDecisions(true);
              if (version === epoch && decisionProject === activeProject) {
                notice(`Decision ${choice === 'approve' ? 'approval' : 'rejection'} recorded. This is an intent record; no grant or execution started.`);
              }
            } catch {
              if (version === epoch && decisionProject === activeProject) {
                notice('Decision recorded. The inbox could not refresh; use Refresh to inspect the current state.');
              }
            }
          }
        } catch (error) {
          if (version !== epoch || decisionProject !== activeProject) return;
          rationale.value = body.rationale ?? '';
          rationale.disabled = true;
          if (/\(409\)/.test(error.message)) {
            throw new Error('Decision was not recorded because its revision is stale, it expired, or it is no longer pending. Refresh before taking another action.');
          }
          if (/\((401|403)\)/.test(error.message)) {
            throw new Error('This session is no longer authorized to resolve the decision. Reconnect before taking another action.');
          }
          throw new Error('Decision outcome is unknown. Refresh this project before retrying; retry will keep the original request and command identifier.');
        } finally {
          const pending = pendingDecisionResolutions.get(decision.id);
          approve.disabled = recoveryHeld || (pending?.projectId === decisionProject.id && pending.revision === decision.revision && pending.body.decision === 'reject');
          reject.disabled = recoveryHeld || (pending?.projectId === decisionProject.id && pending.revision === decision.revision && pending.body.decision === 'approve');
        }
      });
      approve.addEventListener('click', () => resolve('approve'));
      reject.addEventListener('click', () => resolve('reject'));
      actions.append(approve, reject); card.append(rationale, actions); $('decisions').append(card);
    }
    decisionOffset += response.decisions.length; $('decisions-more').hidden = response.decisions.length < 100 || decisionOffset > 100000;
    if (!decisionOffset) $('decisions').append(element('p', 'No pending decisions.', 'muted'));
  } catch (error) {
    if (version === epoch && requestVersion === readVersion.decisions) {
      if (reset) $('decisions').replaceChildren(element('p', 'Could not load decisions. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey); if (version === epoch && requestVersion === readVersion.decisions) $('decisions-more').disabled = false;
  }
}
function studyReadIsCurrent(version, project, selection, study, requestVersion) {
  return version === epoch && project === activeProject && selection === studyEpoch &&
    study === activeStudy && requestVersion === readVersion.study;
}
function ledgerCard(title, id, value) {
  const card = element('article', undefined, 'card');
  card.dataset[id] = value.id;
  card.append(element('h5', title));
  return card;
}
function ledgerText(value) { return JSON.stringify(value); }
function isDigest(value) { return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value); }
function isIdentifier(value) { return typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value); }
function isOperationId(value) { return typeof value === 'string' && /^[0-9a-f]{32}$/.test(value); }
function isPositiveInteger(value) { return Number.isSafeInteger(value) && value >= 1; }
function isText(value) { return typeof value === 'string' && value.trim().length > 0; }
function isRecordedText(value) { return value === null || isText(value); }
const artifactCustodyStates = new Set([
  'active', 'tombstoned', 'delete_intent', 'degraded', 'deleted',
  'retention_metadata_missing', 'legacy_unplaced',
]);
function sameValue(left, right) {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  const leftKeys = Object.keys(left).sort(); const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && sameValue(left[key], right[key]));
}
function uniqueRecords(records, field = 'id', valid = isIdentifier) {
  const values = records.map(record => record?.[field]);
  return values.every(valid) && new Set(values).size === values.length;
}
function sameRecordSets(left, right, field = 'id') {
  if (left.length !== right.length || !uniqueRecords(left, field) || !uniqueRecords(right, field)) return false;
  const byId = new Map(right.map(record => [record[field], record]));
  return left.every(record => sameValue(record, byId.get(record[field])));
}
function validRegisteredVariant(variant, experimentId, designRevision) {
  return variant && isIdentifier(variant.id) && variant.experiment_id === experimentId &&
    variant.design_revision === designRevision && ['baseline', 'intervention'].includes(variant.kind) &&
    isText(variant.name) && isDigest(variant.method_digest) && isDigest(variant.input_digest) &&
    variant.metadata && typeof variant.metadata === 'object' && !Array.isArray(variant.metadata) &&
    ((variant.execution_task_id === null && variant.execution_plan_digest === null) ||
      (isIdentifier(variant.execution_task_id) && isDigest(variant.execution_plan_digest)));
}
function validDesign(design, experimentId) {
  return design && design.experiment_id === experimentId && isPositiveInteger(design.revision) &&
    isRecordedText(design.goal) && isRecordedText(design.success_criterion) && isText(design.protocol) &&
    Array.isArray(design.variants) && design.variants.length === 2 && uniqueRecords(design.variants) &&
    new Set(design.variants.map(variant => variant.kind)).size === 2 &&
    design.variants.every(variant => validRegisteredVariant(variant, experimentId, design.revision));
}
const admittedAttemptStates = new Set([
  'admitted', 'preparing', 'running', 'unknown', 'interrupted', 'restart_pending',
  'succeeded', 'failed', 'cancelled',
]);
const admittedAttemptOutcomes = new Set(['succeeded', 'failed', 'cancelled', 'unknown']);
const admittedOperationalStates = new Set([
  'admitted', 'preparing', 'running', 'unknown', 'interrupted', 'restart_pending', 'legacy_unverified',
]);
const operationObservedStates = new Set(['unknown', 'running', 'exited']);
function validAdmittedAttemptHistory(history, attempt, operationalState) {
  if (!Array.isArray(history) || !history.length || history.length > 1000) return false;
  if (!history.every((entry, index) => entry && entry.attempt_id === attempt.id &&
    entry.sequence === index + 1 && admittedAttemptStates.has(entry.state) &&
    (entry.observed_state === null || operationObservedStates.has(entry.observed_state)) &&
    (entry.reason === null || isText(entry.reason)) &&
    (entry.operation_id === null || entry.operation_id === attempt.operation_id) &&
    isText(entry.recorded_at) && isIdentifier(entry.actor_principal_id)) || history[0].state !== 'admitted') return false;
  // Human resolutions share the append-only history, but do not advance the v33
  // operational projection. Migration deliberately leaves that projection unknown.
  if (operationalState === 'legacy_unverified') return true;
  const operationalHistory = attempt.operational_state === undefined ? history : history.filter(entry =>
    !(attempt.outcome !== null && entry.observed_state === null && entry.state === attempt.outcome &&
      entry.actor_principal_id === attempt.resolved_by_principal_id && entry.reason === attempt.reason));
  return operationalHistory.length > 0 && operationalHistory[operationalHistory.length - 1].state === operationalState;
}
function validAdmittedAttempt(attempt, experimentId, variants, operations) {
  if (!attempt || !isIdentifier(attempt.id) || attempt.experiment_id !== experimentId ||
      !isPositiveInteger(attempt.design_revision) || !isIdentifier(attempt.variant_id) ||
      !isIdentifier(attempt.task_id) || !isDigest(attempt.plan_digest) || !isIdentifier(attempt.grant_id) ||
      !admittedAttemptStates.has(attempt.state) || !isPositiveInteger(attempt.revision) ||
      !isText(attempt.created_at) || !isText(attempt.updated_at) || !isIdentifier(attempt.created_by_principal_id) ||
      (attempt.resolved_by_principal_id !== null && !isIdentifier(attempt.resolved_by_principal_id)) ||
      (attempt.operation_id !== null && !isOperationId(attempt.operation_id)) ||
      (attempt.package_digest !== null && !isDigest(attempt.package_digest)) ||
      (attempt.outcome !== null && !admittedAttemptOutcomes.has(attempt.outcome)) ||
      (attempt.reason !== null && !isText(attempt.reason))) return false;
  const operationalState = attempt.operational_state ?? attempt.state;
  if (!(attempt.operational_state === undefined ? admittedAttemptStates : admittedOperationalStates).has(operationalState) ||
      (attempt.operational_observed_state !== undefined &&
        !['not_observed', 'legacy_unverified', ...operationObservedStates].includes(attempt.operational_observed_state)) ||
      (attempt.operational_termination_confirmed !== undefined &&
        attempt.operational_termination_confirmed !== null && typeof attempt.operational_termination_confirmed !== 'boolean')) return false;
  const variant = variants.get(attempt.variant_id);
  if (!variant || variant.design_revision !== attempt.design_revision ||
      variant.execution_task_id !== attempt.task_id || variant.execution_plan_digest !== attempt.plan_digest ||
      !validAdmittedAttemptHistory(attempt.state_history, attempt, operationalState)) return false;
  if (attempt.outcome === null ? attempt.resolved_by_principal_id !== null || attempt.reason !== null :
    attempt.state !== attempt.outcome || attempt.resolved_by_principal_id === null ||
    (attempt.outcome !== 'succeeded' && !isText(attempt.reason))) return false;
  if (attempt.human_outcome !== undefined) {
    const decision = attempt.human_outcome;
    if (attempt.outcome === null ? decision !== null : !decision || decision.outcome !== attempt.outcome ||
      decision.reason !== attempt.reason || !isPositiveInteger(decision.expected_revision) ||
      !isText(decision.decided_at) || !isIdentifier(decision.decided_by_principal_id) ||
      decision.decided_by_principal_id !== attempt.resolved_by_principal_id) return false;
  }
  if (attempt.operation_id === null) return attempt.package_digest === null;
  const operation = operations.get(attempt.operation_id);
  return !!operation && operation.experiment_attempt_id === attempt.id &&
    operation.experiment_id === experimentId && operation.experiment_design_revision === attempt.design_revision &&
    operation.experiment_variant_id === attempt.variant_id && operation.task_id === attempt.task_id &&
    operation.grant_id === attempt.grant_id && operation.package_digest === attempt.package_digest;
}
function validConclusionAmendment(amendment, conclusion, experimentId) {
  return amendment && isIdentifier(amendment.id) && amendment.conclusion_id === conclusion.id &&
    amendment.experiment_id === experimentId && ['supersedes', 'retracts'].includes(amendment.relation) &&
    isText(amendment.reason) && isPositiveInteger(amendment.expected_revision) &&
    amendment.expected_design_revision === conclusion.design_revision &&
    isPositiveInteger(amendment.prior_lineage_revision) && isPositiveInteger(amendment.lineage_revision) &&
    amendment.lineage_revision === amendment.prior_lineage_revision + 1 && isText(amendment.created_at) &&
    isIdentifier(amendment.actor_principal_id) && amendment.scientific_validity === 'not_assessed';
}
function completeExperimentLedger(ledger, experiment, project) {
  if (!ledger || ledger.completeness !== 'complete_in_scope_goal_to_decision_chain' ||
      !ledger.experiment || ledger.experiment.id !== experiment.id ||
      ledger.experiment.project_id !== project.id || !ledger.experiment.design ||
      ledger.experiment.current_design_revision !== experiment.current_design_revision ||
      !isPositiveInteger(ledger.experiment.current_design_revision) ||
      !['designs', 'variants', 'attempts', 'admitted_attempts', 'operations', 'artifact_links', 'artifacts', 'measurements', 'conclusions', 'conclusion_amendments'].every(name => Array.isArray(ledger[name]))) return false;
  const experimentId = ledger.experiment.id;
  if (!isIdentifier(experimentId) || !isText(ledger.experiment.goal) || !isText(ledger.experiment.success_criterion) ||
      !ledger.designs.length || !uniqueRecords(ledger.designs, 'revision', isPositiveInteger) ||
      !ledger.designs.every(design => validDesign(design, experimentId))) return false;
  const designs = new Map(ledger.designs.map(design => [design.revision, design]));
  const current = designs.get(ledger.experiment.current_design_revision);
  if (!current || !isText(current.goal) || !isText(current.success_criterion) ||
      !sameValue(ledger.experiment.design, current)) return false;
  const registeredVariants = ledger.designs.flatMap(design => design.variants);
  if (!sameRecordSets(ledger.variants, registeredVariants) || !ledger.variants.every(variant =>
    validRegisteredVariant(variant, experimentId, variant.design_revision) && designs.has(variant.design_revision))) return false;
  const variants = new Map(ledger.variants.map(variant => [variant.id, variant]));
  if (!uniqueRecords(ledger.attempts) || !uniqueRecords(ledger.admitted_attempts) ||
      !uniqueRecords([...ledger.attempts, ...ledger.admitted_attempts]) ||
      !uniqueRecords(ledger.operations, 'operation_id', isOperationId) || !uniqueRecords(ledger.artifacts) ||
      !uniqueRecords(ledger.measurements) || !uniqueRecords(ledger.conclusions) ||
      !uniqueRecords(ledger.conclusion_amendments)) return false;
  const operations = new Map(ledger.operations.map(operation => [operation.operation_id, operation]));
  const attempts = new Map([...ledger.attempts, ...ledger.admitted_attempts].map(attempt => [attempt.id, attempt]));
  for (const attempt of ledger.attempts) {
    const variant = variants.get(attempt.variant_id);
    if (!variant || attempt.design_revision !== variant.design_revision ||
        !['succeeded', 'failed', 'cancelled', 'inconclusive'].includes(attempt.outcome) ||
        attempt.scientific_validity !== 'not_assessed' ||
        !['admitted_operation', 'human_reported_observation', 'legacy_operation_unverified'].includes(attempt.provenance_kind)) return false;
    if (attempt.provenance_kind === 'human_reported_observation') {
      if (attempt.operation_id !== null || attempt.execution_plan_digest !== null || attempt.package_digest !== null ||
          attempt.operation_desired_state !== null || attempt.operation_observed_state !== 'not_observed' ||
          attempt.termination_status !== 'not_applicable' || !isText(attempt.human_observation)) return false;
    } else {
      const operation = operations.get(attempt.operation_id);
      if (!isOperationId(attempt.operation_id) || !operation || operation.operation_id !== attempt.operation_id ||
          operation.project_id !== project.id || attempt.human_observation !== null) return false;
      if (attempt.provenance_kind === 'admitted_operation' &&
          (!['admitted', 'cancel_requested'].includes(attempt.operation_desired_state) || !isDigest(attempt.package_digest) ||
            !isDigest(attempt.execution_plan_digest) || attempt.execution_plan_digest !== variant.execution_plan_digest ||
            attempt.operation_observed_state !== 'exited' ||
            !['confirmed', 'observed_exited_unconfirmed'].includes(attempt.termination_status))) return false;
      if (attempt.provenance_kind === 'legacy_operation_unverified' &&
          (attempt.operation_observed_state !== 'legacy_unverified' || attempt.termination_status !== 'legacy_unknown' ||
           ![null, 'admitted', 'cancel_requested'].includes(attempt.operation_desired_state) ||
           !(attempt.execution_plan_digest === null || isDigest(attempt.execution_plan_digest)) ||
           !(attempt.package_digest === null || isDigest(attempt.package_digest)))) return false;
    }
  }
  if (!ledger.operations.every(operation => isOperationId(operation.operation_id) && operation.project_id === project.id &&
      ['admitted', 'cancel_requested'].includes(operation.desired_state) &&
      ['unknown', 'running', 'exited'].includes(operation.observed_state) &&
      [...ledger.attempts, ...ledger.admitted_attempts].some(attempt => attempt.operation_id === operation.operation_id))) return false;
  if (!ledger.admitted_attempts.every(attempt =>
    validAdmittedAttempt(attempt, experimentId, variants, operations) && attempt.scientific_validity === 'not_assessed')) return false;
  const artifacts = new Map(ledger.artifacts.map(artifact => [artifact.id, artifact]));
  if (!ledger.artifacts.every(artifact => artifact.project_id === project.id && isText(artifact.relative_path) &&
      isDigest(artifact.sha256) && artifact.status === 'quarantined' && artifact.classification === 'unclassified' &&
      artifactCustodyStates.has(artifact.retention_state) && artifactCustodyStates.has(artifact.custody_state))) return false;
  const links = new Set();
  for (const link of ledger.artifact_links) {
    const key = `${link?.attempt_id}:${link?.artifact_id}`;
    if (!attempts.has(link?.attempt_id) || !artifacts.has(link?.artifact_id) || links.has(key)) return false;
    links.add(key);
  }
  if (!ledger.artifacts.every(artifact => [...links].some(link => link.endsWith(`:${artifact.id}`)))) return false;
  const measurements = new Map(ledger.measurements.map(measurement => [measurement.id, measurement]));
  if (!ledger.measurements.every(measurement => attempts.has(measurement.attempt_id) &&
      measurement.value && typeof measurement.value === 'object' && !Array.isArray(measurement.value) &&
      (measurement.artifact_id === null || (artifacts.has(measurement.artifact_id) && links.has(`${measurement.attempt_id}:${measurement.artifact_id}`))))) return false;
  const amendmentsByConclusion = new Map(ledger.conclusions.map(conclusion => [conclusion.id, []]));
  for (const amendment of ledger.conclusion_amendments) {
    if (!amendmentsByConclusion.has(amendment?.conclusion_id)) return false;
    amendmentsByConclusion.get(amendment.conclusion_id).push(amendment);
  }
  for (const conclusion of ledger.conclusions) {
    const boundSnapshot = conclusion.design_revision !== null || conclusion.design_snapshot !== null;
    const snapshot = boundSnapshot ? designs.get(conclusion.design_revision) : null;
    if (conclusion.experiment_id !== experimentId ||
        (boundSnapshot && (!isPositiveInteger(conclusion.design_revision) || !snapshot || !sameValue(conclusion.design_snapshot, snapshot))) ||
        (!boundSnapshot && (conclusion.design_revision !== null || conclusion.design_snapshot !== null)) ||
        !isIdentifier(conclusion.id) || !isText(conclusion.statement) || !['draft', 'reviewed'].includes(conclusion.status) || conclusion.scientific_validity !== 'not_assessed' ||
        !Array.isArray(conclusion.evidence) || !conclusion.evidence.length) return false;
    const evidence = new Set();
    for (const item of conclusion.evidence) {
      const key = `${item?.attempt_id}:${item?.artifact_id ?? item?.measurement_id}`;
      if (!attempts.has(item?.attempt_id) || evidence.has(key) ||
          ((item?.artifact_id === null) === (item?.measurement_id === null)) ||
          (item.artifact_id !== null && !links.has(`${item.attempt_id}:${item.artifact_id}`)) ||
          (item.measurement_id !== null && measurements.get(item.measurement_id)?.attempt_id !== item.attempt_id)) return false;
      evidence.add(key);
    }
    const amendments = amendmentsByConclusion.get(conclusion.id);
    if (!Array.isArray(conclusion.lineage) || !sameRecordSets(conclusion.lineage, amendments) ||
        !amendments.every(amendment => validConclusionAmendment(amendment, conclusion, experimentId)) ||
        !isPositiveInteger(conclusion.lineage_revision) ||
        conclusion.lineage_revision !== (amendments.length ? amendments[amendments.length - 1].lineage_revision : conclusion.revision)) return false;
  }
  return true;
}
function recordedOrUnknown(value) { return value === null ? 'unknown (not recorded)' : value; }
function artifactWarnings(artifact) {
  const states = [artifact.retention_state, artifact.custody_state];
  const warnings = [];
  if (states.includes('degraded')) warnings.push('Warning: artifact retention or custody is degraded; verify retained bytes before relying on this evidence.');
  if (states.includes('deleted')) warnings.push('Warning: artifact retention or custody is deleted; this record does not make evidence bytes available.');
  if (states.includes('legacy_unplaced')) warnings.push('Warning: legacy-unplaced evidence has no recorded project-storage custody.');
  return warnings;
}
function renderExperimentLedger(ledger) {
  const detail = $('experiment-detail'); detail.replaceChildren();
  const experiment = ledger.experiment;
  const summary = element('article', undefined, 'card');
  summary.append(element('span', `Current design revision ${experiment.current_design_revision}`, 'tag'));
  summary.append(element('p', `Current goal: ${experiment.goal}`));
  summary.append(element('p', `Current success criterion: ${experiment.success_criterion}`));
  summary.append(element('p', 'Completeness: complete in-scope goal-to-decision chain. This projection cannot approve, dispatch, or edit conclusions.', 'muted'));
  detail.append(summary);
  const section = (title, records, render) => {
    detail.append(element('h4', `${title} (${records.length})`));
    if (!records.length) detail.append(element('p', `No ${title.toLowerCase()} recorded.`, 'muted'));
    else for (const record of records) detail.append(render(record));
  };
  const attemptsByVariant = new Map(ledger.variants.map(variant => [variant.id, []]));
  for (const attempt of [...ledger.attempts, ...ledger.admitted_attempts]) attemptsByVariant.get(attempt.variant_id).push(attempt);
  section('Historical design revisions', ledger.designs, design => {
    const card = element('article', undefined, 'card');
    card.append(element('h5', `Design revision ${design.revision}`));
    card.append(element('p', `Goal: ${recordedOrUnknown(design.goal)}`));
    card.append(element('p', `Success criterion: ${recordedOrUnknown(design.success_criterion)}`));
    card.append(element('p', `Protocol: ${design.protocol}`));
    for (const variant of design.variants) {
      card.append(element('p', `${variant.kind}: ${variant.name} · method ${variant.method_digest} · inputs ${variant.input_digest}`));
      card.append(element('p', variant.execution_task_id === null ?
        'Registered execution reference: none recorded.' :
        `Registered execution reference: task ${variant.execution_task_id} · plan digest ${variant.execution_plan_digest}`, 'muted'));
      card.append(element('p', `Registered metadata: ${ledgerText(variant.metadata)}`, 'muted'));
      if (!attemptsByVariant.get(variant.id).length) card.append(element('p', 'No attempts recorded for this registered variant.', 'muted'));
    }
    return card;
  });
  section('External operations', ledger.operations, operation => {
    const card = ledgerCard(`Operation ${operation.operation_id}`, 'experimentOperationId', { id: operation.operation_id });
    const admitted = ledger.admitted_attempts.find(attempt => attempt.operation_id === operation.operation_id);
    const legacy = ledger.attempts.find(attempt => attempt.operation_id === operation.operation_id);
    card.append(element('p', admitted ? `Linked admitted attempt: ${admitted.id} · state ${admitted.state}` :
      `Linked legacy attempt: ${legacy.id} · recorded outcome ${legacy.outcome}`));
    card.append(element('p', `Latest desired operation state: ${operation.desired_state}`));
    card.append(element('p', `Latest observed operation state: ${operation.observed_state}`));
    if (operation.desired_state === 'cancel_requested') card.append(element('p', 'Cancellation has been requested; it does not confirm termination.', 'muted'));
    if (operation.observed_state === 'unknown') card.append(element('p', 'Latest operation observation is unknown; external state and exposure remain unresolved.', 'muted'));
    card.append(element('p', 'Operation provenance is operational evidence only; it does not establish scientific success.', 'muted'));
    return card;
  });
  section('Attempts', ledger.attempts, attempt => {
    const card = ledgerCard(`Attempt ${attempt.id}`, 'experimentAttemptId', attempt);
    const outcome = ['succeeded', 'failed', 'cancelled', 'inconclusive'].includes(attempt.outcome) ? attempt.outcome : 'unrecognized';
    card.append(element('span', `Attempt-recorded outcome: ${outcome}`, 'tag'));
    card.append(element('p', `Variant: ${attempt.variant_id} · design revision ${attempt.design_revision}`));
    if (attempt.provenance_kind === 'human_reported_observation') {
      card.append(element('p', `Human-reported observation: ${attempt.human_observation}`));
      card.append(element('p', 'Operation provenance: no operation observed (human-reported status).', 'muted'));
    } else {
      card.append(element('p', `Operation state recorded with attempt: ${attempt.operation_id} · desired ${attempt.operation_desired_state ?? 'not reconstructed'} · observed ${attempt.operation_observed_state}`));
      if (attempt.provenance_kind === 'legacy_operation_unverified') {
        card.append(element('p', 'Legacy operation provenance is unverified; package, plan, and termination details were not reconstructed.', 'muted'));
      } else card.append(element('p', `Package digest: ${attempt.package_digest}`));
      card.append(element('p', attempt.termination_status === 'confirmed' ?
        'Termination status: confirmed by an attributed operational observation.' :
        `Termination status: ${attempt.termination_status} — unresolved; exposure remains visible.`, 'muted'));
    }
    if (attempt.reason) card.append(element('p', `Recorded reason: ${attempt.reason}`));
    card.append(element('p', 'Attempt-recorded outcome is not scientific success. Scientific validity: not assessed.', 'muted'));
    const links = ledger.artifact_links.filter(link => link.attempt_id === attempt.id);
    const measurements = ledger.measurements.filter(measurement => measurement.attempt_id === attempt.id);
    card.append(element('p', `Linked artifacts: ${links.map(link => link.artifact_id).join(', ') || 'none'}`));
    card.append(element('p', `Linked measurements: ${measurements.map(measurement => measurement.id).join(', ') || 'none'}`));
    for (const measurement of measurements) card.append(element('p', `${measurement.name}: ${ledgerText(measurement.value)}${measurement.artifact_id ? ` · artifact ${measurement.artifact_id}` : ''}`));
    return card;
  });
  section('Admitted execution attempts', ledger.admitted_attempts, attempt => {
    const card = ledgerCard(`Admitted attempt ${attempt.id}`, 'experimentAdmittedAttemptId', attempt);
    const operationalState = attempt.operational_state ?? attempt.state;
    card.append(element('span', `Admitted-attempt operational state: ${operationalState}`, 'tag'));
    card.append(element('p', `Variant: ${attempt.variant_id} · design revision ${attempt.design_revision}`));
    card.append(element('p', `Grant: ${attempt.grant_id} · task ${attempt.task_id} · plan digest ${attempt.plan_digest}`));
    if (attempt.operation_id === null) {
      card.append(element('p', 'Operation link: no package or external operation has been recorded.', 'muted'));
    } else {
      card.append(element('p', `Operation link: ${attempt.operation_id} · package digest ${attempt.package_digest}`));
    }
    if (['unknown', 'interrupted', 'restart_pending'].includes(operationalState)) {
      card.append(element('p', `State ${operationalState} is unresolved; external execution and exposure remain visible.`, 'muted'));
    }
    if (operationalState === 'legacy_unverified') card.append(element('p', 'Legacy operational detail was not reconstructed; the historical labels below remain unchanged.', 'muted'));
    if (attempt.operational_observed_state !== undefined) {
      card.append(element('p', `Latest admitted-attempt observation: ${attempt.operational_observed_state} · termination ${attempt.operational_termination_confirmed === true ? 'confirmed (operational evidence only)' : 'unconfirmed or not recorded'}`, 'muted'));
    }
    if (attempt.outcome !== null) {
      const decision = attempt.human_outcome;
      card.append(element('p', `Human-recorded outcome: ${attempt.outcome}${attempt.reason ? ` · reason: ${attempt.reason}` : ''}${decision ? ` · actor ${decision.decided_by_principal_id} · recorded ${decision.decided_at}` : ''}`));
    }
    card.append(element('p', `State history (${attempt.state_history.length})`, 'muted'));
    for (const entry of attempt.state_history) {
      card.append(element('p', `#${entry.sequence}: ${entry.state} · observed ${entry.observed_state ?? 'not recorded'} · actor ${entry.actor_principal_id}${entry.reason ? ` · reason: ${entry.reason}` : ''}`, 'muted'));
    }
    const links = ledger.artifact_links.filter(link => link.attempt_id === attempt.id);
    const measurements = ledger.measurements.filter(measurement => measurement.attempt_id === attempt.id);
    card.append(element('p', `Linked artifacts: ${links.map(link => link.artifact_id).join(', ') || 'none'}`));
    card.append(element('p', `Linked measurements: ${measurements.map(measurement => measurement.id).join(', ') || 'none'}`));
    card.append(element('p', 'Admission, operational state history, human outcome, and an operation link are records only; they do not establish scientific success. Scientific validity: not assessed.', 'muted'));
    return card;
  });
  section('Linked artifact records', ledger.artifacts, artifact => {
    const card = ledgerCard(`Artifact ${artifact.id}`, 'experimentArtifactId', artifact);
    card.append(element('p', `Path: ${artifact.relative_path} · digest: ${artifact.sha256}`));
    card.append(element('p', `Quarantine status: ${artifact.status} · classification: ${artifact.classification}`, 'muted'));
    card.append(element('p', `Retention state: ${artifact.retention_state}`, 'muted'));
    card.append(element('p', `Custody state: ${artifact.custody_state}`, 'muted'));
    for (const warning of artifactWarnings(artifact)) card.append(element('p', warning, 'muted'));
    return card;
  });
  section('Measurements', ledger.measurements, measurement => {
    const card = ledgerCard(`Measurement ${measurement.id}: ${measurement.name}`, 'experimentMeasurementId', measurement);
    card.append(element('p', `Attempt: ${measurement.attempt_id}`));
    card.append(element('p', `Recorded value: ${ledgerText(measurement.value)}`));
    card.append(element('p', `Artifact reference: ${measurement.artifact_id ?? 'none recorded'}`, 'muted'));
    return card;
  });
  section('Human conclusions', ledger.conclusions, conclusion => {
    const card = ledgerCard(`${conclusion.status === 'reviewed' ? 'Reviewed human conclusion' : 'Draft conclusion'} ${conclusion.id}`, 'experimentConclusionId', conclusion);
    card.append(element('p', conclusion.statement));
    card.append(element('p', conclusion.design_snapshot === null ?
      'Design snapshot: unknown (legacy conclusion was not bound to a design revision).' :
      `Design snapshot: revision ${conclusion.design_revision} · goal ${recordedOrUnknown(conclusion.design_snapshot.goal)} · criterion ${recordedOrUnknown(conclusion.design_snapshot.success_criterion)}`));
    card.append(element('p', `Evidence: ${conclusion.evidence.map(item => `${item.attempt_id} [artifacts: ${item.artifact_id ?? 'none'}; measurements: ${item.measurement_id ?? 'none'}]`).join('; ')}`));
    if (!conclusion.lineage.length) card.append(element('p', 'Conclusion lineage: no supersession or retraction recorded.', 'muted'));
    for (const amendment of conclusion.lineage) {
      card.append(element('p', `Conclusion lineage revision ${amendment.lineage_revision}: ${amendment.relation} · reason: ${amendment.reason} · actor: ${amendment.actor_principal_id} · recorded: ${amendment.created_at}`, 'muted'));
    }
    card.append(element('p', `Scientific validity: ${conclusion.scientific_validity}. Human review records an interpretation; it does not establish scientific success.`, 'muted'));
    return card;
  });
}
async function loadExperiments(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) readVersion.experiments++;
  const requestVersion = readVersion.experiments;
  const loadingKey = `experiments:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('experiments-more').disabled = true;
  if (reset) { experimentOffset = 0; $('experiments').replaceChildren(element('p', 'Loading experiments…', 'muted')); }
  try {
    const response = await api.experiments(connectionId, project.id, { offset: experimentOffset, limit: 100 });
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.experiments) return;
    if (!response || !Array.isArray(response.experiments)) throw new Error('The control plane did not return an experiment list.');
    if (reset) $('experiments').replaceChildren();
    for (const experiment of response.experiments) {
      const button = element('button', `${experiment.goal} · design revision ${experiment.current_design_revision}`);
      button.dataset.experimentId = experiment.id;
      button.addEventListener('click', () => guarded(() => selectExperiment(experiment)));
      $('experiments').append(button);
    }
    experimentOffset += response.experiments.length;
    $('experiments-more').hidden = response.experiments.length < 100 || experimentOffset > 100000;
    if (!experimentOffset) $('experiments').append(element('p', 'No goal-to-decision experiments recorded.', 'muted'));
  } catch (error) {
    if (version === epoch && project === activeProject && requestVersion === readVersion.experiments) {
      if (reset) $('experiments').replaceChildren(element('p', 'Could not load experiments. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey);
    if (version === epoch && project === activeProject && requestVersion === readVersion.experiments) $('experiments-more').disabled = false;
  }
}
async function selectExperiment(experiment) {
  const version = epoch; const project = activeProject; const requestVersion = ++readVersion.experiment;
  activeExperiment = experiment;
  $('experiment-title').textContent = `Experiment ${experiment.id}`;
  $('experiment-detail').setAttribute('aria-busy', 'true');
  $('experiment-detail').replaceChildren(element('p', 'Loading complete goal-to-decision chain…', 'muted'));
  for (const button of $('experiments').children) {
    if (button.dataset.experimentId === experiment.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  try {
    const ledger = await api.experiment(connectionId, experiment.id);
    if (version !== epoch || project !== activeProject || experiment !== activeExperiment || requestVersion !== readVersion.experiment) return;
    if (!completeExperimentLedger(ledger, experiment, project)) throw new Error('The control plane did not return a complete current experiment ledger.');
    Object.assign(experiment, ledger.experiment);
    renderExperimentLedger(ledger);
    $('experiment-detail').setAttribute('aria-busy', 'false');
  } catch (error) {
    if (version === epoch && project === activeProject && experiment === activeExperiment && requestVersion === readVersion.experiment) {
      $('experiment-detail').replaceChildren(element('p', 'Could not load a complete current experiment ledger. Partial, stale, or digest-inconsistent records are not shown; use Refresh to retry.', 'muted'));
      $('experiment-detail').setAttribute('aria-busy', 'false');
      throw error;
    }
  }
}
function renderStudyLedger(ledger) {
  const { study, variants, sources, findings, corrections, events } = ledger;
  const detail = $('study-detail'); detail.replaceChildren();
  const summary = element('article', undefined, 'card');
  summary.append(element('span', `${study.mode} · ${study.status} · revision ${study.revision}`, 'tag'));
  summary.append(element('p', `Purpose: ${study.purpose}`));
  summary.append(element('p', `Protocol: ${study.protocol}`));
  summary.append(element('p', 'Completeness: complete in-scope study ledger.', 'muted'));
  summary.append(element('p', 'Manual or imported outcomes remain assertions. No execution receipts are recorded in this ledger.', 'muted'));
  detail.append(summary);
  const appendSection = (title, records, render) => {
    detail.append(element('h4', `${title} (${records.length})`));
    if (!records.length) { detail.append(element('p', `No ${title.toLowerCase()} recorded.`, 'muted')); return; }
    for (const record of records) detail.append(render(record));
  };
  appendSection('Variants', variants, variant => {
    const card = ledgerCard(`Variant ${variant.id}: ${variant.name}`, 'variantId', variant);
    card.append(element('span', `${variant.status} · ${variant.replication_label}`, 'tag'));
    card.append(element('p', `Method: ${variant.method_digest}`), element('p', `Inputs: ${variant.input_digest}`));
    card.append(element('p', `Registered metadata: ${ledgerText(variant.metadata)}`));
    if (variant.reason) card.append(element('p', `Reason: ${variant.reason}`));
    if (variant.outcome !== null) card.append(element('p', `Recorded outcome: ${ledgerText(variant.outcome)}`));
    if (variant.outcome_provenance) card.append(element('p', `Outcome provenance: ${variant.outcome_provenance} (assertion; no execution receipt)`, 'muted'));
    card.append(element('p', `Execution receipt: ${variant.execution_receipt === null ? 'none recorded' : ledgerText(variant.execution_receipt)}`, 'muted'));
    card.append(element('p', `Independence: ${variant.independence_status}`, 'muted'));
    if (variant.status === 'registered') {
      const record = element('button', 'Record outcome');
      record.dataset.recordVariantId = variant.id;
      record.disabled = recoveryHeld || study.status !== 'open';
      record.addEventListener('click', () => guarded(async () => {
        if (recoveryHeld || ledger !== activeLedger || study.status !== 'open') {
          throw new Error('Select a current open study to record an outcome.');
        }
        outcomeContext = researchContext(ledger, variant);
        $('outcome-form').reset();
        $('outcome-form').querySelector('button[type="submit"]').disabled = false;
        $('outcome-dialog').showModal();
      }));
      card.append(record);
    }
    return card;
  });
  appendSection('Sources', sources, source => {
    const card = ledgerCard(`Source ${source.id}`, 'sourceId', source);
    card.append(element('span', `${source.verdict} · revision ${source.revision}`, 'tag'));
    card.append(element('p', `Recorded URI: ${source.uri}`));
    card.append(element('p', `Passage: ${source.quoted_passage}`));
    card.append(element('p', `Digest: ${source.content_digest}`, 'muted'));
    if (source.locator) card.append(element('p', `Locator: ${source.locator}`, 'muted'));
    if (source.assessment_reason) card.append(element('p', `Assessment: ${source.assessment_reason}`));
    if (source.supersedes_source_id) card.append(element('p', `Supersedes source: ${source.supersedes_source_id}`, 'muted'));
    if (source.superseded_at) card.append(element('p', `Superseded at: ${source.superseded_at}`, 'muted'));
    if (!source.superseded_at) {
      for (const [kind, label] of [['assessment', 'Assess source'], ['correction', 'Correct source']]) {
        const button = element('button', label); button.dataset[kind + 'SourceId'] = source.id;
        button.disabled = recoveryHeld;
        button.addEventListener('click', () => guarded(async () => {
          openEvidenceForm(kind, ledger, { source });
          $(kind === 'assessment' ? 'assessment-source' : 'correction-original').textContent =
            `Source ${source.id} · revision ${source.revision} · ${source.verdict}\n${source.uri}\n${source.quoted_passage}`;
        }));
        card.append(button);
      }
    }
    return card;
  });
  appendSection('Findings', findings, finding => {
    const card = ledgerCard(`Finding ${finding.id}`, 'findingId', finding);
    card.append(element('span', `${finding.status} · revision ${finding.revision}`, 'tag'));
    card.append(element('p', finding.statement));
    card.append(element('p', `Source IDs: ${finding.source_ids.join(', ') || 'none'}`, 'muted'));
    card.append(element('p', `Variant IDs: ${finding.variant_ids.join(', ') || 'none'}`, 'muted'));
    if (finding.supersedes_finding_id) card.append(element('p', `Supersedes finding: ${finding.supersedes_finding_id}`, 'muted'));
    if (finding.revision_kind) card.append(element('p', `Revision kind: ${finding.revision_kind}`, 'muted'));
    if (finding.revision_reason) card.append(element('p', `Revision reason: ${finding.revision_reason}`, 'muted'));
    if (finding.status === 'needs_review') card.append(element('p', 'Needs review. Consult the linked sources and ledger history.', 'muted'));
    if (['candidate', 'needs_review'].includes(finding.status)) {
      const reaffirm = finding.status === 'needs_review';
      const accept = element('button', reaffirm ? 'Review reaffirmation' : 'Review acceptance');
      accept.dataset[reaffirm ? 'reaffirmFindingId' : 'acceptFindingId'] = finding.id;
      accept.disabled = recoveryHeld || !findingIsAcceptable(ledger, finding);
      if (!findingIsAcceptable(ledger, finding)) {
        card.append(element('p', 'Acceptance needs currently supported cited sources. Confirmatory studies must also be closed and fully accounted for.', 'muted'));
      }
      accept.addEventListener('click', () => guarded(async () => {
        if (!findingIsAcceptable(ledger, finding)) throw new Error('Review the cited sources and study accounting first.');
        openEvidenceForm('finding-accept', ledger, { finding });
        const action = reaffirm ? 'Reaffirm this finding' : 'Accept this finding';
        $('finding-review-title').textContent = action;
        $('finding-accept-form').querySelector('button[type="submit"]').textContent = action;
        $('finding-reaffirmation').hidden = !reaffirm;
        $('finding-reaffirmation-reason').required = reaffirm;
        const review = $('finding-review'); review.replaceChildren(element('p', finding.statement));
        for (const id of finding.source_ids) {
          const source = ledger.sources.find(item => item.id === id);
          review.append(element('p', `${source.id} · revision ${source.revision} · ${source.verdict}`),
            element('p', source.uri), element('p', source.quoted_passage),
            element('p', source.assessment_reason || 'No assessment rationale recorded.'));
        }
      }));
      card.append(accept);
    }
    if (['candidate', 'accepted', 'needs_review'].includes(finding.status)) {
      const retract = element('button', 'Review retraction');
      retract.dataset.retractFindingId = finding.id;
      retract.disabled = recoveryHeld;
      retract.addEventListener('click', () => guarded(async () => {
        const context = openEvidenceForm('finding-retract', ledger, { finding });
        const review = $('finding-retract-review');
        review.replaceChildren(element('p', finding.statement),
          element('p', `Source IDs: ${finding.source_ids.join(', ') || 'none'}`, 'muted'));
      }));
      card.append(retract);
    }
    if (['candidate', 'accepted', 'needs_review'].includes(finding.status)) {
      const revise = element('button', 'Review revision');
      revise.dataset.reviseFindingId = finding.id;
      revise.disabled = recoveryHeld;
      revise.addEventListener('click', () => guarded(async () => {
        const context = openEvidenceForm('finding-revise', ledger, { finding });
        $('finding-revise-original').replaceChildren(
          element('p', finding.statement),
          element('p', `Source IDs: ${finding.source_ids.join(', ') || 'none'}`, 'muted'),
          element('p', `Variant IDs: ${finding.variant_ids.join(', ') || 'none'}`, 'muted'));
        for (const section of ['sources', 'variants']) {
          const container = $('finding-revise-' + section); container.replaceChildren();
          context['revision' + section[0].toUpperCase() + section.slice(1)] = ledger[section].map(record => {
            const label = element('label'); const input = element('input'); input.type = 'checkbox';
            input.dataset[section === 'sources' ? 'revisedSourceChoiceId' : 'revisedVariantChoiceId'] = record.id;
            label.append(input, element('span', section === 'sources' ?
              `${record.id} · ${record.verdict} · ${record.uri}\n${record.quoted_passage}` :
              `${record.id} · ${record.status} · ${record.name}`));
            container.append(label); return { input, record };
          });
        }
      }));
      card.append(revise);
    }
    return card;
  });
  appendSection('Corrections', corrections, correction => {
    const card = ledgerCard(`Correction ${correction.id}`, 'correctionId', correction);
    card.append(element('p', `Original source: ${correction.original_source_id}`));
    card.append(element('p', `Replacement source: ${correction.replacement_source_id}`));
    card.append(element('p', `Reason: ${correction.reason}`));
    card.append(element('p', `Recorded by ${correction.actor_principal_id} at ${correction.created_at}`, 'muted'));
    return card;
  });
  appendSection('Ledger history', events, event => {
    const card = element('article', undefined, 'card');
    card.append(element('span', `${event.event_type} · revision ${event.resource_revision}`, 'tag'));
    card.append(element('p', `Actor: ${event.actor_principal_id} · recorded ${event.created_at}`, 'muted'));
    card.append(element('p', `Record: ${ledgerText(event.payload)}`, 'muted'));
    return card;
  });
}
async function loadStudies(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) readVersion.studies++;
  const requestVersion = readVersion.studies;
  const loadingKey = `studies:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('studies-more').disabled = true;
  if (reset) { studyOffset = 0; $('studies').replaceChildren(element('p', 'Loading studies…', 'muted')); }
  try {
    const response = await api.studies(connectionId, project.id, { offset: studyOffset, limit: 100 });
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.studies) return;
    if (reset) $('studies').replaceChildren();
    for (const study of response.studies) {
      const button = element('button', `${study.purpose} · ${study.status}`);
      button.dataset.studyId = study.id;
      button.addEventListener('click', () => guarded(() => selectStudy(study)));
      $('studies').append(button);
    }
    studyOffset += response.studies.length;
    $('studies-more').hidden = response.studies.length < 100 || studyOffset > 100000;
    if (!studyOffset) $('studies').append(element('p', 'No studies recorded.', 'muted'));
  } catch (error) {
    if (version === epoch && project === activeProject && requestVersion === readVersion.studies) {
      if (reset) $('studies').replaceChildren(element('p', 'Could not load studies. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey);
    if (version === epoch && project === activeProject && requestVersion === readVersion.studies) $('studies-more').disabled = false;
  }
}
async function selectStudy(study) {
  const version = epoch; const project = activeProject; const selection = ++studyEpoch;
  const requestVersion = ++readVersion.study;
  activeStudy = study; activeLedger = null;
  // A list row's status is advisory until the complete authoritative ledger
  // arrives. Keep authoring disabled while that read is pending or failed.
  study.status = null;
  $('study-export').hidden = true; $('study-export').disabled = false;
  $('study-title').textContent = `Study ${study.id}`;
  $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true; $('variant-new').disabled = true;
  $('variant-dialog').close(); resetAuthoringForms();
  $('study-detail').replaceChildren(element('p', 'Loading complete study ledger…', 'muted'));
  for (const button of $('studies').children) {
    if (button.dataset.studyId === study.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  try {
    await api.studySelection(connectionId, study.id, selection, project.id);
    if (!studyReadIsCurrent(version, project, selection, study, requestVersion)) return;
    const ledger = await api.study(connectionId, study.id);
    if (!studyReadIsCurrent(version, project, selection, study, requestVersion)) return;
    if (ledger.completeness !== 'complete_in_scope_study_ledger' || !ledger.study ||
        ledger.study.id !== study.id || ledger.study.project_id !== project.id ||
        !['variants', 'sources', 'findings', 'corrections', 'events'].every(name => Array.isArray(ledger[name]))) {
      throw new Error('The control plane did not return a complete study ledger.');
    }
    Object.assign(study, ledger.study);
    activeLedger = ledger;
    renderStudyLedger(ledger);
    $('source-new').hidden = false; $('source-new').disabled = recoveryHeld;
    $('finding-new').hidden = ledger.study.status !== 'open'; $('finding-new').disabled = recoveryHeld;
    $('study-close').hidden = ledger.study.status !== 'open';
    $('study-close').disabled = recoveryHeld || ledger.variants.some(variant => variant.status === 'registered');
    $('study-export').hidden = false;
    $('variant-new').hidden = ledger.study.status !== 'open';
    $('variant-new').disabled = recoveryHeld;
  } catch (error) {
    if (studyReadIsCurrent(version, project, selection, study, requestVersion)) {
      $('study-detail').replaceChildren(element('p', 'Could not load a complete study ledger. Partial records are not shown as complete; use Refresh to retry.', 'muted'));
      throw error;
    }
  }
}
function researchContext(ledger, variant = null) {
  return { ledger, variant, project: activeProject, study: activeStudy, version: epoch, selection: studyEpoch };
}
function currentResearchContext(context) {
  return context && !recoveryHeld && context.version === epoch && context.project === activeProject &&
    context.study === activeStudy && context.selection === studyEpoch && context.ledger === activeLedger;
}
async function submitResearchRecord(form, context, scope, body, invoke) {
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  if (!currentResearchContext(context)) throw new Error('Select the current study first.');
  button.disabled = true;
  try {
    try {
      await command(scope, body, invoke, () => currentResearchContext(context));
    } catch (error) {
      if (currentResearchContext(context)) throw error;
      return;
    }
    if (!currentResearchContext(context)) return;
    $(form.id.replace('form', 'dialog')).close(); form.reset();
    await selectStudy(context.study);
    if (context.version === epoch && context.project === activeProject && context.study === activeStudy) {
      await loadStudies(true);
    }
  } finally {
    if (currentResearchContext(context)) button.disabled = false;
  }
}
$('outcome-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const context = outcomeContext;
    if (!currentResearchContext(context) || context.variant?.status !== 'registered') {
      throw new Error('Select a current registered variant first.');
    }
    const fields = new FormData(event.target);
    const status = fields.get('status'); const reason = fields.get('reason')?.trim();
    if (!['completed', 'failed', 'excluded', 'abandoned'].includes(status)) throw new Error('Choose an outcome status.');
    if (['excluded', 'abandoned'].includes(status) && !reason) throw new Error('Exclusion or abandonment requires a reason.');
    const raw = fields.get('outcome')?.trim();
    let outcome = null;
    if (raw) {
      try { outcome = JSON.parse(raw); } catch { throw new Error('Outcome must be a JSON object.'); }
      if (!outcome || typeof outcome !== 'object' || Array.isArray(outcome)) throw new Error('Outcome must be a JSON object.');
    }
    const body = { expected_revision: context.variant.revision, status, reason: reason || null,
      outcome, provenance: fields.get('provenance') };
    await submitResearchRecord(event.target, context, `outcome:${context.variant.id}`, body,
      (id, key) => api.recordVariant(id, context.variant.id, body, key));
  });
});
$('study-close').addEventListener('click', () => guarded(async () => {
  if (!activeLedger || recoveryHeld || activeLedger.study.status !== 'open' ||
      activeLedger.variants.some(variant => variant.status === 'registered')) {
    throw new Error('Account for every registered variant before closing the study.');
  }
  closeContext = researchContext(activeLedger);
  $('study-close-form').querySelector('button[type="submit"]').disabled = false;
  $('study-close-dialog').showModal();
}));
$('study-close-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const context = closeContext;
    if (!currentResearchContext(context) || context.ledger.study.status !== 'open' ||
        context.ledger.variants.some(variant => variant.status === 'registered')) {
      throw new Error('Account for every registered variant before closing the study.');
    }
    const body = { expected_revision: context.ledger.study.revision };
    await submitResearchRecord(event.target, context, `close-study:${context.study.id}`, body,
      (id, key) => api.closeStudy(id, context.study.id, body, key));
  });
});
function findingIsAcceptable(ledger, finding) {
  return ['candidate', 'needs_review'].includes(finding.status) && finding.source_ids.length > 0 &&
    finding.source_ids.every(id => ledger.sources.some(source => source.id === id &&
      source.verdict === 'supported' && !source.superseded_at)) &&
    (ledger.study.mode !== 'confirmatory' || (ledger.study.status === 'closed' &&
      !ledger.variants.some(variant => variant.status === 'registered')));
}
function openEvidenceForm(kind, ledger, resource = {}) {
  if (recoveryHeld || !ledger || ledger !== activeLedger) throw new Error('Select the current study first.');
  const context = { ...researchContext(ledger), ...resource };
  evidenceContexts.set(kind, context);
  const form = $(kind + '-form'); form.reset();
  form.querySelector('button[type="submit"]').disabled = false;
  $(kind + '-dialog').showModal();
  return context;
}
$('source-new').addEventListener('click', () => guarded(async () => openEvidenceForm('source', activeLedger)));
$('finding-new').addEventListener('click', () => guarded(async () => {
  if (activeLedger?.study.status !== 'open') throw new Error('Select an open study to propose a finding.');
  const context = openEvidenceForm('finding', activeLedger);
  for (const section of ['sources', 'variants']) {
    const container = $('finding-' + section); container.replaceChildren();
    context[section] = activeLedger[section].map(record => {
      const label = element('label'); const input = element('input'); input.type = 'checkbox';
      input.dataset[section === 'sources' ? 'sourceChoiceId' : 'variantChoiceId'] = record.id;
      label.append(input, element('span', section === 'sources' ?
        `${record.id} · ${record.verdict} · ${record.uri}` : `${record.id} · ${record.status} · ${record.name}`));
      container.append(label); return { input, record };
    });
  }
}));
function sourceFields(form) {
  const fields = new FormData(form);
  return { uri: fields.get('uri'), quoted_passage: fields.get('quoted_passage'),
    content_digest: fields.get('content_digest'), locator: fields.get('locator')?.trim() || null };
}
for (const kind of ['source', 'assessment', 'correction', 'finding', 'finding-accept', 'finding-retract', 'finding-revise']) {
  $(kind + '-form').addEventListener('submit', event => {
    event.preventDefault();
    guarded(async () => {
      const context = evidenceContexts.get(kind);
      if (!currentResearchContext(context)) throw new Error('Select the current study first.');
      const fields = new FormData(event.target);
      let body; let target; let method;
      if (kind === 'source') {
        body = sourceFields(event.target); target = context.study.id; method = 'createSource';
      } else if (kind === 'assessment') {
        body = { expected_revision: context.source.revision, verdict: fields.get('verdict'), reason: fields.get('reason')?.trim() || null };
        target = context.source.id; method = 'assessSource';
      } else if (kind === 'correction') {
        const reason = fields.get('reason')?.trim();
        if (!reason) throw new Error('A correction requires a reason.');
        body = { ...sourceFields(event.target), expected_revision: context.source.revision, reason };
        target = context.source.id; method = 'correctSource';
      } else if (kind === 'finding') {
        if (context.ledger.study.status !== 'open') throw new Error('Select an open study to propose a finding.');
        body = { statement: fields.get('statement'),
          source_ids: context.sources.filter(choice => choice.input.checked).map(choice => choice.record.id),
          variant_ids: context.variants.filter(choice => choice.input.checked).map(choice => choice.record.id) };
        if (![body.source_ids, body.variant_ids].every(ids => ids.length >= 1 && ids.length <= 200)) {
          throw new Error('Select between 1 and 200 sources and variants.');
        }
        target = context.study.id; method = 'createFinding';
      } else if (kind === 'finding-accept') {
        if (!findingIsAcceptable(context.ledger, context.finding)) throw new Error('Review the cited sources and study accounting first.');
        body = { expected_revision: context.finding.revision }; target = context.finding.id; method = 'acceptFinding';
        if (context.finding.status === 'needs_review') {
          const reason = fields.get('reason')?.trim();
          if (!reason) throw new Error('Reaffirmation requires a reason.');
          body.reason = reason; method = 'reaffirmFinding';
        }
      } else if (kind === 'finding-retract') {
        const reason = fields.get('reason')?.trim();
        if (!reason) throw new Error('Retraction requires a reason.');
        body = { expected_revision: context.finding.revision, reason };
        target = context.finding.id; method = 'retractFinding';
      } else {
        const kindValue = fields.get('kind');
        const statement = fields.get('statement')?.trim();
        const reason = fields.get('reason')?.trim();
        if (!['narrow', 'supersede'].includes(kindValue)) throw new Error('Choose a revision kind.');
        if (!statement) throw new Error('A revised finding requires a statement.');
        if (!reason) throw new Error('Revision requires a reason.');
        const sourceIds = context.revisionSources.filter(choice => choice.input.checked).map(choice => choice.record.id);
        const variantIds = context.revisionVariants.filter(choice => choice.input.checked).map(choice => choice.record.id);
        if (![sourceIds, variantIds].every(ids => ids.length >= 1 && ids.length <= 200)) {
          throw new Error('Select between 1 and 200 sources and variants for the revised finding.');
        }
        body = { expected_revision: context.finding.revision, kind: kindValue, reason, statement,
          source_ids: sourceIds, variant_ids: variantIds };
        target = context.finding.id; method = 'reviseFinding';
      }
      await submitResearchRecord(event.target, context, `${kind}:${target}`, body,
        (id, key) => api[method](id, target, body, key));
    });
  });
}
$('study-export').addEventListener('click', () => guarded(async () => {
  const version = epoch; const study = activeStudy; const selection = studyEpoch;
  const button = $('study-export');
  if (button.disabled) return;
  if (!study) throw new Error('Select a study first.');
  const current = () => version === epoch && study === activeStudy && selection === studyEpoch;
  button.disabled = true;
  try {
    const result = await api.saveStudyExport(connectionId, study.id, selection);
    if (!current() || result.status === 'stale') return;
    if (result.status === 'cancelled') { notice('Study ledger export cancelled.'); return; }
    if (result.status !== 'saved' || typeof result.basename !== 'string') throw new Error('Study ledger export failed.');
    notice(`Saved study ledger JSON: ${result.basename}`);
  } catch (error) {
    if (current()) throw error;
  } finally { if (current()) button.disabled = false; }
}));
const eventLabels = {
  'project.created': 'Project created', 'task.created': 'Research plan drafted',
  'task.approved': 'Plan approved by a human', 'task.plan_revised': 'Plan revised; new review required',
};
function cancellationEventLabel(event) {
  const state = event?.payload?.cancellation?.state;
  if (event.event_type === 'execution.grant_cancel_requested') {
    return 'Cancellation requested — termination remains unresolved';
  }
  if (event.event_type === 'execution.observation_reported' && state === 'confirmed_termination') {
    return 'Cancellation termination confirmed — operational observation only';
  }
  if (event.event_type === 'execution.observation_reported' && state === 'unresolved') {
    return 'Cancellation unresolved or unknown — exposure retained';
  }
  return eventLabels[event.event_type] ?? event.event_type;
}
async function loadEvents() {
  const version = epoch;
  const response = await api.labbook(connectionId, activeProject.id, eventCursor);
  if (version !== epoch) return;
  for (const event of response.events) {
    const entry = element('li');
    entry.append(element('span', cancellationEventLabel(event)));
    entry.append(element('time', `${new Date(event.created_at).toLocaleString()} · revision ${event.resource_revision}`));
    $('events').append(entry);
    eventCursor = event.cursor;
  }
  $('events-more').hidden = response.events.length < 200;
}
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}
$('connection-open').addEventListener('click', () => $('connection-dialog').showModal());
$('project-new').addEventListener('click', () => $('project-dialog').showModal());
$('task-new').addEventListener('click', () => $('task-dialog').showModal());
$('thread-new').addEventListener('click', () => $('thread-dialog').showModal());
$('study-new').addEventListener('click', () => $('study-dialog').showModal());
$('variant-new').addEventListener('click', () => $('variant-dialog').showModal());
$('disconnect').addEventListener('click', () => guarded(async () => {
  clearContext(); await api.disconnect(); $('connection-status').textContent = 'Not connected';
}));
$('connection-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const fields = new FormData(event.target);
    const settings = { name: fields.get('name'), endpoint: fields.get('endpoint') };
    clearContext();
    const version = epoch;
    const state = await api.connect(settings);
    if (version !== epoch) return;
    if (!state) { $('connection-status').textContent = 'Not connected'; return; }
    connectionId = state.id;
    $('connection-status').textContent = `${state.name}\n${state.endpoint}\n${state.organizationId}`;
    $('disconnect').hidden = false;
    let recovery;
    try { recovery = await api.recovery(connectionId); } catch (error) {
      if (version !== epoch) return;
      throw error;
    }
    if (version !== epoch) return;
    applyRecoveryStatus(recovery);
    $('refresh').disabled = false;
    await loadProjects(true);
  });
});
$('project-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const version = epoch;
    const body = { name: new FormData(event.target).get('name') };
    const project = await command('project', body, (id, key) => api.createProject(id, body, key));
    if (version !== epoch) return;
    $('project-dialog').close(); event.target.reset();
    await loadProjects(true); await selectProject(project);
  });
});
$('task-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const version = epoch;
    const fields = new FormData(event.target);
    const body = { title: fields.get('title'), purpose: fields.get('purpose'),
      budget_cents: Math.round(Number(fields.get('budget')) * 100),
      max_duration_seconds: Number(fields.get('duration')) };
    const projectId = activeProject.id;
    await command(`task:${projectId}`, body, (id, key) => api.createTask(id, projectId, body, key));
    if (version !== epoch) return;
    $('task-dialog').close(); event.target.reset(); await selectProject(activeProject);
  });
});
$('thread-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const version = epoch; const body = { project_id: activeProject.id, title: new FormData(event.target).get('title') };
      const thread = await command(`thread:${activeProject.id}`, body, (id, key) => api.createThread(id, body, key));
      if (version !== epoch) return;
      $('thread-dialog').close(); event.target.reset(); await loadThreads(true);
      if (version !== epoch) return;
      await selectThread(thread);
    } finally { button.disabled = recoveryHeld; }
  });
});
$('study-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]');
    if (button.disabled) return;
    const version = epoch; const project = activeProject;
    if (!project) throw new Error('Select a project first.');
    const fields = new FormData(event.target);
    const body = { purpose: fields.get('purpose'), protocol: fields.get('protocol'), mode: fields.get('mode') };
    button.disabled = true;
    try {
      let study;
      try {
        study = await command(`study:${project.id}`, body,
          (id, key) => api.createStudy(id, project.id, body, key),
          () => version === epoch && project === activeProject);
      } catch (error) {
        if (version !== epoch || project !== activeProject) return;
        throw error;
      }
      if (version !== epoch || project !== activeProject) return;
      $('study-dialog').close(); event.target.reset();
      await loadStudies(true);
      if (version === epoch && project === activeProject) await selectStudy(study);
    } finally { if (version === epoch && project === activeProject) button.disabled = recoveryHeld; }
  });
});
$('variant-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]');
    if (button.disabled) return;
    const version = epoch; const project = activeProject; const study = activeStudy; const selection = studyEpoch;
    const fields = new FormData(event.target);
    let metadata;
    try { metadata = JSON.parse(fields.get('metadata')); } catch { throw new Error('Metadata must be valid JSON.'); }
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('Metadata must be a JSON object.');
    const body = { name: fields.get('name'), method_digest: fields.get('method_digest'), input_digest: fields.get('input_digest'), metadata, replication_label: fields.get('replication_label') };
    button.disabled = true;
    try {
      if (!study || study.status !== 'open') throw new Error('Select an open study first.');
      try {
        await command(`variant:${study.id}`, body,
          (id, key) => api.registerVariant(id, study.id, body, key),
          () => version === epoch && project === activeProject && study === activeStudy && selection === studyEpoch);
      } catch (error) {
        if (version !== epoch || project !== activeProject || study !== activeStudy || selection !== studyEpoch) return;
        throw error;
      }
      if (version !== epoch || project !== activeProject || study !== activeStudy || selection !== studyEpoch) return;
      $('variant-dialog').close(); event.target.reset(); await selectStudy(study);
    } finally {
      if (version === epoch && project === activeProject && study === activeStudy && selection === studyEpoch) {
        button.disabled = recoveryHeld;
      }
    }
  });
});
$('message-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const version = epoch; const selection = threadEpoch; const thread = activeThread; const fields = new FormData(event.target);
      const body = { thread_id: thread.id, intent: fields.get('intent'), body: fields.get('body') };
      await command(`message:${thread.id}`, body, (id, key) => api.sendMessage(id, body, key),
        () => version === epoch && thread === activeThread && selection === threadEpoch);
      if (version === epoch && thread === activeThread && selection === threadEpoch) { event.target.reset(); await loadMessages(version, threadEpoch, true); }
    } finally { button.disabled = recoveryHeld; }
  });
});
$('refresh').addEventListener('click', () => guarded(async () => {
  await loadProjects(true); if (activeProject) await selectProject(activeProject);
}));
$('projects-more').addEventListener('click', () => guarded(() => loadProjects()));
$('tasks-more').addEventListener('click', () => guarded(() => loadTasks()));
$('events-more').addEventListener('click', () => guarded(() => loadEvents()));
$('threads-more').addEventListener('click', () => guarded(() => loadThreads()));
$('messages-more').addEventListener('click', () => guarded(() => loadMessages()));
$('decisions-more').addEventListener('click', () => guarded(() => loadDecisions()));
$('experiments-more').addEventListener('click', () => guarded(() => loadExperiments()));
$('studies-more').addEventListener('click', () => guarded(() => loadStudies()));
if (!api) {
  $('connection-open').disabled = true;
  notice('Open this workspace in the Tessera desktop application.');
}
