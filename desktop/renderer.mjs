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
let connectionOnline = false;
let connectionState = null;
let activeProject = null;
let activeExperiment = null;
let activeExperimentLedger = null;
let activeLedger = null;
let outcomeContext = null;
let closeContext = null;
const evidenceContexts = new Map();
let projectOffset = 0;
let taskOffset = 0;
let workerOffset = 0;
let eventCursor = 0;
let workbenchView = 'conversation';
let workbenchEntries = [];
let coordinatorConversationRevision = 0;
const selectedCoordinatorContext = [];
let coordinatorMessageOffset = 0;
let proposalOffset = 0;
let statusTurnOffset = 0;
let coordinatorMessagesMore = true;
let proposalsMore = true;
let statusTurnsMore = true;
let threadOffset = 0;
let decisionOffset = 0;
let studyOffset = 0;
let experimentOffset = 0;
let remoteWorkflowOffset = 0;
let messageOffset = 0;
let activeThread = null;
let threadEpoch = 0;
let activeHarnessBinding = null;
let activeStudy = null;
let studyEpoch = 0;
const pendingKeys = new Map();
const pendingDecisionResolutions = new Map();
const loading = new Set();
const readVersion = { workers: 0, threads: 0, messages: 0, binding: 0, decisions: 0, experiments: 0, experiment: 0, remoteWorkflows: 0, studies: 0, study: 0, workbench: 0 };

function notice(message = '') { $('notice').textContent = message; }
function canMutate() { return !recoveryHeld && connectionOnline; }
function applyConnectionHealth(state) {
  if (state) connectionState = state;
  connectionOnline = state?.health === 'online';
  const unavailable = !canMutate();
  for (const id of ['project-new', 'task-new', 'thread-new', 'study-new', 'variant-new', 'study-close', 'source-new', 'finding-new']) $(id).disabled = unavailable;
  for (const id of ['project-form', 'task-form', 'thread-form', 'study-form', 'variant-form', 'outcome-form', 'study-close-form', 'source-form', 'assessment-form', 'correction-form', 'finding-form', 'finding-accept-form', 'finding-retract-form', 'finding-revise-form', 'message-form', 'context-form', 'coordinator-form']) {
    $(id).querySelector('button[type="submit"]').disabled = unavailable;
  }
  for (const button of document.querySelectorAll('#tasks button, #decisions button, #events button, #message-form button, #context-form button, #remote-workflows button')) button.disabled = unavailable;
  // Study-detail controls have additional source, accounting, and revision fences.
  // Going offline disables them all; coming back online must not undo those fences.
  if (unavailable) {
    for (const button of document.querySelectorAll('#study-detail button')) button.disabled = true;
  }
  // Connection health must not loosen the authoritative study-ledger fences that
  // were applied after the current complete study read.
  if (activeLedger?.study) {
    const open = activeLedger.study.status === 'open';
    $('variant-new').disabled = unavailable || !open;
    $('study-close').disabled = unavailable || !open || activeLedger.variants.some(variant => variant.status === 'registered');
    $('source-new').disabled = unavailable;
    $('finding-new').disabled = unavailable || !open;
  }
  if (connectionId || state?.name) {
    const suffix = connectionOnline ? 'online' : 'offline — displayed data may be stale; consequential actions are disabled';
    $('connection-status').textContent = `${state.name}\n${state.endpoint}\n${state.organizationId}\n${suffix}`;
  }
  $('coordinator-health').textContent = connectionOnline ?
    'Coordinator status: provider disabled. Proposals require separate human review; no approval, grant, dispatch, or execution is authorized.' :
    'Coordinator status: offline — displayed context may be stale; consequential actions are disabled.';
}
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
  applyConnectionHealth(connectionState ?? { health: recoveryHeld ? 'offline' : 'online' });
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function clearHarnessBinding() {
  activeHarnessBinding = null;
  readVersion.binding++;
  $('binding-form').reset(); $('context-form').reset();
  $('binding-detail').textContent = 'No server-derived harness binding loaded.';
}
function validHarnessBinding(binding, requestedId, project, thread) {
  return binding && binding.id === requestedId && binding.project_id === project.id &&
    typeof binding.task_id === 'string' && typeof binding.admitted_attempt_id === 'string' &&
    Number.isSafeInteger(binding.attempt_revision) && binding.attempt_revision >= 1 &&
    binding.adapter_profile === 'opencode-v1.18.30-context-only' && binding.provider_disabled === true &&
    (thread.task_id === null || thread.task_id === undefined || thread.task_id === binding.task_id);
}
function renderHarnessBinding(binding) {
  $('binding-detail').replaceChildren(
    element('p', `Server-derived binding: ${binding.id}`),
    element('p', `Admitted attempt: ${binding.admitted_attempt_id} · attempt revision ${binding.attempt_revision}`),
    element('p', `Context-only profile: ${binding.adapter_profile} · provider disabled`, 'muted'),
    element('p', 'Skills metadata: not loaded. This binding does not claim any skill was loaded.', 'muted'),
    element('p', 'The server derives worker, epoch, harness instance, and session routing. They are not desktop inputs.', 'muted'),
  );
}
function contextStateLabel(state) {
  return {
    queued: 'Context queued for harness delivery',
    delivered: 'Context delivered to the harness worker — not an answer, execution, or acceptance',
    acknowledged: 'Context acknowledged by the harness — not an answer, execution, or acceptance',
    uncertain: 'Context delivery is uncertain — it will not be sent again automatically',
    failed: 'Context delivery failed',
    cancelled: 'Context delivery was cancelled in control — this does not terminate a harness session',
  }[state] ?? 'Context delivery state is unrecognized';
}
function resetAuthoringForms() {
  outcomeContext = closeContext = null; evidenceContexts.clear();
  for (const id of ['assessment-source', 'correction-original', 'finding-review', 'finding-sources', 'finding-variants', 'finding-retract-review', 'finding-revise-original', 'finding-revise-sources', 'finding-revise-variants']) {
    $(id).replaceChildren();
  }
  for (const id of ['project-form', 'task-form', 'thread-form', 'study-form', 'variant-form', 'outcome-form', 'study-close-form', 'source-form', 'assessment-form', 'correction-form', 'finding-form', 'finding-accept-form', 'finding-retract-form', 'finding-revise-form']) {
    const form = $(id); form.reset(); $(id.replace('form', 'dialog')).close();
    form.querySelector('button[type="submit"]').disabled = !canMutate();
  }
}
function resetConnectionForm() {
  const form = $('connection-form'); form.reset(); $('connection-dialog').close();
}
function clearContext() {
  epoch++;
  connectionId = null;
  recoveryHeld = true;
  connectionOnline = false;
  connectionState = null;
  $('recovery-banner').hidden = true;
  activeProject = null;
  projectOffset = taskOffset = workerOffset = eventCursor = threadOffset = decisionOffset = experimentOffset = remoteWorkflowOffset = studyOffset = messageOffset = 0;
  coordinatorMessageOffset = proposalOffset = statusTurnOffset = 0;
  coordinatorMessagesMore = proposalsMore = statusTurnsMore = true;
  activeThread = null; threadEpoch++; activeHarnessBinding = null; readVersion.binding++; activeStudy = null; activeLedger = null; studyEpoch++;
  pendingKeys.clear();
  pendingDecisionResolutions.clear();
  activeExperiment = null; activeExperimentLedger = null;
  workbenchEntries = []; coordinatorConversationRevision = 0; selectedCoordinatorContext.splice(0); readVersion.workbench++;
  for (const name of ['projects', 'tasks', 'workers', 'events', 'threads', 'messages', 'decisions', 'experiments', 'experiment-detail', 'remote-workflows', 'studies', 'study-detail']) $(name).replaceChildren();
  $('project-title').textContent = 'Start with a question.';
  $('welcome').hidden = false;
  $('workspace').hidden = true;
  $('project-new').disabled = $('refresh').disabled = true;
  $('disconnect').hidden = true;
  for (const name of ['projects-more', 'tasks-more', 'workers-more', 'events-more', 'threads-more', 'messages-more', 'decisions-more', 'remote-workflows-more', 'studies-more']) $(name).hidden = true;
  $('experiments-more').hidden = true;
  $('experiment-title').textContent = 'Select an experiment';
  $('study-title').textContent = 'Select a study'; $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true;
  resetAuthoringForms();
  resetConnectionForm();
  $('study-export').hidden = true;
  $('message-form').hidden = true; $('message-form').reset(); $('harness-context').hidden = true; $('binding-form').reset(); $('context-form').reset(); $('binding-detail').textContent = 'No server-derived harness binding loaded.'; $('thread-title').textContent = 'Select a thread';
  $('coordinator-form').reset(); renderCoordinatorContext();
  loading.clear();
}
async function guarded(work) {
  notice();
  try {
    await work();
    const state = api && await api.status();
    if (state) applyConnectionHealth(state);
  } catch (error) {
    notice(error.message || 'The request could not be completed.');
    const state = api && await api.status();
    if (!state) {
      clearContext();
      $('connection-status').textContent = 'Not connected';
    } else {
      applyConnectionHealth(state);
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
  if (!connectionOnline) throw new Error('Control plane is offline; displayed context may be stale. Reconnect or Refresh before a new command.');
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
  taskOffset = workerOffset = eventCursor = threadOffset = decisionOffset = experimentOffset = remoteWorkflowOffset = studyOffset = messageOffset = 0;
  coordinatorMessageOffset = proposalOffset = statusTurnOffset = 0;
  coordinatorMessagesMore = proposalsMore = statusTurnsMore = true;
  activeThread = null; threadEpoch++; activeHarnessBinding = null; readVersion.binding++; activeStudy = null; activeLedger = null; studyEpoch++;
  const selectionUpdate = api.studySelection(connectionId, null, studyEpoch, null);
  activeExperiment = null; activeExperimentLedger = null;
  workbenchEntries = []; coordinatorConversationRevision = 0; selectedCoordinatorContext.splice(0); readVersion.workbench++;
  for (const name of ['workers-more', 'threads-more', 'messages-more', 'decisions-more', 'experiments-more', 'remote-workflows-more', 'studies-more']) $(name).hidden = true;
  $('tasks').replaceChildren(); $('workers').replaceChildren(); $('events').replaceChildren(); $('threads').replaceChildren(); $('messages').replaceChildren(); $('decisions').replaceChildren(); $('experiments').replaceChildren(); $('experiment-detail').replaceChildren(); $('remote-workflows').replaceChildren(); $('studies').replaceChildren(); $('study-detail').replaceChildren();
  $('message-form').hidden = true; $('message-form').reset(); $('harness-context').hidden = true; clearHarnessBinding(); $('thread-title').textContent = 'Select a thread';
  $('study-title').textContent = 'Select a study';
  $('experiment-title').textContent = 'Select an experiment';
  $('study-export').hidden = true;
  $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true;
  resetAuthoringForms();
  $('coordinator-form').reset(); renderCoordinatorContext();
  $('project-title').textContent = project.name;
  $('welcome').hidden = true; $('workspace').hidden = false;
  for (const button of $('projects').children) {
    if (button.dataset.projectId === project.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  await Promise.all([selectionUpdate, loadTasks(), loadWorkers(true), loadWorkbench(true), loadThreads(true), loadDecisions(true), loadExperiments(true), loadRemoteWorkflows(true), loadStudies(true)]);
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
function validWorkerDiscovery(worker, project) {
  return worker && worker.project_id === project.id && isIdentifier(worker.id) &&
    worker.approved_metadata && typeof worker.approved_metadata === 'object' &&
    typeof worker.approved_metadata.build_identifier === 'string' &&
    typeof worker.approved_metadata.build_digest === 'string' &&
    typeof worker.approved_metadata.region === 'string' &&
    worker.lifecycle && typeof worker.lifecycle === 'object' &&
    ['ready', 'draining', 'disconnected', 'quarantined', 'retired'].includes(worker.lifecycle.state) &&
    Number.isSafeInteger(worker.lifecycle.revision) && worker.lifecycle.revision >= 1 &&
    ['active', 'revoked'].includes(worker.lifecycle.credential_status) &&
    worker.freshness && ['fresh', 'stale', 'unknown'].includes(worker.freshness.status) &&
    worker.capabilities?.status === 'not_probed' && worker.capabilities.probed === false &&
    worker.trust_zone?.status === 'declared_unverified' && typeof worker.trust_zone.trust_class === 'string' &&
    worker.lease_admission?.registration_is_not_scheduling === true &&
    ['requires_separate_active_grant', 'fenced'].includes(worker.lease_admission.status) &&
    typeof worker.lease_admission.fence_reason === 'string';
}
function renderWorkerDiscovery(worker) {
  const card = element('article', undefined, 'card');
  card.append(element('span', `Lifecycle: ${worker.lifecycle.state} · revision ${worker.lifecycle.revision}`, 'tag'));
  card.append(element('h3', `Worker ${worker.id}`));
  card.append(element('p', `Approved build: ${worker.approved_metadata.build_identifier}`));
  card.append(element('p', `Approved build digest: ${worker.approved_metadata.build_digest}`, 'muted'));
  card.append(element('p', `Region: ${worker.approved_metadata.region}`));
  card.append(element('p', `Freshness: ${worker.freshness.status} · last seen ${worker.freshness.last_seen_at ?? 'not recorded'}`));
  card.append(element('p', `Worker credential: ${worker.lifecycle.credential_status}`));
  card.append(element('p', 'Capabilities: not probed. No capability attestation is implied.'));
  card.append(element('p', `Trust zone: ${worker.trust_zone.trust_class} (${worker.trust_zone.status})`));
  const admission = worker.lease_admission.status === 'fenced' ?
    `New lease admission is fenced (${worker.lease_admission.fence_reason}).` :
    'New lease admission requires a separate active grant.';
  card.append(element('p', admission, 'muted'));
  card.append(element('p', 'Registration is metadata only; it does not schedule, dispatch, or authorize work.', 'muted'));
  return card;
}
async function loadWorkers(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) { workerOffset = 0; readVersion.workers++; $('workers').replaceChildren(element('p', 'Loading worker discovery…', 'muted')); }
  const requestVersion = readVersion.workers;
  const loadingKey = `workers:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('workers-more').disabled = true;
  try {
    const response = await api.workers(connectionId, project.id, { offset: workerOffset, limit: 100 });
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.workers) return;
    if (!response || !Array.isArray(response.workers) || !response.workers.every(worker => validWorkerDiscovery(worker, project))) {
      throw new Error('The control plane did not return a safe project worker-discovery projection.');
    }
    if (reset) $('workers').replaceChildren();
    for (const worker of response.workers) $('workers').append(renderWorkerDiscovery(worker));
    workerOffset += response.workers.length;
    $('workers-more').hidden = response.workers.length < 100 || workerOffset > 100000;
    if (!workerOffset) $('workers').append(element('p', 'No approved workers are registered for this project.', 'muted'));
  } catch (error) {
    if (version === epoch && project === activeProject && requestVersion === readVersion.workers) {
      if (reset) $('workers').replaceChildren(element('p', 'Could not load worker discovery. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey);
    if (version === epoch && project === activeProject && requestVersion === readVersion.workers) $('workers-more').disabled = false;
  }
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
    const context = element('button', 'Include plan in coordinator context'); context.type = 'button';
    context.disabled = !connectionOnline || recoveryHeld;
    context.addEventListener('click', () => toggleCoordinatorContext({ kind: 'task', record_id: task.id, expected_revision: plan.revision }));
    card.append(context);
    const button = element('button', task.status === 'approved' ? 'Plan approved' : 'Approve this plan');
    button.disabled = !canMutate() || task.status !== 'draft';
    button.addEventListener('click', () => guarded(async () => {
      button.disabled = true;
      const body = { expected_revision: plan.revision, expected_plan_digest: plan.plan_digest };
      try {
        if (version !== epoch || project !== activeProject) return;
        await command(`approve:${task.id}`, body, (id, key) => api.approve(id, task.id, body, key));
        if (version === epoch && project === activeProject) { await selectProject(project); notice('Plan approval recorded. No execution was started.'); }
      } finally { if (button.isConnected) button.disabled = !canMutate() || task.status !== 'draft'; }
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
  activeThread = thread; activeHarnessBinding = null; readVersion.binding++; messageOffset = 0; $('messages').replaceChildren();
  $('messages-more').hidden = true;
  $('thread-title').textContent = thread.title; $('message-form').reset(); $('message-form').hidden = false; $('harness-context').hidden = false; clearHarnessBinding();
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
      if (message.delivery && typeof message.delivery === 'object') {
        const delivery = message.delivery;
        row.append(element('p', contextStateLabel(delivery.state), 'muted'));
        row.append(element('p', 'Server-derived delivery destination recorded; raw harness and session identifiers are not shown.', 'muted'));
        row.append(element('p', `Context delivery revision ${delivery.revision} · context digest ${delivery.context_digest}`, 'muted'));
        if (delivery.state === 'failed') row.append(element('p', `Context delivery failure code: ${delivery.error_code ?? 'unclassified'}.`, 'muted'));
        if (['queued', 'delivered'].includes(delivery.state) &&
            Number.isSafeInteger(delivery.revision) && delivery.revision >= 1) {
          const cancel = element('button', 'Cancel queued context'); cancel.type = 'button'; cancel.disabled = !canMutate();
          cancel.addEventListener('click', () => guarded(async () => {
            cancel.disabled = true;
            const body = { expected_revision: delivery.revision };
            try {
              await command(`context-cancel:${delivery.delivery_id}`, body,
                (id, key) => api.cancelHarnessDelivery(id, delivery.delivery_id, body, key),
                () => version === epoch && thread === activeThread && selection === threadEpoch);
              if (version === epoch && thread === activeThread && selection === threadEpoch) {
                await loadMessages(version, selection, true);
                notice('Context cancellation recorded. This does not terminate a harness session.');
              }
            } finally { if (cancel.isConnected) cancel.disabled = !canMutate(); }
          }));
          row.append(cancel);
        }
      } else if (message.delivery_legacy) {
        row.append(element('p', 'Legacy conversation record — no harness context delivery or session record was created.', 'muted'));
      }
      row.append(element('p', message.body)); row.append(element('time', new Date(message.created_at).toLocaleString(), 'muted'));
      $('messages').append(row);
    }
    messageOffset += response.messages.length; $('messages-more').hidden = response.messages.length < 100 || messageOffset > 100000;
    if (!messageOffset) $('messages').append(element('p', 'No messages yet.', 'muted'));
  } catch (error) {
    if (version === epoch && requestVersion === readVersion.messages && selection === threadEpoch) {
      if (reset) $('messages').replaceChildren(element('p', 'Messages may be stale because the control plane is offline or the selected context changed. Use Refresh to retry.', 'muted'));
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
      approve.disabled = reject.disabled = !canMutate();
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
          approve.disabled = !canMutate() || (pending?.projectId === decisionProject.id && pending.revision === decision.revision && pending.body.decision === 'reject');
          reject.disabled = !canMutate() || (pending?.projectId === decisionProject.id && pending.revision === decision.revision && pending.body.decision === 'approve');
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
        attempt.operational_termination_confirmed !== null && typeof attempt.operational_termination_confirmed !== 'boolean') ||
      (attempt.operational_observation_cursor !== undefined &&
        attempt.operational_observation_cursor !== null && !isText(attempt.operational_observation_cursor)) ||
      (attempt.operational_observed_at !== undefined &&
        attempt.operational_observed_at !== null && !isText(attempt.operational_observed_at))) return false;
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
      const cursor = attempt.operational_observation_cursor ?? 'not recorded';
      const observedAt = attempt.operational_observed_at ?? 'not recorded';
      card.append(element('p', `Latest admitted-attempt observation: ${attempt.operational_observed_state} · termination ${attempt.operational_termination_confirmed === true ? 'confirmed (operational evidence only)' : 'unconfirmed or not recorded'} · observed ${observedAt} · cursor ${cursor}`, 'muted'));
    }
    if (attempt.outcome !== null) {
      const decision = attempt.human_outcome;
      card.append(element('p', `Human-recorded outcome: ${attempt.outcome}${attempt.reason ? ` · reason: ${attempt.reason}` : ''}${decision ? ` · actor ${decision.decided_by_principal_id} · recorded ${decision.decided_at}` : ''}`));
    }
    card.append(element('p', `Semantic state history (${attempt.state_history.length})`, 'muted'));
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
function validRemoteWorkflow(workflow, project) {
  const reconciliation = workflow?.reconciliation;
  return workflow && isOperationId(workflow.operation_id) && workflow.project_id === project.id &&
    isIdentifier(workflow.task_id) && isIdentifier(workflow.grant_id) && isIdentifier(workflow.lease_id) &&
    isIdentifier(workflow.worker_principal_id) && isPositiveInteger(workflow.epoch) &&
    ['prepared', 'running', 'unknown', 'exited'].includes(workflow.state) &&
    ['admitted', 'cancel_requested'].includes(workflow.operation_desired_state) &&
    typeof workflow.reconciliation_required === 'boolean' && workflow.operational_only === true &&
    (workflow.experiment_attempt_id === null || isIdentifier(workflow.experiment_attempt_id)) &&
    (workflow.experiment_id === null || isIdentifier(workflow.experiment_id)) &&
    (workflow.experiment_design_revision === null || isPositiveInteger(workflow.experiment_design_revision)) &&
    (workflow.experiment_variant_id === null || isIdentifier(workflow.experiment_variant_id)) &&
    (reconciliation === null || (reconciliation && operationObservedStates.has(reconciliation.observed_state) &&
      isText(reconciliation.observed_at) && typeof reconciliation.termination_confirmed === 'boolean' &&
      isText(reconciliation.cursor) && Array.isArray(reconciliation.sources) &&
      ['none', 'same_time_contradiction', 'later_adverse_observation', 'unconfirmed_current_observation'].includes(reconciliation.contradiction_state)));
}
function remoteWorkflowStateText(workflow) {
  if (workflow.state === 'prepared') return 'Execution admission/package intent is recorded; no run observation is recorded.';
  if (workflow.state === 'running') return 'A remote run observation is recorded; it is not a result, evidence, or conclusion.';
  if (workflow.state === 'unknown') return 'Remote state is unknown; execution, cost exposure, and termination require reconciliation.';
  if (workflow.reconciliation?.termination_confirmed) return 'Termination is confirmed by attributed operational evidence only; evidence and conclusions remain separate.';
  return 'An exit is observed but termination is not confirmed; reconciliation remains required.';
}
function renderRemoteWorkflow(workflow, project, version, requestVersion) {
  const card = ledgerCard(`Remote operation ${workflow.operation_id}`, 'remoteWorkflowOperationId', { id: workflow.operation_id });
  card.append(element('span', `Operational state: ${workflow.state}`, 'tag'));
  card.append(element('p', `Admitted attempt: ${workflow.experiment_attempt_id ?? 'not linked'} · grant ${workflow.grant_id}`));
  card.append(element('p', workflow.operation_desired_state === 'cancel_requested' ?
    'Stop requested in control. This is not confirmed termination.' :
    'No stop request is recorded in control.'));
  card.append(element('p', `Task ${workflow.task_id} · lease epoch ${workflow.epoch} · worker ${workflow.worker_principal_id}`, 'muted'));
  card.append(element('p', remoteWorkflowStateText(workflow), 'muted'));
  const reconciliation = workflow.reconciliation;
  if (reconciliation) {
    card.append(element('p', `Reconciled observation: ${reconciliation.observed_state} · ${reconciliation.termination_confirmed ? 'termination confirmed' : 'termination unconfirmed'} · ${reconciliation.observed_at}`, 'muted'));
    card.append(element('p', `Reconciliation cursor: ${reconciliation.cursor} · ${reconciliation.contradiction_state}`, 'muted'));
  } else card.append(element('p', 'No attributed worker/provider observation has been reconciled yet.', 'muted'));
  if (workflow.reconciliation_required) card.append(element('p', 'Reconciliation required. Do not treat a stop request, reconnect, or displayed exit as confirmed termination.', 'muted'));
  const inspect = element('button', 'Refresh reconciled operation');
  inspect.addEventListener('click', () => guarded(async () => {
    const detail = await api.remoteWorkflow(connectionId, workflow.operation_id);
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.remoteWorkflows || !validRemoteWorkflow(detail, project)) return;
    await loadRemoteWorkflows(true);
    notice('Remote workflow projection refreshed. No run, dispatch, stop, evidence, or conclusion was replayed.');
  }));
  card.append(inspect);
  if (!reconciliation?.termination_confirmed && workflow.operation_desired_state !== 'cancel_requested') {
    const stop = element('button', 'Request stop');
    stop.addEventListener('click', () => guarded(async () => {
      if (stop.disabled) return;
      stop.disabled = true;
      try {
        const grant = await api.executionGrant(connectionId, workflow.grant_id);
        if (version !== epoch || project !== activeProject || requestVersion !== readVersion.remoteWorkflows ||
            !grant || grant.id !== workflow.grant_id || !isPositiveInteger(grant.revision) ||
            !['active', 'cancel_requested'].includes(grant.status)) {
          throw new Error('The current execution grant cannot be used for a stop request. Refresh the workflow projection.');
        }
        if (grant.status === 'cancel_requested') {
          notice('Stop was already requested. Termination remains unconfirmed until reconciled operational evidence says otherwise.');
          return;
        }
        const body = { expected_revision: grant.revision };
        await command(`remote-stop:${workflow.operation_id}:${workflow.grant_id}`, body,
          (id, key) => api.revokeExecutionGrant(id, workflow.grant_id, body, key),
          () => version === epoch && project === activeProject && requestVersion === readVersion.remoteWorkflows);
        await Promise.all([loadRemoteWorkflows(true), loadExperiments(true)]);
        notice('Stop requested. This fences new work but does not confirm termination, settle exposure, link evidence, or resolve a conclusion.');
      } finally {
        if (version === epoch && project === activeProject) stop.disabled = !canMutate();
      }
    }));
    card.append(stop);
  }
  return card;
}
async function loadRemoteWorkflows(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) { remoteWorkflowOffset = 0; readVersion.remoteWorkflows++; $('remote-workflows').replaceChildren(element('p', 'Loading reconciled remote workflows…', 'muted')); }
  const requestVersion = readVersion.remoteWorkflows;
  const loadingKey = `remote-workflows:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('remote-workflows-more').disabled = true;
  try {
    const response = await api.remoteWorkflows(connectionId, project.id, { offset: remoteWorkflowOffset, limit: 100 });
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.remoteWorkflows) return;
    if (!response || !Array.isArray(response.remote_workflows) || !response.remote_workflows.every(workflow => validRemoteWorkflow(workflow, project))) {
      throw new Error('The control plane did not return a safe reconciled remote-workflow projection.');
    }
    if (reset) $('remote-workflows').replaceChildren();
    for (const workflow of response.remote_workflows) $('remote-workflows').append(renderRemoteWorkflow(workflow, project, version, requestVersion));
    remoteWorkflowOffset += response.remote_workflows.length;
    $('remote-workflows-more').hidden = response.remote_workflows.length < 100 || remoteWorkflowOffset > 100000;
    if (!remoteWorkflowOffset) $('remote-workflows').append(element('p', 'No admitted remote operation has a workflow projection yet.', 'muted'));
  } finally {
    loading.delete(loadingKey);
    if (version === epoch && project === activeProject && requestVersion === readVersion.remoteWorkflows) $('remote-workflows-more').disabled = false;
  }
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
      const context = element('button', 'Include experiment in coordinator context'); context.type = 'button';
      context.disabled = !connectionOnline || recoveryHeld;
      context.addEventListener('click', () => toggleCoordinatorContext({ kind: 'experiment', record_id: experiment.id, expected_revision: experiment.current_design_revision }));
      $('experiments').append(button, context);
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
  // Projections belong to an exact complete experiment ledger.  Remove the old
  // experiment's cards before the next read settles, rather than showing them
  // as context for the newly selected experiment.
  activeExperimentLedger = null;
  workbenchEntries = workbenchEntries.filter(item => item.source !== 'projection');
  renderWorkbenchEntries();
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
    activeExperimentLedger = ledger;
    renderExperimentLedger(ledger);
    workbenchEntries = [...workbenchEntries.filter(item => item.source !== 'projection'), ...workbenchEvidenceEntries(ledger)];
    renderWorkbenchEntries();
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
      record.disabled = !canMutate() || study.status !== 'open';
      record.addEventListener('click', () => guarded(async () => {
        if (!canMutate() || ledger !== activeLedger || study.status !== 'open') {
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
        button.disabled = !canMutate();
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
      accept.disabled = !canMutate() || !findingIsAcceptable(ledger, finding);
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
      retract.disabled = !canMutate();
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
      revise.disabled = !canMutate();
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
    $('source-new').hidden = false; $('source-new').disabled = !canMutate();
    $('finding-new').hidden = ledger.study.status !== 'open'; $('finding-new').disabled = !canMutate();
    $('study-close').hidden = ledger.study.status !== 'open';
    $('study-close').disabled = !canMutate() || ledger.variants.some(variant => variant.status === 'registered');
    $('study-export').hidden = false;
    $('variant-new').hidden = ledger.study.status !== 'open';
    $('variant-new').disabled = !canMutate();
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
function renderCoordinatorContext() {
  const container = $('coordinator-context'); container.replaceChildren();
  if (!selectedCoordinatorContext.length) {
    container.append(element('span', 'No selected context. Select a plan or experiment below to include its current revision.', 'muted'));
    return;
  }
  for (const reference of selectedCoordinatorContext) {
    const chip = element('span', `${reference.kind} ${reference.record_id} · revision ${reference.expected_revision}`, 'context-chip');
    const remove = element('button', 'Remove'); remove.type = 'button'; remove.disabled = !canMutate();
    remove.addEventListener('click', () => {
      const index = selectedCoordinatorContext.findIndex(item => item.kind === reference.kind && item.record_id === reference.record_id);
      if (index >= 0) selectedCoordinatorContext.splice(index, 1);
      renderCoordinatorContext();
    });
    chip.append(remove); container.append(chip);
  }
}
function toggleCoordinatorContext(reference) {
  if (!activeProject || !isPositiveInteger(reference.expected_revision)) return;
  const index = selectedCoordinatorContext.findIndex(item => item.kind === reference.kind && item.record_id === reference.record_id);
  if (index >= 0) selectedCoordinatorContext.splice(index, 1);
  else if (selectedCoordinatorContext.length >= 16) throw new Error('Select at most 16 current context records.');
  else selectedCoordinatorContext.push(reference);
  renderCoordinatorContext();
}
function validCoordinatorDesign(design) {
  if (!design || !isText(design.goal) || !isText(design.success_criterion) || !isText(design.protocol) ||
      !Array.isArray(design.variants) || design.variants.length !== 2 ||
      new Set(design.variants.map(variant => variant?.kind)).size !== 2 ||
      !['baseline', 'intervention'].every(kind => design.variants.some(variant => variant?.kind === kind))) return false;
  return design.variants.every(variant => isText(variant.name) && isDigest(variant.method_digest) &&
    isDigest(variant.input_digest) && variant.metadata && typeof variant.metadata === 'object' &&
    !Array.isArray(variant.metadata) && ((variant.execution_task_id === null && variant.execution_plan_digest === null) ||
      (isIdentifier(variant.execution_task_id) && isDigest(variant.execution_plan_digest))));
}
function typedWorkbenchEvent(event) {
  if (event.event_type.startsWith('astra_coordinator_')) return null;
  const type = event.event_type === 'decision_resolved' || event.event_type === 'decision_expired' ? 'human-decision-receipt' :
    (event.event_type.startsWith('execution.') || event.event_type.startsWith('interaction_message_delivery_')) ? 'execution-milestone' : 'labbook-record';
  return { id: `event:${event.cursor}`, source: 'labbook', type, created_at: event.created_at, event };
}
function workbenchEvidenceEntries(ledger) {
  if (!ledger) return [];
  const entries = [];
  const projection = { experimentId: ledger.experiment.id, ledgerRevision: ledger.experiment.current_design_revision };
  for (const conclusion of ledger.conclusions ?? []) {
    entries.push({ id: `conclusion:${conclusion.id}`, source: 'projection', type: 'conclusion-card', created_at: null,
      projection: { ...projection, designRevision: conclusion.design_revision }, conclusion });
    for (const evidence of conclusion.evidence ?? []) {
      entries.push({ id: `evidence:${conclusion.id}:${evidence.attempt_id}:${evidence.artifact_id ?? evidence.measurement_id}`,
        source: 'projection', type: 'evidence-link', created_at: null,
        projection: { ...projection, designRevision: conclusion.design_revision }, evidence, conclusion });
    }
  }
  return entries;
}
function renderWorkbenchEntry(item) {
  const entry = element('li'); entry.dataset.workbenchType = item.type;
  const labels = { 'scientist-message': 'Scientist message', 'coordinator-reply': 'Deterministic local coordinator reply',
    'coordinator-proposal': 'Coordinator proposal', 'status-turn': 'Explain status turn', 'human-decision-receipt': 'Human decision receipt',
    'execution-milestone': 'Execution milestone', 'evidence-link': 'Evidence link', 'conclusion-card': 'Conclusion card', 'labbook-record': 'Labbook record' };
  if (item.event) entry.append(element('span', cancellationEventLabel(item.event)));
  entry.append(element('span', labels[item.type] ?? 'Recorded item', 'tag'));
  if (item.message) {
    entry.append(element('p', item.message.body));
    if (item.message.sender_kind === 'coordinator') entry.append(element('p', 'Provider disabled. This reply has no approval, grant, dispatch, tool, shell, or skill-loading authority.', 'muted'));
  } else if (item.proposal) {
    entry.append(element('p', item.proposal.body));
    entry.append(element('p', `Recap: ${item.proposal.recap}`));
    entry.append(element('p', `Status: ${item.proposal.status.join(' | ')}`));
    entry.append(element('p', 'Human review needed. Authority effect: none. Execution is not authorized.', 'muted'));
    if (item.proposal.design) {
      const design = item.proposal.design;
      const preview = element('article', undefined, 'card');
      preview.append(element('h4', 'Complete design preview'));
      preview.append(element('p', `Goal: ${design.goal}`));
      preview.append(element('p', `Success criterion: ${design.success_criterion}`));
      preview.append(element('p', `Protocol: ${design.protocol}`));
      for (const variant of design.variants) {
        const execution = variant.execution_task_id === null ? 'No registered execution reference.' :
          `Registered execution reference: task ${variant.execution_task_id} · plan ${variant.execution_plan_digest}.`;
        preview.append(element('p', `${variant.kind} variant: ${variant.name}`));
        preview.append(element('p', `Method digest: ${variant.method_digest}`));
        preview.append(element('p', `Input digest: ${variant.input_digest}`));
        preview.append(element('p', `Metadata: ${JSON.stringify(variant.metadata)}`));
        preview.append(element('p', execution, 'muted'));
      }
      preview.append(element('p', `Registration context digest: ${item.proposal.context_digest}`, 'muted'));
      preview.append(element('p', 'Review this complete candidate before registration. Registration does not authorize execution.', 'muted'));
      entry.append(preview);
      if (item.proposal.registration) {
        entry.append(element('p', `Registered design revision ${item.proposal.registration.design_revision} for experiment ${item.proposal.registration.experiment_id}. Execution remains unauthorized.`, 'muted'));
      } else {
        const register = element('button', 'Register design — execution not authorized'); register.type = 'button';
        register.disabled = !canMutate();
        register.addEventListener('click', () => guarded(async () => {
          const project = activeProject; const proposal = item.proposal;
          if (!project || !validCoordinatorDesign(proposal.design) || !isDigest(proposal.context_digest)) {
            throw new Error('This coordinator design proposal is incomplete or stale. Refresh before registering it.');
          }
          register.disabled = true;
          const body = { expected_context_digest: proposal.context_digest };
          try {
            await command(`register-design:${proposal.id}`, body,
              (id, key) => api.registerProposalDesign(id, proposal.id, body, key),
              () => project === activeProject);
            if (project === activeProject) {
              await Promise.all([loadWorkbench(true), loadExperiments(true)]);
              notice('Design registered. Execution is not authorized.');
            }
          } finally { if (register.isConnected) register.disabled = !canMutate(); }
        }));
        entry.append(register);
      }
    }
  } else if (item.statusTurn) {
    const turn = item.statusTurn;
    entry.append(element('p', `Turn state: ${turn.state}. Design revision: ${turn.design_revision}.`));
    if (turn.binding) entry.append(element('p', `Discussion binding: ${turn.binding.state}; lease expires ${turn.binding.lease_expires_at}.`, 'muted'));
    if (turn.tool_invocation) entry.append(element('p', `Mediated tool: ${turn.tool_invocation.tool_name}; outcome ${turn.tool_invocation.outcome}; input digest ${turn.tool_invocation.input_digest}.`, 'muted'));
    if (turn.answer) entry.append(element('p', turn.answer.body));
    entry.append(element('p', 'Provider disabled. This status turn has no approval, grant, dispatch, execution, shell, MCP, or skill-loading authority.', 'muted'));
  } else if (item.event) {
    if (item.type === 'human-decision-receipt') entry.append(element('p', `Recorded human decision: ${item.event.payload?.decision ?? 'status not recorded'}. This receipt does not issue a grant or start execution.`, 'muted'));
  } else if (item.evidence) {
    entry.append(element('p', `Experiment ${item.projection.experimentId} · loaded design revision ${item.projection.ledgerRevision}; conclusion design revision ${item.projection.designRevision ?? 'unknown legacy revision'}.`, 'muted'));
    entry.append(element('p', `Conclusion ${item.conclusion.id} links attempt ${item.evidence.attempt_id} to ${item.evidence.artifact_id ? `artifact ${item.evidence.artifact_id}` : `measurement ${item.evidence.measurement_id}`}.`));
    entry.append(element('p', 'Evidence link only; it does not establish scientific success.', 'muted'));
  } else if (item.conclusion) {
    entry.append(element('p', `Experiment ${item.projection.experimentId} · loaded design revision ${item.projection.ledgerRevision}; conclusion design revision ${item.projection.designRevision ?? 'unknown legacy revision'}.`, 'muted'));
    entry.append(element('p', item.conclusion.statement));
    entry.append(element('p', `Human conclusion status: ${item.conclusion.status}. Scientific validity: ${item.conclusion.scientific_validity}.`, 'muted'));
  }
  const timestamp = item.created_at ?? item.event?.created_at ?? item.proposal?.created_at ?? item.message?.created_at;
  if (timestamp) entry.append(element('time', new Date(timestamp).toLocaleString()));
  return entry;
}
function renderWorkbenchEntries() {
  const visible = workbenchView === 'labbook' ?
    workbenchEntries.filter(item => item.source === 'labbook' || item.source === 'projection') : workbenchEntries;
  $('events').replaceChildren();
  if (!visible.length) $('events').append(element('li', workbenchView === 'labbook' ? 'No labbook records yet.' : 'No coordinator workbench records yet.', 'muted'));
  else for (const item of visible) $('events').append(renderWorkbenchEntry(item));
  $('conversation-view').setAttribute('aria-pressed', String(workbenchView === 'conversation'));
  $('labbook-view').setAttribute('aria-pressed', String(workbenchView === 'labbook'));
}
async function loadWorkbench(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) {
    eventCursor = coordinatorMessageOffset = proposalOffset = statusTurnOffset = 0;
    coordinatorMessagesMore = proposalsMore = statusTurnsMore = true;
    readVersion.workbench++;
  }
  const requestVersion = readVersion.workbench;
  const messagePage = coordinatorMessagesMore ? { offset: coordinatorMessageOffset, limit: 100 } : null;
  const proposalPage = proposalsMore ? { offset: proposalOffset, limit: 100 } : null;
  const statusTurnPage = statusTurnsMore ? { offset: statusTurnOffset, limit: 100 } : null;
  const loadingKey = `workbench:${version}:${requestVersion}:${eventCursor}:${coordinatorMessageOffset}:${proposalOffset}:${statusTurnOffset}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey); $('events-more').disabled = true;
  try {
    const [conversation, proposals, statusTurns, labbook] = await Promise.all([
      messagePage ? api.astraConversation(connectionId, project.id, messagePage) : Promise.resolve({ messages: [] }),
      proposalPage ? api.astraProposals(connectionId, project.id, proposalPage) : Promise.resolve({ proposals: [] }),
      statusTurnPage ? api.explainStatusTurns(connectionId, project.id, statusTurnPage) : Promise.resolve({ turns: [] }),
      api.labbook(connectionId, project.id, eventCursor),
    ]);
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.workbench) return;
    if (!conversation || !Array.isArray(conversation.messages) || !Array.isArray(proposals.proposals) || !Array.isArray(statusTurns.turns) || !Array.isArray(labbook.events)) {
      throw new Error('The control plane did not return a complete coordinator projection.');
    }
    const messages = conversation.messages.filter(message => message.project_id === project.id &&
      ['human', 'coordinator'].includes(message.sender_kind) && isIdentifier(message.id) && isText(message.body)).map(message => ({
      id: `message:${message.id}`, source: 'conversation', type: message.sender_kind === 'human' ? 'scientist-message' : 'coordinator-reply', created_at: message.created_at, message,
    }));
    const proposalEntries = proposals.proposals.filter(proposal => proposal.project_id === project.id &&
      proposal.provider_disabled === true && proposal.authority_effect === 'none' && proposal.execution_authorized === false &&
      proposal.proposal_type === 'human_review_needed' && Array.isArray(proposal.status) &&
      (proposal.design === null || validCoordinatorDesign(proposal.design)) &&
      (proposal.context_digest === undefined || isDigest(proposal.context_digest))).map(proposal => ({
      id: `proposal:${proposal.id}`, source: 'conversation', type: 'coordinator-proposal', created_at: proposal.created_at, proposal,
    }));
    const statusEntries = statusTurns.turns.filter(turn => turn.project_id === project.id &&
      isIdentifier(turn.turn_id) && isIdentifier(turn.experiment_id) && isPositiveInteger(turn.design_revision) &&
      ['requested', 'claimed', 'completed', 'cancelled'].includes(turn.state)).map(turn => ({
      id: `status-turn:${turn.turn_id}`, source: 'conversation', type: 'status-turn', created_at: turn.answer?.created_at, statusTurn: turn,
    }));
    const events = labbook.events.map(typedWorkbenchEvent).filter(Boolean);
    if (messagePage) coordinatorConversationRevision = conversation.conversation === null ? 0 : conversation.conversation.revision;
    if (messagePage) {
      coordinatorMessageOffset += conversation.messages.length;
      coordinatorMessagesMore = conversation.messages.length === 100 && coordinatorMessageOffset <= 100000;
    }
    if (proposalPage) {
      proposalOffset += proposals.proposals.length;
      proposalsMore = proposals.proposals.length === 100 && proposalOffset <= 100000;
    }
    if (statusTurnPage) {
      statusTurnOffset += statusTurns.turns.length;
      statusTurnsMore = statusTurns.turns.length === 100 && statusTurnOffset <= 100000;
    }
    eventCursor = labbook.events.length ? labbook.events[labbook.events.length - 1].cursor : eventCursor;
    const evidence = workbenchEvidenceEntries(activeExperimentLedger);
    const merged = [...messages, ...proposalEntries, ...statusEntries, ...events, ...evidence];
    if (new Set(merged.map(item => item.id)).size !== merged.length) throw new Error('The control plane returned duplicate workbench records.');
    workbenchEntries = reset ? merged : [...workbenchEntries, ...merged.filter(item => !workbenchEntries.some(existing => existing.id === item.id))];
    workbenchEntries.sort((left, right) => String(left.created_at ?? '').localeCompare(String(right.created_at ?? '')) || left.id.localeCompare(right.id));
    $('events-more').hidden = !coordinatorMessagesMore && !proposalsMore && !statusTurnsMore && labbook.events.length < 200;
    renderWorkbenchEntries();
  } catch (error) {
    if (version === epoch && project === activeProject && requestVersion === readVersion.workbench) {
      $('events').replaceChildren(element('li', 'Coordinator workbench may be stale because the control plane is offline or selected context changed. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey); if (version === epoch && requestVersion === readVersion.workbench) $('events-more').disabled = false;
  }
}
async function loadEvents() { return loadWorkbench(false); }
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
    applyConnectionHealth(state);
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
    } finally { button.disabled = !canMutate(); }
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
    } finally { if (version === epoch && project === activeProject) button.disabled = !canMutate(); }
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
        button.disabled = !canMutate();
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
      if (version === epoch && thread === activeThread && selection === threadEpoch) {
        event.target.reset(); await loadMessages(version, threadEpoch, true);
        notice('Conversation message recorded. It was not sent to a harness.');
      }
    } finally { button.disabled = !canMutate(); }
  });
});
$('binding-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const version = epoch; const selection = threadEpoch; const thread = activeThread; const project = activeProject;
    const requestedId = new FormData(event.target).get('harness_session_binding_id');
    if (!thread || !project || typeof requestedId !== 'string' || !requestedId) throw new Error('Select a thread and enter a server-derived harness binding ID.');
    const requestVersion = ++readVersion.binding;
    $('binding-detail').textContent = 'Fetching server-derived harness binding…';
    const binding = await api.harnessSessionBinding(connectionId, requestedId);
    if (version !== epoch || selection !== threadEpoch || thread !== activeThread || project !== activeProject || requestVersion !== readVersion.binding) return;
    if (!validHarnessBinding(binding, requestedId, project, thread)) {
      activeHarnessBinding = null;
      $('binding-detail').textContent = 'The server binding is missing, stale, or does not belong to this thread/project. Context was not queued.';
      throw new Error('The server-derived harness binding is not valid for the selected context.');
    }
    activeHarnessBinding = binding;
    renderHarnessBinding(binding);
  });
});
$('context-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const version = epoch; const selection = threadEpoch; const thread = activeThread; const project = activeProject;
      const fields = new FormData(event.target);
      const bindingId = new FormData($('binding-form')).get('harness_session_binding_id');
      const contextRevision = Number(fields.get('context_revision'));
      if (!thread || !project || !activeHarnessBinding || activeHarnessBinding.id !== bindingId ||
          !Number.isSafeInteger(contextRevision) || contextRevision < 1) {
        throw new Error('Fetch a current server-derived harness binding and enter a valid context revision first.');
      }
      const body = { thread_id: thread.id, harness_session_binding_id: bindingId,
        context_revision: contextRevision, intent: fields.get('intent'), body: fields.get('body') };
      await command(`harness-context:${thread.id}:${bindingId}`, body,
        (id, key) => api.enqueueHarnessDelivery(id, body, key),
        () => version === epoch && selection === threadEpoch && thread === activeThread && project === activeProject &&
          activeHarnessBinding?.id === bindingId);
      if (version === epoch && selection === threadEpoch && thread === activeThread && project === activeProject) {
        event.target.reset(); await loadMessages(version, selection, true);
        notice('Context queued for harness delivery. It is not an answer, execution, or acceptance.');
      }
    } finally { button.disabled = !canMutate(); }
  });
});
$('coordinator-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]');
    if (button.disabled) return;
    const version = epoch; const project = activeProject; const fields = new FormData(event.target);
    const mode = fields.get('mode'); const prompt = fields.get('prompt')?.trim();
    if (!project || !['Discuss', 'Draft experiment', 'Explain status', 'Review results'].includes(mode) || !prompt) {
      throw new Error('Choose a composer mode and enter a message.');
    }
    if (!Number.isSafeInteger(coordinatorConversationRevision) || coordinatorConversationRevision < 0) {
      throw new Error('Refresh the coordinator workbench before requesting a turn.');
    }
    const statusReference = selectedCoordinatorContext.length === 1 && selectedCoordinatorContext[0].kind === 'experiment' ? selectedCoordinatorContext[0] : null;
    if (mode === 'Explain status' && !statusReference) throw new Error('Explain status requires exactly one current experiment context.');
    const body = mode === 'Explain status' ? { expected_conversation_revision: coordinatorConversationRevision,
      prompt, experiment_id: statusReference.record_id, expected_design_revision: statusReference.expected_revision } :
      { expected_conversation_revision: coordinatorConversationRevision,
        prompt: `[${mode}]\n${prompt}`, selected_context: selectedCoordinatorContext.map(reference => ({ ...reference })) };
    button.disabled = true;
    try {
      await command(`coordinator:${project.id}:${mode}`, body,
        (id, key) => mode === 'Explain status' ? api.requestExplainStatusTurn(id, project.id, body, key) : api.requestCoordinatorTurn(id, project.id, body, key),
        () => version === epoch && project === activeProject);
      if (version === epoch && project === activeProject) {
        event.target.reset(); await loadWorkbench(true);
        notice(mode === 'Explain status' ? 'Explain status turn requested. It is provider-disabled and runs only the mediated status read.' : 'Coordinator turn requested. It is provider-disabled and does not approve, grant, dispatch, or execute work.');
      }
    } catch (error) {
      if (/\(409\)/.test(error.message)) {
        throw new Error('The selected coordinator context or conversation revision is stale. Refresh before requesting another turn.');
      }
      throw error;
    } finally { if (version === epoch && project === activeProject) button.disabled = !canMutate(); }
  });
});
$('conversation-view').addEventListener('click', () => { workbenchView = 'conversation'; renderWorkbenchEntries(); });
$('labbook-view').addEventListener('click', () => { workbenchView = 'labbook'; renderWorkbenchEntries(); });
$('refresh').addEventListener('click', () => guarded(async () => {
  await loadProjects(true); if (activeProject) await selectProject(activeProject);
}));
$('projects-more').addEventListener('click', () => guarded(() => loadProjects()));
$('tasks-more').addEventListener('click', () => guarded(() => loadTasks()));
$('workers-more').addEventListener('click', () => guarded(() => loadWorkers()));
$('events-more').addEventListener('click', () => guarded(() => loadEvents()));
$('threads-more').addEventListener('click', () => guarded(() => loadThreads()));
$('messages-more').addEventListener('click', () => guarded(() => loadMessages()));
$('decisions-more').addEventListener('click', () => guarded(() => loadDecisions()));
$('experiments-more').addEventListener('click', () => guarded(() => loadExperiments()));
$('remote-workflows-more').addEventListener('click', () => guarded(() => loadRemoteWorkflows()));
$('studies-more').addEventListener('click', () => guarded(() => loadStudies()));
if (!api) {
  $('connection-open').disabled = true;
  notice('Open this workspace in the Tessera desktop application.');
}
