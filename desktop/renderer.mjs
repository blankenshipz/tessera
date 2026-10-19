function interactionReadIsCurrent({
  requestedEpoch, currentEpoch, requestedThreadEpoch, currentThreadEpoch,
  requestedProject, currentProject, requestedThread, currentThread,
}) {
  return requestedEpoch === currentEpoch && requestedProject === currentProject &&
    (requestedThreadEpoch === undefined || requestedThreadEpoch === currentThreadEpoch) &&
    (requestedThread === undefined || requestedThread === currentThread);
}

const api = window.tessera;
let adminView = 'setup';
let adminState = null;
let adminPending = null;
let adminRead = 0;
let adminBusy = false;
let adminEditing = null;
const $ = id => document.getElementById(id);
// Keep the trusted application URL exact; skip navigation moves focus only.
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault(); $('main-content').focus();
});
let epoch = 0;
let connectionId = null;
let recoveryHeld = true;
let connectionOnline = false;
let connectionState = null;
let namespaceContext = null;
let namespaceRequest = 0;
let activeProject = null;
let activeExperiment = null;
let activeExperimentLedger = null;
let activeLedger = null;
let outcomeContext = null;
let closeContext = null;
const evidenceContexts = new Map();
let experimentCommandContext = null;
let conclusionContext = null;
let amendmentContext = null;
let artifactReviewContext = null;
let artifactDispositionContext = null;
let artifactDispositionRead = 0;
let artifactPreviewRequest = 0;
let recoveryCommandContext = null;
let taskRevisionContext = null;
let projectOffset = 0;
let taskOffset = 0;
let workerOffset = 0;
let artifactOffset = 0;
let eventCursor = 0;
let workbenchView = 'conversation';
let workbenchEntries = [];
let coordinatorConversationRevision = 0;
let coordinatorAvailable = false;
let coordinatorBusy = null;
let coordinatorConsumerDetail = null;
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
const remoteWorkflowIds = new Set();
const artifactIds = new Set();
let messageOffset = 0;
let activeThread = null;
let threadEpoch = 0;
let activeHarnessBinding = null;
let activeStudy = null;
let studyEpoch = 0;
let activeProgramContext = null;
let programAuthorInputs = null;
let programContextRequest = 0;
let pendingProgramReview = null;
const pendingKeys = new Map();
const pendingDecisionResolutions = new Map();
const loading = new Set();
const readVersion = { workers: 0, artifacts: 0, threads: 0, messages: 0, binding: 0, decisions: 0, experiments: 0, experiment: 0, remoteWorkflows: 0, studies: 0, study: 0, program: 0, workbench: 0 };

const dialogNotices = new WeakMap();
function activeDialog() { return [...document.querySelectorAll('dialog[open]')].at(-1) ?? null; }
function clearDialogNotice(dialog) {
  const state = dialog && dialogNotices.get(dialog);
  if (!state) return;
  if (dialog.open && document.activeElement === state.region && state.previousFocus?.isConnected && dialog.contains(state.previousFocus) && !state.previousFocus.disabled) state.previousFocus.focus();
  state.region.textContent = ''; state.region.hidden = true; state.previousFocus = null;
}
function notice(message = '', { dialog = activeDialog(), error = false } = {}) {
  // Messages have already been mapped by the typed client/renderer. Never render
  // response bodies or HTML. Keep feedback accessible above an inert backdrop.
  $('notice').textContent = message;
  if (!dialog?.open) return;
  if (!message) { clearDialogNotice(dialog); return; }
  let state = dialogNotices.get(dialog);
  if (!state) {
    const region = element('p', undefined, 'dialog-notice'); region.tabIndex = -1;
    dialog.prepend(region); state = { region, previousFocus: null }; dialogNotices.set(dialog, state);
  }
  state.region.setAttribute('role', error ? 'alert' : 'status');
  state.region.setAttribute('aria-live', error ? 'assertive' : 'polite');
  state.region.textContent = message; state.region.hidden = false;
  if (error) {
    if (document.activeElement !== state.region) state.previousFocus = document.activeElement;
    state.region.focus();
  }
}
const workspaceViews = { experiments: 'Experiments', plans: 'Plans & decisions', ledger: 'Research ledger', conversation: 'Conversation', operations: 'Operations' };
let workspaceView = 'experiments';
function selectWorkspaceView(view) {
  if (!Object.hasOwn(workspaceViews, view)) return;
  workspaceView = view;
  if (!$('admin-workspace').hidden && activeProject) clearAdmin();
  $('workspace').hidden = !activeProject;
  $('workspace-navigation').hidden = !activeProject;
  for (const panel of document.querySelectorAll('[data-workspace-view]')) panel.hidden = panel.dataset.workspaceView !== view;
  for (const name of Object.keys(workspaceViews)) {
    if (name === view) $(`view-${name}`).setAttribute('aria-current', 'page');
    else $(`view-${name}`).removeAttribute('aria-current');
  }
  $('workspace-view-title').textContent = workspaceViews[view];
}
function canMutate() { return !recoveryHeld && connectionOnline; }
const unavailableControls = new WeakMap();
function availabilityBasis() {
  return [epoch, activeProject, activeExperimentLedger, activeLedger, activeProgramContext,
    artifactReviewContext, artifactDispositionContext, artifactDispositionRead, taskRevisionContext, readVersion.decisions, readVersion.workbench,
    readVersion.remoteWorkflows];
}
function applyAvailability(control, unavailable) {
  if (unavailable) {
    if (!unavailableControls.has(control)) unavailableControls.set(control, { disabled: control.disabled, basis: availabilityBasis() });
    control.disabled = true;
  } else if (unavailableControls.has(control)) {
    const saved = unavailableControls.get(control); const basis = availabilityBasis();
    // Availability is an overlay, never a replacement for the record's eligibility
    // or a busy flag. Changed contexts need a fresh projection before re-enabling.
    control.disabled = saved.disabled || !saved.basis.every((value, index) => value === basis[index]);
    unavailableControls.delete(control);
  }
}
function clearAdmin() {
  adminRead++; adminState = null; adminPending = null; adminBusy = false; adminEditing = null;
  $('admin-workspace').hidden = true; $('admin-preview').hidden = true;
  $('admin-preview-body').textContent = ''; $('admin-records').replaceChildren();
  $('admin-status').textContent = 'Select an authenticated project. Configuration is not readiness.';
  $('admin-issuance').hidden = true;
  $('admin-issuance-status').textContent = 'Select an authenticated root connection and project.';
  for (const id of ['admin-configuration-form', 'admin-secret-form', 'admin-delegation-form']) $(id).reset();
}
function adminCurrent(context) {
  return context.epoch === epoch && context.connectionId === connectionId && context.projectId === activeProject?.id;
}
function adminContext() { return { epoch, connectionId, projectId: activeProject?.id }; }
function adminPreview(body, record = null) {
  if (!canMutate() || !adminState || adminState.held || adminBusy) throw new Error('Administration is held, stale or unavailable.');
  adminPending = { ...adminContext(), body, recordId: record?.id ?? null };
  $('admin-preview-body').textContent = JSON.stringify({ record_id: record?.id ?? null, ...body }, null, 2);
  $('admin-preview').hidden = false; $('admin-confirm').disabled = false;
}
function renderAdmin() {
  const state = adminState;
  $('admin-issuance').hidden = !state?.root_admin || state.held || !activeProject || !canMutate();
  $('admin-configuration-editor').hidden = !['setup', 'targets'].includes(adminView);
  $('admin-secret-editor').hidden = adminView !== 'secrets' || connectionState?.role !== 'human';
  $('admin-delegation-editor').hidden = adminView !== 'delegations';
  for (const view of ['setup', 'targets', 'secrets', 'delegations', 'readiness', 'audit']) {
    $(`admin-${view}`).setAttribute('aria-pressed', String(view === adminView));
  }
  const explanations = {
    setup: 'Desired deployment metadata only. Use Connect to select a running service. Saving here never changes the current connection or deploys a control plane.',
    targets: 'Kubernetes, local fixture and Salad profiles remain unverified. Digests and configured limits are not proof of enforcement.',
    secrets: 'Protected enrollment: propose exact provider/purpose/project scope, obtain independent root-human approval, then enter once in the native hidden prompt. macOS Keychain and same-host native control database only; remote, Windows and Linux enrollment unavailable. No renderer token field or secret-read grant. Provider live activation stays disabled.',
    delegations: 'Exact project / target / provider / action scope. Independent root-human review required; no self-approval. Existing credentials are not proof of independent organization members. Worker, proxy and monitor identity enrollment is unavailable.',
    readiness: 'Configure → human review → local verification → BLOCKED. Verification makes no network calls. Live enablement needs deployed secret custody, independent monitor/termination, cost accounting, artifact custody and separately authorized conformance. No enable switch is available.',
    audit: 'Attributed metadata command history. Denials remain in the existing owner-only /operator/audit ledger; no request bodies or secret values in command summaries.',
  };
  $('admin-explanation').textContent = explanations[adminView];
  if (!state) return;
  $('admin-status').textContent = `Principal ${connectionState?.principalId ?? 'unknown'} · project ${activeProject?.id ?? 'unknown'} · control database: ${state.control_status ?? 'unknown'} · observed ${state.observed_at ?? 'unknown'} · ${state.held ? 'held/read-only' : 'metadata workspace'} · provider DISABLED · native custody requires explicit same-host setup. Refresh to observe changes.`;
  $('admin-records').replaceChildren();
  $('admin-more').hidden = state.next_offset === null || adminView === 'audit';
  if (adminView === 'audit') return;
  const records = state.records.filter(record => {
    if (adminView === 'secrets') return record.spec.kind === 'secret';
    if (adminView === 'delegations') return record.spec.kind === 'delegation';
    if (record.spec.kind !== 'configuration') return false;
    return adminView === 'readiness' || (adminView === 'setup' ? record.spec.component === 'control_plane' : record.spec.component !== 'control_plane');
  });
  if (!records.length) $('admin-records').append(element('p', 'No visible records in this page. Nothing is implicitly configured or enabled.', 'muted'));
  for (const record of records) {
    const card = element('article', undefined, 'card');
    card.append(element('h3', `${record.spec.kind} · ${record.spec.provider} · ${record.state}`),
      element('p', `${record.id} · revision ${record.revision} · version ${record.version}`),
      element('p', `Health ${record.health} · verification ${record.verification} · live disabled · last used ${record.last_used_at ?? 'unknown / never observed'}`));
    if (record.spec.kind === 'delegation') card.append(element('p', `Effective administration scope: ${record.effective ? 'active' : 'not active'} · no execution or secret-read permission`));
    const detail = element('details'); detail.append(element('summary', 'Inspect exact scope / metadata'), element('pre', JSON.stringify(record.spec, null, 2)));
    if (record.versions?.length) detail.append(element('pre', JSON.stringify(record.versions, null, 2)));
    card.append(detail);
    const action = (label, name, extra = {}) => {
      const button = element('button', label); button.type = 'button';
      button.disabled = !canMutate() || state.held || adminBusy;
      button.addEventListener('click', () => guarded(async () => adminPreview({ expected_revision: record.revision, action: name, ...extra }, record)));
      card.append(button);
    };
    if (state.root_admin && record.state === 'proposed' && record.proposed_by !== connectionState?.principalId && record.spec.subject_id !== connectionState?.principalId) {
      action('Review approval', 'approve'); action('Review rejection', 'reject');
    }
    if (record.spec.kind === 'configuration' && record.state === 'approved') action('Verify local bindings (live remains blocked)', 'verify');
    if (record.spec.kind === 'configuration') {
      if (!['revoked', 'expired', 'deleted', 'rejected'].includes(record.state)) {
        const edit = element('button', 'Revise configuration proposal'); edit.type = 'button';
        edit.disabled = !canMutate() || state.held || adminBusy;
        edit.addEventListener('click', () => {
          adminEditing = { ...adminContext(), record };
          adminPending = null; $('admin-preview').hidden = true;
          for (const [key, value] of Object.entries(record.spec)) {
            const input = $('admin-configuration-form').elements.namedItem(key);
            if (input) input.value = value ?? '';
          }
          $('admin-configuration-editor').hidden = false;
          $('admin-configuration-editor').open = true;
          $('admin-status').textContent = `Editing exact record ${record.id} at revision ${record.revision}. Preview before submission; approval will be invalidated.`;
        });
        card.append(edit);
      }
      const enable = element('button', 'Enable live — unavailable'); enable.disabled = true; card.append(enable);
    }
    if (state.root_admin && !['deleted', 'expired', 'revoked', 'rejected'].includes(record.state)) {
      action('Review revocation', 'revoke'); action('Review immediate expiry', 'expire');
      if (record.spec.kind === 'secret' && record.spec.backend === 'fake') action('Review synthetic rotation (+60 minutes)', 'rotate', { expires_at: new Date(Date.now() + 3600000).toISOString() });
    }
    if (record.spec.backend === 'os_keychain') {
      card.append(element('p', `Custody: ${record.custody ?? 'not enrolled'} · cleanup ${record.cleanup_pending ? 'pending — revoke uncertain enrollment, then purge' : 'not pending'}. Approval expires after five minutes; cancelled/uncertain enrollment cannot replay.`, 'muted'));
      if (!state.root_admin && connectionState?.principalId === record.proposed_by && record.state === 'active') action('Propose rotation (+60 minutes; invalidates old version)', 'rotate', { expires_at: new Date(Date.now() + 3600000).toISOString() });
      const nativeAction = (label, operation) => {
        const button = element('button', label); button.type = 'button';
        button.disabled = !canMutate() || state.held || adminBusy;
        button.addEventListener('click', () => guarded(async () => {
          const context = adminContext();
          if (adminBusy || !adminCurrent(context)) return;
          adminBusy = true;
          try {
            const result = await api.protectedSecret(context.connectionId, { project_id: context.projectId,
              record_id: record.id, expected_revision: record.revision, enrollment_id: record.enrollment_id, operation });
            if (!adminCurrent(context)) return;
            await loadAdmin();
            if (adminCurrent(context)) $('admin-status').textContent = `Protected operation: ${result.state ?? (result.available ? 'available' : 'unavailable')} · version ${result.version} · provider DISABLED. Metadata only; no value returned.`;
          } finally { if (adminCurrent(context)) adminBusy = false; }
        }));
        card.append(button);
      };
      if (state.root_admin && record.enrollment_id) {
        if (record.state === 'approved') { nativeAction('Enroll secret — native hidden prompt', 'enroll'); nativeAction('Import existing secret — native hidden prompt', 'import'); }
        nativeAction('Check local OS store (boolean only)', 'health');
        if (record.cleanup_pending && ['active', 'revoked', 'expired', 'deleted'].includes(record.state)) nativeAction('Purge inaccessible OS-store versions', 'purge');
      }
    }
    if (state.root_admin && record.spec.kind === 'secret' && ['revoked', 'expired'].includes(record.state)) action('Review metadata tombstone', 'delete');
    $('admin-records').append(card);
  }
}
async function loadAdmin(append = false) {
  if (!activeProject || !connectionId) { $('admin-status').textContent = 'Select an authenticated project first.'; return; }
  const context = adminContext(), request = ++adminRead;
  const offset = append ? adminState?.next_offset : 0;
  if (offset === null) return;
  $('admin-status').textContent = 'Refreshing; previous observations may be stale.';
  try {
    const result = await api.adminWorkspace(connectionId, activeProject.id, { offset, limit: 100 });
    if (!adminCurrent(context) || request !== adminRead) return;
    adminState = { ...result, records: append ? [...adminState.records, ...result.records] : result.records };
    adminEditing = null;
    adminPending = null; $('admin-preview').hidden = true; $('admin-preview-body').textContent = '';
    renderAdmin();
    if (adminView === 'audit' && result.root_admin) {
      const audit = await api.adminAudit(context.connectionId, context.projectId);
      if (!adminCurrent(context) || request !== adminRead || adminView !== 'audit') return;
      $('admin-records').append(element('pre', JSON.stringify(audit.events, null, 2)), element('p', 'First 100 command summaries. Use the paginated audit API for complete history.', 'muted'));
    }
  } catch (error) {
    if (adminCurrent(context) && request === adminRead) {
      adminState = null; adminPending = null; $('admin-preview').hidden = true;
      $('admin-preview-body').textContent = ''; $('admin-records').replaceChildren();
      $('admin-status').textContent = 'Unknown / stale: administrative state unavailable. No commands were replayed.';
    }
    throw error;
  }
}
async function confirmAdmin() {
  const pending = adminPending;
  if (!pending || !adminCurrent(pending) || !canMutate() || adminBusy || adminState?.held) return;
  adminBusy = true; $('admin-confirm').disabled = true;
  try {
    const key = await api.commandId();
    if (!adminCurrent(pending) || pending !== adminPending || !canMutate() || !adminState || adminState.held) return;
    if (pending.recordId) await api.adminCommand(pending.connectionId, pending.projectId, pending.recordId, pending.body, key);
    else await api.adminCreate(pending.connectionId, pending.projectId, pending.body, key);
    if (adminCurrent(pending)) { adminPending = null; await loadAdmin(); }
  } catch (error) {
    if (adminCurrent(pending)) {
      // Do not mint a second intent after an uncertain response. Inspect history first.
      adminPending = null; adminState = null; $('admin-preview').hidden = true;
      $('admin-preview-body').textContent = ''; $('admin-records').replaceChildren();
      $('admin-status').textContent = 'Command failed or outcome unknown. Refresh history before another command; nothing was replayed.';
    }
    throw error;
  } finally {
    if (adminCurrent(pending)) {
      adminBusy = false;
      if (adminState && adminView !== 'audit') renderAdmin();
    }
  }
}
function renderOnboarding() {
  const connectedWithoutProject = Boolean(connectionId && connectionOnline && !activeProject);
  $('welcome-heading').textContent = connectedWithoutProject ? 'Connected. Start your first project.' : 'Start your local workspace.';
  $('welcome-copy').textContent = connectedWithoutProject ?
    'This control plane is connected but has no selected project. Create a project to begin a separate research workspace.' :
    'Start the installed local control service below, or Connect to an existing endpoint supplied by its operator.';
  $('local-control-start').hidden = Boolean(connectionId && connectionOnline);
  $('welcome-project-new').hidden = !connectedWithoutProject;
  $('welcome-project-new').disabled = !canMutate();
  $('namespace-setup').disabled = !api?.namespaceSelectKubeconfig;
  $('workspace-availability').textContent = !connectionOnline ? 'Not connected — connect before recording changes.' : recoveryHeld ? 'Recovery hold — inspect history; changes remain disabled.' : 'Connected · records only. Approval, execution and scientific review remain separate.';
  $('journey-next').textContent = !connectionId ?
    'Next: Follow the init-once and serve commands below, then Connect with the endpoint and private owner.header file. For an existing service, use its operator-supplied endpoint and header. Kubernetes setup is optional.' :
    !connectionOnline ? 'Next: Reconnect with a current credential, then refresh history. Do not replay uncertain commands.' :
    recoveryHeld ? 'Next: Inspect recovered history. Mutations stay disabled until an operator resolves the recovery hold.' :
    !activeProject ? 'Next: Select an existing project or create one. Connection switching is not project import or migration.' :
    'Next: Record an experiment design, or review plans and human decisions; admission and dispatch are separate. No implicit execution.';
}
function namespaceIsCurrent(context) { return context === namespaceContext && context?.epoch === epoch; }
function renderNamespaceInspection(inspection) {
  const target = inspection.target;
  const conflicts = inspection.state === 'terminating' ?
    'Conflict: this namespace is terminating and cannot be used or created.' :
    inspection.state === 'absent' ? 'No name conflict observed: the named namespace is absent.' :
    'Name conflict for dedicated creation: the named namespace already exists.';
  $('namespace-detail').replaceChildren(
    element('p', `Identity: ${target.context} · ${target.server} · cluster UID ${target.clusterUid}`),
    element('p', `Namespace: ${target.namespace} · ${inspection.state}${inspection.uid ? ` · UID ${inspection.uid}` : ''}`),
    element('p', `Kubeconfig reference digest: ${target.kubeconfigReferenceSha256} · CA digest: ${target.caSha256}`, 'muted'),
    element('p', 'Ownership: not inferred. The namespace-only contract does not expose arbitrary labels or read Secrets.', 'muted'),
    element('p', conflicts, inspection.state === 'terminating' ? 'error' : 'muted'),
    element('p', 'Inspection is read-only: zero Kubernetes writes have occurred; no control plane is installed.', 'muted'),
  );
}
function clearNamespaceSetup({ clearMain = true } = {}) {
  namespaceRequest++;
  namespaceContext = null;
  $('namespace-dialog').close(); $('namespace-confirm-dialog').close();
  $('namespace-form').reset(); $('namespace-confirm-form').reset();
  $('namespace-selection').textContent = 'Select a kubeconfig through the native file picker. Its path and contents are not shown here.';
  $('namespace-detail').replaceChildren(element('p', 'No namespace selected.', 'muted'));
  for (const id of ['namespace-use-existing', 'namespace-create-plan', 'namespace-reconcile']) $(id).disabled = true;
  if (clearMain && api?.namespaceClear) void api.namespaceClear().catch(() => {});
}
function namespaceFailure(error) {
  const message = error?.message || 'Namespace operation failed.';
  if (/forbidden|denied|RBAC/i.test(message)) return 'Namespace access is forbidden. Check the selected context and Kubernetes RBAC; no write was retried.';
  if (/expired/i.test(message)) return 'Namespace plan expired. Inspect and create a fresh plan; no create was sent.';
  if (/unknown|timed out|unavailable/i.test(message)) return `${message} Reconcile read-only before any new plan.`;
  return message;
}
async function namespaceWork(work) {
  notice();
  try { await work(); } catch (error) { notice(namespaceFailure(error)); }
}
async function selectNamespaceKubeconfig() {
  const request = ++namespaceRequest;
  const selection = await api.namespaceSelectKubeconfig();
  if (request !== namespaceRequest) return;
  if (!selection || selection.state === 'cancelled') { notice('Kubeconfig selection cancelled; no Kubernetes request was made.'); return; }
  if (selection.state !== 'selected' || typeof selection.reference !== 'string' || typeof selection.name !== 'string') throw new Error('Native kubeconfig selection was incomplete.');
  namespaceContext = { epoch, request, reference: selection.reference, name: selection.name, inspection: null, plan: null };
  $('namespace-selection').textContent = `Selected kubeconfig: ${selection.name}. Its path and contents remain in the main process.`;
  notice('Kubeconfig selected. Enter its explicit context and one namespace name.');
}
function namespaceRequestFromForm(form) {
  const fields = new FormData(form);
  const context = String(fields.get('context') ?? '').trim(); const namespace = String(fields.get('namespace') ?? '').trim();
  if (!namespaceContext || !namespaceIsCurrent(namespaceContext)) throw new Error('Kubeconfig selection changed; choose it again.');
  if (!context || !namespace) throw new Error('Enter an explicit context and namespace.');
  return { reference: namespaceContext.reference, context, namespace };
}
async function inspectNamespace() {
  const context = namespaceContext; const request = namespaceRequestFromForm($('namespace-form'));
  const inspection = await api.namespaceInspect(request);
  if (!namespaceIsCurrent(context)) return;
  context.request = request; context.inspection = inspection; context.plan = null;
  renderNamespaceInspection(inspection);
  $('namespace-use-existing').disabled = inspection.state !== 'present';
  $('namespace-create-plan').disabled = inspection.state !== 'absent';
  $('namespace-reconcile').disabled = true;
  notice('Namespace inspected with zero writes. Choose a separate explicit next action.');
}
async function planNamespace(mode) {
  const context = namespaceContext;
  if (!namespaceIsCurrent(context) || !context.inspection) throw new Error('Inspect the current namespace before planning.');
  const plan = mode === 'use-existing' ? await api.namespacePlanUseExisting(context.request) : await api.namespacePlanCreate(context.request);
  if (!namespaceIsCurrent(context)) return;
  if (!plan || !/^[0-9a-f]{64}$/.test(plan.digest) || typeof plan.planId !== 'string' || typeof plan.expiresAt !== 'string' || plan.mode !== mode) throw new Error('Namespace plan was incomplete.');
  context.plan = plan;
  $('namespace-confirm-heading').textContent = mode === 'use-existing' ? 'Confirm existing namespace' : 'Confirm dedicated namespace creation';
  $('namespace-confirm-detail').replaceChildren(
    element('p', `Namespace-only plan digest: ${plan.digest}`),
    element('p', `Approval expires: ${plan.expiresAt}`),
    element('p', mode === 'use-existing' ? 'This confirms use of the displayed existing namespace. It performs zero writes.' : 'This may create exactly one Namespace after confirmation. It installs no control plane.'),
  );
  $('namespace-confirm-form').reset(); $('namespace-confirm-dialog').showModal();
}
async function confirmNamespace() {
  const context = namespaceContext; const plan = context?.plan;
  if (!namespaceIsCurrent(context) || !plan) throw new Error('Namespace plan changed; inspect and plan again.');
  if (plan.mode === 'use-existing') {
    $('namespace-confirm-dialog').close();
    $('namespace-detail').append(element('p', 'Connected to namespace; control plane not installed.', 'tag'));
    notice('Connected to namespace; control plane not installed. Choosing an existing namespace made zero writes.');
    return;
  }
  const submit = $('namespace-confirm-submit');
  if (submit.disabled) return;
  submit.disabled = true;
  try {
    const outcome = await api.namespaceCreate({ planId: plan.planId, approvedDigest: plan.digest });
    if (!namespaceIsCurrent(context)) return;
    $('namespace-confirm-dialog').close(); $('namespace-reconcile').disabled = false;
    if (outcome?.state === 'namespace_created_control_plane_not_installed') {
      $('namespace-detail').append(element('p', `Namespace created; control plane not installed. Receipt UID ${outcome.uid} · resourceVersion ${outcome.resourceVersion}.`, 'tag'));
      notice('Namespace created; control plane not installed. The creation receipt is held in the main process for read-only reconciliation.');
    } else {
      $('namespace-detail').append(element('p', 'Namespace create outcome is unknown. Reconciliation is required; this desktop will not retry create.', 'error'));
      notice('Namespace create outcome is unknown. Reconcile read-only; create will not be replayed.');
    }
  } finally { if (namespaceIsCurrent(context)) submit.disabled = false; }
}
async function reconcileNamespace() {
  const context = namespaceContext; const plan = context?.plan;
  if (!namespaceIsCurrent(context) || !plan) throw new Error('No current namespace plan is available for reconciliation.');
  const result = await api.namespaceReconcile({ planId: plan.planId });
  if (!namespaceIsCurrent(context)) return;
  $('namespace-detail').append(element('p', result.state === 'namespace_created_control_plane_not_installed' ?
    `Reconciled: namespace created; control plane not installed. UID ${result.uid ?? 'not returned'}.` :
    `Reconciliation result: ${result.state}. No create was retried.`, result.state === 'namespace_created_control_plane_not_installed' ? 'tag' : 'muted'));
  notice('Namespace reconciliation is read-only and did not replay create.');
}
function applyConnectionHealth(state) {
  if (state) connectionState = state;
  connectionOnline = state?.health === 'online';
  const unavailable = !canMutate();
  applyAvailability($('artifact-disposition'), unavailable || connectionState?.role !== 'human');
  applyAvailability($('artifact-disposition-form').querySelector('button[type="submit"]'), unavailable || connectionState?.role !== 'human');
  $('admin-confirm').disabled = unavailable || adminBusy || !adminPending;
  for (const button of document.querySelectorAll('#admin-records button')) applyAvailability(button, unavailable);
  if (unavailable) {
    $('admin-status').textContent = 'Read-only or offline; administrative observations may be stale. Refresh before changes.';
  }
  for (const id of ['project-new', 'task-new', 'thread-new', 'study-new', 'experiment-new', 'artifact-custody']) $(id).disabled = unavailable;
  for (const id of ['variant-new', 'study-close', 'source-new', 'finding-new', 'experiment-revise', 'conclusion-new']) applyAvailability($(id), unavailable);
  $('welcome-project-new').disabled = unavailable;
  // Reviewing already released bytes is a read path and remains available on a
  // held restore; recording a release or bundle remains fenced as a mutation.
  $('artifact-review').disabled = !connectionOnline;
  for (const id of ['project-form', 'task-form', 'thread-form', 'study-form', 'variant-form', 'outcome-form', 'study-close-form', 'source-form', 'assessment-form', 'correction-form', 'finding-form', 'finding-accept-form', 'finding-retract-form', 'finding-revise-form', 'message-form', 'context-form', 'coordinator-form', 'experiment-form', 'admission-form', 'dispatch-form', 'attempt-resolution-form', 'measurement-form', 'attempt-artifacts-form', 'conclusion-form', 'conclusion-amendment-form', 'artifact-form']) {
    const submit = $(id).querySelector('button[type="submit"]');
    if (id === 'coordinator-form') {
      submit.disabled = unavailable || !coordinatorAvailable || (coordinatorBusy?.project === activeProject && coordinatorBusy?.version === epoch);
    } else applyAvailability(submit, id === 'artifact-form' ? !connectionOnline : unavailable);
  }
  for (const button of document.querySelectorAll('#tasks button, #decisions button, #events button, #message-form button, #context-form button, #remote-workflows button')) applyAvailability(button, unavailable);
  applyAvailability($('artifact-download'), !connectionOnline);
  applyAvailability($('artifact-bundle'), unavailable);
  $('artifacts-more').disabled = !connectionOnline;
  // Study-detail controls have additional source, accounting, and revision fences.
  // Going offline disables them all; coming back online must not undo those fences.
  for (const button of document.querySelectorAll('#study-detail button')) applyAvailability(button, unavailable);
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
  renderOnboarding();
  const coordinatorDetail = coordinatorConsumerDetail;
  const consumerStatus = coordinatorDetail ? (coordinatorAvailable ?
    `Coordinator consumer available · heartbeat ${coordinatorDetail.heartbeat_at} · credential active. Provider disabled; no approval, grant, dispatch, or execution authority.` :
    `Coordinator consumer unavailable · health ${coordinatorDetail.health} · freshness ${coordinatorDetail.freshness} · credential ${coordinatorDetail.credential_status}${coordinatorDetail.credential_expires_at ? ` (${coordinatorDetail.credential_expires_at})` : ''}. Existing records may be stale; new coordinator requests are disabled.`) :
    'Coordinator consumer: unavailable or not yet verified. New coordinator requests are disabled; existing records may be stale.';
  $('coordinator-health').textContent = connectionOnline ? consumerStatus :
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
  artifactDispositionContext = null;
  artifactDispositionRead++;
  $('artifact-disposition-form').reset(); $('artifact-disposition-dialog').close();
  $('artifact-disposition-form').querySelector('button[type="submit"]').disabled = true;
  outcomeContext = closeContext = experimentCommandContext = conclusionContext = amendmentContext = artifactReviewContext = recoveryCommandContext = taskRevisionContext = null; programAuthorInputs = null; pendingProgramReview = null; artifactPreviewRequest++; evidenceContexts.clear();
  for (const id of ['assessment-source', 'correction-original', 'finding-review', 'finding-sources', 'finding-variants', 'finding-retract-review', 'finding-revise-original', 'finding-revise-sources', 'finding-revise-variants']) {
    $(id).replaceChildren();
  }
  for (const id of ['project-form', 'task-form', 'thread-form', 'study-form', 'program-review-form', 'variant-form', 'outcome-form', 'study-close-form', 'source-form', 'assessment-form', 'correction-form', 'finding-form', 'finding-accept-form', 'finding-retract-form', 'finding-revise-form']) {
    const form = $(id); form.reset(); $(id.replace('form', 'dialog')).close();
    form.querySelector('button[type="submit"]').disabled = !canMutate();
  }
}
function resetConnectionForm() {
  const form = $('connection-form'); form.reset(); $('connection-dialog').close();
}
function clearContext({ preserveConnectionForm = false } = {}) {
  if (connectionId && api.secretContext) void api.secretContext(connectionId, null).catch(() => {});
  clearAdmin();
  clearNamespaceSetup();
  epoch++;
  connectionId = null;
  recoveryHeld = true;
  connectionOnline = false;
  connectionState = null;
  $('recovery-banner').hidden = true;
  activeProject = null;
  projectOffset = taskOffset = workerOffset = artifactOffset = eventCursor = threadOffset = decisionOffset = experimentOffset = remoteWorkflowOffset = studyOffset = messageOffset = 0;
  artifactIds.clear();
  coordinatorMessageOffset = proposalOffset = statusTurnOffset = 0;
  coordinatorMessagesMore = proposalsMore = statusTurnsMore = true;
  activeThread = null; threadEpoch++; activeHarnessBinding = null; readVersion.binding++; activeStudy = null; activeLedger = null; activeProgramContext = null; pendingProgramReview = null; programContextRequest++; readVersion.program++; studyEpoch++;
  pendingKeys.clear();
  pendingDecisionResolutions.clear();
  activeExperiment = null; activeExperimentLedger = null;
  workbenchEntries = []; coordinatorConversationRevision = 0; coordinatorAvailable = false; coordinatorConsumerDetail = null; selectedCoordinatorContext.splice(0); readVersion.workbench++;
  for (const name of ['projects', 'tasks', 'workers', 'artifacts', 'events', 'threads', 'messages', 'decisions', 'experiments', 'experiment-detail', 'remote-workflows', 'studies', 'study-detail', 'program-review-detail']) $(name).replaceChildren();
  $('project-title').textContent = 'Start with a question.';
  $('welcome').hidden = false;
  $('workspace').hidden = true;
  $('workspace-navigation').hidden = true;
  renderOnboarding();
  $('project-new').disabled = $('refresh').disabled = true;
  $('disconnect').hidden = true;
  for (const name of ['projects-more', 'tasks-more', 'workers-more', 'artifacts-more', 'events-more', 'threads-more', 'messages-more', 'decisions-more', 'remote-workflows-more', 'studies-more']) $(name).hidden = true;
  $('experiments-more').hidden = true;
  $('experiment-title').textContent = 'Select an experiment';
  $('experiment-revise').disabled = $('conclusion-new').disabled = true;
  $('study-title').textContent = 'Select a study'; $('program-review').hidden = true; $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true;
  resetAuthoringForms();
  if (!preserveConnectionForm) resetConnectionForm();
  $('study-export').hidden = true;
  $('message-form').hidden = true; $('message-form').reset(); $('harness-context').hidden = true; $('binding-form').reset(); $('context-form').reset(); $('binding-detail').textContent = 'No server-derived harness binding loaded.'; $('thread-title').textContent = 'Select a thread';
  $('coordinator-form').reset(); renderCoordinatorContext();
  loading.clear();
}
async function guarded(work, { preserveConnectionForm = false } = {}) {
  const dialog = activeDialog();
  notice('', { dialog });
  try {
    await work();
    const state = api && await api.status();
    if (state) applyConnectionHealth(state);
    clearDialogNotice(dialog);
  } catch (error) {
    const state = api && await api.status();
    if (!state) {
      clearContext({ preserveConnectionForm });
      $('connection-status').textContent = 'Not connected';
    } else {
      applyConnectionHealth(state);
    }
    notice(error.message || 'The request could not be completed.', { dialog, error: true });
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
  const sameProject = activeProject?.id === project.id;
  const restoreSelection = sameProject ? { experiment: activeExperiment, study: activeStudy, thread: activeThread } : {};
  const secretSelectionUpdate = connectionId && api.secretContext ? api.secretContext(connectionId, project.id) : Promise.resolve();
  clearAdmin();
  clearNamespaceSetup();
  epoch++;
  if (activeProject?.id !== project.id) pendingDecisionResolutions.clear();
  activeProject = project;
  // Selection is already known even when remote reads are slow or unavailable.
  renderOnboarding();
  taskOffset = workerOffset = artifactOffset = eventCursor = threadOffset = decisionOffset = experimentOffset = remoteWorkflowOffset = studyOffset = messageOffset = 0;
  remoteWorkflowIds.clear(); artifactIds.clear(); coordinatorAvailable = false; coordinatorConsumerDetail = null;
  coordinatorMessageOffset = proposalOffset = statusTurnOffset = 0;
  coordinatorMessagesMore = proposalsMore = statusTurnsMore = true;
  activeThread = null; threadEpoch++; activeHarnessBinding = null; readVersion.binding++; activeStudy = null; activeLedger = null; activeProgramContext = null; pendingProgramReview = null; programContextRequest++; readVersion.program++; studyEpoch++;
  const selectionUpdate = api.studySelection(connectionId, null, studyEpoch, null);
  activeExperiment = null; activeExperimentLedger = null;
  workbenchEntries = []; coordinatorConversationRevision = 0; selectedCoordinatorContext.splice(0); readVersion.workbench++;
  for (const name of ['workers-more', 'artifacts-more', 'threads-more', 'messages-more', 'decisions-more', 'experiments-more', 'remote-workflows-more', 'studies-more']) $(name).hidden = true;
  $('tasks').replaceChildren(); $('workers').replaceChildren(); $('artifacts').replaceChildren(); $('events').replaceChildren(); $('threads').replaceChildren(); $('messages').replaceChildren(); $('decisions').replaceChildren(); $('experiments').replaceChildren(); $('experiment-detail').replaceChildren(); $('remote-workflows').replaceChildren(); $('studies').replaceChildren(); $('study-detail').replaceChildren(); $('program-review-detail').replaceChildren();
  $('message-form').hidden = true; $('message-form').reset(); $('harness-context').hidden = true; clearHarnessBinding(); $('thread-title').textContent = 'Select a thread';
  $('study-title').textContent = 'Select a study';
  $('experiment-title').textContent = 'Select an experiment';
  $('experiment-revise').disabled = $('conclusion-new').disabled = true;
  $('study-export').hidden = true;
  $('program-review').hidden = true; $('variant-new').hidden = true; $('study-close').hidden = true; $('source-new').hidden = $('finding-new').hidden = true;
  resetAuthoringForms();
  $('coordinator-form').reset(); renderCoordinatorContext();
  $('project-title').textContent = project.name;
  $('welcome').hidden = true; $('workspace').hidden = false;
  selectWorkspaceView(sameProject ? workspaceView : 'experiments');
  for (const button of $('projects').children) {
    if (button.dataset.projectId === project.id) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  await Promise.all([secretSelectionUpdate, selectionUpdate, loadTasks(), loadWorkers(true), loadArtifacts(true), loadWorkbench(true), loadThreads(true), loadDecisions(true), loadExperiments(true), loadRemoteWorkflows(true), loadStudies(true)]);
  if (activeProject !== project) return;
  // Refresh is a read: restore selections from fresh ledgers, never replay commands.
  await Promise.all([
    restoreSelection.experiment ? selectExperiment(restoreSelection.experiment) : Promise.resolve(),
    restoreSelection.study ? selectStudy(restoreSelection.study) : Promise.resolve(),
    restoreSelection.thread ? selectThread(restoreSelection.thread) : Promise.resolve(),
  ]);
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
function validArtifactSummary(artifact, project) {
  return artifact && isIdentifier(artifact.id) && artifact.project_id === project.id &&
    isText(artifact.relative_path) && isDigest(artifact.sha256) && Number.isSafeInteger(artifact.size) && artifact.size >= 0 &&
    isText(artifact.status) && isText(artifact.classification);
}
async function loadArtifacts(reset = false) {
  const version = epoch; const project = activeProject;
  if (reset) { artifactOffset = 0; artifactIds.clear(); readVersion.artifacts++; $('artifacts').replaceChildren(element('p', 'Loading project artifact metadata…', 'muted')); }
  const requestVersion = readVersion.artifacts;
  const loadingKey = `artifacts:${version}:${requestVersion}`;
  if (loading.has(loadingKey)) return;
  loading.add(loadingKey);
  try {
    const response = await api.artifacts(connectionId, project.id, { offset: artifactOffset, limit: 100 });
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.artifacts) return;
    if (!response || !Array.isArray(response.artifacts) || !response.artifacts.every(artifact => validArtifactSummary(artifact, project))) {
      throw new Error('The control plane did not return a safe project artifact projection.');
    }
    if (reset) $('artifacts').replaceChildren();
    for (const artifact of response.artifacts) {
      if (artifactIds.has(artifact.id)) continue;
      artifactIds.add(artifact.id);
      const card = element('article', undefined, 'card');
      card.append(element('h3', artifact.relative_path));
      card.append(element('p', `Artifact ${artifact.id} · ${artifact.size} bytes · ${artifact.sha256}`, 'muted'));
      card.append(element('p', `Custody status: ${artifact.status} · classification: ${artifact.classification}. Artifact metadata is not evidence acceptance.`, 'muted'));
      $('artifacts').append(card);
    }
    artifactOffset += response.artifacts.length;
    $('artifacts-more').hidden = response.artifacts.length < 100 || artifactOffset > 100000;
    if (!artifactOffset) $('artifacts').append(element('p', 'No project artifacts recorded.', 'muted'));
  } catch (error) {
    if (version === epoch && project === activeProject && requestVersion === readVersion.artifacts) {
      if (reset) $('artifacts').replaceChildren(element('p', 'Could not load project artifacts. Use Refresh to retry.', 'muted'));
      throw error;
    }
  } finally {
    loading.delete(loadingKey);
    if (version === epoch && project === activeProject && requestVersion === readVersion.artifacts) $('artifacts-more').disabled = !connectionOnline;
  }
}
function validTaskPlan(plan, task, project) {
  return plan && plan.task_id === task.id && plan.project_id === project.id &&
    /^[a-f0-9]{64}$/.test(plan.plan_digest) && plan.plan_digest === task.plan_digest &&
    Number.isSafeInteger(plan.revision) && plan.revision >= 1 &&
    plan.revision === task.revision - (task.status === 'approved' ? 1 : 0) &&
    (plan.execution_spec === null || (plan.execution_spec && typeof plan.execution_spec === 'object' &&
    Array.isArray(plan.execution_spec.argv) && Array.isArray(plan.execution_spec.pinned_skill_digests) &&
    Array.isArray(plan.execution_spec.output_paths)));
}
function specificationLines(plan) {
  const spec = plan.execution_spec;
  if (spec === null) return [['Execution specification', 'Not yet supplied. Revise this proposal to declare command, inputs, outputs and limits before execution.']];
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
async function loadAutonomy(panel, task, version, project) {
  try {
    const response = await api.autonomySessions(connectionId, task.id);
    if (version !== epoch || project !== activeProject || !panel.isConnected) return;
    if (!Array.isArray(response.sessions) || response.sessions.length > 32 || response.sessions.some(s =>
      s.task_id !== task.id || s.project_id !== project.id || s.org_id !== connectionState?.organizationId ||
      s.execution_enabled !== false || s.provider_enabled !== false || s.live_activation !== 'blocked' ||
      !Number.isSafeInteger(s.revision) || !/^[a-f0-9]{64}$/.test(s.plan_digest) ||
      !Array.isArray(s.actions) || !Array.isArray(s.blocked_reasons))) throw new Error('Invalid autonomy projection.');
    panel.replaceChildren(element('p', 'Bounded autonomy · synthetic only · billing unknown · scientific validity not assessed', 'muted'));
    for (const session of response.sessions) {
      const detail = element('details');
      detail.append(element('summary', `${session.state} · revision ${session.revision} · ${session.pending_approval ? 'Human review pending' : 'No pending review'}`));
      detail.append(element('p', `Blocked: ${session.blocked_reasons.join(', ') || 'Live activation remains blocked'}`));
      detail.append(element('p', `Checkpoint: ${session.checkpoint?.question ?? 'None'} · Resume is API-first, human-reviewed, and unavailable for unknown actions.`));
      // Untrusted plans/receipts are text only, never HTML, links or executable previews.
      detail.append(element('pre', JSON.stringify(session, null, 2)));
      panel.append(detail);
    }
    if (!response.sessions.length) panel.append(element('p', 'No autonomy sessions.'));
  } catch {
    if (version === epoch && project === activeProject && panel.isConnected) {
      panel.replaceChildren(element('p', 'Autonomy state unavailable. No authority or termination inferred.', 'muted'));
    }
  }
}
async function loadRemoteExecutions(panel, task, version, project) {
  try {
    const response = await api.remoteExecutions(connectionId, task.id);
    if (version !== epoch || project !== activeProject || !panel.isConnected) return;
    if (!Array.isArray(response.routes) || response.routes.length > 32 || response.routes.some(r =>
      r.task_id !== task.id || r.project_id !== project.id || r.org_id !== connectionState?.organizationId ||
      r.provider_enabled !== false || r.live_activation !== 'blocked' ||
      !['kubernetes', 'salad_container', 'salad_inference'].includes(r.proposal?.selection?.target) ||
      !Number.isSafeInteger(r.revision) || !/^[a-f0-9]{64}$/.test(r.proposal_digest))) {
      throw new Error('Invalid remote route projection.');
    }
    panel.replaceChildren(element('p', 'Desktop: control only. Closing/disconnecting does not stop remote work. Offline effects only; live activation blocked.', 'muted'));
    for (const route of response.routes) {
      const detail = element('details');
      detail.append(element('summary', `${route.proposal.selection.target} · ${route.state} · cancellation ${route.cancellation}`));
      detail.append(element('p', `Provider: ${route.provider_status} · go/no-go: ${route.go_no_go} · cost: ${route.cost_cents ?? 'unknown'}`));
      if (route.proxy_safety) {
        detail.append(element('p', `Salad proxy blocked: ${route.proxy_safety.blocked_reasons.join(', ')}. Required: ${route.proxy_safety.safety_prerequisites.join(', ')}. Not an isolation witness or trusted autonomous sandbox.`, 'muted'));
      }
      detail.append(element('p', 'Pause requests stop/hold, not process suspension. Unknown effects require reconciliation, not automatic retry. Logs/checkpoints/artifacts below are evidence references; custody and scientific acceptance remain separate.'));
      detail.append(element('pre', JSON.stringify(route, null, 2)));
      for (const [choice, label, allowed] of [
        ['approve', 'Review and approve exact remote proposal', route.state === 'proposed'],
        ['start', 'Queue reviewed remote work (offline only)', route.state === 'reviewed'],
        ['pause', 'Pause: request remote stop/hold', route.desired !== 'stop'],
        ['cancel', 'Request remote cancellation', route.desired !== 'stop'],
      ]) {
        const button = element('button', label); button.type = 'button'; button.disabled = !canMutate() || !allowed;
        button.addEventListener('click', () => guarded(async () => {
          if (version !== epoch || project !== activeProject || !canMutate()) return;
          const body = { expected_revision: route.revision, proposal_digest: route.proposal_digest, command: choice };
          await command(`remote:${route.id}:${choice}`, body, (id, key) => api.remoteExecutionCommand(id, route.id, body, key));
          await loadRemoteExecutions(panel, task, version, project);
        }));
        detail.append(button);
      }
      panel.append(detail);
    }
    if (!response.routes.length) panel.append(element('p', 'No remote proposals. Structured proposal authoring is API-first.'));
  } catch {
    if (version === epoch && project === activeProject && panel.isConnected) {
      panel.replaceChildren(element('p', 'Remote state unavailable. Reconnect and inspect; no cancellation or termination inferred.', 'muted'));
    }
  }
}
async function loadTaskPlan(card, task, version, project) {
  try {
    const plan = await api.taskPlan(connectionId, task.id, task.plan_digest);
    if (version !== epoch || project !== activeProject || !card.isConnected) return;
    if (!validTaskPlan(plan, task, project)) throw new Error('The control plane returned an unverified plan.');
    card.replaceChildren(element('span', `${task.status} · task revision ${task.revision} · plan revision ${plan.revision}`, 'tag'),
      element('h3', plan.title), element('p', plan.purpose));
    for (const [label, value] of specificationLines(plan)) {
      card.append(element('p', `${label}: ${value}`));
    }
    card.append(element('p', `Verified immutable plan digest: ${plan.plan_digest}`, 'muted'));
    if (['draft', 'approved'].includes(task.status)) {
      const revise = element('button', 'Revise this proposal'); revise.type = 'button'; revise.disabled = !canMutate();
      revise.addEventListener('click', () => guarded(async () => {
        if (version !== epoch || project !== activeProject) throw new Error('Refresh the selected project before revising this proposal.');
        taskRevisionContext = { task, plan, project, version };
        const form = $('task-revise-form'); form.reset();
        const set = (name, value) => { if (form.elements?.[name]) form.elements[name].value = value; else { form.fields ??= {}; form.fields[name] = value; } };
        set('title', plan.title); set('purpose', plan.purpose); set('budget_cents', plan.budget_cents);
        const spec = plan.execution_spec;
        set('max_duration_seconds', plan.max_duration_seconds); set('argv', JSON.stringify(spec?.argv ?? []));
        set('input_snapshot_digest', spec?.input_snapshot_digest ?? ''); set('pinned_skill_digests', JSON.stringify(spec?.pinned_skill_digests ?? []));
        set('output_paths', JSON.stringify(spec?.output_paths ?? [])); set('profile', spec?.profile ?? 'kubernetes-opencode');
        set('max_output_bytes', spec?.max_output_bytes ?? 1);
        $('task-revise-review').textContent = `Current proposal revision ${plan.revision} · immutable digest ${plan.plan_digest}. Saving invalidates this approval basis.`;
        $('task-revise-dialog').showModal();
      }));
      card.append(revise);
    }
    const context = element('button', 'Include plan in coordinator context'); context.type = 'button';
    context.disabled = !connectionOnline || recoveryHeld;
    context.addEventListener('click', () => toggleCoordinatorContext({ kind: 'task', record_id: task.id, expected_revision: task.revision }));
    card.append(context);
    const button = element('button', task.status === 'approved' ? 'Plan approved' : 'Approve this plan');
    button.disabled = !canMutate() || task.status !== 'draft';
    button.addEventListener('click', () => guarded(async () => {
      button.disabled = true;
      const body = { expected_revision: task.revision, expected_plan_digest: plan.plan_digest };
      try {
        if (version !== epoch || project !== activeProject) return;
        await command(`approve:${task.id}`, body, (id, key) => api.approve(id, task.id, body, key));
        if (version === epoch && project === activeProject) { await selectProject(project); notice('Plan approval recorded. No execution was started.'); }
      } finally { if (button.isConnected) button.disabled = !canMutate() || task.status !== 'draft'; }
    }));
    card.append(button);
    const autonomy = element('button', 'Inspect autonomy sessions'); autonomy.type = 'button';
    const autonomyPanel = element('section');
    autonomy.addEventListener('click', () => loadAutonomy(autonomyPanel, task, version, project));
    card.append(autonomy, autonomyPanel);
    const remote = element('button', 'Inspect / reconnect remote execution'); remote.type = 'button';
    const remotePanel = element('section');
    remote.addEventListener('click', () => loadRemoteExecutions(remotePanel, task, version, project));
    card.append(remote, remotePanel);
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
      card.append(element('h3', `${decision.kind} review`));
      card.append(element('p', `Decision ${decision.id}`, 'muted identifiers'));
      const researchReview = decision.scope?.type === 'research_program';
      if (researchReview) {
        const scope = decision.scope;
        card.append(element('h3', `${scope.outcome} · ${scope.action}${scope.recommendation ? ` · ${scope.recommendation}` : ''}`));
        card.append(element('p', `Study ${scope.study_id} · evidence basis ${scope.basis_digest}`, 'muted'));
        card.append(element('p', `Proposal reason: ${scope.reason}`));
        if (scope.supersedes_id) card.append(element('p', `Proposed replacement for ${scope.supersedes_id}. Existing human review is not reversed by an agent suggestion.`, 'muted'));
        card.append(element('p', 'Review only: source-use approval is not a byte release; go/no-go is not execution or proof of scientific success. The server rechecks evidence and proposal freshness.', 'muted'));
      }
      card.append(element('p', `Scope: ${JSON.stringify(decision.scope)}`));
      card.append(element('p', `Reviewer: ${decision.reviewer_principal_id} · expires ${decision.expires_at}`, 'muted'));
      card.append(element('p', `Task: ${decision.task_id ?? 'Project-level'} · task basis revision ${decision.expected_task_revision ?? 'none'}`, 'muted'));
      card.append(element('p', `Decision revision ${decision.revision}`, 'muted'));
      card.append(element('p', 'Safe default: leave this request pending. Silence does not approve it.', 'muted'));
      card.append(element('p', 'A recorded decision is intent only. It does not issue a grant or start execution.', 'muted'));
      const rationale = document.createElement('textarea');
      rationale.maxLength = 8000; rationale.rows = 3;
      rationale.setAttribute('aria-label', `Rationale for decision ${decision.id}`);
      rationale.placeholder = researchReview ? 'Required human rationale' : 'Optional rationale';
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
        if (researchReview && !text && !existing) {
          approve.disabled = reject.disabled = !canMutate();
          throw new Error('A research-program review requires a human rationale.');
        }
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
              if (researchReview && activeLedger?.study?.id === decision.scope.study_id) {
                void loadProgramContext(researchContext(activeLedger)).catch(() => {});
              }
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
function recordFields(fields) {
  const list = element('dl', undefined, 'record-fields');
  for (const [label, value] of fields) list.append(element('dt', label), element('dd', String(value)));
  return list;
}
function disclosure(label, ...content) {
  const details = element('details'); details.append(element('summary', label), ...content); return details;
}
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
function experimentContext(ledger = activeExperimentLedger) {
  return { ledger, experiment: activeExperiment, project: activeProject, version: epoch, experimentVersion: readVersion.experiment };
}
function currentExperimentContext(context) {
  return context && !recoveryHeld && connectionOnline && context.version === epoch &&
    context.project === activeProject && context.experiment === activeExperiment &&
    context.ledger === activeExperimentLedger && context.experimentVersion === readVersion.experiment;
}
async function refreshExperimentContext(context) {
  if (!currentExperimentContext(context)) return;
  await Promise.all([selectExperiment(context.experiment), loadArtifacts(true), loadRemoteWorkflows(true)]);
}
function designFormBody(form, expectedRevision = null) {
  const fields = new FormData(form);
  const variants = ['baseline', 'intervention'].map(kind => {
    let metadata;
    try { metadata = JSON.parse(String(fields.get(`${kind}_metadata`) ?? '')); } catch { throw new Error(`${kind} metadata must be a JSON object.`); }
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error(`${kind} metadata must be a JSON object.`);
    const taskId = String(fields.get(`${kind}_task_id`) ?? '').trim() || null;
    const planDigest = String(fields.get(`${kind}_plan_digest`) ?? '').trim() || null;
    if ((taskId === null) !== (planDigest === null)) throw new Error(`${kind} execution task and immutable plan digest must be supplied together.`);
    return { kind, name: fields.get(`${kind}_name`), method_digest: fields.get(`${kind}_method_digest`),
      input_digest: fields.get(`${kind}_input_digest`), metadata, execution_task_id: taskId, execution_plan_digest: planDigest };
  });
  return { ...(expectedRevision === null ? {} : { expected_revision: expectedRevision }),
    goal: fields.get('goal'), success_criterion: fields.get('success_criterion'), protocol: fields.get('protocol'), variants };
}
function fillExperimentForm(design) {
  const form = $('experiment-form'); form.reset();
  form.fields ??= {};
  const set = (name, value) => { if (form.elements?.[name]) form.elements[name].value = value ?? ''; else form.fields[name] = value ?? ''; };
  set('goal', design?.goal); set('success_criterion', design?.success_criterion); set('protocol', design?.protocol);
  for (const kind of ['baseline', 'intervention']) {
    const variant = design?.variants?.find(item => item.kind === kind);
    for (const field of ['name', 'method_digest', 'input_digest']) set(`${kind}_${field}`, variant?.[field]);
    set(`${kind}_metadata`, JSON.stringify(variant?.metadata ?? {}));
    set(`${kind}_task_id`, variant?.execution_task_id); set(`${kind}_plan_digest`, variant?.execution_plan_digest);
  }
}
function openConclusionForm(context, conclusion = null) {
  if (!currentExperimentContext(context)) throw new Error('Select a current complete experiment ledger first.');
  conclusionContext = { ...context, conclusion };
  const form = $('conclusion-form'); form.reset();
  const fields = form.elements;
  if (fields?.statement) fields.statement.value = conclusion?.statement ?? '';
  $('conclusion-form-title').textContent = conclusion ? `Revise draft conclusion ${conclusion.id}` : 'Author conclusion';
  form.querySelector('button[type="submit"]').textContent = conclusion ? 'Save revised draft' : 'Save draft';
  const choices = $('conclusion-evidence'); choices.replaceChildren();
  const selected = new Set((conclusion?.evidence ?? []).map(item => `${item.attempt_id}:${item.artifact_id ?? item.measurement_id}`));
  for (const attempt of [...context.ledger.attempts, ...context.ledger.admitted_attempts]) {
    for (const link of context.ledger.artifact_links.filter(item => item.attempt_id === attempt.id)) {
      const key = `${attempt.id}:${link.artifact_id}`; const label = element('label'); const input = element('input'); input.type = 'checkbox'; input.checked = selected.has(key); input.dataset.conclusionAttemptId = attempt.id; input.dataset.conclusionArtifactId = link.artifact_id;
      label.append(input, element('span', `Attempt ${attempt.id} · artifact ${link.artifact_id}`)); choices.append(label);
    }
    for (const measurement of context.ledger.measurements.filter(item => item.attempt_id === attempt.id)) {
      const key = `${attempt.id}:${measurement.id}`; const label = element('label'); const input = element('input'); input.type = 'checkbox'; input.checked = selected.has(key); input.dataset.conclusionAttemptId = attempt.id; input.dataset.conclusionMeasurementId = measurement.id;
      label.append(input, element('span', `Attempt ${attempt.id} · measurement ${measurement.id}: ${measurement.name}`)); choices.append(label);
    }
  }
  $('conclusion-review').textContent = `Design snapshot revision ${context.ledger.experiment.current_design_revision}. Evidence must already be linked to its attempt.`;
  $('conclusion-dialog').showModal();
}
function selectedConclusionEvidence() {
  const citations = new Map();
  for (const input of document.querySelectorAll('#conclusion-evidence input')) {
    if (!input.checked) continue;
    const attemptId = input.dataset.conclusionAttemptId;
    if (!citations.has(attemptId)) citations.set(attemptId, { attempt_id: attemptId, artifact_ids: [], measurement_ids: [] });
    if (input.dataset.conclusionArtifactId) citations.get(attemptId).artifact_ids.push(input.dataset.conclusionArtifactId);
    if (input.dataset.conclusionMeasurementId) citations.get(attemptId).measurement_ids.push(input.dataset.conclusionMeasurementId);
  }
  return [...citations.values()];
}
function renderExperimentLedger(ledger) {
  const detail = $('experiment-detail'); detail.replaceChildren();
  const experiment = ledger.experiment;
  const context = experimentContext(ledger);
  const summary = element('article', undefined, 'card');
  summary.append(element('span', `Current design revision ${experiment.current_design_revision}`, 'tag'));
  summary.append(element('p', `Current success criterion: ${experiment.success_criterion}`));
  summary.append(element('p', 'Design recorded, not execution authority. Scientific validity: not assessed.', 'muted'));
  detail.append(summary);
  const section = (title, records, render) => {
    detail.append(element('h4', `${title} (${records.length})`));
    if (!records.length) detail.append(element('p', `No ${title.toLowerCase()} recorded.`, 'muted'));
    else for (const record of records) detail.append(render(record));
  };
  const attemptsByVariant = new Map(ledger.variants.map(variant => [variant.id, []]));
  for (const attempt of [...ledger.attempts, ...ledger.admitted_attempts]) attemptsByVariant.get(attempt.variant_id).push(attempt);
  const renderDesign = design => {
    const card = element('article', undefined, 'card');
    if (design.revision !== experiment.current_design_revision) {
      card.append(element('h5', `Design revision ${design.revision}`));
      card.append(element('p', `Goal: ${recordedOrUnknown(design.goal)}`));
      card.append(element('p', `Success criterion: ${recordedOrUnknown(design.success_criterion)}`));
    }
    card.append(element('p', `Protocol: ${design.protocol}`));
    for (const variant of design.variants) {
      card.append(element('h5', `${variant.kind}: ${variant.name}`));
      card.append(disclosure('Identifiers & provenance', recordFields([
        ['Variant ID', variant.id], ['Method digest', variant.method_digest], ['Input digest', variant.input_digest],
      ]), element('pre', JSON.stringify(variant.metadata, null, 2))));
      card.append(element('p', variant.execution_task_id === null ?
        'Registered execution reference: none recorded.' :
        `Registered execution reference: task ${variant.execution_task_id} · plan digest ${variant.execution_plan_digest}`, 'muted'));
      if (!attemptsByVariant.get(variant.id).length) card.append(element('p', 'No attempts recorded for this registered variant.', 'muted'));
      if (design.revision === experiment.current_design_revision && variant.execution_task_id !== null) {
        const admit = element('button', 'Review separate execution admission'); admit.type = 'button'; admit.disabled = !canMutate();
        admit.addEventListener('click', () => guarded(async () => {
          if (!currentExperimentContext(context)) throw new Error('Refresh the selected experiment before admission.');
          experimentCommandContext = { ...context, variant };
          $('admission-form').reset();
          $('admission-review').textContent = `Variant ${variant.name} · design revision ${design.revision} · task ${variant.execution_task_id} · immutable plan ${variant.execution_plan_digest}`;
          $('admission-dialog').showModal();
        }));
        card.append(admit);
      }
    }
    return card;
  };
  summary.append(...renderDesign(ledger.designs.find(design => design.revision === experiment.current_design_revision)).children);
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
    if (operationalState === 'interrupted') {
      card.append(element('p', 'Interrupted is a control operational label, not proof of workload failure. A reported exit may map to this label; inspect the attributed termination observation, original Job/provider state and retained-output timing separately. Scientific outcome is not inferred.', 'muted'));
    } else if (['unknown', 'restart_pending'].includes(operationalState)) {
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
    if (attempt.outcome === null) {
      const resolve = element('button', 'Record human outcome'); resolve.type = 'button'; resolve.disabled = !canMutate();
      resolve.addEventListener('click', () => guarded(async () => {
        if (!currentExperimentContext(context)) throw new Error('Refresh the selected experiment before recording an outcome.');
        experimentCommandContext = { ...context, attempt };
        $('attempt-resolution-form').reset();
        $('attempt-resolution-review').textContent = `Attempt ${attempt.id} · current revision ${attempt.revision} · operational state ${operationalState}. Unknown remains available when evidence is incomplete.`;
        $('attempt-resolution-dialog').showModal();
      }));
      card.append(resolve);
    }
    if (attempt.operation_id !== null && attempt.package_digest !== null && attempt.outcome === null) {
      const dispatch = element('button', 'Review Kubernetes dispatch'); dispatch.type = 'button'; dispatch.disabled = !canMutate();
      dispatch.addEventListener('click', () => guarded(async () => {
        if (!currentExperimentContext(context)) throw new Error('Refresh the selected experiment before dispatch review.');
        experimentCommandContext = { ...context, attempt };
        $('dispatch-form').reset();
        $('dispatch-review').textContent = `Attempt ${attempt.id} · package ${attempt.package_digest} · revision ${attempt.revision}. A target-bound dispatch is separate from admission and from execution.`;
        $('dispatch-dialog').showModal();
      }));
      card.append(dispatch);
    }
    const measurement = element('button', 'Record measurement'); measurement.type = 'button'; measurement.disabled = !canMutate();
    measurement.addEventListener('click', () => guarded(async () => {
      if (!currentExperimentContext(context)) throw new Error('Refresh the selected experiment before recording a measurement.');
      experimentCommandContext = { ...context, attempt };
      $('measurement-form').reset(); $('measurement-review').textContent = `Attempt ${attempt.id} · revision ${attempt.revision}. An optional artifact must already be linked to this attempt.`;
      $('measurement-dialog').showModal();
    }));
    card.append(measurement);
    const linkArtifacts = element('button', 'Link retained artifact evidence'); linkArtifacts.type = 'button'; linkArtifacts.disabled = !canMutate();
    linkArtifacts.addEventListener('click', () => guarded(async () => {
      if (!currentExperimentContext(context)) throw new Error('Refresh the selected experiment before linking artifact evidence.');
      experimentCommandContext = { ...context, attempt }; $('attempt-artifacts-form').reset();
      $('attempt-artifacts-review').textContent = `Attempt ${attempt.id} · revision ${attempt.revision}. Review project artifact metadata and released custody separately before linking.`;
      $('attempt-artifacts-dialog').showModal();
    }));
    card.append(linkArtifacts);
    const history = disclosure(`Semantic state history (${attempt.state_history.length})`);
    history.open = attempt.state_history.some(entry => ['unknown', 'interrupted', 'restart_pending'].includes(entry.state) || entry.observed_state === 'unknown');
    for (const entry of attempt.state_history) {
      history.append(element('p', `#${entry.sequence}: ${entry.state} · observed ${entry.observed_state ?? 'not recorded'} · actor ${entry.actor_principal_id}${entry.reason ? ` · reason: ${entry.reason}` : ''}`, 'muted'));
    }
    card.append(history);
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
    card.append(element('p', 'To inspect bytes, use an already released manifest in Input and artifact review. Listing this record does not release custody.', 'muted'));
    return card;
  });
  section('Measurements', ledger.measurements, measurement => {
    const card = ledgerCard(`Measurement ${measurement.id}: ${measurement.name}`, 'experimentMeasurementId', measurement);
    card.append(element('p', `Attempt: ${measurement.attempt_id}`));
    card.append(disclosure('Recorded value — full measurement JSON', element('pre', JSON.stringify(measurement.value, null, 2))));
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
    if (conclusion.status === 'draft') {
      const revise = element('button', 'Revise draft conclusion'); revise.type = 'button'; revise.disabled = !canMutate();
      revise.addEventListener('click', () => guarded(async () => openConclusionForm(context, conclusion)));
      const review = element('button', 'Review conclusion'); review.type = 'button'; review.disabled = !canMutate();
      review.addEventListener('click', () => guarded(async () => {
        const body = { expected_revision: conclusion.revision, design_revision: conclusion.design_revision };
        await command(`review-conclusion:${conclusion.id}`, body, (id, key) => api.reviewConclusion(id, conclusion.id, body, key), () => currentExperimentContext(context));
        await refreshExperimentContext(context); notice('Human conclusion review recorded. It does not establish scientific success.');
      }));
      card.append(revise, review);
    } else if (conclusion.status === 'reviewed' && !conclusion.lineage.length) {
      const amend = element('button', 'Amend reviewed conclusion'); amend.type = 'button'; amend.disabled = !canMutate();
      amend.addEventListener('click', () => guarded(async () => {
        amendmentContext = { ...context, conclusion }; $('conclusion-amendment-form').reset();
        $('conclusion-amendment-review').textContent = `Conclusion ${conclusion.id} · revision ${conclusion.revision} · design revision ${conclusion.design_revision}. The original will remain immutable.`;
        $('conclusion-amendment-dialog').showModal();
      }));
      card.append(amend);
    }
    return card;
  });
  const historicalDesigns = ledger.designs.filter(design => design.revision !== experiment.current_design_revision);
  const designHistory = disclosure(`Historical design revisions (${historicalDesigns.length})`, ...historicalDesigns.map(renderDesign));
  designHistory.open = historicalDesigns.some(design => design.goal === null || design.success_criterion === null);
  detail.append(designHistory);
  detail.append(disclosure('Experiment identifiers & provenance', recordFields([
    ['Experiment ID', experiment.id], ['Project ID', experiment.project_id], ['Current revision', experiment.current_design_revision],
    ['Completeness', ledger.completeness],
  ])));
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
    (workflow.last_heartbeat_at === null || isText(workflow.last_heartbeat_at)) &&
    isPositiveInteger(workflow.last_checkpoint_sequence + 1) &&
    (workflow.last_checkpoint_cursor === null || isText(workflow.last_checkpoint_cursor)) &&
    (workflow.last_checkpoint_digest === null || isDigest(workflow.last_checkpoint_digest)) &&
    (workflow.last_reconnected_at === null || isText(workflow.last_reconnected_at)) &&
    (workflow.control_outage_at === null || isText(workflow.control_outage_at)) &&
    (workflow.last_observed_at === null || isText(workflow.last_observed_at)) &&
    (workflow.last_observed_state === null || operationObservedStates.has(workflow.last_observed_state)) &&
    (reconciliation === null || (reconciliation && operationObservedStates.has(reconciliation.observed_state) &&
      isText(reconciliation.observed_at) && typeof reconciliation.termination_confirmed === 'boolean' &&
      isText(reconciliation.cursor) && Array.isArray(reconciliation.sources) &&
      ['none', 'same_time_contradiction', 'later_adverse_observation', 'unconfirmed_current_observation'].includes(reconciliation.contradiction_state)));
}
function remoteWorkflowStateText(workflow) {
  if (workflow.state === 'prepared') return 'Execution admission/package intent is recorded; no run observation is recorded.';
  if (workflow.state === 'running') return 'A remote run observation is recorded; it is not a result, evidence, or conclusion.';
  if (workflow.state === 'unknown') return 'Remote state is unknown; execution, cost exposure, and termination require reconciliation.';
  if (workflow.reconciliation?.termination_confirmed) return workflow.operation_desired_state === 'cancel_requested' ?
    'Requested stop has confirmed cancellation from attributed operational evidence only; evidence and conclusions remain separate.' :
    'Termination is confirmed by attributed operational evidence only; evidence and conclusions remain separate.';
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
  card.append(element('p', `Freshness: heartbeat ${workflow.last_heartbeat_at ?? 'not recorded'} · latest observation ${workflow.last_observed_state ?? 'not recorded'} at ${workflow.last_observed_at ?? 'not recorded'}`, 'muted'));
  card.append(element('p', workflow.last_checkpoint_sequence > 0 ?
    `Latest checkpoint: #${workflow.last_checkpoint_sequence} · cursor ${workflow.last_checkpoint_cursor} · digest ${workflow.last_checkpoint_digest}` :
    'Latest checkpoint: none recorded.', 'muted'));
  if (workflow.control_outage_at !== null || workflow.last_reconnected_at !== null) {
    card.append(element('p', `Control outage: ${workflow.control_outage_at ?? 'not recorded'} · last reconnect: ${workflow.last_reconnected_at ?? 'not recorded'}. Reconnection does not replay work.`, 'muted'));
  }
  const reconciliation = workflow.reconciliation;
  if (reconciliation) {
    const termination = reconciliation.termination_confirmed ?
      (workflow.operation_desired_state === 'cancel_requested' ? 'cancellation confirmed' : 'termination confirmed') :
      'termination unconfirmed';
    card.append(element('p', `Reconciled observation: ${reconciliation.observed_state} · ${termination} · ${reconciliation.observed_at}`, 'muted'));
    card.append(element('p', `Reconciliation cursor: ${reconciliation.cursor} · ${reconciliation.contradiction_state}`, 'muted'));
  } else card.append(element('p', 'No attributed worker/provider observation has been reconciled yet.', 'muted'));
  if (workflow.reconciliation_required) card.append(element('p', 'Reconciliation required. Do not treat a stop request, reconnect, or displayed exit as confirmed termination.', 'muted'));
  const recovery = element('button', 'Inspect lost-worker recovery'); recovery.type = 'button'; recovery.disabled = !connectionOnline;
  recovery.addEventListener('click', () => guarded(async () => {
    const records = await api.recoveryReconciliations(connectionId, workflow.grant_id);
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.remoteWorkflows) return;
    if (!records || !Array.isArray(records.recovery_reconciliations)) throw new Error('The control plane did not return recovery records.');
    const record = records.recovery_reconciliations.find(item => item.operation_id === workflow.operation_id);
    if (!record) {
      notice('No lost-worker recovery record is present for this operation. The normal workflow projection remains read-only.');
      return;
    }
    if (!isPositiveInteger(record.revision) || !['identity_lost', 'cancel_requested', 'terminal_confirmed', 'unresolved'].includes(record.state)) {
      throw new Error('The control plane returned an unrecognized recovery record.');
    }
    recoveryCommandContext = { project, version, requestVersion, workflow, record };
    $('recovery-review').textContent = `Operation ${workflow.operation_id} · grant revision ${record.grant_revision ?? 'not recorded'} · recovery revision ${record.revision} · state ${record.state}.`;
    $('recovery-form').querySelector('button[type="submit"]').disabled = !canMutate() || ['terminal_confirmed', 'unresolved'].includes(record.state);
    $('recovery-dialog').showModal();
  }));
  card.append(recovery);
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
            !grant || grant.id !== workflow.grant_id || grant.project_id !== project.id || !isPositiveInteger(grant.revision) ||
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
  if (reset) { remoteWorkflowOffset = 0; remoteWorkflowIds.clear(); readVersion.remoteWorkflows++; $('remote-workflows').replaceChildren(element('p', 'Loading reconciled remote workflows…', 'muted')); }
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
    for (const workflow of response.remote_workflows) {
      if (remoteWorkflowIds.has(workflow.operation_id)) continue;
      remoteWorkflowIds.add(workflow.operation_id);
      $('remote-workflows').append(renderRemoteWorkflow(workflow, project, version, requestVersion));
    }
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
  $('experiment-revise').disabled = $('conclusion-new').disabled = true;
  workbenchEntries = workbenchEntries.filter(item => item.source !== 'projection');
  renderWorkbenchEntries();
   $('experiment-title').textContent = experiment.goal;
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
    $('experiment-revise').disabled = !canMutate();
    $('conclusion-new').disabled = !canMutate();
    workbenchEntries = [...workbenchEntries.filter(item => item.source !== 'projection'), ...workbenchEvidenceEntries(ledger)];
    renderWorkbenchEntries();
    $('experiment-detail').setAttribute('aria-busy', 'false');
  } catch (error) {
    if (version === epoch && project === activeProject && experiment === activeExperiment && requestVersion === readVersion.experiment) {
      $('experiment-detail').replaceChildren(element('p', 'Could not load a complete current experiment ledger. Partial, stale, or digest-inconsistent records are not shown; use Refresh to retry.', 'muted'));
      $('experiment-revise').disabled = $('conclusion-new').disabled = true;
      $('experiment-detail').setAttribute('aria-busy', 'false');
      throw error;
    }
  }
}
function programReviewCurrent(context) {
  return context && context === activeProgramContext && context.version === epoch &&
    context.project === activeProject && context.study === activeStudy &&
    context.selection === studyEpoch && context.ledger === activeLedger;
}
function programChoice(label, id, detail = '') {
  const input = element('input'); input.type = 'checkbox'; input.dataset.programChoiceId = id;
  const row = element('label'); row.append(input, element('span', `${id}${detail ? ` · ${detail}` : ''}`));
  return { input, id, row };
}
function renderProgramReview(context) {
  const { program } = context; const detail = $('program-review-detail'); detail.replaceChildren();
  const summary = element('article', undefined, 'card');
  summary.append(element('h3', 'Outcome 0/1 program review'));
  summary.append(element('p', `Current immutable evidence basis: ${program.basis_digest}`, 'muted'));
  summary.append(element('p', 'This is a planning and human-review record. It is not a model output, source-byte release, compute reservation, execution grant, dispatch, result, or scientific validity assessment.', 'muted'));
  summary.append(element('p', `Execution authority: ${program.execution_granted ? 'granted' : 'not granted'} · scientific validity: ${program.scientific_validity}`, 'muted'));
  detail.append(summary);
  const reviews = Array.isArray(program.reviews) ? program.reviews : [];
  detail.append(element('h4', `Requester / agent proposals (${reviews.length})`));
  if (!reviews.length) detail.append(element('p', 'No program proposal has been recorded. Agent proposals, if submitted through the API, appear here as requester records; authoring does not invent an agent recommendation.', 'muted'));
  for (const review of reviews) {
    const scope = review.scope ?? {}; const proposal = element('article', undefined, 'card');
    proposal.append(element('span', `${scope.outcome ?? 'unknown outcome'} · ${scope.action ?? 'unknown action'} · proposal`, 'tag'));
    proposal.append(element('p', `Requester: ${review.requester_principal_id ?? 'not recorded'} · proposal ${review.id}`));
    proposal.append(element('p', `Reason: ${scope.reason ?? 'not recorded'}`));
    proposal.append(element('p', `Basis at proposal: ${scope.basis_digest ?? 'not recorded'}`, 'muted'));
    if (scope.supersedes_id) proposal.append(element('p', `Replacement proposal for ${scope.supersedes_id}; earlier review remains historical until a human approves this replacement.`, 'muted'));
    if (scope.brief) proposal.append(element('p', `Objective: ${scope.brief.objective}\nHypothesis: ${scope.brief.hypothesis}\nGates: success — ${scope.brief.success_gate}; failure — ${scope.brief.failure_gate}; stop — ${scope.brief.stop_gate}`));
    if (scope.action === 'go_no_go') proposal.append(element('p', `Requested direction: ${scope.recommendation} · findings: ${(scope.finding_ids ?? []).join(', ') || 'none'}`));
    proposal.append(element('p', review.basis_current ? 'Evidence basis is current.' : 'STALE EVIDENCE: this proposal basis no longer matches the current study context. It remains historical and cannot be resolved as current.', review.basis_current ? 'muted' : 'error'));
    detail.append(proposal);
  }
  detail.append(element('h4', 'Human decisions'));
  if (!reviews.length) detail.append(element('p', 'No human approval, rejection, or supersession has been recorded.', 'muted'));
  for (const review of reviews) {
    const human = element('article', undefined, 'card'); const scope = review.scope ?? {};
    human.append(element('span', `${review.status ?? 'unknown'}${review.is_effective_review ? ' · current effective review' : ''}`, 'tag'));
    human.append(element('p', `Proposal ${review.id} · human resolver: ${review.resolved_by_principal_id ?? 'pending'}`));
    human.append(element('p', review.status === 'pending' ? 'Awaiting an eligible human decision. An agent suggestion cannot approve, reject, or supersede this review.' : `Human ${review.status}: ${review.rationale ?? 'no rationale recorded'}`));
    if (scope.supersedes_id) human.append(element('p', `Lineage: replaces ${scope.supersedes_id}; historical decisions are retained.`, 'muted'));
    if (!review.basis_current) human.append(element('p', 'Marked stale against current evidence. Reinspect before authoring or resolving a replacement.', 'error'));
    detail.append(human);
  }
}
async function loadProgramContext(research) {
  const request = ++programContextRequest; const version = epoch;
  activeProgramContext = null;
  $('program-review-detail').replaceChildren(element('p', 'Loading complete program-review context…', 'muted'));
  try {
    const program = await api.programContext(connectionId, research.study.id);
    if (request !== programContextRequest || version !== epoch || !currentResearchContext(research)) return;
    if (!program || program.study_id !== research.study.id || program.project_id !== research.project.id ||
        !isDigest(program.basis_digest) || !Array.isArray(program.reviews) || !program.condition_catalog || !program.basis) {
      throw new Error('The control plane did not return a complete program-review context.');
    }
    const context = { ...research, program, version, request };
    activeProgramContext = context; renderProgramReview(context);
    $('program-review').hidden = false; $('program-review').disabled = !canMutate();
  } catch (error) {
    if (request === programContextRequest && version === epoch && currentResearchContext(research)) {
      $('program-review-detail').replaceChildren(element('p', 'Could not load complete program-review history. Partial history is not shown; use Refresh to retry.', 'muted'));
      $('program-review').hidden = true;
      throw error;
    }
  }
}
function renderProgramAuthoring(context) {
  const program = context.program; const form = $('program-review-form'); form.reset();
  $('program-review-basis').textContent = `Study ${program.study_id} · current basis ${program.basis_digest}. A new request is fenced to this exact basis and latest lane head.`;
  const sources = program.basis.sources ?? []; const variants = program.basis.variants ?? []; const findings = program.basis.findings ?? [];
  const planSources = sources.map(source => programChoice('data source', source.id, `${source.verdict} · ${source.uri}`));
  const useSources = sources.map(source => programChoice('source use', source.id, `${source.verdict} · ${source.uri}`));
  const findingChoices = findings.map(finding => programChoice('finding', finding.id, finding.status));
  const conditions = {};
  $('program-data-sources').replaceChildren(element('h3', 'Data sources for this plan'), ...planSources.map(choice => choice.row));
  $('program-use-sources').replaceChildren(element('h3', 'Sources requiring human use review'), ...useSources.map(choice => choice.row));
  $('program-findings').replaceChildren(element('h3', 'Finding evidence for go'), ...findingChoices.map(choice => choice.row));
  $('program-conditions').replaceChildren();
  for (const [outcome, keys] of Object.entries(program.condition_catalog)) {
    const section = element('section'); section.dataset.programOutcome = outcome; section.append(element('h3', `${outcome} condition coverage`));
    for (const key of keys) {
      const disposition = element('select'); disposition.value = 'selected';
      const selected = element('option', 'Selected'); selected.value = 'selected'; const deferred = element('option', 'Deferred'); deferred.value = 'deferred'; disposition.append(selected, deferred);
      const rationale = element('textarea'); rationale.rows = 2; rationale.placeholder = 'Required condition rationale';
      const choices = variants.map(variant => programChoice('variant', variant.id, `${variant.status} · ${variant.name}`));
      const card = element('article', undefined, 'card'); card.append(element('h4', key), disposition, rationale, ...choices.map(choice => choice.row)); section.append(card);
      conditions[key] = { outcome, disposition, rationale, choices };
    }
    $('program-conditions').append(section);
  }
  programAuthorInputs = { context, planSources, useSources, findingChoices, conditions };
  updateProgramAuthoringAction();
}
function updateProgramAuthoringAction() {
  const action = $('program-review-form').fields?.action ?? $('program-review-form').querySelector?.('[name="action"]')?.value;
  $('program-plan-fields').hidden = action !== 'plan'; $('program-source-use-fields').hidden = action !== 'source_use'; $('program-go-fields').hidden = action !== 'go_no_go';
}
function chosenProgramIds(choices) { return choices.filter(choice => choice.input.checked).map(choice => choice.id); }
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
  activeStudy = study; activeLedger = null; activeProgramContext = null; pendingProgramReview = null; programContextRequest++; readVersion.program++;
  // A list row's status is advisory until the complete authoritative ledger
  // arrives. Keep authoring disabled while that read is pending or failed.
  study.status = null;
  $('study-export').hidden = true; $('study-export').disabled = false; $('program-review').hidden = true; $('program-review').disabled = true;
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
    void loadProgramContext(researchContext(ledger)).catch(() => {});
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
    container.append(element('span', 'No selected context. Open Experiments or Plans & decisions, choose “Include experiment in coordinator context” or “Include plan in coordinator context”, then return to Conversation.', 'muted'));
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
function validCoordinatorConsumerStatus(status) {
  return status && status.provider_disabled === true && status.provider_enabled === false &&
    ['available', 'unavailable'].includes(status.availability) &&
    ['healthy', 'stale', 'revoked', 'expired', 'unknown', 'unavailable'].includes(status.health) &&
    ['fresh', 'stale', 'unknown'].includes(status.freshness) &&
    ['active', 'revoked', 'expired', 'unknown', 'not_recorded'].includes(status.credential_status) &&
    (status.heartbeat_at === null || isText(status.heartbeat_at)) &&
    (status.credential_expires_at === null || isText(status.credential_expires_at)) &&
    (status.last_outcome === null || ['idle', 'claimed', 'completed', 'retrying'].includes(status.last_outcome)) &&
    Number.isSafeInteger(status.observed_consumers) && status.observed_consumers >= 0 && status.observed_consumers <= 100;
}
function applyCoordinatorConsumerStatus(status, project, version, requestVersion) {
  if (version !== epoch || project !== activeProject || requestVersion !== readVersion.workbench) return;
  if (!validCoordinatorConsumerStatus(status)) {
    coordinatorAvailable = false;
    coordinatorConsumerDetail = null;
    $('coordinator-health').textContent = 'Coordinator consumer status is incomplete or unrecognized. New coordinator requests are disabled.';
    applyConnectionHealth(connectionState);
    throw new Error('The control plane did not return a safe coordinator consumer status.');
  }
  coordinatorAvailable = status.availability === 'available' && status.health === 'healthy' &&
    status.freshness === 'fresh' && status.credential_status === 'active';
  coordinatorConsumerDetail = status;
  applyConnectionHealth(connectionState);
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
    const [conversation, proposals, statusTurns, labbook, consumer] = await Promise.all([
      messagePage ? api.astraConversation(connectionId, project.id, messagePage) : Promise.resolve({ messages: [] }),
      proposalPage ? api.astraProposals(connectionId, project.id, proposalPage) : Promise.resolve({ proposals: [] }),
      statusTurnPage ? api.explainStatusTurns(connectionId, project.id, statusTurnPage) : Promise.resolve({ turns: [] }),
      api.labbook(connectionId, project.id, eventCursor),
      api.coordinatorConsumerStatus(connectionId, project.id),
    ]);
    if (version !== epoch || project !== activeProject || requestVersion !== readVersion.workbench) return;
    if (!conversation || !Array.isArray(conversation.messages) || !Array.isArray(proposals.proposals) || !Array.isArray(statusTurns.turns) || !Array.isArray(labbook.events)) {
      throw new Error('The control plane did not return a complete coordinator projection.');
    }
    applyCoordinatorConsumerStatus(consumer, project, version, requestVersion);
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
$('admin-open').addEventListener('click', () => guarded(async () => {
  $('admin-workspace').hidden = false; $('workspace').hidden = true; $('welcome').hidden = true;
  $('workspace-view-title').textContent = 'Administration'; renderAdmin(); await loadAdmin();
}));
$('admin-return').addEventListener('click', () => {
  clearAdmin(); $('workspace').hidden = !activeProject; $('welcome').hidden = Boolean(activeProject); selectWorkspaceView(workspaceView);
});
for (const view of Object.keys(workspaceViews)) $(`view-${view}`).addEventListener('click', () => selectWorkspaceView(view));
// Native dialog Escape/cancel behavior and focus restoration are retained. Give
// every modal a programmatic name, including dynamically titled review dialogs.
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('close', () => clearDialogNotice(dialog));
  const heading = dialog.querySelector('h2');
  if (heading) { if (!heading.id) heading.id = `${dialog.id}-heading`; dialog.setAttribute('aria-labelledby', heading.id); }
}
for (const group of document.querySelectorAll('#experiment-form fieldset')) {
  const reference = group.querySelector('input[name$="_task_id"]');
  const digest = group.querySelector('input[name$="_plan_digest"]');
  if (reference && digest) {
    const details = disclosure('Optional execution reference — no execution authority');
    details.append(reference.closest('label'), digest.closest('label')); group.append(details);
  }
}
$('admin-refresh').addEventListener('click', () => guarded(() => loadAdmin()));
$('admin-more').addEventListener('click', () => guarded(() => loadAdmin(true)));
for (const view of ['setup', 'targets', 'secrets', 'delegations', 'readiness', 'audit']) {
  $(`admin-${view}`).addEventListener('click', () => guarded(async () => {
    adminView = view; adminPending = null; $('admin-preview').hidden = true;
    $('admin-preview-body').textContent = ''; renderAdmin(); await loadAdmin();
  }));
}
$('admin-confirm').addEventListener('click', () => guarded(confirmAdmin));
$('admin-cancel').addEventListener('click', () => {
  adminPending = null; $('admin-preview').hidden = true; $('admin-preview-body').textContent = '';
});
$('admin-configuration-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const fields = new FormData(event.target);
    const spec = { kind: 'configuration', custody_policy: 'quarantine_only', data_policy: 'public_released_only' };
    for (const name of ['component', 'provider', 'deployment', 'network_policy']) spec[name] = fields.get(name);
    for (const name of ['endpoint', 'namespace', 'salad_organization', 'salad_project', 'country', 'cluster_identity', 'ca_digest', 'kubeconfig_secret_id', 'management_secret_id', 'gateway_secret_id', 'artifact_secret_id', 'image_digest', 'artifact_profile_digest', 'monitor_profile_digest']) spec[name] = fields.get(name) || null;
    for (const name of ['max_cpu', 'max_gpu', 'max_memory_mib', 'max_seconds', 'max_output_bytes']) spec[name] = Number(fields.get(name));
    if (adminEditing && adminCurrent(adminEditing)) {
      adminPreview({ expected_revision: adminEditing.record.revision, action: 'revise', configuration: spec }, adminEditing.record);
    } else adminPreview({ expected_revision: 0, spec });
  });
});
$('admin-secret-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const fields = new FormData(event.target), purpose = fields.get('purpose');
    const [provider, audience] = { salad_management: ['salad', 'server_proxy'], ai_gateway: ['ai_gateway', 'gateway'], kubeconfig: ['kubernetes', 'operator'], artifact_store: ['artifact_store', 'artifact_service'], monitor: ['none', 'monitor'] }[purpose];
    adminPreview({ expected_revision: 0, spec: { kind: 'secret', backend: fields.get('backend') || 'fake', purpose, provider, audience, expires_at: new Date(Date.now() + Number(fields.get('minutes')) * 60000).toISOString() } });
  });
});
$('admin-delegation-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const fields = new FormData(event.target);
    const actions = fields.getAll('action');
    if (!actions.length || actions.some(action => !['configuration.read', 'configuration.propose', 'readiness.read'].includes(action))) throw new Error('Select at least one exact administration action.');
    const expires = Date.now() + Number(fields.get('minutes')) * 60000;
    if (fields.get('subject_id') === connectionState?.principalId && connectionState?.expiresAt && expires > Date.parse(connectionState.expiresAt)) throw new Error('Delegation cannot outlive your credential. Shorten its lifetime.');
    adminPreview({ expected_revision: 0, spec: { kind: 'delegation', subject_id: fields.get('subject_id'), subject_role: fields.get('subject_role'), target_id: fields.get('target_id'), provider: fields.get('provider'), resource: 'configuration', actions, expires_at: new Date(expires).toISOString() } });
  });
});
for (const [id, mode] of [['admin-issue', 'new'], ['admin-issue-resume', 'resume']]) {
  $(id).addEventListener('click', () => guarded(async () => {
    if (!canMutate() || !adminState?.root_admin || adminState.held || adminBusy) throw new Error('Current root administration is required.');
    const context = adminContext();
    const minutes = Number($('admin-issuance-minutes').value);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) throw new Error('Choose 1–1440 minutes.');
    adminBusy = true; $('admin-issue').disabled = true; $('admin-issue-resume').disabled = true;
    try {
      const result = await api.issueOperatorControlCredential(context.connectionId, context.projectId, mode, minutes);
      if (!adminCurrent(context)) return;
      $('admin-issuance-status').textContent = result.canceled ? 'Canceled; no credential request sent.' : `Issued principal ${result.principal_id}; expires ${result.expires_at}. Private token file: ${result.token_file}. Disconnect, then Connect with that file to propose your own exact-record delegation. Root reviews separately.`;
    } finally {
      if (adminCurrent(context)) { adminBusy = false; $('admin-issue').disabled = false; $('admin-issue-resume').disabled = false; }
    }
  }));
}
$('connection-open').addEventListener('click', () => $('connection-dialog').showModal());
function openCreationDialog(kind) {
  const dialog = $(`${kind}-dialog`);
  if (dialog.open) return; // A repeated open cannot undo an in-flight submission.
  $(`${kind}-form`).querySelector('button[type="submit"]').disabled = !canMutate();
  dialog.showModal();
}
$('project-new').addEventListener('click', () => openCreationDialog('project'));
$('welcome-project-new').addEventListener('click', () => openCreationDialog('project'));
$('task-new').addEventListener('click', () => openCreationDialog('task'));
$('thread-new').addEventListener('click', () => openCreationDialog('thread'));
$('study-new').addEventListener('click', () => openCreationDialog('study'));
$('variant-new').addEventListener('click', () => openCreationDialog('variant'));
$('disconnect').addEventListener('click', () => guarded(async () => {
  clearContext(); await api.disconnect(); $('connection-status').textContent = 'Not connected';
}));
let connectionPending = false;
$('connection-form').addEventListener('submit', event => {
  event.preventDefault();
  if (connectionPending) return;
  const button = event.target.querySelector('button[type="submit"]');
  connectionPending = true;
  button.disabled = true;
  guarded(async () => {
    try {
      const fields = new FormData(event.target);
      const settings = { name: fields.get('name'), endpoint: fields.get('endpoint') };
      for (const dialog of document.querySelectorAll('dialog')) clearDialogNotice(dialog);
      // Failed enrollment must discard the previous connection context, but the
      // operator's endpoint and name are safe retry inputs and should remain.
      clearContext({ preserveConnectionForm: true });
      const version = epoch;
      const state = await api.connect(settings);
      if (version !== epoch) return;
      if (!state) { $('connection-status').textContent = 'Not connected'; return; }
      connectionId = state.id;
      applyConnectionHealth(state);
      $('connection-dialog').close();
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
    } finally { connectionPending = false; button.disabled = false; }
  }, { preserveConnectionForm: true });
});
$('project-form').addEventListener('submit', event => {
  event.preventDefault();
  guarded(async () => {
    const form = event.target;
    const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    const version = epoch;
    const selectedConnection = connectionId;
    const body = { name: new FormData(form).get('name') };
    button.disabled = true;
    try {
      const project = await command('project', body, (id, key) => api.createProject(id, body, key),
        () => version === epoch && selectedConnection === connectionId);
      if (version !== epoch || selectedConnection !== connectionId) return;
      $('project-dialog').close(); form.reset();
      await loadProjects(true);
      if (version === epoch && selectedConnection === connectionId) await selectProject(project);
    } finally {
      if (version === epoch && selectedConnection === connectionId && form.isConnected) {
        button.disabled = !canMutate();
      }
    }
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
$('task-revise-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = taskRevisionContext; const form = event.target; const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    if (!context || context.version !== epoch || context.project !== activeProject) throw new Error('Project context changed; refresh the proposal before revising it.');
    const fields = new FormData(form);
    const array = (name) => {
      let value; try { value = JSON.parse(String(fields.get(name) ?? '')); } catch { throw new Error(`${name} must be a JSON array.`); }
      if (!Array.isArray(value)) throw new Error(`${name} must be a JSON array.`); return value;
    };
    const body = { expected_revision: context.task.revision, title: fields.get('title'), purpose: fields.get('purpose'),
      budget_cents: Number(fields.get('budget_cents')), max_duration_seconds: Number(fields.get('max_duration_seconds')),
      execution_spec: { argv: array('argv'), input_snapshot_digest: fields.get('input_snapshot_digest'),
        pinned_skill_digests: array('pinned_skill_digests'), output_paths: array('output_paths'), profile: fields.get('profile'),
        network_enabled: false, max_output_bytes: Number(fields.get('max_output_bytes')) } };
    button.disabled = true;
    try {
      await command(`revise-task:${context.task.id}`, body, (id, key) => api.reviseTaskPlan(id, context.task.id, body, key),
        () => context.version === epoch && context.project === activeProject);
      $('task-revise-dialog').close(); form.reset(); await selectProject(context.project);
      notice('Proposal revision recorded. The prior approval basis is invalidated; no admission, dispatch, or run occurred.');
    } finally { if (context.version === epoch && context.project === activeProject) button.disabled = !canMutate(); }
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
$('experiment-new').addEventListener('click', () => guarded(async () => {
  experimentCommandContext = null; fillExperimentForm(null); $('experiment-form-title').textContent = 'Register experiment design';
  $('experiment-form').querySelector('button[type="submit"]').textContent = 'Record design'; $('experiment-dialog').showModal();
}));
$('experiment-revise').addEventListener('click', () => guarded(async () => {
  const context = experimentContext();
  if (!currentExperimentContext(context)) throw new Error('Select a complete current experiment before revising its design.');
  experimentCommandContext = context; fillExperimentForm(context.ledger.experiment.design);
  $('experiment-form-title').textContent = `Revise experiment design revision ${context.ledger.experiment.current_design_revision}`;
  $('experiment-form').querySelector('button[type="submit"]').textContent = 'Record revised design'; $('experiment-dialog').showModal();
}));
$('experiment-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const form = event.target; const button = form.querySelector('button[type="submit"]'); if (button.disabled) return;
    const context = experimentCommandContext; const project = activeProject;
    if (!project || (context && !currentExperimentContext(context))) throw new Error('Refresh the selected project before recording this design.');
    const body = designFormBody(form, context ? context.ledger.experiment.current_design_revision : null); button.disabled = true;
    try {
      const result = await command(context ? `revise-experiment:${context.experiment.id}` : `create-experiment:${project.id}`, body,
        (id, key) => context ? api.reviseExperiment(id, context.experiment.id, body, key) : api.createExperiment(id, project.id, body, key),
        () => context ? currentExperimentContext(context) : project === activeProject);
      $('experiment-dialog').close(); form.reset(); await loadExperiments(true);
      if (context) await selectExperiment(context.experiment); else await selectExperiment(result);
      notice('Experiment design recorded. A design revision does not approve, admit, dispatch, or run work.');
    } finally { button.disabled = !canMutate(); }
  });
});
$('admission-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = experimentCommandContext; const variant = context?.variant;
    if (!currentExperimentContext(context) || !variant || !isPositiveInteger(variant.revision)) throw new Error('Refresh the selected variant before admission.');
    const body = { expected_variant_revision: variant.revision, expected_design_revision: context.ledger.experiment.current_design_revision,
      grant_id: new FormData(event.target).get('grant_id') };
    await command(`admit-attempt:${variant.id}`, body, (id, key) => api.admitExperimentAttempt(id, variant.id, body, key), () => currentExperimentContext(context));
    $('admission-dialog').close(); await refreshExperimentContext(context);
    notice('Execution admission recorded. No package was returned, dispatch authorized, or run started.');
  });
});
$('dispatch-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = experimentCommandContext; const attempt = context?.attempt;
    if (!currentExperimentContext(context) || !attempt) throw new Error('Refresh the admitted attempt before dispatch review.');
    const body = { expected_revision: attempt.revision, target_digest: new FormData(event.target).get('target_digest'), profile_version: 'development-minikube-opencode-v1' };
    await command(`dispatch-attempt:${attempt.id}`, body, (id, key) => api.dispatchAdmittedAttempt(id, attempt.id, body, key), () => currentExperimentContext(context));
    $('dispatch-dialog').close(); await refreshExperimentContext(context);
    notice('Kubernetes dispatch authorization recorded. It does not prove that a worker claimed or ran the package.');
  });
});
$('attempt-resolution-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = experimentCommandContext; const attempt = context?.attempt; const fields = new FormData(event.target);
    if (!currentExperimentContext(context) || !attempt) throw new Error('Refresh the admitted attempt before recording an outcome.');
    const outcome = fields.get('outcome'); const reason = String(fields.get('reason') ?? '').trim() || null;
    if (outcome !== 'succeeded' && !reason) throw new Error('Unknown, failed, and cancelled outcomes require a reason.');
    const body = { expected_revision: attempt.revision, outcome, reason };
    await command(`resolve-attempt:${attempt.id}`, body, (id, key) => api.resolveAdmittedAttempt(id, attempt.id, body, key), () => currentExperimentContext(context));
    $('attempt-resolution-dialog').close(); await refreshExperimentContext(context);
    notice('Human admitted-attempt outcome recorded. It does not establish scientific success.');
  });
});
$('measurement-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = experimentCommandContext; const attempt = context?.attempt; const fields = new FormData(event.target);
    if (!currentExperimentContext(context) || !attempt) throw new Error('Refresh the attempt before recording a measurement.');
    let value; try { value = JSON.parse(String(fields.get('value') ?? '')); } catch { throw new Error('Measurement value must be a JSON object.'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Measurement value must be a JSON object.');
    const body = { expected_revision: attempt.revision, name: fields.get('name'), value, artifact_id: String(fields.get('artifact_id') ?? '').trim() || null };
    await command(`measurement:${attempt.id}`, body, (id, key) => api.recordMeasurement(id, attempt.id, body, key), () => currentExperimentContext(context));
    $('measurement-dialog').close(); await refreshExperimentContext(context);
    notice('Measurement recorded as evidence metadata. It does not establish scientific validity.');
  });
});
$('attempt-artifacts-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = experimentCommandContext; const attempt = context?.attempt;
    if (!currentExperimentContext(context) || !attempt) throw new Error('Refresh the attempt before linking artifact evidence.');
    let artifactIds; try { artifactIds = JSON.parse(String(new FormData(event.target).get('artifact_ids') ?? '')); } catch { throw new Error('Artifact IDs must be a JSON array.'); }
    if (!Array.isArray(artifactIds) || !artifactIds.length || artifactIds.some(id => !isIdentifier(id)) || new Set(artifactIds).size !== artifactIds.length) {
      throw new Error('Provide one or more unique artifact IDs.');
    }
    const body = { expected_revision: attempt.revision, artifact_ids: artifactIds };
    await command(`link-attempt-artifacts:${attempt.id}`, body, (id, key) => api.linkAttemptArtifacts(id, attempt.id, body, key), () => currentExperimentContext(context));
    $('attempt-artifacts-dialog').close(); await refreshExperimentContext(context);
    notice('Artifact evidence links recorded. Bytes remain subject to separate released-manifest custody.');
  });
});
$('conclusion-new').addEventListener('click', () => guarded(async () => openConclusionForm(experimentContext())));
$('conclusion-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = conclusionContext; const conclusion = context?.conclusion;
    if (!currentExperimentContext(context)) throw new Error('Refresh the complete experiment ledger before authoring a conclusion.');
    const evidence = selectedConclusionEvidence(); if (!evidence.length) throw new Error('Select linked artifact or measurement evidence for the conclusion.');
    const fields = new FormData(event.target); const body = { statement: fields.get('statement'), evidence,
      design_revision: conclusion?.design_revision ?? context.ledger.experiment.current_design_revision,
      ...(conclusion ? { expected_revision: conclusion.revision } : {}) };
    await command(conclusion ? `update-conclusion:${conclusion.id}` : `create-conclusion:${context.experiment.id}`, body,
      (id, key) => conclusion ? api.updateConclusion(id, conclusion.id, body, key) : api.createConclusion(id, context.experiment.id, body, key),
      () => currentExperimentContext(context));
    $('conclusion-dialog').close(); await refreshExperimentContext(context);
    notice('Conclusion draft recorded with linked evidence. It remains distinct from execution and scientific validity.');
  });
});
$('conclusion-amendment-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = amendmentContext; const conclusion = context?.conclusion; const fields = new FormData(event.target);
    if (!currentExperimentContext(context) || !conclusion) throw new Error('Refresh the reviewed conclusion before amendment.');
    const body = { expected_revision: conclusion.lineage_revision, expected_design_revision: conclusion.design_revision, relation: fields.get('relation'), reason: fields.get('reason') };
    await command(`amend-conclusion:${conclusion.id}`, body, (id, key) => api.amendConclusion(id, conclusion.id, body, key), () => currentExperimentContext(context));
    $('conclusion-amendment-dialog').close(); await refreshExperimentContext(context);
    notice('Conclusion amendment recorded. The reviewed conclusion and evidence remain preserved.');
  });
});
  $('artifact-custody').addEventListener('click', () => guarded(async () => {
    if (!activeProject) throw new Error('Select a project before recording custody release.');
    $('artifact-custody-form').reset(); $('artifact-custody-dialog').showModal();
  }));
  $('artifact-custody-form').addEventListener('submit', event => {
    event.preventDefault(); guarded(async () => {
      const project = activeProject; const version = epoch; const fields = new FormData(event.target);
      if (!project) throw new Error('Select a project before recording custody release.');
      const revision = Number(fields.get('expected_revision'));
      if (!Number.isSafeInteger(revision) || revision < 0) throw new Error('Custody revision must be a non-negative integer.');
      const manifestId = String(fields.get('manifest_id') ?? '').trim();
      const body = { expected_manifest_digest: String(fields.get('expected_manifest_digest') ?? '').trim(),
        expected_revision: revision, action: fields.get('action'), rationale: String(fields.get('rationale') ?? '').trim() };
      if (!isIdentifier(manifestId) || !isDigest(body.expected_manifest_digest) || !['release', 'revoke'].includes(body.action) || !body.rationale) {
        throw new Error('Enter a manifest ID, exact digest, current revision, action, and rationale.');
      }
      await command(`artifact-custody:${manifestId}`, body,
        (id, key) => api.recordArtifactCustodyRelease(id, manifestId, body, key),
        () => version === epoch && project === activeProject);
      if (version === epoch && project === activeProject) {
        $('artifact-custody-dialog').close(); event.target.reset();
        notice(`Artifact custody ${body.action} recorded. This does not classify, accept, publish, or alter evidence.`);
      }
    });
  });
  $('artifact-disposition').addEventListener('click', () => guarded(async () => {
    if (!activeProject || !canMutate() || connectionState?.role !== 'human') throw new Error('Select an online human project for disposition review.');
    artifactDispositionContext = null;
    artifactDispositionRead++;
    $('artifact-disposition-form').reset(); $('artifact-disposition-detail').textContent = 'Load an exact manifest from this project before reviewing.';
    $('artifact-disposition-form').querySelector('button[type="submit"]').disabled = true;
    $('artifact-disposition-dialog').showModal();
  }));
  $('artifact-disposition-load').addEventListener('click', () => guarded(async () => {
    const form = $('artifact-disposition-form');
    const manifestId = String(new FormData(form).get('manifest_id') ?? '').trim();
    const project = activeProject; const version = epoch; const read = ++artifactDispositionRead;
    artifactDispositionContext = null; form.querySelector('button[type="submit"]').disabled = true;
    if (!project || !isIdentifier(manifestId)) throw new Error('Enter a manifest ID from the selected project.');
    let manifest = null;
    for (let offset = 0; offset < 1000 && !manifest; offset += 100) {
      const page = await api.artifactManifests(connectionId, project.id, { offset, limit: 100 });
      if (version !== epoch || project !== activeProject || read !== artifactDispositionRead) return;
      if (!Array.isArray(page?.artifact_manifests)) throw new Error('Manifest metadata is incomplete.');
      manifest = page.artifact_manifests.find(item => item.id === manifestId);
      if (page.artifact_manifests.length < 100) break;
    }
    if (!manifest || manifest.project_id !== project.id || manifest.status !== 'quarantined' || !isDigest(manifest.manifest_digest) || !Array.isArray(manifest.artifacts) || !manifest.artifacts.every(a => isIdentifier(a.id) && isDigest(a.sha256) && isText(a.relative_path) && Number.isSafeInteger(a.size) && a.size >= 0)) throw new Error('No eligible exact quarantine manifest was found in this project.');
    const rows = [];
    for (let offset = 0; offset < 1000; offset += 100) {
      const page = await api.artifactDispositions(connectionId, manifestId, { offset, limit: 100 });
      if (version !== epoch || project !== activeProject || read !== artifactDispositionRead) return;
      if (!Array.isArray(page?.dispositions) || !page.dispositions.every(row => row.manifest_id === manifestId && row.manifest_digest === manifest.manifest_digest && isPositiveInteger(row.revision))) throw new Error('Disposition metadata is incomplete.');
      rows.push(...page.dispositions);
      if (page.dispositions.length < 100) break;
      if (offset === 900) throw new Error('Disposition history exceeds the bounded review; use the operator API.');
    }
    if (!$('artifact-disposition-dialog').open || read !== artifactDispositionRead) return;
    const revision = Math.max(0, ...rows.map(row => row.revision));
    artifactDispositionContext = { project, version, manifest, revision };
    const set = (name, value) => { if (form.elements?.[name]) form.elements[name].value = value; else { form.fields ??= {}; form.fields[name] = value; } };
    set('expected_manifest_digest', manifest.manifest_digest); set('expected_revision', revision);
    const review = $('artifact-disposition-detail');
    review.replaceChildren(element('p', `Quarantine ${manifest.status} · classification ${manifest.classification} · late ${manifest.late}`, 'tag'),
      element('p', 'Scientific validity: not assessed. Retention review does not release custody.', 'muted'));
    for (const artifact of manifest.artifacts) review.append(recordFields([
      ['Output', artifact.relative_path], ['Artifact ID', artifact.id], ['Size', `${artifact.size} bytes`], ['sha256', artifact.sha256],
    ]));
    form.querySelector('button[type="submit"]').disabled = !canMutate() || connectionState?.role !== 'human';
  }));
  $('artifact-disposition-form').addEventListener('submit', event => {
    event.preventDefault(); guarded(async () => {
      const context = artifactDispositionContext; const form = event.target; const fields = new FormData(form);
      if (!context || !$('artifact-disposition-dialog').open || context.project !== activeProject || context.version !== epoch || !canMutate() || connectionState?.role !== 'human' || form.querySelector('button[type="submit"]').disabled) throw new Error('Refresh the exact human disposition review before recording.');
      if (fields.get('manifest_id') !== context.manifest.id || fields.get('expected_manifest_digest') !== context.manifest.manifest_digest || Number(fields.get('expected_revision')) !== context.revision) throw new Error('Reviewed manifest basis changed; reload exact metadata.');
      const rationale = String(fields.get('rationale') ?? '').trim();
      if (!rationale || rationale.length > 8000) throw new Error('Enter a bounded human review rationale.');
      const body = { expected_manifest_digest: context.manifest.manifest_digest, expected_revision: context.revision, disposition: 'retain_quarantined', rationale };
      const button = form.querySelector('button[type="submit"]'); button.disabled = true;
      try {
        await command(`artifact-disposition:${context.manifest.id}`, body, (id, key) => api.recordArtifactDisposition(id, context.manifest.id, body, key), () => context.project === activeProject && context.version === epoch);
        $('artifact-disposition-dialog').close(); artifactDispositionContext = null;
        notice('Human retain-quarantined disposition recorded. Bytes remain quarantined; separately review custody release. No classification, scientific acceptance, publication or agent disclosure.');
      } finally { if (artifactDispositionContext === context) button.disabled = !canMutate(); }
    });
  });
  $('artifact-review').addEventListener('click', () => guarded(async () => {
    if (!activeProject) throw new Error('Select a project before inspecting a released artifact.');
    artifactReviewContext = { project: activeProject, version: epoch, request: ++artifactPreviewRequest };
    $('artifact-form').reset(); $('artifact-review-detail').textContent = 'Enter an already released manifest and one artifact ID.';
    $('artifact-download').disabled = true; $('artifact-bundle').disabled = true; $('artifact-dialog').showModal();
  }));
$('artifact-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = artifactReviewContext; const fields = new FormData(event.target); const manifestId = String(fields.get('manifest_id') ?? '').trim(); const artifactId = String(fields.get('artifact_id') ?? '').trim();
    if (!context || context.project !== activeProject || context.version !== epoch) throw new Error('Project context changed; reopen artifact review.');
    if (!isIdentifier(manifestId) || !isIdentifier(artifactId)) throw new Error('Enter an already released manifest ID and artifact ID.');
    const request = ++artifactPreviewRequest;
    const previewContext = { ...context, request };
    artifactReviewContext = previewContext;
    $('artifact-download').disabled = true; $('artifact-bundle').disabled = true;
    const submit = event.target.querySelector('button[type="submit"]'); submit.disabled = true;
    try {
    const [released, preview] = await Promise.all([api.releasedArtifactManifest(connectionId, manifestId), api.artifactPreview(connectionId, manifestId, artifactId, 4096)]);
    if (artifactReviewContext !== previewContext || context.project !== activeProject || context.version !== epoch) return;
    if (!released?.manifest || released.manifest.id !== manifestId || released.manifest.project_id !== context.project.id ||
        preview?.artifact_id !== artifactId || !isText(preview.relative_path) || !isDigest(preview.sha256) ||
        !Number.isSafeInteger(preview.size) || preview.size < 0 || !Number.isSafeInteger(preview.preview_bytes) ||
        preview.preview_bytes < 0 || preview.preview_bytes > preview.size || typeof preview.truncated !== 'boolean' ||
        typeof preview.preview_base64 !== 'string' || preview.status !== 'quarantined' || preview.classification !== 'unclassified') {
      throw new Error('The control plane did not return a released quarantined artifact preview for this project.');
    }
    let text = '(binary preview omitted)';
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(atob(preview.preview_base64), char => char.charCodeAt(0))); } catch {}
    let formatted = text;
    if (!preview.truncated) { try { formatted = JSON.stringify(JSON.parse(text), null, 2); } catch {} }
    $('artifact-review-detail').replaceChildren(
      element('p', `Quarantine ${preview.status} · classification ${preview.classification} · late ${recordedOrUnknown(released.manifest.late ?? null)}`, 'tag'),
      element('p', 'Custody: released for private researcher retrieval. Scientific validity: not assessed.', 'muted'),
      recordFields([['Artifact', preview.relative_path], ['Preview', `${preview.preview_bytes}/${preview.size} bytes${preview.truncated ? ' (truncated)' : ''}`], ['sha256', preview.sha256]]),
      element('pre', formatted));
    artifactReviewContext = { ...previewContext, manifestId, artifactId, sha256: preview.sha256 }; $('artifact-download').disabled = !connectionOnline; $('artifact-bundle').disabled = !canMutate();
    } finally {
      if (artifactReviewContext === previewContext && context.project === activeProject && context.version === epoch) submit.disabled = !connectionOnline;
    }
  });
});
$('artifact-download').addEventListener('click', () => guarded(async () => {
  const context = artifactReviewContext; if (!context?.manifestId || !context?.artifactId || context.project !== activeProject || context.version !== epoch) throw new Error('Load a current released artifact preview first.');
  const button = $('artifact-download');
  if (button.disabled) return;
  const current = () => artifactReviewContext === context && context.project === activeProject && context.version === epoch;
  button.disabled = true;
  try {
    const result = await api.saveReleasedArtifact(connectionId, context.manifestId, context.artifactId);
    if (!current() || result.status === 'stale') return;
    if (result.status === 'cancelled') { notice('Artifact retrieval cancelled.'); return; }
    if (result.status !== 'saved' || result.sha256 !== context.sha256) throw new Error('Released artifact retrieval integrity check failed.');
    notice(`Saved exact released artifact bytes: ${result.basename}`);
  } catch (error) {
    if (current()) throw error;
  } finally {
    if (current()) {
      // Finish the busy state before caching current eligibility under offline health.
      unavailableControls.delete(button);
      button.disabled = false;
      applyAvailability(button, !connectionOnline);
    }
  }
}));
$('artifact-bundle').addEventListener('click', () => guarded(async () => {
    const context = artifactReviewContext;
    if (!context?.manifestId || context.project !== activeProject || context.version !== epoch) throw new Error('Load a current released artifact preview first.');
    const body = { max_bytes: 4 * 1024 * 1024 };
    const exported = await command(`reproduction-bundle:${context.manifestId}`, body,
      (id, key) => api.exportReproductionBundle(id, context.manifestId, body, key),
      () => context.project === activeProject && context.version === epoch && artifactReviewContext === context);
    if (context.project === activeProject && context.version === epoch && artifactReviewContext === context) {
      if (!exported || !isIdentifier(exported.id) || !isDigest(exported.bundle_digest) || !exported.bundle) throw new Error('The control plane did not return a bounded reproduction bundle record.');
      notice(`Reproduction bundle record exported: ${exported.id} · digest ${exported.bundle_digest}. Review its missing or restricted material before relying on it.`);
    }
  }));
$('program-review').addEventListener('click', () => guarded(async () => {
  const context = activeProgramContext;
  if (!programReviewCurrent(context)) throw new Error('Load the current complete program-review context before authoring.');
  renderProgramAuthoring(context); $('program-review-dialog').showModal();
}));
$('program-review-form').addEventListener('change', event => {
  if (event.target?.name === 'action') updateProgramAuthoringAction();
});
$('program-review-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const button = event.target.querySelector('button[type="submit"]'); if (button.disabled) return;
    const inputs = programAuthorInputs; const context = inputs?.context;
    if (!programReviewCurrent(context)) throw new Error('Program evidence changed or the selected study is no longer current. Reopen program review.');
    const fields = new FormData(event.target); const text = name => String(fields.get(name) ?? '').trim();
    const outcome = text('outcome'); const action = text('action'); const reason = text('reason');
    if (!['outcome_0', 'outcome_1'].includes(outcome) || !['plan', 'source_use', 'go_no_go'].includes(action) || !reason) {
      throw new Error('Choose an outcome and action, and provide a proposal reason.');
    }
    const scope = { type: 'research_program', version: 1, study_id: context.study.id, outcome, action,
      basis_digest: context.program.basis_digest, supersedes_id: null, reason };
    const lane = context.program.reviews.filter(review => review.scope?.outcome === outcome && review.scope?.action === action);
    const head = lane.find(review => review.is_proposal_head) ?? lane.at(-1);
    if (head) scope.supersedes_id = head.id;
    if (action === 'plan') {
      const required = ['objective', 'hypothesis', 'success_gate', 'failure_gate', 'stop_gate', 'target_domain', 'provenance_policy', 'resources', 'dataset_recommendation', 'representation_recommendation', 'split_recommendation', 'metric_recommendation', 'uncertainty'];
      const missing = required.filter(name => !text(name));
      const sourceIds = chosenProgramIds(inputs.planSources);
      const budget = Number(text('budget_cents')); const wall = Number(text('max_wall_seconds')); const gpu = Number(text('max_gpu_hours'));
      if (missing.length || !sourceIds.length || !Number.isSafeInteger(budget) || budget < 0 || !Number.isSafeInteger(wall) || wall < 1 || !Number.isFinite(gpu) || gpu < 0) {
        throw new Error('Complete every scientific-brief field, select registered data sources, and provide valid declared compute limits.');
      }
      const conditions = {};
      for (const [key, item] of Object.entries(inputs.conditions)) {
        if (item.outcome !== outcome) continue;
        const disposition = item.disposition.value; const rationale = String(item.rationale.value ?? '').trim(); const variantIds = chosenProgramIds(item.choices);
        if (!rationale || !['selected', 'deferred'].includes(disposition) || (disposition === 'selected') !== Boolean(variantIds.length)) {
          throw new Error(`Condition ${key} needs a rationale and selected conditions need registered variants while deferred conditions need none.`);
        }
        conditions[key] = { disposition, rationale, variant_ids: variantIds };
      }
      const expected = context.program.condition_catalog[outcome] ?? [];
      if (Object.keys(conditions).length !== expected.length) throw new Error('The complete selected Outcome condition catalog is required.');
      scope.brief = { objective: text('objective'), hypothesis: text('hypothesis'), success_gate: text('success_gate'), failure_gate: text('failure_gate'), stop_gate: text('stop_gate'), target_domain: text('target_domain'), provenance_policy: text('provenance_policy'), data_source_ids: sourceIds,
        compute: { budget_cents: budget, max_wall_seconds: wall, max_gpu_hours: gpu, target: text('target'), resources: text('resources') }, dataset_recommendation: text('dataset_recommendation'), representation_recommendation: text('representation_recommendation'), split_recommendation: text('split_recommendation'), metric_recommendation: text('metric_recommendation'), uncertainty: text('uncertainty'), missing_prerequisites: text('missing_prerequisites').split('\n').map(value => value.trim()).filter(Boolean) };
      scope.conditions = conditions;
    } else if (action === 'source_use') {
      const sourceIds = chosenProgramIds(inputs.useSources); const intendedUse = text('intended_use');
      if (!sourceIds.length || !intendedUse) throw new Error('Select the registered sources and state the intended use.');
      scope.source_ids = sourceIds; scope.intended_use = intendedUse;
    } else {
      const recommendation = text('recommendation'); const findingIds = chosenProgramIds(inputs.findingChoices);
      if (!['go', 'no_go'].includes(recommendation) || (recommendation === 'go' && !findingIds.length)) {
        throw new Error('Choose go or no-go; go requires selected human-reviewed finding evidence.');
      }
      scope.recommendation = recommendation; scope.finding_ids = findingIds;
    }
    const body = { project_id: context.project.id, task_id: null, kind: action === 'source_use' ? 'access' : 'direction', scope,
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(), priority: 50 };
    const retry = pendingProgramReview?.context === context && pendingProgramReview.outcome === outcome && pendingProgramReview.action === action ? pendingProgramReview : null;
    const commandBody = retry?.body ?? body;
    if (!retry) pendingProgramReview = { context, outcome, action, body: commandBody };
    button.disabled = true;
    try {
      await command(`program-review:${context.study.id}:${outcome}:${action}`, commandBody,
        (id, key) => api.createDecision(id, commandBody, key), () => programReviewCurrent(context));
      if (!programReviewCurrent(context)) return;
      pendingProgramReview = null;
      $('program-review-dialog').close(); event.target.reset();
      try {
        await loadProgramContext(context); await loadDecisions(true);
        notice('Program review request recorded for human review. It grants no data release, execution, or scientific conclusion.');
      } catch {
        notice('Program review request was recorded, but its refreshed history is unavailable. Refresh before authoring or resolving another review.');
      }
    } catch (error) {
      if (/\(409\)/.test(error.message)) {
        pendingProgramReview = null;
        throw new Error('Program evidence or proposal lineage is stale. Reinspect and author a complete replacement.');
      }
      if (/outcome is unknown/i.test(error.message)) {
        throw new Error('Program-review outcome is unknown. Reopen or retry this same request after inspecting current history; its command identifier and immutable basis are retained.');
      }
      pendingProgramReview = null;
      throw error;
    } finally { if (programReviewCurrent(context)) button.disabled = !canMutate(); }
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
    const busy = { version, project }; coordinatorBusy = busy;
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
    } finally {
      if (coordinatorBusy === busy) coordinatorBusy = null;
      if (version === epoch && project === activeProject) button.disabled = !canMutate() || !coordinatorAvailable;
    }
  });
});
$('conversation-view').addEventListener('click', () => { workbenchView = 'conversation'; renderWorkbenchEntries(); });
$('labbook-view').addEventListener('click', () => { workbenchView = 'labbook'; renderWorkbenchEntries(); });
$('refresh').addEventListener('click', () => guarded(async () => {
  await loadProjects(true); if (activeProject) await selectProject(activeProject);
}));
$('recovery-form').addEventListener('submit', event => {
  event.preventDefault(); guarded(async () => {
    const context = recoveryCommandContext;
    if (!context || context.version !== epoch || context.project !== activeProject || context.requestVersion !== readVersion.remoteWorkflows) {
      throw new Error('Remote workflow context changed; inspect recovery again.');
    }
    const grant = await api.executionGrant(connectionId, context.workflow.grant_id);
    if (!grant || grant.id !== context.workflow.grant_id || grant.project_id !== context.project.id || !isPositiveInteger(grant.revision)) {
      throw new Error('The current grant does not match this project recovery record.');
    }
    const body = { expected_grant_revision: grant.revision, expected_recovery_revision: context.record.revision,
      operation_id: context.workflow.operation_id, request_cancellation: true };
    await command(`recovery-cancel:${context.workflow.operation_id}`, body,
      (id, key) => api.reconcileLostWorkerOperation(id, context.workflow.grant_id, body, key),
      () => context.version === epoch && context.project === activeProject && context.requestVersion === readVersion.remoteWorkflows);
    if (context.version === epoch && context.project === activeProject) {
      $('recovery-dialog').close(); await loadRemoteWorkflows(true);
      notice('Recovery cancellation requested. Termination and exposure remain unresolved until independently reconciled evidence is recorded.');
    }
  });
});
$('projects-more').addEventListener('click', () => guarded(() => loadProjects()));
$('tasks-more').addEventListener('click', () => guarded(() => loadTasks()));
$('workers-more').addEventListener('click', () => guarded(() => loadWorkers()));
$('artifacts-more').addEventListener('click', () => guarded(() => loadArtifacts()));
$('events-more').addEventListener('click', () => guarded(() => loadEvents()));
$('threads-more').addEventListener('click', () => guarded(() => loadThreads()));
$('messages-more').addEventListener('click', () => guarded(() => loadMessages()));
$('decisions-more').addEventListener('click', () => guarded(() => loadDecisions()));
$('experiments-more').addEventListener('click', () => guarded(() => loadExperiments()));
$('remote-workflows-more').addEventListener('click', () => guarded(() => loadRemoteWorkflows()));
$('studies-more').addEventListener('click', () => guarded(() => loadStudies()));
 $('namespace-setup').addEventListener('click', () => {
   clearNamespaceSetup(); $('namespace-dialog').showModal();
 });
 $('namespace-kubeconfig-select').addEventListener('click', () => namespaceWork(selectNamespaceKubeconfig));
 $('namespace-form').addEventListener('submit', event => {
   event.preventDefault(); namespaceWork(inspectNamespace);
 });
 $('namespace-use-existing').addEventListener('click', () => namespaceWork(() => planNamespace('use-existing')));
 $('namespace-create-plan').addEventListener('click', () => namespaceWork(() => planNamespace('create-dedicated')));
 $('namespace-confirm-form').addEventListener('submit', event => {
   event.preventDefault(); namespaceWork(confirmNamespace);
 });
 $('namespace-reconcile').addEventListener('click', () => namespaceWork(reconcileNamespace));
if (!api) {
  $('connection-open').disabled = true;
  notice('Open this workspace in the Tessera desktop application.');
}
