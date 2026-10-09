/**
 * Main-process-only adapter for the narrow `tessera astra-namespace` CLI.
 * The renderer receives an opaque selection id and bounded public lifecycle
 * facts; it never receives a kubeconfig path, its bytes, or an argv capability.
 */
import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { lstat, mkdtemp, open, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { spawn } from 'node:child_process';

const MAX_JSON_BYTES = 128 * 1024;
const PLAN_TTL_MS = 5 * 60 * 1000;
const NAME = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const DIGEST = /^[0-9a-f]{64}$/;
const CONTEXT = /^[^\x00-\x1f\x7f]{1,253}$/;

export class NamespaceDesktopError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

export function validateNamespaceRequest({ reference, context, namespace } = {}) {
  if (typeof reference !== 'string' || !/^[0-9a-f-]{36}$/.test(reference)) throw new NamespaceDesktopError('invalid', 'Choose a kubeconfig again.');
  if (typeof context !== 'string' || !CONTEXT.test(context)) throw new NamespaceDesktopError('invalid', 'Enter one explicit kubeconfig context.');
  if (typeof namespace !== 'string' || namespace.length > 63 || !NAME.test(namespace)) throw new NamespaceDesktopError('invalid', 'Enter a bounded Kubernetes namespace name.');
  return { reference, context, namespace };
}

function boundedJson(text) {
  if (Buffer.byteLength(text) > MAX_JSON_BYTES) throw new NamespaceDesktopError('unavailable', 'Namespace response exceeded its safety bound.');
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw new NamespaceDesktopError('unavailable', 'Namespace command returned invalid data.'); }
}

function childRunner(executable, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    let stdout = ''; let stderr = ''; let settled = false;
    const child = spawn(executable, args, { shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const finish = value => { if (!settled) { settled = true; clearTimeout(timer); resolve(value); } };
    const fail = error => { if (!settled) { settled = true; clearTimeout(timer); reject(error); } };
    const append = (current, chunk) => {
      const next = current + chunk.toString('utf8');
      if (Buffer.byteLength(next) > MAX_JSON_BYTES) { child.kill('SIGTERM'); fail(new NamespaceDesktopError('unavailable', 'Namespace response exceeded its safety bound.')); }
      return next;
    };
    child.stdout.on('data', chunk => { stdout = append(stdout, chunk); });
    child.stderr.on('data', chunk => { stderr = append(stderr, chunk); });
    child.once('error', () => fail(new NamespaceDesktopError('unavailable', 'Namespace command is unavailable.')));
    child.once('close', code => finish({ code, stdout, stderr }));
    const timer = setTimeout(() => { child.kill('SIGTERM'); fail(new NamespaceDesktopError('unknown', 'Namespace command timed out; reconcile before trying again.')); }, timeoutMs);
  });
}

function safeInspection(value) {
  const inspection = value?.inspection ?? value;
  const target = inspection?.target;
  if (!target || typeof target !== 'object' || !['absent', 'present', 'terminating'].includes(inspection.state) ||
      typeof target.context !== 'string' || typeof target.server !== 'string' || !DIGEST.test(target.kubeconfig_reference_sha256 ?? '') ||
      !DIGEST.test(target.ca_sha256 ?? '') || typeof target.cluster_uid !== 'string' || !NAME.test(target.namespace ?? '')) {
    throw new NamespaceDesktopError('unavailable', 'Namespace inspection was incomplete.');
  }
  return { state: inspection.state, uid: typeof inspection.uid === 'string' ? inspection.uid : null,
    resourceVersion: typeof inspection.resource_version === 'string' ? inspection.resource_version : null,
    target: { kubeconfigReferenceSha256: target.kubeconfig_reference_sha256, context: target.context,
      server: target.server, caSha256: target.ca_sha256, clusterUid: target.cluster_uid, namespace: target.namespace } };
}

export class NamespaceLifecycle {
  #picker; #run; #selections = new Map(); #plans = new Map(); #directoryPromise;

  constructor({ picker, runner = childRunner, executable = 'tessera', timeoutMs = 15_000, temporaryDirectory } = {}) {
    this.#picker = picker;
    this.#run = (args, timeout = timeoutMs) => runner(executable, args, timeout);
    this.#directoryPromise = temporaryDirectory ? Promise.resolve(temporaryDirectory) : mkdtemp(join(tmpdir(), 'tessera-namespace-'));
  }

  async selectKubeconfig(parent) {
    const selected = await this.#picker(parent, { title: 'Select kubeconfig', properties: ['openFile'], filters: [{ name: 'Kubeconfig', extensions: ['yaml', 'yml', 'config'] }] });
    if (!selected || selected.canceled || selected.filePaths?.length !== 1) return { state: 'cancelled' };
    if (!constants.O_NOFOLLOW || !constants.O_NONBLOCK) throw new NamespaceDesktopError('unavailable', 'Safe kubeconfig selection is not supported on this platform yet.');
    const path = selected.filePaths[0];
    let handle;
    try {
      handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      const info = await handle.stat();
      if (!info.isFile() || info.size > 4 * 1024 * 1024) throw new NamespaceDesktopError('invalid', 'Select a regular kubeconfig smaller than 4 MiB.');
      const reference = randomUUID();
      this.#selections.set(reference, { path, dev: info.dev, ino: info.ino });
      return { state: 'selected', reference, name: basename(path) };
    } catch (error) {
      if (error instanceof NamespaceDesktopError) throw error;
      throw new NamespaceDesktopError('invalid', 'Select a readable regular kubeconfig.');
    } finally { await handle?.close(); }
  }

  async #selection(request) {
    const checked = validateNamespaceRequest(request); const selected = this.#selections.get(checked.reference);
    if (!selected) throw new NamespaceDesktopError('stale', 'Kubeconfig selection expired; choose it again.');
    try {
      const info = await lstat(selected.path);
      if (!info.isFile() || info.isSymbolicLink() || info.dev !== selected.dev || info.ino !== selected.ino) throw new Error();
    } catch { throw new NamespaceDesktopError('stale', 'Kubeconfig selection changed; choose it again.'); }
    return { ...checked, path: selected.path };
  }

  async #command(action, request, extras = []) {
    const selected = await this.#selection(request);
    const result = await this.#run(['astra-namespace', action, '--kubeconfig', selected.path, '--context', selected.context, '--namespace', selected.namespace, ...extras]);
    if (result.code !== 0) {
      const failure = boundedJson(result.stderr || '{}');
      const message = typeof failure.message === 'string' ? failure.message : 'Namespace command failed.';
      const code = /expired/.test(message) ? 'expired' : /forbidden|denied|RBAC/i.test(message) ? 'forbidden' : 'failed';
      throw new NamespaceDesktopError(code, `Namespace ${code}: ${message}`);
    }
    return { selected, value: boundedJson(result.stdout) };
  }

  async inspect(request) { return safeInspection((await this.#command('inspect-existing', request)).value); }

  async #plan(action, request) {
    const expiresAt = new Date(Date.now() + PLAN_TTL_MS).toISOString();
    const extras = ['--approval-expires-at', expiresAt];
    if (action === 'plan-create') extras.push('--installation', 'tessera-desktop', '--operation', `desktop-${randomUUID().replaceAll('-', '').slice(0, 32)}`);
    const { selected, value } = await this.#command(action, request, extras);
    const inspection = safeInspection(value);
    if (!DIGEST.test(value.digest ?? '') || typeof value.approval_expires_at !== 'string' || value.mode !== (action === 'plan-create' ? 'create-dedicated' : 'use-existing')) {
      throw new NamespaceDesktopError('unavailable', 'Namespace plan was incomplete.');
    }
    const planId = randomUUID(); const directory = await this.#directoryPromise; const path = join(directory, `${planId}.plan.json`);
    await writeFile(path, JSON.stringify(value), { mode: 0o600, flag: 'wx' });
    this.#plans.set(planId, { planId, path, request: { reference: selected.reference, context: selected.context, namespace: selected.namespace }, digest: value.digest, expiresAt: value.approval_expires_at, mode: value.mode, receiptPath: null, creating: false, createAttempted: false });
    return { planId, mode: value.mode, digest: value.digest, expiresAt: value.approval_expires_at, inspection };
  }

  planUseExisting(request) { return this.#plan('plan-use-existing', request); }
  planCreate(request) { return this.#plan('plan-create', request); }

  async create({ planId, approvedDigest } = {}) {
    const plan = this.#plans.get(planId);
    if (!plan || plan.mode !== 'create-dedicated') throw new NamespaceDesktopError('stale', 'Namespace plan is no longer available; inspect and plan again.');
    if (plan.creating || plan.createAttempted) throw new NamespaceDesktopError('duplicate', 'This plan was already submitted. Reconcile it; do not create again.');
    if (approvedDigest !== plan.digest) throw new NamespaceDesktopError('stale', 'Confirm the exact displayed namespace plan digest.');
    if (Date.parse(plan.expiresAt) <= Date.now()) throw new NamespaceDesktopError('expired', 'Namespace plan expired; inspect and plan again.');
    plan.creating = true; plan.createAttempted = true;
    const directory = await this.#directoryPromise; const receiptPath = join(directory, `${planId}.receipt.json`);
    try {
      const selected = await this.#selection(plan.request);
      const result = await this.#run(['astra-namespace', 'create', '--plan', plan.path, '--kubeconfig', selected.path, '--context', selected.context, '--approved-plan', approvedDigest, '--receipt-output', receiptPath]);
      if (result.code !== 0) {
        const failure = boundedJson(result.stderr || '{}');
        const message = typeof failure.message === 'string' ? failure.message : 'Create outcome is unknown.';
        if (/expired/.test(message)) throw new NamespaceDesktopError('expired', 'Namespace plan expired; inspect and plan again.');
        return { state: 'reconciliation_required', digest: plan.digest, message: 'Namespace create did not return a receipt; reconcile before taking another action.' };
      }
      const value = boundedJson(result.stdout);
      if (value.state === 'reconciliation_required') return { state: value.state, digest: plan.digest, message: 'Namespace create is uncertain; reconcile before taking another action.' };
      if (value.plan_digest !== plan.digest || typeof value.uid !== 'string' || typeof value.resource_version !== 'string') throw new NamespaceDesktopError('unknown', 'Namespace create response is incomplete; reconcile before taking another action.');
      plan.receiptPath = receiptPath;
      return { state: 'namespace_created_control_plane_not_installed', digest: plan.digest, uid: value.uid, resourceVersion: value.resource_version };
    } catch (error) {
      if (error instanceof NamespaceDesktopError && error.code === 'expired') throw error;
      return { state: 'reconciliation_required', digest: plan.digest, message: 'Namespace create outcome is unknown; reconcile before taking another action.' };
    } finally { plan.creating = false; }
  }

  async #observe(action, { planId } = {}) {
    const plan = this.#plans.get(planId);
    if (!plan) throw new NamespaceDesktopError('stale', 'Namespace plan is no longer available; inspect again.');
    const selected = await this.#selection(plan.request);
    const args = ['astra-namespace', action, '--plan', plan.path, '--kubeconfig', selected.path, '--context', selected.context];
    if (plan.receiptPath) args.push('--receipt', plan.receiptPath);
    const result = await this.#run(args);
    if (result.code !== 0) throw new NamespaceDesktopError('unavailable', 'Namespace reconciliation is unavailable; no create was retried.');
    const value = boundedJson(result.stdout);
    if (value.plan_digest !== plan.digest || typeof value.state !== 'string') throw new NamespaceDesktopError('unavailable', 'Namespace reconciliation was incomplete.');
    return { state: value.state, digest: value.plan_digest, uid: typeof value.uid === 'string' ? value.uid : null, resourceVersion: typeof value.resource_version === 'string' ? value.resource_version : null };
  }

  reconcile(value) { return this.#observe('reconcile', value); }
  status(value) { return this.#observe('status', value); }
  async clear() {
    this.#selections.clear(); this.#plans.clear();
    const directory = await this.#directoryPromise;
    await rm(directory, { recursive: true, force: true });
    this.#directoryPromise = mkdtemp(join(tmpdir(), 'tessera-namespace-'));
  }
}
