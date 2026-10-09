'use strict';
// Public metadata only. Neither credentials nor authority are created here.
const spec = Object.freeze({ argv: ['/hello', '--cpu-sum-squares', '10', '/outputs/result.txt'],
  input_snapshot_digest: 'e1c6b77848dcd107e8ae978cd59ae1890468d5595a5b32b4c76b58d0c8e5b108',
  pinned_skill_digests: [], output_paths: ['result.txt'], profile: 'kubernetes-opencode',
  network_enabled: false, max_output_bytes: 1024 });
const workloadImage = 'docker.io/library/tessera-e2e-hello@sha256:7c971d2093ccdde2cd13160f87a50ee4a5150bbb58ff7d723e1cfc046dcd7584';
function validateAdmission(refs, route, phase, now = Date.now()) {
  if (!['plan', 'admit', 'dispatch', 'inspect', 'export'].includes(phase)) throw new Error('Explicit plan/admit/dispatch/inspect/export phase required.');
  if (!refs.execution_ready || !route.ready || route.origin !== refs.origin || !Number.isFinite(Date.parse(route.expires_at)) || Date.parse(route.expires_at) <= now) throw new Error('Fresh integration-owned readiness admission required.');
  for (const field of ['plan', 'admit', 'dispatch'].includes(phase) ? ['ready_expires_at', 'witness_expires_at', 'worker_expires_at', 'release_expires_at'] : []) {
    if (!Number.isFinite(Date.parse(refs[field])) || Date.parse(refs[field]) <= now + 30000) throw new Error(`Fresh ${field} required with at least30seconds remaining.`);
  }
  if (refs.snapshot_id !== 'snapshot_78647f9103fd4130b5de0f902e83f8bd' || refs.snapshot_digest !== spec.input_snapshot_digest || refs.pinned_workload_image !== workloadImage) throw new Error('Reviewed retained input/workload image basis differs.');
  if (refs.project_id !== 'project_f080b734cd5849ec998265c7a2f34a4f' || refs.worker_principal_id !== 'principal_863671c7542943e4b2137963faa22b0e') throw new Error('Reviewed project/worker identity differs.');
  if (!refs.snapshot_release_id || !refs.worker_principal_id || !/^[0-9a-f]{64}$/.test(refs.target_digest)) throw new Error('Explicit release/worker/target metadata required.');
  if (phase !== 'plan' && (!refs.grant_id || !refs.experiment_id || !refs.variant_id)) throw new Error('Explicit operator grant and registered experiment variant required.');
  if (['dispatch', 'inspect', 'export'].includes(phase) && (!refs.admitted_attempt_id || !/^[0-9a-f]{64}$/.test(refs.package_digest))) throw new Error('Explicit worker-prepared admitted package required.');
  return spec;
}
function validateJobWitness(witness, state, refs) {
  if (!state.dispatch_authorized_at || witness.operation_id !== state.operation_id || witness.package_digest !== state.package_digest || witness.admitted_attempt_id !== state.admitted_attempt_id || witness.target_digest !== refs.target_digest || witness.new_job !== true || !/^[0-9a-f-]{36}$/.test(witness.job_uid || '') || witness.provider_state !== 'Complete') throw new Error('Actual new ordinary Job identity/completion proof does not match dispatch.');
  const created = Date.parse(witness.created_at); const observed = Date.parse(witness.observed_at);
  if (!Number.isFinite(Date.parse(state.dispatch_authorized_at)) || !Number.isFinite(created) || !Number.isFinite(observed) || created < Date.parse(state.dispatch_authorized_at) || observed < created || observed > Date.now() + 5000) throw new Error('Actual Job proof timestamps do not follow native dispatch.');
  return { job_uid: witness.job_uid, operation_id: witness.operation_id, provider_state: witness.provider_state,
    created_at: witness.created_at, observed_at: witness.observed_at, evidence_reference: refs.job_evidence_reference };
}
function operatorPackageRequest(lease, descriptor) {
  if (!Number.isSafeInteger(lease.expected_epoch) || lease.expected_epoch < 1 || !lease.admitted_attempt_id) throw new Error('Explicit lease epoch and native admitted attempt required.');
  const body = { expected_epoch: lease.expected_epoch, execution_adapter: 'kubernetes-job', admitted_attempt_id: lease.admitted_attempt_id };
  // This is normal operator-request preparation, NOT a package API call. The
  // installed Kubernetes collector operates independently of optional storage
  // capability admission. Omission grants no verified mount guarantees.
  if (descriptor == null) return body;
  const ids = ['allocation_id', 'filesystem_id', 'mount_id'];
  const capacities = ['capacity_bytes', 'capacity_inodes'];
  const guarantees = ['noexec', 'nosuid', 'nodev', 'write_access_verified', 'supervisor_custody_verified', 'no_nested_mounts_verified'];
  const fields = ['contract_version', ...ids, ...capacities, ...guarantees, 'write_uid'];
  if (typeof descriptor !== 'object' || Array.isArray(descriptor) || Object.keys(descriptor).length !== fields.length || fields.some(key => !Object.hasOwn(descriptor, key)) || descriptor.contract_version !== 1 || descriptor.write_uid !== 65532 || ids.some(key => typeof descriptor[key] !== 'string' || !descriptor[key].trim() || descriptor[key].length > 200) || capacities.some(key => !Number.isSafeInteger(descriptor[key]) || descriptor[key] < 1) || guarantees.some(key => descriptor[key] !== true)) throw new Error('Supplied storage descriptor does not satisfy the backend contract; do not relabel false guarantees as verified.');
  if (descriptor.capacity_bytes < spec.max_output_bytes) throw new Error('Storage capacity does not cover declared outputs.');
  return { ...body, persistent_output_collection: true, persistent_output_storage: descriptor };
}
function dispositionReview(review, manifest) {
  if (!review || review.human_approved !== true || review.manifest_id !== manifest.id || review.expected_manifest_digest !== manifest.manifest_digest || !Number.isSafeInteger(review.expected_revision) || review.expected_revision < 0 || typeof review.rationale !== 'string' || !review.rationale.trim() || review.rationale.length > 8000 || (review.disposition !== undefined && review.disposition !== 'retain_quarantined')) throw new Error('Explicit exact-manifest human retain-quarantined review required.');
  return { expected_manifest_digest: review.expected_manifest_digest, expected_revision: review.expected_revision, disposition: 'retain_quarantined', rationale: review.rationale };
}
module.exports = { spec, workloadImage, validateAdmission, validateJobWitness, operatorPackageRequest, dispositionReview };
