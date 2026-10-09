'use strict';
// Opt-in NEW ordinary execution, explicit phased handoffs. Never issue grant,
// worker lease/package, fixture reports, output uploads or scientific decisions.
const { app, dialog, ipcMain } = require('electron');
const { readFileSync, writeFileSync, mkdirSync, existsSync, chmodSync, lstatSync } = require('node:fs');
const { join, resolve } = require('node:path');
const { createHash } = require('node:crypto');
const { spec, validateAdmission, validateJobWitness, dispositionReview } = require('./bench-execution-contract.cjs');
const { safeError } = require('./native-safe-diagnostics.cjs');
const { validatePrivateLayout } = require('./native-private-layout.cjs');
let stage = 'initialisation';
const refs = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const phase = process.argv[3];
const output = resolve(process.argv[4]);
const runtime = resolve(refs.private_runtime_dir || join(__dirname, '../../.tessera/private/native-execution', createHash('sha256').update(output).digest('hex').slice(0, 20)));
validatePrivateLayout(output, runtime);
const route = JSON.parse(readFileSync(refs.route_service_reference, 'utf8'));
validateAdmission(refs, route, phase);
if (process.env.NODE_EXTRA_CA_CERTS !== refs.ca_file) throw new Error('Referenced public CA must be loaded before native launch.');
mkdirSync(output, { recursive: true, mode: 0o700 });
mkdirSync(runtime, { recursive: true, mode: 0o700 }); chmodSync(runtime, 0o700);
const statePath = join(output, 'execution-state.json');
const prior = existsSync(statePath) ? JSON.parse(readFileSync(statePath, 'utf8')) : {};
if (phase !== 'plan' && (!prior.task_id || !prior.plan_digest)) throw new Error('Native approved-plan receipt required first.');
const title = prior.title || 'Native retained-input CPU execution 20261004';
const receipt = { ...prior, title, phase, steps: [], screens: [], scientificValidity: 'not_assessed',
  newExecutionJobs: prior.newExecutionJobs === true,
  storageCapabilityConformance: 'not asserted by native receipt; optional descriptor omission does not attest noexec/nosuid/nodev or storage-slot conformance',
  picker: { mode: 'labelled main-process file selection fallback; NOT actual OS picker acceptance', historicalTimeoutMs: 8000 },
  operatorBoundary: 'grant and worker lease/package supplied explicitly through normal APIs; harness does not create them' };
if (prior.firstFailure) receipt.previousFailure = prior.firstFailure;
delete receipt.firstFailure;
const header = join(runtime, 'private-owner.header');
if (existsSync(header) && (!lstatSync(header).isFile() || lstatSync(header).isSymbolicLink() || lstatSync(header).nlink !== 1 || lstatSync(header).uid !== process.getuid())) throw new Error('Private header must be a single owned regular file.');
const raw = readFileSync(refs.owner_credential_file);
let token = raw.toString('utf8').trim().replace(/^Authorization: Bearer /, ''); raw.fill(0);
writeFileSync(header, `Authorization: Bearer ${token}\n`, { mode: 0o600 });
chmodSync(header, 0o600);
receipt.privateCredentialHeaderReference = header;
const profile = join(runtime, 'profile');
if (existsSync(profile) && (lstatSync(profile).isSymbolicLink() || !lstatSync(profile).isDirectory() || lstatSync(profile).uid !== process.getuid())) throw new Error('Private Electron profile must be an owned real directory.');
mkdirSync(profile, { recursive: true, mode: 0o700 }); chmodSync(profile, 0o700);
app.setPath('userData', profile);
dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [header] });
let savedPath;
dialog.showSaveDialog = async () => { savedPath = join(output, `released-result-${Date.now()}.txt`); return { canceled: false, filePath: savedPath }; };
const handle = ipcMain.handle.bind(ipcMain);
const mutations = ['createTask', 'reviseTaskPlan', 'approve', 'createExperiment', 'admitExperimentAttempt', 'dispatchAdmittedAttempt', 'recordArtifactDisposition', 'recordArtifactCustodyRelease', 'exportReproductionBundle'];
ipcMain.handle = (channel, handler) => handle(channel, (event, ...args) => {
  if (mutations.some(name => channel === `tessera:${name}`)) args[args.length - 1] = 'native-execution-' + createHash('sha256').update(title + ':' + channel).digest('hex');
  return handler(event, ...args);
});
let window, loadResolve;
const loaded = new Promise(resolve => { loadResolve = resolve; });
app.on('browser-window-created', (_event, candidate) => {
  window = candidate;
  candidate.webContents.once('did-finish-load', loadResolve);
  const eventError = (error, eventStage) => { receipt.nativeDiagnostics ??= []; if (receipt.nativeDiagnostics.length < 8) receipt.nativeDiagnostics.push(safeError(error, eventStage)); };
  candidate.webContents.on('did-fail-load', (_event, code) => eventError({ code: String(code), message: 'Native load failed' }, 'renderer-load'));
  candidate.webContents.on('preload-error', (_event, _path, error) => eventError(error, 'preload'));
  candidate.webContents.on('console-message', details => { if (details.level === 'error') eventError({ message: details.message }, 'renderer-console'); });
});
const js = code => window.webContents.executeJavaScript(code, true);
async function wait(code) {
  const deadline = Date.now() + 20000;
  while (!await js(code)) { if (Date.now() > deadline) throw new Error('Native execution condition timed out. Inspect committed state before retry.'); await new Promise(resolve => setTimeout(resolve, 75)); }
}
async function screen(name) {
  await new Promise(resolve => setTimeout(resolve, 200));
  writeFileSync(join(output, name), (await window.webContents.capturePage()).toPNG(), { mode: 0o600 });
  receipt.screens.push({ name, kind: 'actual Electron pixels, not OS picker pixels' });
}
async function connectNative() {
  stage = 'native-connect-form';
  await js(`document.querySelector('#connection-open').click(); { const f=document.querySelector('#connection-form'); f.elements.name.value='Ordinary new execution'; f.elements.endpoint.value=${JSON.stringify(refs.origin)}; f.requestSubmit(); }`);
  await wait(`!![...document.querySelectorAll('#projects button')].find(n=>n.dataset.projectId===${JSON.stringify(refs.project_id)})`);
  stage = 'native-project-selection';
  await js(`[...document.querySelectorAll('#projects button')].find(n=>n.dataset.projectId===${JSON.stringify(refs.project_id)}).click()`);
  await wait(`!document.querySelector('#task-new').disabled`);
}
async function selectNativeExperiment(id) {
  await wait(`!![...document.querySelectorAll('[data-experiment-id]')].find(n=>n.dataset.experimentId===${JSON.stringify(id)})`);
  await js(`[...document.querySelectorAll('[data-experiment-id]')].find(n=>n.dataset.experimentId===${JSON.stringify(id)}).click()`);
  await wait(`document.querySelector('#experiment-detail').textContent.includes('Completeness: complete in-scope goal-to-decision chain.')`);
}
async function main() {
  // Privileged scheme registration in production main must precede app.ready.
  // Never defer its import behind an authenticated HTTP round trip.
  stage = 'production-main-import-and-load';
  await import('../main.mjs'); await loaded;
  stage = 'import-main-readback-client';
  const { ControlConnection } = await import('../connection.mjs');
  const verify = new ControlConnection();
  stage = 'main-readback-connect';
  await verify.connect({ id: 'native-execution-verification', name: 'Readback', endpoint: refs.origin, token }); token = null;
  try {
    await connectNative();
    if (phase === 'plan') {
      stage = 'main-task-inventory';
      let task = (await verify.tasks(refs.project_id, { offset: 0, limit: 100 })).tasks.find(t => t.title === title);
      if (task && !['draft', 'approved'].includes(task.status)) throw new Error('Unexpected committed task state; inspect before retry.');
      stage = 'native-task-create-form';
      if (!task) await js(`document.querySelector('#task-new').click(); { const f=document.querySelector('#task-form'); f.elements.title.value=${JSON.stringify(title)}; f.elements.purpose.value='Bounded ordinary CPU sum-squares; actual retained input basis, no scientific acceptance.'; f.elements.budget.value='0'; f.elements.duration.value='120'; f.requestSubmit(); }`);
      const card = `[...document.querySelectorAll('#tasks article')].find(n=>n.textContent.includes(${JSON.stringify(title)}))`;
      await wait(`!!${card}`);
      task = (await verify.tasks(refs.project_id, { offset: 0, limit: 100 })).tasks.find(t => t.title === title);
      if (task.status === 'draft' && task.revision === 1) {
        stage = 'native-revision-open';
        await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Revise this proposal').click()`);
        await wait(`document.querySelector('#task-revise-dialog').open`);
        stage = 'native-real-spec-form';
        await js(`{ const f=document.querySelector('#task-revise-form'); const s=${JSON.stringify(spec)}; for(const key of ['argv','pinned_skill_digests','output_paths'])f.elements[key].value=JSON.stringify(s[key]); for(const key of ['input_snapshot_digest','profile','max_output_bytes'])f.elements[key].value=s[key]; }`);
        await screen('01-real-plan-review.png');
        stage = 'native-real-spec-submit';
        await js(`document.querySelector('#task-revise-form').requestSubmit()`);
        await wait(`${card}?.textContent.includes('draft · task revision 2 · plan revision 2')`);
      }
      task = (await verify.tasks(refs.project_id, { offset: 0, limit: 100 })).tasks.find(t => t.title === title);
      stage = 'main-approved-basis-readback';
      const plan = await verify.taskPlan(task.id, task.plan_digest);
      if (plan.max_duration_seconds !== 120 || plan.budget_cents !== 0) throw new Error('Committed duration/budget differs; approval refused.');
      if (JSON.stringify(plan.execution_spec) !== JSON.stringify(spec) && createHash('sha256').update(JSON.stringify(Object.entries(plan.execution_spec).sort())).digest('hex') !== createHash('sha256').update(JSON.stringify(Object.entries(spec).sort())).digest('hex')) throw new Error('Committed execution spec differs; approval refused.');
      stage = 'native-human-approve';
      if (task.status === 'draft' && task.revision === 2) await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Approve this plan').click()`);
      await wait(`${card}?.textContent.includes('approved · task revision 3 · plan revision 2')`);
      task = (await verify.tasks(refs.project_id, { offset: 0, limit: 100 })).tasks.find(t => t.title === title);
      Object.assign(receipt, { task_id: task.id, task_revision: task.revision, plan_digest: task.plan_digest, execution_spec: spec, duration: 120, budget_cents: 0 });
      await js(`(${card}).scrollIntoView({block:'start',behavior:'instant'})`);
      await screen('02-approved-real-plan.png');
      const goal = `${title}: baseline10 and proposed intervention20`;
      let experiment = (await verify.experiments(refs.project_id, { offset: 0, limit: 100 })).experiments.find(e => e.goal === goal);
      if (!experiment) {
        stage = 'native-experiment-registration';
        const interventionSpec = { ...spec, argv: ['/hello', '--cpu-sum-squares', '20', '/outputs/result.txt'] };
        const fields = { goal, success_criterion: 'Operational check only: baseline reports iterations10/sum_squares385 in36retained bytes; no scientific acceptance.',
          protocol: 'Review retained protocol snapshot; execute approved baseline10 through ordinary worker. Intervention20 is a registered future procedure only, unapproved/unexecuted with no result or task binding.',
          baseline_name: 'Bounded sum squares10', baseline_method_digest: task.plan_digest,
          baseline_input_digest: spec.input_snapshot_digest, baseline_metadata: JSON.stringify({ procedure: spec.argv, expected_value: 385, expected_bytes: 36, expectation_only: true }),
          baseline_task_id: task.id, baseline_plan_digest: task.plan_digest,
          intervention_name: 'Proposed sum squares20 (not executed)', intervention_method_digest: createHash('sha256').update(JSON.stringify(interventionSpec)).digest('hex'),
          intervention_input_digest: spec.input_snapshot_digest, intervention_metadata: JSON.stringify({ procedure: interventionSpec.argv, proposal_only: true, execution_authority: false }) };
        await js(`document.querySelector('#experiment-new').click(); { const f=document.querySelector('#experiment-form'); const fields=${JSON.stringify(fields)}; for(const [key,value] of Object.entries(fields))f.elements[key].value=value; }`);
        await screen('03-native-design-review.png');
        await js(`document.querySelector('#experiment-form').requestSubmit()`);
        await wait(`!document.querySelector('#experiment-dialog').open`);
        experiment = (await verify.experiments(refs.project_id, { offset: 0, limit: 100 })).experiments.find(e => e.goal === goal);
      }
      if (!experiment) throw new Error('Native design registration not committed.');
      const ledger = await verify.experiment(experiment.id);
      const variant = ledger.variants.find(v => v.execution_task_id === task.id && v.execution_plan_digest === task.plan_digest);
      if (!variant) throw new Error('Native design does not retain exact reviewed task/plan.');
      receipt.experiment_id = experiment.id; receipt.variant_id = variant.id;
      await selectNativeExperiment(experiment.id); await screen('04-native-registered-variant.png');
      receipt.steps.push('Native real plan approval and experiment/variant registration committed; STOP for explicit operator human grant. Intervention20 is proposal-only, no result claimed.');
    } else {
      const grant = await verify.executionGrant(refs.grant_id);
      if (grant.task_id !== prior.task_id || grant.task_revision !== prior.task_revision || grant.plan_digest !== prior.plan_digest || grant.worker_principal_id !== refs.worker_principal_id || grant.snapshot_id !== refs.snapshot_id || grant.pinned_image !== refs.pinned_workload_image) throw new Error('Operator grant does not match the native reviewed basis.');
      if (['admit', 'dispatch'].includes(phase) && grant.status !== 'active') throw new Error('Operator grant is not active.');
      if (refs.experiment_id !== prior.experiment_id || refs.variant_id !== prior.variant_id) throw new Error('Operator design handoff differs from native registration.');
      await selectNativeExperiment(refs.experiment_id);
      const ledger = await verify.experiment(refs.experiment_id);
      if (phase === 'admit') {
        const variant = ledger.variants.find(v => v.id === refs.variant_id);
        if (!variant || variant.execution_task_id !== prior.task_id || variant.execution_plan_digest !== prior.plan_digest) throw new Error('Registered variant basis differs.');
        await wait(`!![...document.querySelectorAll('#experiment-detail article')].find(n=>n.textContent.includes(${JSON.stringify(prior.task_id)}) && n.querySelector('button'))`);
        await js(`[...document.querySelectorAll('#experiment-detail button')].find(n=>n.textContent==='Review separate execution admission' && n.parentElement.textContent.includes(${JSON.stringify(prior.task_id)})).click()`);
        await js(`document.querySelector('#admission-form').elements.grant_id.value=${JSON.stringify(refs.grant_id)}`); await screen('03-human-grant-admission.png');
        await js(`document.querySelector('#admission-form').requestSubmit()`);
        await wait(`!document.querySelector('#admission-dialog').open`);
        const after = await verify.experiment(refs.experiment_id);
        const attempt = after.admitted_attempts.find(a => a.grant_id === refs.grant_id && a.variant_id === refs.variant_id);
        if (!attempt) throw new Error('Committed admission not found.');
        Object.assign(receipt, { grant_id: refs.grant_id, experiment_id: refs.experiment_id, variant_id: refs.variant_id, admitted_attempt_id: attempt.id });
        receipt.steps.push('Native admission committed; STOP for enrolled worker lease/package via normal APIs. No dispatch created.');
      } else if (phase === 'dispatch') {
        const attempt = ledger.admitted_attempts.find(a => a.id === refs.admitted_attempt_id);
        if (!attempt || attempt.id !== prior.admitted_attempt_id || attempt.package_digest !== refs.package_digest) throw new Error('Worker-prepared package differs from admitted handoff.');
        await wait(`!!document.querySelector('[data-experiment-admitted-attempt-id="${attempt.id}"]')`);
        await js(`[...document.querySelector('[data-experiment-admitted-attempt-id="${attempt.id}"]').querySelectorAll('button')].find(n=>n.textContent==='Review Kubernetes dispatch').click()`);
        await js(`document.querySelector('#dispatch-form').elements.target_digest.value=${JSON.stringify(refs.target_digest)}`);
        // Keep dispatch prompt immediate: lease is short-lived, screenshots follow commit.
        const dispatchRequestedAt = prior.dispatch_authorized_at || new Date().toISOString();
        await js(`document.querySelector('#dispatch-form').requestSubmit()`);
        await wait(`!document.querySelector('#dispatch-dialog').open`); await screen('04-native-dispatch.png');
        receipt.steps.push('Explicit native target dispatch committed; ordinary worker observation/auto-output and review/export/reconnect remain next acceptance steps.');
        receipt.package_digest = attempt.package_digest; receipt.operation_id = attempt.operation_id;
        receipt.dispatch_authorized_at = dispatchRequestedAt;
      } else {
        if (!prior.dispatch_authorized_at || prior.operation_id === undefined) throw new Error('Committed native dispatch receipt required for output review.');
        const attempt = ledger.admitted_attempts.find(a => a.id === prior.admitted_attempt_id);
        if (!attempt || attempt.operation_id !== prior.operation_id || attempt.package_digest !== prior.package_digest || attempt.outcome !== null) throw new Error('Attempt/provenance/outcome basis changed before review.');
        const workflow = await verify.remoteWorkflow(prior.operation_id);
        receipt.operationalObservation = workflow.reconciliation;
        if (!workflow.reconciliation || workflow.reconciliation.observed_state !== 'exited' || workflow.reconciliation.termination_confirmed !== true) throw new Error('Ordinary worker terminal observation is not yet confirmed; resume later.');
        if (!refs.job_evidence_reference || !existsSync(refs.job_evidence_reference)) throw new Error('Integration-owned actual Job UID readback reference required, not a fixture assertion.');
        receipt.jobProof = validateJobWitness(JSON.parse(readFileSync(refs.job_evidence_reference, 'utf8')), prior, refs);
        receipt.newExecutionJobs = true; // Only after native dispatch + matching actual new Job identity/completion evidence.
        const manifests = (await verify.artifactManifests(refs.project_id, { offset: 0, limit: 100 })).artifact_manifests;
        const manifest = manifests.find(m => m.operation_id === prior.operation_id && m.plan_digest === prior.plan_digest);
        if (!manifest || manifest.late !== false || manifest.status !== 'quarantined' || manifest.artifacts.length !== 1) throw new Error('Automatic timely quarantine manifest not yet available or differs.');
        const artifact = manifest.artifacts[0];
        const expectedBytes = Buffer.from('{"iterations":10,"sum_squares":385}\n');
        const expectedHash = createHash('sha256').update(expectedBytes).digest('hex');
        if (artifact.size !== 36 || artifact.sha256 !== expectedHash || artifact.relative_path !== 'result.txt') throw new Error('Automatic retained output does not match bounded workload expectation.');
        receipt.output = { manifest_id: manifest.id, manifest_digest: manifest.manifest_digest, artifact_id: artifact.id, bytes: artifact.size, sha256: artifact.sha256, late: manifest.late, status: manifest.status, classification: manifest.classification };
        await js(`document.querySelector('#experiment-detail').scrollIntoView({block:'start',behavior:'instant'})`); await screen('05-new-attempt-provenance.png');
        await js(`document.querySelector('#artifacts').scrollIntoView({block:'start',behavior:'instant'})`); await screen('06-automatic-quarantine.png');
        receipt.steps.push('Actual new ordinary Job identity and worker terminal observation checked; automatic timely quarantined output matched36bytes/hash; scientific outcome remains null.');
        if (phase === 'export') {
          stage = 'native-human-disposition-review';
          const disposition = dispositionReview(refs.disposition_review, manifest);
          const current = (await verify.artifactDispositions(manifest.id, { offset: 0, limit: 100 })).dispositions;
          if (current.length === 100) throw new Error('Disposition history exceeds this bounded acceptance review.');
          const last = current.at(-1);
          if (!(prior.dispositionRecorded && last?.revision === prior.dispositionRecorded.revision && last.disposition === 'retain_quarantined' && last.manifest_digest === manifest.manifest_digest)) {
            await js(`document.querySelector('#artifact-disposition').click(); document.querySelector('#artifact-disposition-form').elements.manifest_id.value=${JSON.stringify(manifest.id)}; document.querySelector('#artifact-disposition-load').click()`);
            await wait(`!document.querySelector('#artifact-disposition-form button[type="submit"]').disabled`);
            if (!await js(`document.querySelector('#artifact-disposition-form').elements.expected_manifest_digest.value===${JSON.stringify(disposition.expected_manifest_digest)} && Number(document.querySelector('#artifact-disposition-form').elements.expected_revision.value)===${disposition.expected_revision}`)) throw new Error('Loaded disposition revision/digest differs from explicit human review.');
            await js(`document.querySelector('#artifact-disposition-form').elements.rationale.value=${JSON.stringify(disposition.rationale)}`);
            await screen('07a-human-disposition-review.png');
            await js(`document.querySelector('#artifact-disposition-form').requestSubmit()`);
            await wait(`!document.querySelector('#artifact-disposition-dialog').open`);
            const recorded = (await verify.artifactDispositions(manifest.id, { offset: 0, limit: 100 })).dispositions.at(-1);
            if (!recorded || recorded.disposition !== 'retain_quarantined' || recorded.manifest_digest !== manifest.manifest_digest || recorded.revision !== disposition.expected_revision + 1) throw new Error('Human disposition did not commit on exact reviewed basis.');
            receipt.dispositionRecorded = { manifest_id: manifest.id, revision: recorded.revision, disposition: recorded.disposition };
            writeFileSync(statePath, JSON.stringify(receipt, null, 2), { mode: 0o600 });
          }
          stage = 'native-separate-custody-release';
          const review = refs.custody_review;
          if (!review || review.human_approved !== true || review.manifest_id !== manifest.id || review.expected_manifest_digest !== manifest.manifest_digest || !Number.isSafeInteger(review.expected_revision) || review.expected_revision < 0 || typeof review.rationale !== 'string' || !review.rationale.trim()) throw new Error('Explicit exact-manifest human custody review required before release.');
          await js(`document.querySelector('#artifact-custody').click(); { const f=document.querySelector('#artifact-custody-form'); const r=${JSON.stringify(review)}; for(const key of ['manifest_id','expected_manifest_digest','expected_revision','rationale'])f.elements[key].value=r[key]; f.elements.action.value='release'; }`);
          await screen('07-human-custody-review.png');
          await js(`document.querySelector('#artifact-custody-form').requestSubmit()`);
          await wait(`!document.querySelector('#artifact-custody-dialog').open`);
          await js(`document.querySelector('#artifact-review').click(); { const f=document.querySelector('#artifact-form'); f.elements.manifest_id.value=${JSON.stringify(manifest.id)}; f.elements.artifact_id.value=${JSON.stringify(artifact.id)}; f.requestSubmit(); }`);
          await wait(`!document.querySelector('#artifact-download').disabled`); await screen('08-released-new-output.png');
          await js(`document.querySelector('#artifact-download').click()`);
          await wait(`document.querySelector('#notice').textContent.startsWith('Saved exact released artifact bytes:')`);
          const bytes = readFileSync(savedPath);
          if (!bytes.equals(expectedBytes)) throw new Error('Native saved bytes do not match exact retained workload output.');
          receipt.savedOutput = { path: savedPath, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
          await js(`document.querySelector('#artifact-bundle').click()`);
          await wait(`document.querySelector('#notice').textContent.startsWith('Reproduction bundle record exported:')`);
          receipt.reproductionBundle = await js(`document.querySelector('#notice').textContent`);
          await js(`document.querySelector('#artifact-dialog').close(); document.querySelector('#disconnect').click()`);
          await wait(`document.querySelector('#connection-status').textContent.includes('Not connected')`);
          await connectNative(); await selectNativeExperiment(refs.experiment_id);
          const after = await verify.experiment(refs.experiment_id);
          const restored = after.admitted_attempts.find(a => a.id === attempt.id);
          if (!restored || restored.operation_id !== attempt.operation_id || restored.package_digest !== attempt.package_digest || restored.outcome !== null || after.admitted_attempts.length !== ledger.admitted_attempts.length || after.operations.length !== ledger.operations.length) throw new Error('Reconnect changed the durable attempt/operation or replayed execution.');
          const retained = (await verify.artifactManifests(refs.project_id, { offset: 0, limit: 100 })).artifact_manifests.find(m => m.id === manifest.id);
          if (!retained || retained.manifest_digest !== manifest.manifest_digest) throw new Error('Reconnect changed retained output identity.');
          await js(`document.querySelector('#experiment-detail').scrollIntoView({block:'start',behavior:'instant'})`); await screen('09-reconnect-same-new-attempt.png');
          receipt.reconnect = { unchanged_attempt_id: attempt.id, unchanged_operation_id: attempt.operation_id, unchanged_manifest_id: manifest.id, no_command_replay: true };
          receipt.steps.push('Native explicit human custody release, exact36byte save, reproduction bundle and reconnect completed; no scientific outcome/acceptance recorded. OS file dialogs are labelled fallback only.');
        }
      }
    }
    receipt.status = phase === 'export' ? 'new native/API execution review/export/reconnect checks completed; actual OS file selection remains unverified' : 'phase completed; full new execution acceptance is not yet claimed';
  } finally { verify.disconnect(); }
}
const timer = setTimeout(() => { receipt.status = 'bounded timeout; inspect uncertain effects before retry'; finish(1); }, 90000);
function finish(code) { clearTimeout(timer); const bytes=JSON.stringify(receipt, null, 2); writeFileSync(statePath, bytes, { mode: 0o600 }); writeFileSync(join(output, `execution-${phase}-receipt.json`), bytes, { mode: 0o600 }); console.log(JSON.stringify({ phase, status: receipt.status, task_id: receipt.task_id, experiment_id: receipt.experiment_id, admitted_attempt_id: receipt.admitted_attempt_id, newExecutionJobs: receipt.newExecutionJobs })); app.exit(code); }
main().then(() => finish(0), error => { receipt.firstFailure = safeError(error, stage); receipt.status = 'failed; inspect initiating stage and committed effects before retry'; finish(1); });
