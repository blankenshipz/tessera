const api = window.tessera;
const $ = id => document.getElementById(id);
let epoch = 0;
let connectionId = null;
let activeProject = null;
let projectOffset = 0;
let taskOffset = 0;
let eventCursor = 0;
const pendingKeys = new Map();

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
  projectOffset = taskOffset = eventCursor = 0;
  pendingKeys.clear();
  for (const name of ['projects', 'tasks', 'events']) $(name).replaceChildren();
  $('project-title').textContent = 'Start with a question.';
  $('welcome').hidden = false;
  $('workspace').hidden = true;
  $('project-new').disabled = $('refresh').disabled = true;
  $('disconnect').hidden = true;
  for (const name of ['projects-more', 'tasks-more', 'events-more']) $(name).hidden = true;
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
  if (!pendingKeys.has(fingerprint)) pendingKeys.set(fingerprint, await api.commandId());
  return pendingKeys.get(fingerprint);
}
async function command(scope, body, execute) {
  const id = connectionId;
  const version = epoch;
  const key = await keyFor(scope, body);
  if (version !== epoch || id !== connectionId) throw new Error('Connection or project changed.');
  const result = await execute(id, key);
  pendingKeys.delete(JSON.stringify([scope, body]));
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
  activeProject = project;
  taskOffset = eventCursor = 0;
  $('tasks').replaceChildren(); $('events').replaceChildren();
  $('project-title').textContent = project.name;
  $('welcome').hidden = true; $('workspace').hidden = false;
  for (const button of $('projects').children) {
    if (button.dataset.projectId === project.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  await Promise.all([loadTasks(), loadEvents()]);
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
$('refresh').addEventListener('click', () => guarded(async () => {
  await loadProjects(true); if (activeProject) await selectProject(activeProject);
}));
$('projects-more').addEventListener('click', () => guarded(() => loadProjects()));
$('tasks-more').addEventListener('click', () => guarded(() => loadTasks()));
$('events-more').addEventListener('click', () => guarded(() => loadEvents()));
if (!api) {
  $('connection-open').disabled = true;
  notice('Open this workspace in the Tessera desktop application.');
}
