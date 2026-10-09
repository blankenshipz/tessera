import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = name => readFileSync(new URL(`../${name}`, import.meta.url), 'utf8');
const journeyMethods = ['reviseTaskPlan', 'createExperiment', 'reviseExperiment', 'admitExperimentAttempt',
  'dispatchAdmittedAttempt', 'resolveAdmittedAttempt', 'linkAttemptArtifacts', 'recordMeasurement',
  'createConclusion', 'updateConclusion', 'reviewConclusion', 'amendConclusion', 'artifacts',
  'artifactManifests', 'artifactDispositions', 'recordArtifactDisposition', 'recordArtifactCustodyRelease', 'releasedArtifactManifest', 'artifactPreview',
  'saveReleasedArtifact', 'exportReproductionBundle', 'reproductionBundle',
  'recoveryReconciliations', 'reconcileLostWorkerOperation', 'coordinatorConsumerStatus'];

test('preload exposes only named researcher-journey IPC methods', () => {
  const preload = source('preload.cjs');
  for (const method of journeyMethods) assert.match(preload, new RegExp(`'${method}'`));
  assert.doesNotMatch(preload, /contextBridge\.exposeInMainWorld\([^\n]*\b(?:send|fetch|require)\b/);
});

test('main binds each journey bridge method to a narrow control operation and keeps retrieval in main', () => {
  const main = source('main.mjs');
  for (const method of journeyMethods) assert.match(main, new RegExp(`\\b${method}\\b`));
  assert.match(main, /import \{ saveReleasedArtifact \} from '\.\/artifact-save\.mjs'/);
  assert.match(main, /saveReleasedArtifact\(\{ dialog, window, connection, manifestId, artifactId \}\)/);
  const save = source('artifact-save.mjs');
  assert.match(save, /connection\.captureOperationContext\(\)/);
  assert.match(save, /connection\.retrieveReleasedArtifact\(manifestId, artifactId\)/);
  assert.match(save, /constants\.O_WRONLY \| constants\.O_CREAT \| constants\.O_EXCL, 0o600/);
  assert.match(save, /file\.sync\(\)/);
  assert.match(save, /fs\.linkSync\(stage, choice\.filePath\)/);
  assert.doesNotMatch(source('renderer.mjs'), /retrieveReleasedArtifact|node:fs|linkSync/);
  assert.doesNotMatch(main, /contextBridge|webContents\.executeJavaScript\([^)]*token/);
});

test('artifact inventory pagination has a named accessible control and live result region', () => {
  const html = source('index.html');
  assert.match(html, /<div id="artifacts" class="cards" aria-live="polite"><\/div><button id="artifacts-more" hidden>More artifacts<\/button>/);
  assert.match(html, /<section class="artifacts" aria-labelledby="artifacts-heading">/);
});
