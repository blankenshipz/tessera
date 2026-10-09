import test from 'node:test';
import assert from 'node:assert/strict';
import contract from './bench-execution-contract.cjs';
const now = Date.parse('2026-10-04T07:00:00Z');
const expiry = '2026-10-04T07:10:00Z';
function inputs() { return { execution_ready: true, origin: 'https://localhost:18443',
  ready_expires_at: expiry, witness_expires_at: expiry, worker_expires_at: expiry,
  release_expires_at: expiry, snapshot_id: 'snapshot_78647f9103fd4130b5de0f902e83f8bd',
  snapshot_digest: contract.spec.input_snapshot_digest, pinned_workload_image: contract.workloadImage,
  project_id: 'project_f080b734cd5849ec998265c7a2f34a4f', snapshot_release_id: 'release_reviewed',
  worker_principal_id: 'principal_863671c7542943e4b2137963faa22b0e', target_digest: 'b'.repeat(64) }; }
const route = { ready: true, origin: 'https://localhost:18443', expires_at: expiry };
test('native execution preparation uses real bounded retained-input spec, not historical placeholder', () => {
  assert.equal(contract.validateAdmission(inputs(), route, 'plan', now), contract.spec);
  assert.deepEqual(contract.spec.argv, ['/hello', '--cpu-sum-squares', '10', '/outputs/result.txt']);
  assert.deepEqual(contract.spec.output_paths, ['result.txt']);
  assert.equal(contract.spec.max_output_bytes, 1024);
  assert.throws(() => contract.validateAdmission({ ...inputs(), snapshot_digest: 'a'.repeat(64) }, route, 'plan', now));
});
test('native execution phases fail closed on stale readiness/release/witness or absent explicit operator grant', () => {
  for (const field of ['ready_expires_at', 'witness_expires_at', 'worker_expires_at', 'release_expires_at']) assert.throws(() => contract.validateAdmission({ ...inputs(), [field]: '2026-10-04T06:59:00Z' }, route, 'plan', now));
  assert.throws(() => contract.validateAdmission(inputs(), { ...route, ready: false }, 'plan', now));
  assert.throws(() => contract.validateAdmission(inputs(), route, 'admit', now));
  assert.throws(() => contract.validateAdmission(inputs(), route, 'dispatch', now));
  const admitted = { ...inputs(), grant_id: 'grant', experiment_id: 'experiment', variant_id: 'variant' };
  assert.equal(contract.validateAdmission(admitted, route, 'admit', now), contract.spec);
  assert.throws(() => contract.validateAdmission(admitted, route, 'dispatch', now));
  assert.equal(contract.validateAdmission({ ...admitted, admitted_attempt_id: 'attempt', package_digest: 'c'.repeat(64) }, route, 'dispatch', now), contract.spec);
});

test('new native Job evidence must follow the actual dispatch and match all provenance identities', () => {
  const testNow = Date.now();
  const state = { operation_id: 'operation', package_digest: 'c'.repeat(64), admitted_attempt_id: 'attempt', dispatch_authorized_at: new Date(testNow - 30000).toISOString() };
  const witness = { operation_id: 'operation', package_digest: state.package_digest, admitted_attempt_id: 'attempt',
    target_digest: 'b'.repeat(64), new_job: true, job_uid: '49908fef-ca14-4fcc-be33-4e098df97a75', provider_state: 'Complete',
    created_at: new Date(testNow - 20000).toISOString(), observed_at: new Date(testNow - 10000).toISOString() };
  // Synthetic validation only, not evidence that this sample Job executed.
  const refs = { target_digest: witness.target_digest, job_evidence_reference: '/private/metadata-job-proof.json' };
  assert.equal(contract.validateJobWitness(witness, state, refs).job_uid, witness.job_uid);
  for (const patch of [{ new_job: false }, { operation_id: 'old' }, { package_digest: 'old' }, { admitted_attempt_id: 'old' }, { target_digest: 'old' }, { job_uid: null }, { provider_state: 'Unknown' }, { created_at: new Date(testNow - 40000).toISOString() }]) assert.throws(() => contract.validateJobWitness({ ...witness, ...patch }, state, refs));
  assert.throws(() => contract.validateJobWitness(witness, { ...state, dispatch_authorized_at: null }, refs));
});

test('ordinary Kubernetes package handoff supports omitted/null optional storage without asserting mount guarantees', () => {
  const lease = { expected_epoch: 1, admitted_attempt_id: 'attempt_native' };
  for (const descriptor of [undefined, null]) {
    assert.deepEqual(contract.operatorPackageRequest(lease, descriptor), {
      expected_epoch: 1, execution_adapter: 'kubernetes-job', admitted_attempt_id: 'attempt_native' });
    assert.equal(Object.hasOwn(contract.operatorPackageRequest(lease, descriptor), 'persistent_output_collection'), false);
    assert.equal(Object.hasOwn(contract.operatorPackageRequest(lease, descriptor), 'persistent_output_storage'), false);
  }
});

test('supplied storage false guarantees are rejected, never relabelled or silently omitted', () => {
  const descriptor = { contract_version: 1, allocation_id: 'allocation', filesystem_id: 'filesystem', mount_id: 'mount',
    capacity_bytes: 1024, capacity_inodes: 100, write_uid: 65532, noexec: true, nosuid: true, nodev: true,
    write_access_verified: true, supervisor_custody_verified: true, no_nested_mounts_verified: true };
  const lease = { expected_epoch: 1, admitted_attempt_id: 'attempt_native' };
  for (const field of ['noexec', 'nosuid', 'nodev', 'write_access_verified', 'supervisor_custody_verified', 'no_nested_mounts_verified']) {
    const invalid = { ...descriptor, [field]: false };
    assert.throws(() => contract.operatorPackageRequest(lease, invalid));
    assert.equal(invalid[field], false);
  }
  assert.equal(contract.operatorPackageRequest(lease, descriptor).persistent_output_collection, true);
});
test('native export requires separate exact human disposition acknowledgement, never derives it from custody', () => {
  const manifest = { id: 'manifest', manifest_digest: 'a'.repeat(64) };
  const review = { human_approved: true, manifest_id: 'manifest', expected_manifest_digest: manifest.manifest_digest, expected_revision: 0, rationale: 'Operational public toy output review only.' };
  assert.equal(contract.dispositionReview(review, manifest).disposition, 'retain_quarantined');
  for (const patch of [{human_approved:false},{manifest_id:'other'},{expected_revision:-1},{expected_manifest_digest:'b'.repeat(64)},{rationale:''},{disposition:'accept'}]) assert.throws(() => contract.dispositionReview({...review,...patch},manifest));
  assert.throws(() => contract.dispositionReview(undefined,manifest));
});
