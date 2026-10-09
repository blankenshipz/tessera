'use strict';
// Opt-in installed-bench acceptance. No provider/job creation, token IPC or TLS bypass.
const { app, dialog, ipcMain } = require('electron');
const { createHash } = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync, existsSync } = require('node:fs');
const { join, resolve, basename } = require('node:path');
const run = promisify(execFile);
const refs = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (refs.route_service_reference) {
  const route = JSON.parse(readFileSync(refs.route_service_reference, 'utf8'));
  if (!route.ready || route.origin !== refs.origin || Date.parse(route.expires_at) <= Date.now()) {
    throw new Error('Integration-owned client route is unavailable or expired. Contact its owner; do not start or replace a forward.');
  }
}
const output = resolve(process.argv[3] || '/private/tmp/opencode/tessera-native-bench');
if (process.env.NODE_EXTRA_CA_CERTS !== refs.ca_file) throw new Error('Launch with NODE_EXTRA_CA_CERTS pointing to the referenced public CA.');
mkdirSync(output, { recursive: true, mode: 0o700 });
const receiptPath = join(output, 'receipt.json');
const previous = existsSync(receiptPath) ? JSON.parse(readFileSync(receiptPath, 'utf8')) : null;
const privateDir = mkdtempSync(join(output, 'private-'));
const headerPath = join(privateDir, 'owner.header');
// Authorized reference is read in main only. No token appears in args, renderer,
// screenshots or receipts. The selected production main reader revalidates it.
const credentialBytes = readFileSync(refs.owner_credential_file);
let token = credentialBytes.toString('utf8').trim();
if (token.startsWith('Authorization: Bearer ')) token = token.slice(22);
writeFileSync(headerPath, `Authorization: Bearer ${token}\n`, { mode: 0o600 });
credentialBytes.fill(0);
app.setPath('userData', join(privateDir, 'profile'));
let window, timer, loadedResolve, loadedReject;
const loaded = new Promise((yes, no) => { loadedResolve = yes; loadedReject = no; });
const receipt = { target: refs.origin, project: refs.project_id, experiment: refs.experiment_id,
  installedImage: refs.installed_image, screens: [], steps: [], picker: null,
  scientificValidity: 'not_assessed', newExecutionJobs: false };
// Keep keys across interrupted acceptance runs. The real production dispatcher,
// sender fences and API still run; only these three user-selected command keys
// are stable, never an approval/grant substitute.
const realHandle = ipcMain.handle.bind(ipcMain);
ipcMain.handle = (channel, handler) => realHandle(channel, async (event, ...args) => {
  if (['tessera:createTask', 'tessera:reviseTaskPlan', 'tessera:approve', 'tessera:exportReproductionBundle'].includes(channel) && receipt.proposedTitle) {
    args[args.length - 1] = 'native-bench-' + createHash('sha256').update(receipt.proposedTitle + ':' + channel + (channel === 'tessera:exportReproductionBundle' ? ':' + args[1] : '')).digest('hex');
  }
  return handler(event, ...args);
});
app.on('browser-window-created', (_event, candidate) => {
  window = candidate;
  candidate.webContents.once('did-finish-load', loadedResolve);
  candidate.webContents.once('did-fail-load', () => loadedReject(new Error('Native renderer failed to load.')));
  candidate.webContents.once('preload-error', () => loadedReject(new Error('Native preload failed.')));
  candidate.webContents.on('console-message', details => {
    if (details.level === 'error') {
      receipt.rendererErrors ??= [];
      receipt.rendererErrors.push(String(details.message).slice(0, 500));
    }
  });
});
async function js(code) { return window.webContents.executeJavaScript(code, true); }
async function wait(code) {
  const deadline = Date.now() + 20000;
  while (!await js(code)) {
    if (Date.now() > deadline) throw new Error(`Native bench condition timed out: ${code}`);
    await new Promise(resolve => setTimeout(resolve, 75));
  }
}
async function screen(name, scrollSelector) {
  if (scrollSelector) await js(`document.querySelector(${JSON.stringify(scrollSelector)}).scrollIntoView({block:'start',behavior:'instant'})`);
  await js('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  // Native compositor pixels can lag the DOM scroll/dialog state by a frame.
  await new Promise(resolve => setTimeout(resolve, 200));
  const image = await window.webContents.capturePage();
  writeFileSync(join(output, name), image.toPNG(), { mode: 0o600 });
  receipt.screens.push({ name, kind: 'actual Electron rendered pixels; not OS dialog pixels' });
}
async function automatePicker(path, save = false) {
  const script = `tell application "System Events"
tell (first application process whose unix id is ${process.pid})
set frontmost to true
delay 0.5
keystroke "g" using {command down, shift down}
delay 0.5
keystroke ${JSON.stringify(path)}
key code 36
delay 0.5
key code 36
end tell
end tell`;
  await run('/usr/bin/osascript', ['-e', script], { timeout: 8000 });
  return save;
}
async function main() {
  await import('../main.mjs'); await loaded;
  if (!window.isVisible()) throw new Error('Native application window is not visible.');
  await screen('01-disconnected.png');
  const realOpen = dialog.showOpenDialog.bind(dialog);
  const realSave = dialog.showSaveDialog.bind(dialog);
  let accessible = false;
  try {
    if (process.env.TESSERA_BENCH_TRY_ACTUAL_PICKER === '1') {
      accessible = true;
      receipt.picker = { mode: 'bounded actual production OS picker attempt', osDialogPixels: false };
    } else if (previous?.picker?.blocker) {
      receipt.picker = { ...previous.picker, evidenceReused: true, actualAttemptIsHistorical: true,
        mode: 'main-process file selection fallback; NOT actual OS picker acceptance',
        probeStatus: 'Bounded System Events probe unavailable; no assistive-access denial inferred.' };
    } else {
    await run('/usr/bin/osascript', ['-e', `tell application "System Events" to tell (first application process whose unix id is ${process.pid}) to get name of every window`], { timeout: 8000 });
    accessible = true;
    receipt.picker = { mode: 'actual OS dialog with targeted Accessibility automation', osDialogPixels: false };
    }
  } catch (error) {
    receipt.picker = { mode: 'main-process credential file selection fallback; NOT actual OS picker acceptance',
      blocker: String(error.stderr || error.message).replace(privateDir, '[private fixture directory]').slice(0, 1800),
      timedOut: Boolean(error.killed), probeTimeoutMs: 8000, osDialogPixels: false };
  }
  dialog.showOpenDialog = async (...args) => {
    if (!accessible) return { canceled: false, filePaths: [headerPath] };
    const selection = realOpen(...args);
    receipt.picker.actualOSDialogInvoked = true;
    try { await automatePicker(headerPath); }
    catch (error) {
      receipt.picker = { mode: 'actual OS picker invoked; file selection NOT observed',
        actualOSDialogInvoked: true, osDialogPixels: false, timedOut: Boolean(error.killed),
        blocker: `Targeted osascript interaction failed (8000ms bound; code ${error.code}; signal ${error.signal}). ` + String(error.stderr || '').slice(0, 1000),
        humanStep: 'Connect -> Select credential -> choose the private control credential header. Automation/Accessibility access was unavailable; no denial inferred.' };
      throw new Error('Actual OS picker interaction unavailable; own dialog will close on bounded test teardown.');
    }
    return selection;
  };
  let lastExportPath;
  dialog.showSaveDialog = async (...args) => {
    const options = args[1] || args[0];
    const exportPath = join(output, `${basename(options.defaultPath || 'bench-export')}-${process.pid}`);
    lastExportPath = exportPath;
    if (!accessible) return { canceled: false, filePath: exportPath };
    const selection = realSave(...args);
    await automatePicker(exportPath, true);
    return selection;
  };
  // Main-only independent read snapshot fences existing ordinary executions.
  const { ControlConnection } = await import('../connection.mjs');
  const verify = new ControlConnection();
  await verify.connect({ id: 'bench-verification', name: 'Bench acceptance', endpoint: refs.origin, token });
  token = null;
  const before = await verify.experiment(refs.experiment_id);
  const beforeAttempts = before.admitted_attempts.map(a => ({ id: a.id, outcome: a.outcome, revision: a.revision }));
  const title = previous?.proposedTitle || previous?.error?.match(/Native bench draft \d+/)?.[0] || `Native bench draft ${Date.now()}`;
  receipt.proposedTitle = title;
  const currentTasks = await verify.tasks(refs.project_id, { offset: 0, limit: 100 });
  const existingDraft = currentTasks.tasks.find(t => t.title === title);
  if (process.env.TESSERA_BENCH_HEALTH_ONLY === '1' && (!existingDraft || existingDraft.status !== 'approved' || existingDraft.revision !== 3)) throw new Error('Health-only admission requires the already approved native proposal; no mutation is permitted.');
  if (existingDraft && !((existingDraft.status === 'draft' && [1, 2].includes(existingDraft.revision)) || (existingDraft.status === 'approved' && existingDraft.revision === 3))) throw new Error('Prior native proposal has an unexpected state; inspect it before mutation.');
  if (existingDraft && existingDraft.revision > 1) {
    const plan = await verify.taskPlan(existingDraft.id, existingDraft.plan_digest);
    if (plan.execution_spec?.profile !== 'local-cpu' || JSON.stringify(plan.execution_spec.argv) !== '["/usr/bin/true"]' || plan.execution_spec.input_snapshot_digest !== 'a'.repeat(64)) throw new Error('Existing native proposal differs from the intended acceptance specification.');
  }
  receipt.priorDraftInspection = existingDraft ? `existing ${existingDraft.status} revision${existingDraft.revision} verified; no mutation replay` : 'prior project task inventory inspected; no matching draft';
  receipt.priorCompletedNativeSteps = previous?.steps || [];
  async function connect(name) {
    await js(`document.querySelector('#connection-open').click(); document.querySelector('#connection-form').elements.name.value=${JSON.stringify(name)}; document.querySelector('#connection-form').elements.endpoint.value=${JSON.stringify(refs.origin)}`);
    await screen('02-connect-dialog.png');
    await js(`document.querySelector('#connection-form').requestSubmit()`);
    await wait(`!![...document.querySelectorAll('#projects button')].find(n=>n.dataset.projectId===${JSON.stringify(refs.project_id)})`);
    await js(`[...document.querySelectorAll('#projects button')].find(n=>n.dataset.projectId===${JSON.stringify(refs.project_id)}).click()`);
    await wait(`document.querySelector('#workspace').hidden===false && !document.querySelector('#task-new').disabled`);
  }
  await connect('Installed ordinary research bench'); receipt.steps.push('connect: real TLS and production main-process private header reader');
  await screen('03-connected-project.png');
  if (!existingDraft) await js(`{ document.querySelector('#task-new').click(); const f=document.querySelector('#task-form'); f.elements.title.value=${JSON.stringify(title)}; f.elements.purpose.value='Native UI acceptance proposal only; no grant or new execution.'; f.elements.budget.value='0'; f.elements.duration.value='30'; f.requestSubmit(); }`);
  const card = `[...document.querySelectorAll('#tasks article')].find(n=>n.textContent.includes(${JSON.stringify(title)}))`;
  if (!existingDraft || existingDraft.revision === 1) {
    await wait(`${card}?.textContent.includes('Not yet supplied')`);
    receipt.steps.push('create: actual API null-spec research draft in existing bench project');
    await screen('04-null-spec-draft.png', '#tasks');
    await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Revise this proposal').click()`);
    await wait(`document.querySelector('#task-revise-dialog').open`);
    await js(`{ const f=document.querySelector('#task-revise-form'); f.elements.argv.value='["/usr/bin/true"]'; f.elements.input_snapshot_digest.value='${'a'.repeat(64)}'; f.elements.pinned_skill_digests.value='[]'; f.elements.output_paths.value='[]'; f.elements.profile.value='local-cpu'; f.elements.max_output_bytes.value='1'; }`);
    await screen('05-revision-editor.png');
    await js(`document.querySelector('#task-revise-form').requestSubmit()`);
    await wait(`${card}?.textContent.includes('draft · task revision 2 · plan revision 2')`);
    receipt.steps.push('propose: strict CPU spec revision persisted; placeholder input digest is proposal-only, not validated scientific input');
  }
  if (existingDraft?.status !== 'approved') {
    await wait(`${card}?.textContent.includes('draft · task revision 2 · plan revision 2')`);
    await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Approve this plan').click()`);
    await wait(`document.querySelector('#notice').textContent.includes('No execution was started')`);
  }
  await wait(`${card}?.textContent.includes('approved · task revision 3 · plan revision 2')`);
  if (!await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Plan approved').disabled`)) {
    receipt.uiAuthorityFenceIssues = ['Online health refresh re-enabled the Plan approved button on approved task3/plan2. No duplicate approval was invoked. Production renderer.mjs:398 resets all task button disabled states.'];
  }
  await screen('06-approved-task3-plan2.png', '#tasks');
  await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Revise this proposal').click()`);
  await wait(`document.querySelector('#task-revise-dialog').open`);
  if (!await js(`document.querySelector('#task-revise-form').elements.argv.value==='["/usr/bin/true"]'`)) throw new Error('Approved proposal editor lost its specification.');
  await screen('07-approved-edit-controls.png');
  await js(`document.querySelector('#task-revise-dialog').close()`);
  await new Promise(resolve => setTimeout(resolve, 200));
  receipt.approvedControlAfterEdit = await js(`({label:[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Plan approved').textContent,disabled:[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Plan approved').disabled})`);
  if (!receipt.approvedControlAfterEdit.disabled) receipt.uiAuthorityFenceIssues = ['After an explicit edit-control inspection, online health refresh re-enabled Plan approved on approved task3/plan2. No duplicate approval was invoked. Production renderer.mjs:398 resets task button disabled states.'];
  receipt.steps.push('approve/edit: explicit human plan approval, task3/plan2 and retained edit controls; no execution authority added');
  if (process.env.TESSERA_BENCH_HEALTH_ONLY === '1') {
    if (!existingDraft || existingDraft.status !== 'approved') throw new Error('Health-only admission requires the already approved native proposal; no mutations are needed.');
    if (!receipt.approvedControlAfterEdit.disabled) throw new Error('Native health refresh loosened the approved-plan fence.');
    const editEnabled = await js(`![...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Revise this proposal').disabled`);
    if (!editEnabled) throw new Error('Eligible revision editor did not remain available after the health tick.');
    await js(`[...(${card}).querySelectorAll('button')].find(n=>n.textContent==='Plan approved').scrollIntoView({block:'center',behavior:'instant'})`);
    await screen('13-health-approved-fence.png');
    receipt.healthFence = { taskRevision: 3, planRevision: 2, approvedDisabled: true, editEnabled: true,
      source: 'actual native guarded edit inspection followed by real main-process status health refresh' };
    receipt.previousJourneyReceiptReference = receiptPath;
    receipt.status = 'focused native health fence passed; actual OS picker selection remains an external human step';
    verify.disconnect();
    return;
  }
  await wait(`!![...document.querySelectorAll('[data-experiment-id]')].find(n=>n.dataset.experimentId===${JSON.stringify(refs.experiment_id)})`);
  await js(`[...document.querySelectorAll('[data-experiment-id]')].find(n=>n.dataset.experimentId===${JSON.stringify(refs.experiment_id)}).click()`);
  await wait(`document.querySelector('#experiment-detail').textContent.includes('Completeness: complete in-scope goal-to-decision chain.')`);
  await screen('08-existing-ordinary-execution-review.png', '#experiment-detail');
  receipt.experimentText = await js(`document.querySelector('#experiment-detail').textContent`);
  receipt.steps.push('execution/status review: existing ordinary bench attempts only; no new job/admission/dispatch');
  receipt.lateArtifacts = (await verify.artifacts(refs.project_id, { offset: 0, limit: 100 })).artifacts.map(a => ({ id: a.id, bytes: a.size, status: a.status, classification: a.classification }));
  if (!receipt.lateArtifacts.length || receipt.lateArtifacts.some(a => a.status !== 'quarantined' || a.classification !== 'unclassified')) throw new Error('Late output custody changed; inspect before acceptance.');
  await wait(`document.querySelector('#artifacts').textContent.includes(${JSON.stringify(receipt.lateArtifacts[0].bytes + ' bytes')})`);
  await screen('08-late-quarantine-inventory.png', '#artifacts');
  const projects = await verify.projects({ offset: 0, limit: 100 });
  const completedProject = projects.projects.find(p => p.name === 'Ordinary installed bench repaired acceptance r2');
  if (!completedProject) throw new Error('Completed ordinary comparison project is unavailable.');
  const manifests = (await verify.artifactManifests(completedProject.id, { offset: 0, limit: 100 })).artifact_manifests;
  const released = [];
  for (const manifest of manifests) {
    const projection = await verify.releasedArtifactManifest(manifest.id);
    for (const artifact of projection.manifest.artifacts) released.push({ manifest: manifest.id, artifact, operation: manifest.operation_id });
  }
  if (released.length !== 2) throw new Error('Expected two already human-released ordinary comparison outputs.');
  const experiments = await verify.experiments(completedProject.id, { offset: 0, limit: 100 });
  let completedLedger;
  for (const experiment of experiments.experiments) {
    const ledger = await verify.experiment(experiment.id);
    if (ledger.operations.some(o => released.some(r => r.operation === o.operation_id))) { completedLedger = ledger; break; }
  }
  if (!completedLedger) throw new Error('Retained ordinary output provenance does not match an experiment.');
  await js(`[...document.querySelectorAll('#projects button')].find(n=>n.dataset.projectId===${JSON.stringify(completedProject.id)}).click()`);
  await wait(`!![...document.querySelectorAll('[data-experiment-id]')].find(n=>n.dataset.experimentId===${JSON.stringify(completedLedger.experiment.id)})`);
  await js(`[...document.querySelectorAll('[data-experiment-id]')].find(n=>n.dataset.experimentId===${JSON.stringify(completedLedger.experiment.id)}).click()`);
  await wait(`document.querySelector('#experiment-detail').textContent.includes('Completeness: complete in-scope goal-to-decision chain.')`);
  await screen('09-completed-comparison-review.png', '#experiment-detail');
  receipt.completedComparison = { project: completedProject.id, experiment: completedLedger.experiment.id,
    admittedAttempts: completedLedger.admitted_attempts.map(a => ({ id: a.id, outcome: a.outcome, operationalState: a.operational_state })) };
  receipt.completedComparisonText = await js(`document.querySelector('#experiment-detail').textContent`);
  receipt.exports = [];
  for (const [index, item] of released.entries()) {
    await js(`document.querySelector('#artifact-review').click(); { const f=document.querySelector('#artifact-form'); f.elements.manifest_id.value=${JSON.stringify(item.manifest)}; f.elements.artifact_id.value=${JSON.stringify(item.artifact.id)}; f.requestSubmit(); }`);
    await wait(`!document.querySelector('#artifact-download').disabled`);
    await screen(`11-released-output-${index}.png`);
    await js(`document.querySelector('#artifact-download').click()`);
    await wait(`document.querySelector('#notice').textContent.startsWith('Saved exact released artifact bytes:')`);
    const bytes = readFileSync(lastExportPath);
    const sha256 = createHash('sha256').update(bytes).digest('hex');
    if (sha256 !== item.artifact.sha256 || bytes.length !== item.artifact.size) throw new Error('Native output export failed its retained-byte integrity check.');
    receipt.exports.push({ manifest: item.manifest, artifact: item.artifact.id, bytes: bytes.length, sha256, path: lastExportPath });
    if (index === 0) {
      await js(`document.querySelector('#artifact-bundle').click()`);
      await wait(`document.querySelector('#notice').textContent.startsWith('Reproduction bundle record exported:')`);
      receipt.reproductionBundle = await js(`document.querySelector('#notice').textContent`);
    }
    await js(`document.querySelector('#artifact-dialog').close()`);
  }
  receipt.steps.push('review/export: completed ordinary comparison and current late-quarantine metadata reviewed; exact released outputs saved and bounded reproduction bundle exported; no scientific acceptance');
  await js(`document.querySelector('#disconnect').click()`);
  await wait(`document.querySelector('#connection-status').textContent==='Not connected'`);
  await connect('Installed bench reconnect');
  await wait(`${card}?.textContent.includes('approved · task revision 3 · plan revision 2')`);
  await screen('10-reconnected-durable-plan.png', '#tasks');
  const after = await verify.experiment(refs.experiment_id);
  const afterAttempts = after.admitted_attempts.map(a => ({ id: a.id, outcome: a.outcome, revision: a.revision }));
  if (JSON.stringify(beforeAttempts) !== JSON.stringify(afterAttempts) || before.operations.length !== after.operations.length) throw new Error('Existing execution history changed during UI review.');
  receipt.existingAttempts = afterAttempts;
  receipt.steps.push('reconnect: same durable plan/experiment with unchanged attempts/operational count, no command replay');
  receipt.status = accessible && !receipt.uiAuthorityFenceIssues ? 'passed' : 'partial: independent native/API journey completed; OS picker and/or UI approval fence remain explicitly unverified/failed';
  verify.disconnect();
}
Promise.race([main(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Bounded native bench acceptance timed out.')), 180000); })])
  .catch(error => { receipt.status = 'failed'; receipt.error = error.message; process.exitCode = 1; })
  .finally(() => {
    clearTimeout(timer);
    if (receipt.picker?.blocker) {
      receipt.privateCredentialHeaderReference = headerPath;
      receipt.humanPickerStep = 'Open the desktop, Connect to the referenced origin, Select credential, choose privateCredentialHeaderReference. Delete this private header after verification.';
    }
    writeFileSync(join(output, process.env.TESSERA_BENCH_HEALTH_ONLY === '1' ? 'health-receipt.json' : 'receipt.json'), JSON.stringify(receipt, null, 2), { mode: 0o600 });
    console.log(JSON.stringify({ status: receipt.status, picker: receipt.picker, steps: receipt.steps, screens: receipt.screens.length, error: receipt.error }));
    if (window && !window.isDestroyed()) window.destroy();
    rmSync(receipt.picker?.blocker ? join(privateDir, 'profile') : privateDir, { recursive: true, force: true });
    app.exit(process.exitCode || 0);
  });
