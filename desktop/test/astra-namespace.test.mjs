import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { NamespaceLifecycle, validateNamespaceRequest } from '../astra-namespace.mjs';

const target = namespace => ({ kubeconfig_reference_sha256: 'a'.repeat(64), context: 'team/admin@prod',
  server: 'https://cluster.test', ca_sha256: 'b'.repeat(64), cluster_uid: 'cluster-uid', namespace });
const inspection = (namespace, state = 'absent') => ({ state, uid: state === 'absent' ? null : 'namespace-uid',
  resource_version: state === 'absent' ? null : '9', target: target(namespace) });
const plan = (namespace, mode) => ({ version: 'astra-namespace-lifecycle-v1', mode,
  inspection: inspection(namespace, mode === 'use-existing' ? 'present' : 'absent'), installation: mode === 'create-dedicated' ? 'tessera-desktop' : null,
  operation: mode === 'create-dedicated' ? 'desktop-operation' : null,
  approval_expires_at: '2030-01-01T00:00:00.000Z', digest: 'c'.repeat(64) });

async function fixture({ create = 'receipt' } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'tessera-namespace-test-'));
  const config = join(directory, 'private-kubeconfig'); await writeFile(config, 'private kubeconfig bytes');
  const calls = [];
  const runner = async (_executable, args) => {
    calls.push(args);
    const action = args[1]; const namespace = args[args.indexOf('--namespace') + 1] ?? 'astra-system';
    if (action === 'inspect-existing') return { code: 0, stdout: JSON.stringify(inspection(namespace, 'present')), stderr: '' };
    if (action === 'plan-create') return { code: 0, stdout: JSON.stringify(plan(namespace, 'create-dedicated')), stderr: '' };
    if (action === 'plan-use-existing') return { code: 0, stdout: JSON.stringify(plan(namespace, 'use-existing')), stderr: '' };
    if (action === 'create' && create === 'unknown') return { code: 0, stdout: JSON.stringify({ plan_digest: 'c'.repeat(64), state: 'reconciliation_required' }), stderr: '' };
    if (action === 'create') return { code: 0, stdout: JSON.stringify({ plan_digest: 'c'.repeat(64), uid: 'created-uid', resource_version: '10', target: target('astra-system') }), stderr: '' };
    if (action === 'reconcile' || action === 'status') return { code: 0, stdout: JSON.stringify({ plan_digest: 'c'.repeat(64), state: 'namespace_created_control_plane_not_installed', uid: 'created-uid', resource_version: '10' }), stderr: '' };
    throw new Error(`unexpected action ${action}`);
  };
  const plans = join(directory, 'plans'); await mkdir(plans);
  const lifecycle = new NamespaceLifecycle({ picker: async () => ({ canceled: false, filePaths: [config] }), runner, temporaryDirectory: plans });
  return { lifecycle, calls, directory };
}

test('namespace bridge returns an opaque kubeconfig selection and only fixed CLI actions', async () => {
  const { lifecycle, calls } = await fixture();
  const selected = await lifecycle.selectKubeconfig();
  assert.equal(selected.state, 'selected'); assert.equal(selected.name, 'private-kubeconfig');
  assert.ok(!JSON.stringify(selected).includes('/')); assert.ok(!JSON.stringify(selected).includes('private kubeconfig bytes'));
  const inspected = await lifecycle.inspect({ reference: selected.reference, context: 'team/admin@prod', namespace: 'astra-system' });
  assert.equal(inspected.target.server, 'https://cluster.test'); assert.equal(inspected.state, 'present');
  assert.deepEqual(calls[0].slice(0, 2), ['astra-namespace', 'inspect-existing']);
  assert.ok(calls[0].includes('--kubeconfig')); assert.ok(!calls.flat().some(value => /secret|apply|patch|delete|shell/i.test(value)));
  await lifecycle.clear();
});

test('dedicated create is exactly once and its status is a bounded reconciliation read', async () => {
  const { lifecycle, calls } = await fixture(); const selected = await lifecycle.selectKubeconfig();
  const request = { reference: selected.reference, context: 'team/admin@prod', namespace: 'astra-system' };
  const planned = await lifecycle.planCreate(request);
  assert.equal(planned.mode, 'create-dedicated'); assert.equal(planned.digest, 'c'.repeat(64));
  const receipt = await lifecycle.create({ planId: planned.planId, approvedDigest: planned.digest });
  assert.deepEqual(receipt, { state: 'namespace_created_control_plane_not_installed', digest: 'c'.repeat(64), uid: 'created-uid', resourceVersion: '10' });
  await assert.rejects(lifecycle.create({ planId: planned.planId, approvedDigest: planned.digest }), /already submitted/);
  const reconciled = await lifecycle.reconcile({ planId: planned.planId });
  const status = await lifecycle.status({ planId: planned.planId });
  assert.equal(reconciled.state, 'namespace_created_control_plane_not_installed');
  assert.equal(status.state, 'namespace_created_control_plane_not_installed');
  assert.equal(calls.filter(args => args[1] === 'create').length, 1);
  assert.equal(calls.filter(args => args[1] === 'reconcile').length, 1);
  assert.equal(calls.filter(args => args[1] === 'status').length, 1);
  await lifecycle.clear();
});

test('unknown create requires reconciliation and never replays the mutation', async () => {
  const { lifecycle, calls } = await fixture({ create: 'unknown' }); const selected = await lifecycle.selectKubeconfig();
  const planned = await lifecycle.planCreate({ reference: selected.reference, context: 'team/admin@prod', namespace: 'astra-system' });
  const result = await lifecycle.create({ planId: planned.planId, approvedDigest: planned.digest });
  assert.equal(result.state, 'reconciliation_required');
  await assert.rejects(lifecycle.create({ planId: planned.planId, approvedDigest: planned.digest }), /already submitted/);
  assert.equal(calls.filter(args => args[1] === 'create').length, 1);
  await lifecycle.clear();
});

test('request validation rejects renderer-shaped paths, commands, and invalid target names', () => {
  assert.throws(() => validateNamespaceRequest({ reference: 'x', context: 'team', namespace: 'astra-system' }));
  assert.throws(() => validateNamespaceRequest({ reference: '0'.repeat(36), context: 'team', namespace: '../secret' }));
  assert.throws(() => validateNamespaceRequest({ reference: '0'.repeat(36), context: '', namespace: 'astra-system' }));
});
