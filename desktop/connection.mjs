/** Main-process connection boundary. Never export this object to the renderer. */
import { randomUUID } from 'node:crypto';
import { validControlToken } from './control-credential.mjs';

export class ConnectionError extends Error {}

export function normalizeEndpoint(value) {
  let url;
  try { url = new URL(value); } catch { throw new ConnectionError('Invalid control-plane URL.'); }
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
  if (!(url.protocol === 'https:' || (url.protocol === 'http:' && loopback)) ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new ConnectionError('Use HTTPS, or HTTP on loopback, without credentials or a path.');
  }
  return url.origin;
}

function resourceId(value) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(value)) {
    throw new ConnectionError('Invalid resource identifier.');
  }
  return encodeURIComponent(value);
}
function operationId(value) {
  if (typeof value !== 'string' || !/^[0-9a-f]{32}$/.test(value)) {
    throw new ConnectionError('Invalid operation identifier.');
  }
  return value;
}

function pageQuery({ offset = 0, limit = 100 } = {}) {
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 200) {
    throw new ConnectionError('Invalid page bounds.');
  }
  return `offset=${offset}&limit=${limit}`;
}
function interactionPageQuery(page) {
  const query = pageQuery(page);
  const params = new URLSearchParams(query);
  if (Number(params.get('offset')) > 100000 || Number(params.get('limit')) > 100) {
    throw new ConnectionError('Invalid page bounds.');
  }
  return query;
}

// Defense in depth before network serialization. There is intentionally no
// renderer plaintext input, generic JSON blob or locator. Protected native
// enrollment below has a separate closed metadata-only handoff.
function adminObject(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !keys.includes(key))) throw new ConnectionError('Invalid administration metadata.');
}
function adminChoice(value, choices) {
  if (!choices.includes(value)) throw new ConnectionError('Invalid administration metadata.');
}
function adminSpec(spec) {
  const providers = ['none', 'kubernetes', 'local_fixture', 'salad', 'ai_gateway', 'artifact_store'];
  const optional = ['endpoint', 'namespace', 'salad_organization', 'salad_project', 'country', 'cluster_identity', 'ca_digest', 'kubeconfig_secret_id', 'management_secret_id', 'gateway_secret_id', 'artifact_secret_id', 'image_digest', 'artifact_profile_digest', 'monitor_profile_digest'];
  if (spec?.kind === 'configuration') {
    adminObject(spec, ['kind', 'component', 'provider', 'deployment', ...optional, 'network_policy', 'custody_policy', 'data_policy', 'max_cpu', 'max_gpu', 'max_memory_mib', 'max_seconds', 'max_output_bytes']);
    adminChoice(spec.component, ['control_plane', 'execution_target', 'artifact_store', 'monitor', 'model_gateway']);
    if (spec.deployment !== undefined) adminChoice(spec.deployment, ['existing_local', 'remote_https', 'kubernetes_metadata']);
    if (spec.network_policy !== undefined) adminChoice(spec.network_policy, ['networkless', 'restricted_unverified', 'public_egress_unverified']);
    if (spec.custody_policy !== undefined) adminChoice(spec.custody_policy, ['quarantine_only']);
    if (spec.data_policy !== undefined) adminChoice(spec.data_policy, ['public_released_only']);
    for (const key of optional) {
      const value = spec[key];
      if (value === undefined || value === null) continue;
      if (key === 'endpoint') {
        normalizeEndpoint(value);
        if (spec.component !== 'control_plane') throw new ConnectionError('Invalid administration metadata.');
      } else if (typeof value !== 'string' || !(key.endsWith('_secret_id') ? /^admin_[a-f0-9]{32}$/ : ['namespace', 'salad_organization', 'salad_project'].includes(key) ? /^[a-z0-9][a-z0-9-]{0,62}$/ : key === 'country' ? /^[A-Z]{2}$/ : /^[a-f0-9]{64}$/).test(value)) {
        throw new ConnectionError('Invalid administration metadata.');
      }
    }
    for (const [key, min, max] of [['max_cpu', 1, 64], ['max_gpu', 0, 8], ['max_memory_mib', 128, 262144], ['max_seconds', 1, 86400], ['max_output_bytes', 0, 1073741824]]) {
      if (spec[key] !== undefined && (!Number.isSafeInteger(spec[key]) || spec[key] < min || spec[key] > max)) throw new ConnectionError('Invalid administration metadata.');
    }
  } else if (spec?.kind === 'secret') {
    adminObject(spec, ['kind', 'backend', 'purpose', 'provider', 'audience', 'expires_at']);
    adminChoice(spec.backend, ['fake', 'production_unavailable', 'os_keychain']);
    adminChoice(spec.purpose, ['salad_management', 'ai_gateway', 'kubeconfig', 'artifact_store', 'monitor']);
    adminChoice(spec.audience, ['server_proxy', 'gateway', 'operator', 'artifact_service', 'monitor']);
    adminExpiry(spec.expires_at);
  } else if (spec?.kind === 'delegation') {
    adminObject(spec, ['kind', 'subject_id', 'subject_role', 'target_id', 'provider', 'resource', 'actions', 'expires_at']);
    resourceId(spec.subject_id);
    if (typeof spec.target_id !== 'string' || !/^admin_[a-f0-9]{32}$/.test(spec.target_id)) throw new ConnectionError('Invalid administration metadata.');
    adminChoice(spec.subject_role, ['human', 'operator', 'agent', 'worker', 'proxy', 'monitor']);
    if (spec.resource !== undefined) adminChoice(spec.resource, ['configuration']);
    if (!Array.isArray(spec.actions) || spec.actions.length < 1 || spec.actions.length > 3 || new Set(spec.actions).size !== spec.actions.length) throw new ConnectionError('Invalid administration metadata.');
    for (const action of spec.actions) adminChoice(action, ['configuration.read', 'configuration.propose', 'readiness.read']);
    adminExpiry(spec.expires_at);
  } else throw new ConnectionError('Invalid administration metadata.');
  if (spec.provider !== undefined) adminChoice(spec.provider, providers);
}
function adminExpiry(value) {
  if (typeof value !== 'string' || value.length > 40 || !/^\d{4}-\d\d-\d\dT/.test(value) || !Number.isFinite(Date.parse(value))) throw new ConnectionError('Invalid administration metadata.');
}
function validateAdminCreate(body) {
  adminObject(body, ['expected_revision', 'spec']);
  if (body.expected_revision !== undefined && body.expected_revision !== 0) throw new ConnectionError('Invalid administration revision.');
  adminSpec(body.spec);
}
function validateAdminCommand(body) {
  adminObject(body, ['expected_revision', 'action', 'configuration', 'expires_at']);
  if (!Number.isSafeInteger(body.expected_revision) || body.expected_revision < 1) throw new ConnectionError('Invalid administration revision.');
  adminChoice(body.action, ['approve', 'reject', 'revoke', 'expire', 'delete', 'rotate', 'verify', 'enable', 'revise']);
  if (body.action === 'revise') {
    adminSpec(body.configuration);
    if (body.configuration.kind !== 'configuration') throw new ConnectionError('Invalid administration metadata.');
  } else if (body.configuration != null) throw new ConnectionError('Invalid administration metadata.');
  if (body.action === 'rotate') adminExpiry(body.expires_at);
  else if (body.expires_at != null) throw new ConnectionError('Invalid administration metadata.');
}

export class ControlConnection {
  #fetch;
  #active = null;
  #generation = 0;
  // Requests on one connection can settle out of order.  Only the newest
  // request may change the displayed health for that connection generation.
  #healthFreshness = 0;
  #pending = new Set();
  #secretController = null;
  #timeout;

  constructor(fetchImpl = globalThis.fetch, timeoutMs = 15000) {
    this.#fetch = fetchImpl;
    this.#timeout = timeoutMs;
  }

  disconnect() {
    this.#generation++;
    this.#healthFreshness = 0;
    for (const controller of this.#pending) controller.abort();
    this.#pending.clear();
    this.#active = null;
  }

  async connect({ id, name, endpoint, token }) {
    if (typeof id !== 'string' || !id || typeof name !== 'string' || !name.trim() || name.length > 200 ||
        !validControlToken(token)) {
      throw new ConnectionError('Invalid connection credentials.');
    }
    const origin = normalizeEndpoint(endpoint);
    this.disconnect();
    const generation = this.#generation;
    // Candidate credentials can authenticate only this endpoint. Failed connection
    // switches leave no active connection and cannot revive previous context.
    const candidate = { id, name, origin, token, generation };
    const identity = await this.#send(candidate, '/me');
    if (generation !== this.#generation) throw new ConnectionError('Connection changed.');
    if (typeof identity.principal_id !== 'string' || typeof identity.org_id !== 'string') {
      throw new ConnectionError('The endpoint did not return an authenticated identity.');
    }
    this.#active = { ...candidate, identity };
    return this.status();
  }

  captureOperationContext() {
    const active = this.#active;
    const generation = this.#generation;
    // Opaque guard: never return credentials or mutable connection internals.
    return () => active !== null && this.#active === active && this.#generation === generation;
  }

  status() {
    if (!this.#active) return null;
    const { id, name, origin, identity, health = 'online' } = this.#active;
    return { id, name, endpoint: origin, principalId: identity.principal_id, organizationId: identity.org_id,
      role: identity.role, projectId: identity.project_id ?? null, expiresAt: identity.expires_at ?? null, health };
  }

  #setHealth(connection, freshness, health) {
    if (freshness !== null && this.#active === connection &&
        connection.generation === this.#generation && freshness === this.#healthFreshness) {
      connection.health = health;
    }
  }

  assertActive(expectedId) {
    if (!this.#active || typeof expectedId !== 'string' || this.#active.id !== expectedId) {
      throw new ConnectionError('Connection changed; this command belongs to another context.');
    }
  }

  async #send(connection, path, body, key, method = body === undefined ? 'GET' : 'POST') {
    // `/me` authenticates a candidate before it becomes active, so it must not
    // affect the health of the currently selected connection.
    const healthFreshness = this.#active === connection ? ++this.#healthFreshness : null;
    const controller = new AbortController();
    this.#pending.add(controller);
    const timer = setTimeout(() => controller.abort(), this.#timeout);
    try {
      const response = await this.#fetch(connection.origin + path, {
        method,
        headers: { Authorization: `Bearer ${connection.token}`,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json', 'Idempotency-Key': key }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error', signal: controller.signal,
      });
      if (connection.generation !== this.#generation) throw new ConnectionError('Connection changed.');
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) this.disconnect();
        // The control plane uses 503 while its store is not ready (including a
        // schema incompatibility).  A responding process is not consequently a
        // usable backend, so do not leave a prior "online" indication in place.
        if (response.status === 503) this.#setHealth(connection, healthFreshness, 'offline');
        // No response bodies in errors: servers may include sensitive source data.
        const actions = {
          401: 'Reconnect with a current control-plane credential; the session may have expired or been revoked.',
          403: 'Reconnect with an authorized identity and check project/action scope. No approval was inferred.',
          404: 'Refresh the selected project; the record may be unavailable to this identity.',
          409: 'Refresh and inspect the current revision and history before retrying. No stale approval was applied.',
          422: 'Check the form fields and required execution specification, then submit again.',
          429: 'Wait before retrying; inspect command history first if a command was submitted.',
        };
        throw new ConnectionError(response.status === 503 ?
          'Control plane is unavailable or not ready (503). Check service readiness and schema compatibility, then reconnect.' :
          `Control request failed (${response.status}). ${actions[response.status] ?? 'Inspect service status and command history before retrying.'}`);
      }
      const declaredSize = Number(response.headers.get('content-length') ?? 0);
      if (declaredSize > 4 * 1024 * 1024) throw new ConnectionError('Control response is too large.');
      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          size += part.value.byteLength;
          if (size > 4 * 1024 * 1024) throw new ConnectionError('Control response is too large.');
          chunks.push(part.value);
        }
      } finally {
        await reader.cancel();
      }
      if (connection.generation !== this.#generation) throw new ConnectionError('Connection changed.');
      const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      this.#setHealth(connection, healthFreshness, 'online');
      return payload;
    } catch (error) {
      if (error instanceof ConnectionError) throw error;
      this.#setHealth(connection, healthFreshness, 'offline');
      throw new ConnectionError(body === undefined ? 'Control plane unavailable.' :
        'Command outcome is unknown. Reconnect to this connection and inspect its history before retrying.');
    } finally {
      clearTimeout(timer);
      this.#pending.delete(controller);
    }
  }

  #request(path, body, key) {
    if (!this.#active) throw new ConnectionError('Select an authenticated connection first.');
    if (body !== undefined && (typeof key !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(key))) {
      throw new ConnectionError('An explicit command identifier is required.');
    }
    return this.#send(this.#active, path, body, key);
  }
  #put(path, body, key) {
    if (!this.#active) throw new ConnectionError('Select an authenticated connection first.');
    if (typeof key !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(key)) {
      throw new ConnectionError('An explicit command identifier is required.');
    }
    return this.#send(this.#active, path, body, key, 'PUT');
  }

  projects(page) { return this.#request(`/projects?${pageQuery(page)}`); }
  recovery() { return this.#request('/recovery'); }
  adminWorkspace(projectId, page) { return this.#request(`/admin/projects/${resourceId(projectId)}/records?${interactionPageQuery(page)}`); }
  cancelProtectedSecrets() { this.#secretController?.abort(); }
  async protectedSecret(request, helper, contextCurrent) {
    const connection = this.#active;
    if (!connection || !['127.0.0.1', 'localhost', '[::1]'].includes(new URL(connection.origin).hostname)) throw new ConnectionError('Protected enrollment requires a same-host native control database; remote handoff is unavailable.');
    const controller = new AbortController();
    this.#secretController = controller;
    this.#pending.add(controller);
    const current = () => {
      if (controller.signal.aborted || this.#active !== connection || connection.generation !== this.#generation) throw new ConnectionError('Connection changed.');
      contextCurrent();
    };
    try {
      current();
      // The server-minted one-use approval ID must also exist in the helper's
      // database. Loopback alone is insufficient proof (including SSH tunnels).
      const metadata = await this.#request(`/admin/projects/${resourceId(request.project_id)}/records/${resourceId(request.record_id)}/enrollment`);
      current();
      if (metadata.revision !== request.expected_revision || metadata.enrollment_id !== request.enrollment_id || metadata.spec?.backend !== 'os_keychain') throw new ConnectionError('Stale protected enrollment metadata.');
      const result = await helper({ token: connection.token, request, signal: controller.signal, current });
      current();
      return result;
    } finally { controller.abort(); this.#pending.delete(controller); if (this.#secretController === controller) this.#secretController = null; }
  }
  adminCreate(projectId, body, key) { validateAdminCreate(body); return this.#request(`/admin/projects/${resourceId(projectId)}/records`, body, key); }
  adminCommand(projectId, recordId, body, key) { validateAdminCommand(body); return this.#request(`/admin/projects/${resourceId(projectId)}/records/${resourceId(recordId)}/commands`, body, key); }
  adminAudit(projectId, after = 0) {
    if (!Number.isSafeInteger(after) || after < 0) throw new ConnectionError('Invalid audit cursor.');
    return this.#request(`/admin/projects/${resourceId(projectId)}/audit?after=${after}&limit=100`);
  }
  autonomySessions(taskId) { return this.#request(`/tasks/${resourceId(taskId)}/autonomy-sessions`); }
  remoteExecutions(taskId) { return this.#request(`/tasks/${resourceId(taskId)}/remote-executions`); }
  remoteExecution(routeId) { return this.#request(`/remote-executions/${resourceId(routeId)}`); }
  remoteExecutionCommand(routeId, body, key) { return this.#request(`/remote-executions/${resourceId(routeId)}/commands`, body, key); }
  tasks(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/tasks?${pageQuery(page)}`); }
  workers(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/workers?${pageQuery(page)}`); }
  threads(projectId, page) { return this.#request(`/interaction/projects/${resourceId(projectId)}/threads?${interactionPageQuery(page)}`); }
  messages(threadId, page) { return this.#request(`/interaction/threads/${resourceId(threadId)}/messages?${interactionPageQuery(page)}`); }
  decisions(projectId, page) {
    return this.#request(`/interaction/projects/${resourceId(projectId)}/decisions?${interactionPageQuery(page)}&status=pending`);
  }
  programContext(studyId) { return this.#request(`/interaction/studies/${resourceId(studyId)}/program-context`); }
  astraConversation(projectId, page) {
    return this.#request(`/astra/projects/${resourceId(projectId)}/conversation?${interactionPageQuery(page)}`);
  }
  astraProposals(projectId, page) {
    return this.#request(`/astra/projects/${resourceId(projectId)}/proposals?${interactionPageQuery(page)}`);
  }
  coordinatorConsumerStatus(projectId) {
    return this.#request(`/astra/projects/${resourceId(projectId)}/consumer-status`);
  }
  requestCoordinatorTurn(projectId, body, key) {
    return this.#request(`/astra/projects/${resourceId(projectId)}/turns`, body, key);
  }
  explainStatusTurns(projectId, page) {
    return this.#request(`/astra/projects/${resourceId(projectId)}/explain-status-turns?${interactionPageQuery(page)}`);
  }
  requestExplainStatusTurn(projectId, body, key) {
    return this.#request(`/astra/projects/${resourceId(projectId)}/explain-status-turns`, body, key);
  }
  registerProposalDesign(proposalId, body, key) {
    return this.#request(`/astra/proposals/${resourceId(proposalId)}/register-design`, body, key);
  }
  experiments(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/experiments?${pageQuery(page)}`); }
  remoteWorkflows(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/remote-workflows?${pageQuery(page)}`); }
  remoteWorkflow(operationIdValue) { return this.#request(`/remote-workflows/${operationId(operationIdValue)}`); }
  executionGrant(grantId) { return this.#request(`/execution-grants/${resourceId(grantId)}`); }
  revokeExecutionGrant(grantId, body, key) { return this.#request(`/execution-grants/${resourceId(grantId)}/revoke`, body, key); }
  experiment(experimentId) { return this.#request(`/experiments/${resourceId(experimentId)}`); }
  createExperiment(projectId, body, key) { return this.#request(`/projects/${resourceId(projectId)}/experiments`, body, key); }
  reviseExperiment(experimentId, body, key) { return this.#request(`/experiments/${resourceId(experimentId)}/designs`, body, key); }
  admitExperimentAttempt(variantId, body, key) { return this.#request(`/experiment-variants/${resourceId(variantId)}/admitted-attempts`, body, key); }
  dispatchAdmittedAttempt(attemptId, body, key) { return this.#request(`/experiment-admitted-attempts/${resourceId(attemptId)}/dispatch`, body, key); }
  resolveAdmittedAttempt(attemptId, body, key) { return this.#request(`/experiment-admitted-attempts/${resourceId(attemptId)}/resolve`, body, key); }
  linkAttemptArtifacts(attemptId, body, key) { return this.#request(`/experiment-attempts/${resourceId(attemptId)}/artifacts`, body, key); }
  recordMeasurement(attemptId, body, key) { return this.#request(`/experiment-attempts/${resourceId(attemptId)}/measurements`, body, key); }
  createConclusion(experimentId, body, key) { return this.#request(`/experiments/${resourceId(experimentId)}/conclusions`, body, key); }
  updateConclusion(conclusionId, body, key) { return this.#put(`/conclusions/${resourceId(conclusionId)}`, body, key); }
  reviewConclusion(conclusionId, body, key) { return this.#request(`/conclusions/${resourceId(conclusionId)}/review`, body, key); }
  amendConclusion(conclusionId, body, key) { return this.#request(`/conclusions/${resourceId(conclusionId)}/amendments`, body, key); }
  artifacts(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/artifacts?${pageQuery(page)}`); }
  artifactManifests(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/artifact-manifests?${pageQuery(page)}`); }
  artifactDispositions(manifestId, page) { return this.#request(`/artifact-manifests/${resourceId(manifestId)}/dispositions?${pageQuery(page)}`); }
  recordArtifactDisposition(manifestId, body, key) {
    if (!this.#active) throw new ConnectionError('Select an authenticated connection first.');
    if (this.#active.identity.role !== 'human') throw new ConnectionError('Human artifact disposition is required.');
    if (!body || typeof body !== 'object' || Array.isArray(body) ||
        Object.keys(body).length !== 4 || !['expected_manifest_digest', 'expected_revision', 'disposition', 'rationale'].every(field => Object.hasOwn(body, field)) ||
        typeof body.expected_manifest_digest !== 'string' || !/^[0-9a-f]{64}$/.test(body.expected_manifest_digest) || !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 0 ||
        body.disposition !== 'retain_quarantined' || typeof body.rationale !== 'string' || !body.rationale.trim() || body.rationale.length > 8000) {
      throw new ConnectionError('Invalid human quarantine-retention review.');
    }
    return this.#request(`/artifact-manifests/${resourceId(manifestId)}/dispositions`, body, key);
  }
  recordArtifactCustodyRelease(manifestId, body, key) {
    return this.#request(`/artifact-manifests/${resourceId(manifestId)}/custody-releases`, body, key);
  }
  releasedArtifactManifest(manifestId) { return this.#request(`/artifact-manifests/${resourceId(manifestId)}/released`); }
  artifactPreview(manifestId, artifactId, maxBytes = 4096) {
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 1 || maxBytes > 64 * 1024) throw new ConnectionError('Invalid artifact preview byte limit.');
    return this.#request(`/artifact-manifests/${resourceId(manifestId)}/artifacts/${resourceId(artifactId)}/preview?max_bytes=${maxBytes}`);
  }
  async retrieveReleasedArtifact(manifestId, artifactId) {
    const connection = this.#active;
    if (!connection) throw new ConnectionError('Select an authenticated connection first.');
    const path = `/artifact-manifests/${resourceId(manifestId)}/artifacts/${resourceId(artifactId)}/content`;
    const freshness = ++this.#healthFreshness;
    const controller = new AbortController();
    this.#pending.add(controller);
    const timer = setTimeout(() => controller.abort(), this.#timeout);
    try {
      const response = await this.#fetch(connection.origin + path, { method: 'GET',
        headers: { Authorization: `Bearer ${connection.token}` }, redirect: 'error', signal: controller.signal });
      if (connection.generation !== this.#generation) throw new ConnectionError('Connection changed.');
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) this.disconnect();
        throw new ConnectionError(`Control request failed (${response.status}).`);
      }
      const declaredSize = Number(response.headers.get('content-length') ?? 0);
      if (declaredSize > 64 * 1024 * 1024) throw new ConnectionError('Artifact retrieval is too large.');
      const reader = response.body.getReader(); const chunks = []; let size = 0;
      try {
        while (true) {
          const part = await reader.read(); if (part.done) break;
          size += part.value.byteLength;
          if (size > 64 * 1024 * 1024) throw new ConnectionError('Artifact retrieval is too large.');
          chunks.push(part.value);
        }
      } finally { await reader.cancel(); }
      const bytes = Buffer.concat(chunks);
      if (connection.generation !== this.#generation) throw new ConnectionError('Connection changed.');
      this.#setHealth(connection, freshness, 'online');
      return { content: bytes, sha256: response.headers.get('x-tessera-artifact-sha256') };
    } catch (error) {
      if (error instanceof ConnectionError) throw error;
      this.#setHealth(connection, freshness, 'offline');
      throw new ConnectionError('Artifact retrieval is unavailable. Reconnect and inspect its released manifest before retrying.');
    } finally {
      clearTimeout(timer); this.#pending.delete(controller);
    }
  }
  exportReproductionBundle(manifestId, body, key) {
    return this.#request(`/artifact-manifests/${resourceId(manifestId)}/reproduction-bundles`, body, key);
  }
  reproductionBundle(bundleId) { return this.#request(`/reproduction-bundles/${resourceId(bundleId)}`); }
  recoveryReconciliations(grantId) {
    return this.#request(`/execution-grants/${resourceId(grantId)}/recovery-reconciliations`);
  }
  reconcileLostWorkerOperation(grantId, body, key) {
    return this.#request(`/execution-grants/${resourceId(grantId)}/recovery-reconciliations`, body, key);
  }
  studies(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/studies?${pageQuery(page)}`); }
  study(studyId) { return this.#request(`/studies/${resourceId(studyId)}`); }
  studyExport(studyId) { return this.#request(`/studies/${resourceId(studyId)}/export`); }
  createStudy(projectId, body, key) { return this.#request(`/projects/${resourceId(projectId)}/studies`, body, key); }
  registerVariant(studyId, body, key) { return this.#request(`/studies/${resourceId(studyId)}/variants`, body, key); }
  recordVariant(variantId, body, key) { return this.#request(`/variants/${resourceId(variantId)}/record`, body, key); }
  closeStudy(studyId, body, key) { return this.#request(`/studies/${resourceId(studyId)}/close`, body, key); }
  createSource(studyId, body, key) { return this.#request(`/studies/${resourceId(studyId)}/sources`, body, key); }
  assessSource(sourceId, body, key) { return this.#request(`/sources/${resourceId(sourceId)}/assess`, body, key); }
  correctSource(sourceId, body, key) { return this.#request(`/sources/${resourceId(sourceId)}/correct`, body, key); }
  createFinding(studyId, body, key) { return this.#request(`/studies/${resourceId(studyId)}/findings`, body, key); }
  acceptFinding(findingId, body, key) { return this.#request(`/findings/${resourceId(findingId)}/accept`, body, key); }
  reaffirmFinding(findingId, body, key) { return this.#request(`/findings/${resourceId(findingId)}/reaffirm`, body, key); }
  retractFinding(findingId, body, key) { return this.#request(`/findings/${resourceId(findingId)}/retract`, body, key); }
  reviseFinding(findingId, body, key) { return this.#request(`/findings/${resourceId(findingId)}/revise`, body, key); }
  revisions(taskId, page) { return this.#request(`/tasks/${resourceId(taskId)}/revisions?${pageQuery(page)}`); }
  taskPlan(taskId, planDigest) {
    if (typeof planDigest !== 'string' || !/^[0-9a-f]{64}$/.test(planDigest)) {
      throw new ConnectionError('Invalid plan digest.');
    }
    return this.#request(`/tasks/${resourceId(taskId)}/plans/${planDigest}`);
  }
  reviseTaskPlan(taskId, body, key) { return this.#put(`/tasks/${resourceId(taskId)}/plan`, body, key); }
  labbook(projectId, after = 0) {
    if (!Number.isSafeInteger(after) || after < 0) throw new ConnectionError('Invalid event cursor.');
    return this.#request(`/projects/${resourceId(projectId)}/labbook?after=${after}&limit=200`);
  }
  createProject(body, key) { return this.#request('/projects', body, key); }
  createTask(projectId, body, key) { return this.#request(`/projects/${resourceId(projectId)}/tasks`, body, key); }
  createThread(body, key) { return this.#request('/interaction/threads', body, key); }
  sendMessage(body, key) { return this.#request('/interaction/messages', body, key); }
  harnessSessionBinding(bindingId) {
    return this.#request(`/interaction/harness-session-bindings/${resourceId(bindingId)}`);
  }
  enqueueHarnessDelivery(body, key) { return this.#request('/interaction/deliveries', body, key); }
  cancelHarnessDelivery(deliveryId, body, key) {
    return this.#request(`/interaction/deliveries/${resourceId(deliveryId)}/cancel`, body, key);
  }
  resolveDecision(body, key) { return this.#request('/interaction/decisions/resolve', body, key); }
  createDecision(body, key) { return this.#request('/interaction/decisions', body, key); }
  approve(taskId, body, key) { return this.#request(`/tasks/${resourceId(taskId)}/approve`, body, key); }
  commandId() { return randomUUID(); }
}
