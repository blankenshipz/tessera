'use strict';
// Opt-in test entry point. Never loaded by the production application.
const { mkdtempSync, readFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { app, dialog } = require('electron');
const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const userDataDir = mkdtempSync(join(tmpdir(), 'tessera-electron-control-'));
let window;
let timer;
const errors = [];
let loaded;
let failed;
const load = new Promise((resolve, reject) => { loaded = resolve; failed = reject; });
app.on('browser-window-created', (_event, candidate) => {
  window = candidate;
  candidate.webContents.on('did-finish-load', loaded);
  candidate.webContents.on('did-fail-load', () => failed(new Error('Renderer load failed.')));
  candidate.webContents.on('render-process-gone', () => failed(new Error('Renderer exited.')));
  candidate.webContents.on('preload-error', (_event, _path, error) => failed(error));
  candidate.webContents.on('console-message', details => {
    if (details.level === 'error') errors.push(details.message);
  });
});
// Only the native picker is substituted; the main process still reads and
// validates the temporary header, and no bearer token enters renderer memory.
dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [fixture.header] });
let saves = 0;
dialog.showSaveDialog = async () => ({ canceled: false,
  filePath: !fixture.held && saves++ === 0 ? fixture.authoredExportPath : fixture.exportPath });
async function main() {
  if (app.commandLine.hasSwitch('no-sandbox')) throw new Error('Sandbox is required.');
  app.setPath('userData', userDataDir);
  await import('../main.mjs');
  await load;
  const preferences = window.webContents.getLastWebPreferences();
  if (!preferences.sandbox || !preferences.contextIsolation || preferences.nodeIntegration) {
    throw new Error('Application isolation settings are missing.');
  }
  const result = await window.webContents.executeJavaScript(`(async () => {
    const expected = ${JSON.stringify({ endpoint: fixture.endpoint, projectId: fixture.projectId, decisionId: fixture.decisionId, experimentId: fixture.experimentId, studyId: fixture.studyId, coordinatorProposal: fixture.coordinatorProposal, statusAnswer: fixture.statusAnswer, uiCreatedProjectName: fixture.uiCreatedProjectName, held: Boolean(fixture.held) })};
    const waitFor = async predicate => {
      const deadline = Date.now() + 10000;
      while (!predicate()) {
        if (Date.now() > deadline) throw new Error('UI condition timed out at ' + predicate.toString() + '; notice: ' + document.querySelector('#notice').textContent);
        await new Promise(resolve => setTimeout(resolve, 25));
      }
    };
    document.querySelector('#connection-open').click();
    const form = document.querySelector('#connection-form');
    form.elements.name.value = 'Synthetic fixture';
    form.elements.endpoint.value = expected.endpoint;
    form.requestSubmit();
    await waitFor(() => document.querySelector('#projects button'));
    const project = [...document.querySelectorAll('#projects button')].find(button => button.dataset.projectId === expected.projectId);
    if (!project) throw new Error('Expected synthetic project is missing.');
    project.click();
    if (!expected.held) {
      document.querySelector('#project-new').click();
      const projectForm = document.querySelector('#project-form');
      projectForm.elements.name.value = expected.uiCreatedProjectName;
      projectForm.requestSubmit();
      await waitFor(() => [...document.querySelectorAll('#projects button')].some(button => button.textContent === expected.uiCreatedProjectName));
      const createdProject = [...document.querySelectorAll('#projects button')].find(button => button.textContent === expected.uiCreatedProjectName);
      createdProject.click();
      await waitFor(() => document.querySelector('#project-title').textContent === expected.uiCreatedProjectName && !document.querySelector('#workspace').hidden);
      const originalProject = [...document.querySelectorAll('#projects button')].find(button => button.dataset.projectId === expected.projectId);
      if (!originalProject) throw new Error('Original synthetic project is missing after UI project creation.');
      originalProject.click();
    }
    let planRegression = null;
    if (!expected.held) {
      await waitFor(() => !document.querySelector('#task-new').disabled);
      document.querySelector('#task-new').click();
      const draft = document.querySelector('#task-form');
      draft.elements.title.value = 'Synthetic desktop revised plan';
      draft.elements.purpose.value = 'Native regression only: never grant, admit or execute.';
      draft.elements.budget.value = '0'; draft.elements.duration.value = '30';
      draft.requestSubmit();
      const planCard = () => [...document.querySelectorAll('#tasks article')].find(node => node.textContent.includes('Synthetic desktop revised plan'));
      await waitFor(() => planCard()?.textContent.includes('Not yet supplied'));
      [...planCard().querySelectorAll('button')].find(button => button.textContent === 'Revise this proposal').click();
      const revision = document.querySelector('#task-revise-form');
      if (!document.querySelector('#task-revise-dialog').open || revision.elements.input_snapshot_digest.value !== '') throw new Error('Null-spec draft editor unavailable or invented input digest.');
      revision.elements.argv.value = '["/usr/bin/true"]';
      revision.elements.input_snapshot_digest.value = 'a'.repeat(64);
      revision.elements.pinned_skill_digests.value = '[]';
      revision.elements.output_paths.value = '[]';
      revision.elements.profile.value = 'local-cpu';
      revision.elements.max_output_bytes.value = '1';
      revision.requestSubmit();
      await waitFor(() => planCard()?.textContent.includes('draft · task revision 2 · plan revision 2'));
      if (!planCard().textContent.includes('Execution destination: local-cpu')) throw new Error('Strict CPU specification was not displayed.');
      [...planCard().querySelectorAll('button')].find(button => button.textContent === 'Approve this plan').click();
      await waitFor(() => planCard()?.textContent.includes('approved · task revision 3 · plan revision 2'));
      const approved = [...planCard().querySelectorAll('button')].find(button => button.textContent === 'Plan approved');
      if (!approved?.disabled) throw new Error('Approved plan offered duplicate approval.');
      await waitFor(() => document.querySelector('#notice').textContent.includes('No execution was started'));
      [...planCard().querySelectorAll('button')].find(button => button.textContent === 'Revise this proposal').click();
      if (revision.elements.argv.value !== '["/usr/bin/true"]' || !document.querySelector('#task-revise-review').textContent.includes('revision 2')) throw new Error('Post-approval editor lost immutable plan basis.');
      document.querySelector('#task-revise-dialog').close();
      planRegression = { nullSpecEditable: true, profile: 'local-cpu', taskRevision: 3, planRevision: 2, approvedEditorAvailable: true, implicitExecution: false };
    }
    await waitFor(() => document.querySelector('#events').textContent.includes(expected.coordinatorProposal) &&
      document.querySelector('#events').textContent.includes(expected.statusAnswer));
    if (!document.querySelector('#message-form button').textContent.includes('Record conversation') ||
        !document.querySelector('#context-form button').textContent.includes('Queue context') ||
        !document.querySelector('#binding-load').textContent.includes('Fetch binding')) {
      throw new Error('Conversation and harness-context actions are not distinct.');
    }
    if (expected.held) {
      await waitFor(() => !document.querySelector('#recovery-banner').hidden);
      for (const id of ['project-new', 'task-new', 'thread-new', 'study-new']) {
        if (!document.querySelector('#' + id).disabled) throw new Error('Held control is enabled: ' + id);
      }
    } else {
      await waitFor(() => document.querySelector('#decisions textarea'));
      const card = [...document.querySelectorAll('#decisions article')].find(node => node.textContent.includes(expected.decisionId));
      if (!card) throw new Error('Expected synthetic decision is missing.');
      card.querySelector('textarea').value = 'Synthetic UI acceptance fixture.';
      [...card.querySelectorAll('button')].find(button => button.textContent === 'Approve').click();
      await waitFor(() => document.querySelector('#decisions').textContent.includes('No pending decisions.'));
    }
    if (!expected.held) {
      document.querySelector('#study-new').click();
      const studyForm = document.querySelector('#study-form');
      studyForm.elements.purpose.value = 'Synthetic authored study';
      studyForm.elements.protocol.value = 'Record-only UI fixture; no execution.';
      studyForm.elements.mode.value = 'exploratory';
      studyForm.requestSubmit();
      await waitFor(() => [...document.querySelectorAll('[data-study-id]')].some(node => node.textContent.includes('Synthetic authored study')));
      [...document.querySelectorAll('[data-study-id]')].find(node => node.textContent.includes('Synthetic authored study')).click();
      await waitFor(() => !document.querySelector('#variant-new').hidden && !document.querySelector('#variant-new').disabled);
      document.querySelector('#variant-new').click();
      const variantForm = document.querySelector('#variant-form');
      variantForm.elements.name.value = 'Synthetic registered variant';
      variantForm.elements.method_digest.value = 'e'.repeat(64);
      variantForm.elements.input_digest.value = 'f'.repeat(64);
      variantForm.elements.metadata.value = '{"synthetic":true}';
      variantForm.elements.replication_label.value = 'unavailable';
      variantForm.requestSubmit();
      await waitFor(() => document.querySelectorAll('#study-detail [data-variant-id]').length === 1);
      if (!document.querySelector('#study-detail').textContent.includes('Synthetic registered variant')) {
        throw new Error('Registered variant is missing.');
      }
      document.querySelector('#variant-new').click();
      variantForm.elements.name.value = 'Synthetic second registered variant';
      variantForm.elements.method_digest.value = 'e'.repeat(64);
      variantForm.elements.input_digest.value = 'f'.repeat(64);
      variantForm.elements.metadata.value = '{"synthetic":true}';
      variantForm.elements.replication_label.value = 'unavailable';
      variantForm.requestSubmit();
      await waitFor(() => document.querySelectorAll('#study-detail [data-variant-id]').length === 2);
      if (!document.querySelector('#study-close').disabled) throw new Error('Unaccounted study can close.');
      for (const [index, status] of ['failed', 'abandoned'].entries()) {
        document.querySelector('#study-detail [data-record-variant-id]').click();
        const outcomeForm = document.querySelector('#outcome-form');
        outcomeForm.elements.status.value = status;
        outcomeForm.elements.reason.value = 'Synthetic accounting fixture; no provider execution.';
        outcomeForm.elements.provenance.value = 'manual';
        outcomeForm.elements.outcome.value = '{"synthetic":true}';
        outcomeForm.requestSubmit();
        await waitFor(() => document.querySelectorAll('#study-detail [data-variant-id]').length === 2 &&
          document.querySelectorAll('#study-detail [data-record-variant-id]').length === 1 - index);
      }
      document.querySelector('#source-new').click();
      const sourceForm = document.querySelector('#source-form');
      sourceForm.elements.uri.value = 'urn:tessera:synthetic:authored';
      sourceForm.elements.quoted_passage.value = 'Synthetic evidence about retained failed and abandoned variants.';
      sourceForm.elements.content_digest.value = 'c'.repeat(64);
      sourceForm.requestSubmit();
      await waitFor(() => document.querySelectorAll('#study-detail [data-source-id]').length === 1);
      document.querySelector('[data-assessment-source-id]').click();
      const assessmentForm = document.querySelector('#assessment-form');
      assessmentForm.elements.verdict.value = 'unsupported';
      assessmentForm.elements.reason.value = 'Synthetic fixture has not established support.';
      assessmentForm.requestSubmit();
      await waitFor(() => document.querySelector('[data-source-id] .tag')?.textContent.startsWith('unsupported'));
      document.querySelector('#finding-new').click();
      const findingForm = document.querySelector('#finding-form');
      findingForm.elements.statement.value = 'Synthetic finding about retained failed and abandoned variants.';
      document.querySelector('#finding-sources input').checked = true;
      for (const input of document.querySelectorAll('#finding-variants input')) input.checked = true;
      findingForm.requestSubmit();
      await waitFor(() => document.querySelector('[data-accept-finding-id]'));
      if (!document.querySelector('[data-accept-finding-id]').disabled) throw new Error('Unsupported citation permits acceptance.');
      document.querySelector('[data-assessment-source-id]').click();
      assessmentForm.elements.verdict.value = 'supported';
      assessmentForm.elements.reason.value = 'Explicit synthetic human assessment; not scientific validation.';
      assessmentForm.requestSubmit();
      await waitFor(() => document.querySelector('[data-accept-finding-id]') && !document.querySelector('[data-accept-finding-id]').disabled);
      document.querySelector('[data-accept-finding-id]').click();
      if (!document.querySelector('#finding-review').textContent.includes('Synthetic evidence about retained')) throw new Error('Review omits cited passage.');
      document.querySelector('#finding-accept-form').requestSubmit();
      await waitFor(() => document.querySelector('[data-finding-id] .tag')?.textContent.startsWith('accepted'));
      document.querySelector('[data-assessment-source-id]').click();
      assessmentForm.elements.verdict.value = 'unsupported';
      assessmentForm.elements.reason.value = 'Synthetic review reopened.';
      assessmentForm.requestSubmit();
      await waitFor(() => document.querySelector('[data-reaffirm-finding-id]'));
      if (!document.querySelector('[data-reaffirm-finding-id]').disabled) throw new Error('Unsupported evidence permits reaffirmation.');
      document.querySelector('[data-assessment-source-id]').click();
      assessmentForm.elements.verdict.value = 'supported';
      assessmentForm.elements.reason.value = 'Synthetic review resolved.';
      assessmentForm.requestSubmit();
      await waitFor(() => document.querySelector('[data-reaffirm-finding-id]') && !document.querySelector('[data-reaffirm-finding-id]').disabled);
      document.querySelector('[data-reaffirm-finding-id]').click();
      document.querySelector('#finding-reaffirmation-reason').value = 'Synthetic reaffirmation of unchanged evidence.';
      document.querySelector('#finding-accept-form').requestSubmit();
      await waitFor(() => document.querySelector('[data-finding-id] .tag')?.textContent.startsWith('accepted'));
      document.querySelector('[data-correction-source-id]').click();
      const correctionForm = document.querySelector('#correction-form');
      correctionForm.elements.uri.value = 'urn:tessera:synthetic:authored-corrected';
      correctionForm.elements.quoted_passage.value = 'Synthetic corrected evidence supersedes the original assertion.';
      correctionForm.elements.content_digest.value = 'd'.repeat(64);
      correctionForm.elements.reason.value = 'Synthetic correction requires review.';
      correctionForm.requestSubmit();
      await waitFor(() => document.querySelectorAll('#study-detail [data-source-id]').length === 2 &&
        document.querySelector('[data-finding-id] .tag')?.textContent.startsWith('needs_review'));
      if (!document.querySelector('[data-reaffirm-finding-id]').disabled) throw new Error('Superseded citation permits reaffirmation.');
      document.querySelector('[data-revise-finding-id]').click();
      const reviseForm = document.querySelector('#finding-revise-form');
      reviseForm.elements.kind.value = 'supersede';
      reviseForm.elements.statement.value = 'Synthetic replacement claim cites corrected evidence.';
      reviseForm.elements.reason.value = 'Synthetic revision preserves the original claim.';
      const revisedSources = document.querySelectorAll('#finding-revise-sources input');
      revisedSources[revisedSources.length - 1].checked = true;
      for (const input of document.querySelectorAll('#finding-revise-variants input')) input.checked = true;
      reviseForm.requestSubmit();
      await waitFor(() => document.querySelectorAll('[data-finding-id]').length === 2 && document.querySelector('[data-accept-finding-id]'));
      if (!document.querySelector('[data-accept-finding-id]').disabled) throw new Error('Replacement accepted without support review.');
      document.querySelector('[data-assessment-source-id]').click();
      assessmentForm.elements.verdict.value = 'supported';
      assessmentForm.elements.reason.value = 'Synthetic replacement evidence review.';
      assessmentForm.requestSubmit();
      await waitFor(() => document.querySelector('[data-accept-finding-id]') && !document.querySelector('[data-accept-finding-id]').disabled);
      document.querySelector('[data-accept-finding-id]').click();
      document.querySelector('#finding-accept-form').requestSubmit();
      await waitFor(() => Array.from(document.querySelectorAll('[data-finding-id] .tag')).some(tag => tag.textContent.startsWith('accepted')));
      document.querySelector('[data-retract-finding-id]').click();
      const retractForm = document.querySelector('#finding-retract-form');
      retractForm.elements.reason.value = 'Synthetic retraction after corrected evidence.';
      retractForm.requestSubmit();
      await waitFor(() => Array.from(document.querySelectorAll('[data-finding-id] .tag')).some(tag => tag.textContent.startsWith('retracted')));
      if (document.querySelector('[data-retract-finding-id], [data-reaffirm-finding-id], [data-accept-finding-id], [data-revise-finding-id]')) throw new Error('Terminal finding still offers review commands.');
      document.querySelector('#study-close').click();
      document.querySelector('#study-close-form').requestSubmit();
      await waitFor(() => document.querySelector('#study-detail').textContent.includes('closed · revision 2'));

      document.querySelector('#study-export').click();
      await waitFor(() => document.querySelector('#notice').textContent.endsWith('authored-study.json'));

    }
    await waitFor(() => [...document.querySelectorAll('[data-study-id]')].some(node => node.dataset.studyId === expected.studyId));
    [...document.querySelectorAll('[data-study-id]')].find(node => node.dataset.studyId === expected.studyId).click();
    await waitFor(() => document.querySelectorAll('#study-detail [data-variant-id]').length === 80);
    const detail = document.querySelector('#study-detail');
    if (expected.held && (!document.querySelector('#variant-new').disabled ||
        [...detail.querySelectorAll('[data-record-variant-id]')].some(button => !button.disabled))) {
      throw new Error('Held research authoring is enabled.');
    }
    if (detail.querySelectorAll('[data-source-id]').length !== 2 ||
        detail.querySelectorAll('[data-finding-id]').length !== 1 ||
        detail.querySelectorAll('[data-correction-id]').length !== 1) {
      throw new Error('Research history is incomplete.');
    }
    for (const expectedText of ['Synthetic variant 79', 'Synthetic original passage',
        'Synthetic corrected passage', 'Synthetic correction fixture',
        'Needs review.', 'Outcome provenance: manual', 'Execution receipt: none recorded']) {
      if (!detail.textContent.includes(expectedText)) throw new Error('Missing research record: ' + expectedText);
    }
    await waitFor(() => !document.querySelector('#study-export').hidden);
    document.querySelector('#study-export').click();
    await waitFor(() => document.querySelector('#notice').textContent === 'Saved study ledger JSON: ' + (expected.held ? 'held-study.json' : 'study.json'));
    const inspectExperiment = async () => {
      await waitFor(() => [...document.querySelectorAll('[data-experiment-id]')].some(node => node.dataset.experimentId === expected.experimentId));
      [...document.querySelectorAll('[data-experiment-id]')].find(node => node.dataset.experimentId === expected.experimentId).click();
      await waitFor(() => document.querySelector('#experiment-detail').textContent.includes('Completeness: complete in-scope goal-to-decision chain.'));
      const experimentDetail = document.querySelector('#experiment-detail');
      for (const expectedText of ['Current goal: Current synthetic goal.', 'Design revision 1', 'Design revision 2',
          'Synthetic baseline v1', 'Synthetic intervention v2', 'Attempt-recorded outcome: failed',
          'Attempt-recorded outcome: inconclusive', 'Synthetic uncertainty remains unresolved.',
          'No attempts recorded for this registered variant.',
          'Operation provenance: no operation observed (human-reported status).',
          'Attempt-recorded outcome is not scientific success. Scientific validity: not assessed.']) {
        if (!experimentDetail.textContent.includes(expectedText)) throw new Error('Missing experiment history: ' + expectedText);
      }
      if (experimentDetail.querySelector('button')) throw new Error('Read-only experiment history exposes a mutation control.');
      return experimentDetail.textContent;
    };
    // Reload after the UI-authored records so the first comparison is the
    // complete persisted workbench rather than its initial stale projection.
    if (!expected.held) {
      document.querySelector('#refresh').click();
      await waitFor(() => document.querySelector('#events').textContent.includes('research.study_closed'));
    }
    const originalWorkbenchHistory = document.querySelector('#events').textContent;
    const originalExperimentHistory = await inspectExperiment();
    document.querySelector('#disconnect').click();
    await waitFor(() => document.querySelector('#connection-status').textContent === 'Not connected' && document.querySelector('#workspace').hidden);
    document.querySelector('#connection-open').click();
    const reconnectForm = document.querySelector('#connection-form');
    reconnectForm.elements.name.value = 'Synthetic fixture reconnect';
    reconnectForm.elements.endpoint.value = expected.endpoint;
    reconnectForm.requestSubmit();
    await waitFor(() => document.querySelector('#projects button'));
    const reconnectedProject = [...document.querySelectorAll('#projects button')].find(button => button.dataset.projectId === expected.projectId);
    if (!reconnectedProject) throw new Error('Expected project is missing after reconnect.');
    reconnectedProject.click();
    await waitFor(() => document.querySelector('#events').textContent.includes(expected.coordinatorProposal) &&
      document.querySelector('#events').textContent.includes(expected.statusAnswer));
    if (document.querySelector('#events').textContent !== originalWorkbenchHistory) throw new Error('Coordinator workbench history changed across reconnect.');
    const reconnectedExperimentHistory = await inspectExperiment();
    if (reconnectedExperimentHistory !== originalExperimentHistory) throw new Error('Experiment history changed across reconnect.');
    if (typeof window.require !== 'undefined') throw new Error('Node is exposed to renderer.');
    if (planRegression) {
      await waitFor(() => document.querySelector('#tasks').textContent.includes('approved · task revision 3 · plan revision 2'));
      planRegression.reconnected = true;
    }
    return { href: location.href, decisionRemoved: !expected.held, held: expected.held, researchVariants: 80, experimentReconnected: true, workbenchReconnected: true, planRegression };
  })()`, true);
  if (result.href !== 'tessera://app/index.html' || errors.length) throw new Error('Unexpected renderer state.');
  console.log(JSON.stringify(result));
}
(async () => {
  let status = 0;
  try {
    await Promise.race([main(), new Promise((_, reject) => {
      // The plan regression adds three persisted mutations and full project
      // reloads to the existing research/export/reconnect sequence.
      timer = setTimeout(() => reject(new Error('Desktop control smoke timed out.')), 60000);
    })]);
  } catch (error) {
    console.error(error.stack || error); status = 1;
  } finally {
    clearTimeout(timer);
    rmSync(userDataDir, { recursive: true, force: true });
    app.exit(status);
  }
})();
