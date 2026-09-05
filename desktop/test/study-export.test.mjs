import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, lstat, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { saveStudyExport } from '../study-export.mjs';

const id = 'connection_1';
const studyId = 'study_1';
const projectId = 'project_1';
const ledger = (overrides = {}) => ({
  completeness: 'complete_in_scope_study_ledger',
  study: { id: studyId, project_id: projectId },
  variants: [], sources: [], findings: [], corrections: [], events: [], ...overrides,
});
const context = (root, overrides = {}) => ({
  dialog: { showSaveDialog: async () => ({ canceled: false, filePath: join(root, 'study.json') }) },
  window: {},
  connection: { status: () => ({ id }), studyExport: async () => ledger() },
  current: () => true,
  id, studyId, projectId, selection: 3, ...overrides,
});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
async function temporary() { return mkdtemp(join(tmpdir(), 'tessera-study-export-')); }
async function cleanup(root) { await rm(root, { recursive: true, force: true }); }

test('saveStudyExport writes exact JSON with private mode and basename-only response', async () => {
  const root = await temporary();
  try {
    const value = ledger({ events: [{ event_type: 'research.study_created' }] });
    const result = await saveStudyExport(context(root, {
      dialog: { showSaveDialog: async (_window, options) => {
        assert.equal(options.defaultPath, `tessera-study-${studyId}.json`);
        return { canceled: false, filePath: join(root, 'export.json') };
      } },
      connection: { status: () => ({ id }), studyExport: async () => value },
    }));
    assert.deepEqual(result, { status: 'saved', basename: 'export.json' });
    const target = join(root, 'export.json');
    assert.deepEqual(JSON.parse(await readFile(target, 'utf8')), value);
    assert.equal((await lstat(target)).mode & 0o777, 0o600);
  } finally { await cleanup(root); }
});

test('saveStudyExport honors native cancel and stale dialog/fetch contexts', async () => {
  const root = await temporary();
  try {
    assert.deepEqual(await saveStudyExport(context(root, {
      dialog: { showSaveDialog: async () => ({ canceled: true }) },
    })), { status: 'cancelled' });
    const dialog = deferred(); let live = true;
    const waitingDialog = saveStudyExport(context(root, {
      dialog: { showSaveDialog: () => dialog.promise }, current: () => live,
    }));
    live = false; dialog.resolve({ canceled: false, filePath: join(root, 'dialog.json') });
    assert.deepEqual(await waitingDialog, { status: 'stale' });
    const fetch = deferred(); live = true;
    const waitingFetch = saveStudyExport(context(root, {
      connection: { status: () => ({ id }), studyExport: () => fetch.promise }, current: () => live,
    }));
    await Promise.resolve(); live = false; fetch.resolve(ledger());
    assert.deepEqual(await waitingFetch, { status: 'stale' });
    let connectionId = id;
    const connectionFetch = deferred();
    const connectionStale = saveStudyExport(context(root, {
      connection: { status: () => ({ id: connectionId }), studyExport: () => connectionFetch.promise },
    }));
    await Promise.resolve(); connectionId = 'connection_2'; connectionFetch.resolve(ledger());
    assert.deepEqual(await connectionStale, { status: 'stale' });
    let selected = 3;
    const selectionFetch = deferred();
    const selectionStale = saveStudyExport(context(root, {
      selection: selected,
      connection: { status: () => ({ id }), studyExport: () => selectionFetch.promise },
      current: (_id, _study, selection) => selection === selected,
    }));
    await Promise.resolve(); selected = 4; selectionFetch.resolve(ledger());
    assert.deepEqual(await selectionStale, { status: 'stale' });
  } finally { await cleanup(root); }
});

test('saveStudyExport drops staged output when context changes before publication', async () => {
  const root = await temporary(); let checks = 0;
  const result = await saveStudyExport(context(root, { current: () => ++checks < 4 }));
  assert.deepEqual(result, { status: 'stale' });
  assert.deepEqual(await readdir(root), []);
  await cleanup(root);
});

test('saveStudyExport never replaces an existing file or symlink', async () => {
  const root = await temporary();
  try {
    const existing = join(root, 'existing.json'); await writeFile(existing, 'keep');
    await assert.rejects(saveStudyExport(context(root, {
      dialog: { showSaveDialog: async () => ({ canceled: false, filePath: existing }) },
    })), /already exists/);
    assert.equal(await readFile(existing, 'utf8'), 'keep');
    const link = join(root, 'link.json'); await writeFile(join(root, 'source.json'), 'source');
    const { symlink } = await import('node:fs/promises'); await symlink('source.json', link);
    await assert.rejects(saveStudyExport(context(root, {
      dialog: { showSaveDialog: async () => ({ canceled: false, filePath: link }) },
    })), /already exists/);
    assert.equal((await lstat(link)).isSymbolicLink(), true);
  } finally { await cleanup(root); }
});

test('saveStudyExport rejects API failures, wrong identity, incomplete and oversized ledgers', async () => {
  const cases = [
    { connection: { status: () => ({ id }), studyExport: async () => { throw new Error('API failed'); } }, error: /API failed/ },
    { response: ledger({ study: { id: 'other', project_id: projectId } }), error: /complete selected/ },
    { response: ledger({ study: { id: studyId, project_id: 'other' } }), error: /complete selected/ },
    { response: ledger({ completeness: 'partial' }), error: /complete selected/ },
    { response: ledger({ variants: Array.from({ length: 1001 }, () => ({})) }), error: /complete selected/ },
    { response: ledger({ sources: Array.from({ length: 1001 }, () => ({})) }), error: /complete selected/ },
    { response: ledger({ findings: Array.from({ length: 1001 }, () => ({})) }), error: /complete selected/ },
    { response: ledger({ corrections: Array.from({ length: 1001 }, () => ({})) }), error: /complete selected/ },
    { response: ledger({ events: Array.from({ length: 1001 }, () => ({})) }), error: /complete selected/ },
    { response: ledger({ events: [{ payload: 'x'.repeat(4 * 1024 * 1024) }] }), error: /byte limit/ },
  ];
  for (const item of cases) {
    const root = await temporary();
    try {
      const input = item.connection ? { connection: item.connection } : {
        connection: { status: () => ({ id }), studyExport: async () => item.response },
      };
      await assert.rejects(saveStudyExport(context(root, input)), item.error);
      assert.deepEqual(await readdir(root), []);
    } finally { await cleanup(root); }
  }
});
