/** Main-process connection boundary. Never export this object to the renderer. */
import { randomUUID } from 'node:crypto';

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

export class ControlConnection {
  #fetch;
  #active = null;
  #generation = 0;
  #pending = new Set();
  #timeout;

  constructor(fetchImpl = globalThis.fetch, timeoutMs = 15000) {
    this.#fetch = fetchImpl;
    this.#timeout = timeoutMs;
  }

  disconnect() {
    this.#generation++;
    for (const controller of this.#pending) controller.abort();
    this.#pending.clear();
    this.#active = null;
  }

  async connect({ id, name, endpoint, token }) {
    if (typeof id !== 'string' || !id || typeof name !== 'string' || !name.trim() || name.length > 200 ||
        typeof token !== 'string' || !/^[A-Za-z0-9_-]{32,256}$/.test(token)) {
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

  status() {
    if (!this.#active) return null;
    const { id, name, origin, identity } = this.#active;
    return { id, name, endpoint: origin, principalId: identity.principal_id, organizationId: identity.org_id,
      role: identity.role, projectId: identity.project_id ?? null, expiresAt: identity.expires_at ?? null };
  }

  assertActive(expectedId) {
    if (!this.#active || typeof expectedId !== 'string' || this.#active.id !== expectedId) {
      throw new ConnectionError('Connection changed; this command belongs to another context.');
    }
  }

  async #send(connection, path, body, key) {
    const controller = new AbortController();
    this.#pending.add(controller);
    const timer = setTimeout(() => controller.abort(), this.#timeout);
    try {
      const response = await this.#fetch(connection.origin + path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${connection.token}`,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json', 'Idempotency-Key': key }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error', signal: controller.signal,
      });
      if (connection.generation !== this.#generation) throw new ConnectionError('Connection changed.');
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) this.disconnect();
        // No response bodies in errors: servers may include sensitive source data.
        throw new ConnectionError(`Control request failed (${response.status}).`);
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
      return JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch (error) {
      if (error instanceof ConnectionError) throw error;
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

  projects(page) { return this.#request(`/projects?${pageQuery(page)}`); }
  recovery() { return this.#request('/recovery'); }
  tasks(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/tasks?${pageQuery(page)}`); }
  threads(projectId, page) { return this.#request(`/interaction/projects/${resourceId(projectId)}/threads?${interactionPageQuery(page)}`); }
  messages(threadId, page) { return this.#request(`/interaction/threads/${resourceId(threadId)}/messages?${interactionPageQuery(page)}`); }
  decisions(projectId, page) {
    return this.#request(`/interaction/projects/${resourceId(projectId)}/decisions?${interactionPageQuery(page)}&status=pending`);
  }
  experiments(projectId, page) { return this.#request(`/projects/${resourceId(projectId)}/experiments?${pageQuery(page)}`); }
  experiment(experimentId) { return this.#request(`/experiments/${resourceId(experimentId)}`); }
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
  labbook(projectId, after = 0) {
    if (!Number.isSafeInteger(after) || after < 0) throw new ConnectionError('Invalid event cursor.');
    return this.#request(`/projects/${resourceId(projectId)}/labbook?after=${after}&limit=200`);
  }
  createProject(body, key) { return this.#request('/projects', body, key); }
  createTask(projectId, body, key) { return this.#request(`/projects/${resourceId(projectId)}/tasks`, body, key); }
  createThread(body, key) { return this.#request('/interaction/threads', body, key); }
  sendMessage(body, key) { return this.#request('/interaction/messages', body, key); }
  resolveDecision(body, key) { return this.#request('/interaction/decisions/resolve', body, key); }
  approve(taskId, body, key) { return this.#request(`/tasks/${resourceId(taskId)}/approve`, body, key); }
  commandId() { return randomUUID(); }
}
