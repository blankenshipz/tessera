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
let activeProject = null;
let projectOffset = 0;
let taskOffset = 0;
let eventCursor = 0;
let threadOffset = 0;
let decisionOffset = 0;
let messageOffset = 0;
let activeThread = null;
let threadEpoch = 0;
const pendingKeys = new Map();
const pendingDecisionResolutions = new Map();
const loading = new Set();
const readVersion = { threads: 0, messages: 0, decisions: 0 };

function notice(message = '') { $('notice').textContent = message; }
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function clearContext() {
  epoch++;
  connectionId = null;
  activeProject = null;
  projectOffset = taskOffset = eventCursor = threadOffset = decisionOffset = messageOffset = 0;
  activeThread = null; threadEpoch++;
  pendingKeys.clear();
  pendingDecisionResolutions.clear();
  for (const name of ['projects', 'tasks', 'events', 'threads', 'messages', 'decisions']) $(name).replaceChildren();
  $('project-title').textContent = 'Start with a question.';
  $('welcome').hidden = false;
  $('workspace').hidden = true;
  $('project-new').disabled = $('refresh').disabled = true;
  $('disconnect').hidden = true;
  for (const name of ['projects-more', 'tasks-more', 'events-more', 'threads-more', 'messages-more', 'decisions-more']) $(name).hidden = true;
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
  taskOffset = eventCursor = threadOffset = decisionOffset = messageOffset = 0;
  activeThread = null; threadEpoch++;
  for (const name of ['threads-more', 'messages-more', 'decisions-more']) $(name).hidden = true;
  $('tasks').replaceChildren(); $('events').replaceChildren(); $('threads').replaceChildren(); $('messages').replaceChildren(); $('decisions').replaceChildren();
  $('message-form').hidden = true; $('message-form').reset(); $('thread-title').textContent = 'Select a thread';
  $('project-title').textContent = project.name;
  $('welcome').hidden = true; $('workspace').hidden = false;
  for (const button of $('projects').children) {
    if (button.dataset.projectId === project.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  await Promise.all([loadTasks(), loadEvents(), loadThreads(true), loadDecisions(true)]);
}
async function loadTasks() {
  const version = epoch;
  const project = activeProject;
  const response = await api.tasks(connectionId, project.id, { offset: taskOffset, limit: 100 });
  if (version !== epoch) return;
  for (const task of response.tasks) {
    const card = element('article', undefined, 'card');
    card.append(element('span', `${task.status} · revision ${task.revision}`, 'tag'));
    card.append(element('h3', task.title), element('p', task.purpose));
    card.append(element('p', `Recorded budget: ${task.budget_cents} cents · limit: ${task.max_duration_seconds} seconds`, 'muted'));
    const button = element('button', task.status === 'approved' ? 'Plan approved' : 'Approve this plan');
    button.disabled = task.status !== 'draft';
    button.addEventListener('click', () => guarded(async () => {
      button.disabled = true;
      const body = { expected_revision: task.revision, expected_plan_digest: task.plan_digest };
      try {
        if (version !== epoch) return;
        await command(`approve:${task.id}`, body, (id, key) => api.approve(id, task.id, body, key));
        if (version === epoch) { await selectProject(project); notice('Plan approval recorded. No execution was started.'); }
      } finally { if (button.isConnected) button.disabled = false; }
    }));
    card.append(button);
    $('tasks').append(card);
  }
  taskOffset += response.tasks.length;
  $('tasks-more').hidden = response.tasks.length < 100;
  if (!taskOffset) $('tasks').append(element('p', 'No research plans yet. Create a draft to begin.', 'muted'));
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
          approve.disabled = pending?.projectId === decisionProject.id && pending.revision === decision.revision && pending.body.decision === 'reject';
          reject.disabled = pending?.projectId === decisionProject.id && pending.revision === decision.revision && pending.body.decision === 'approve';
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
const eventLabels = {
  'project.created': 'Project created', 'task.created': 'Research plan drafted',
  'task.approved': 'Plan approved by a human', 'task.plan_revised': 'Plan revised; new review required',
};
async function loadEvents() {
  const version = epoch;
  const response = await api.labbook(connectionId, activeProject.id, eventCursor);
  if (version !== epoch) return;
  for (const event of response.events) {
    const entry = element('li');
    entry.append(element('span', eventLabels[event.event_type] ?? event.event_type));
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
$('disconnect').addEventListener('click', () => guarded(async () => {
  clearContext(); await api.disconnect(); $('connection-status').textContent = 'Not connected';
}));
$('connection-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    clearContext(); $('connection-dialog').close();
    const fields = new FormData(event.target);
    const state = await api.connect({ name: fields.get('name'), endpoint: fields.get('endpoint') });
    if (!state) { $('connection-status').textContent = 'Not connected'; return; }
    connectionId = state.id;
    $('connection-status').textContent = `${state.name}\n${state.endpoint}\n${state.organizationId}`;
    $('project-new').disabled = $('refresh').disabled = false;
    $('disconnect').hidden = false;
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
    } finally { button.disabled = false; }
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
    } finally { button.disabled = false; }
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
if (!api) {
  $('connection-open').disabled = true;
  notice('Open this workspace in the Tessera desktop application.');
}
