import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, lstat, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { saveExperimentExport } from '../experiment-export.mjs';

const id = 'connection_1';
const experimentId = 'exp_1';
const exported = (overrides = {}) => ({
  experiment: { id: experimentId, project_id: 'project_1' }, designs: [], measurements: [],
  completeness: 'complete_in_scope_goal_to_decision_chain', ...overrides,
});
const context = (root, overrides = {}) => ({
  dialog: { showSaveDialog: async () => ({ canceled: false, filePath: join(root, 'experiment.json') }) },
  window: {},
  connection: { status: () => ({ id }), experimentExport: async () => exported() },
  id, experimentId, ...overrides,
});
const temporary = () => mkdtemp(join(tmpdir(), 'tessera-experiment-export-'));
const cleanup = root => rm(root, { recursive: true, force: true });

test('saveExperimentExport writes the exact returned bytes with private mode and basename-only response', async () => {
  const root = await temporary();
  try {
    const value = exported({ measurements: [{ id: 'm1', value: 0.5, note: 'café' }] });
    let seen;
    const result = await saveExperimentExport(context(root, {
      dialog: { showSaveDialog: async (_window, options) => {
        seen = options; return { canceled: false, filePath: join(root, 'out.json') };
      } },
      connection: { status: () => ({ id }), experimentExport: async requested => { assert.equal(requested, experimentId); return value; } },
    }));
    assert.deepEqual(result, { status: 'saved', basename: 'out.json' });
    assert.equal(seen.defaultPath, `tessera-experiment-${experimentId}.json`);
    const target = join(root, 'out.json');
    assert.equal(await readFile(target, 'utf8'), JSON.stringify(value) + '\n');
    assert.equal((await lstat(target)).mode & 0o777, 0o600);
    assert.deepEqual(await readdir(root), ['out.json']);
  } finally { await cleanup(root); }
});

test('saveExperimentExport never includes a connection credential and writes only the chosen path', async () => {
  const root = await temporary();
  try {
    await saveExperimentExport(context(root, {
      connection: { status: () => ({ id, token: 'SECRET-TOKEN' }), credential: 'SECRET-TOKEN', experimentExport: async () => exported() },
    }));
    assert.doesNotMatch(await readFile(join(root, 'experiment.json'), 'utf8'), /SECRET-TOKEN/);
    assert.deepEqual(await readdir(root), ['experiment.json']);
  } finally { await cleanup(root); }
});

test('saveExperimentExport honors cancel and a wrong or changed connection id', async () => {
  const root = await temporary();
  try {
    assert.deepEqual(await saveExperimentExport(context(root, {
      dialog: { showSaveDialog: async () => ({ canceled: true }) },
    })), { status: 'cancelled' });
    assert.deepEqual(await saveExperimentExport(context(root, { id: 'other' })), { status: 'stale' });
    let current = id;
    assert.deepEqual(await saveExperimentExport(context(root, {
      connection: { status: () => ({ id: current }), experimentExport: async () => { current = 'connection_2'; return exported(); } },
    })), { status: 'stale' });
    assert.deepEqual(await readdir(root), []);
  } finally { await cleanup(root); }
});

test('saveExperimentExport never replaces an existing file or symlink', async () => {
  const root = await temporary();
  try {
    const existing = join(root, 'existing.json'); await writeFile(existing, 'keep');
    await assert.rejects(saveExperimentExport(context(root, {
      dialog: { showSaveDialog: async () => ({ canceled: false, filePath: existing }) },
    })), /already exists/);
    assert.equal(await readFile(existing, 'utf8'), 'keep');
    const link = join(root, 'link.json'); const { symlink } = await import('node:fs/promises');
    await symlink('existing.json', link);
    await assert.rejects(saveExperimentExport(context(root, {
      dialog: { showSaveDialog: async () => ({ canceled: false, filePath: link }) },
    })), /already exists/);
    assert.equal((await lstat(link)).isSymbolicLink(), true);
    assert.deepEqual((await readdir(root)).sort(), ['existing.json', 'link.json']);
  } finally { await cleanup(root); }
});

test('saveExperimentExport rejects unsafe ids, API failures, mismatched and oversized exports', async () => {
  for (const bad of ['../x', 'a/b', '', 'x'.repeat(129), null, 7]) {
    const root = await temporary();
    try {
      let asked = false;
      await assert.rejects(saveExperimentExport(context(root, {
        experimentId: bad, dialog: { showSaveDialog: async () => { asked = true; return { canceled: true }; } },
      })), /valid experiment/);
      assert.equal(asked, false);
    } finally { await cleanup(root); }
  }
  const cases = [
    { connection: { status: () => ({ id }), experimentExport: async () => { throw new Error('API failed'); } }, error: /API failed/ },
    { response: exported({ experiment: { id: 'other' } }), error: /selected experiment/ },
    { response: null, error: /selected experiment/ },
    { response: [], error: /selected experiment/ },
    { response: exported({ designs: [{ payload: 'x'.repeat(4 * 1024 * 1024) }] }), error: /byte limit/ },
  ];
  for (const item of cases) {
    const root = await temporary();
    try {
      const input = { connection: item.connection ?? { status: () => ({ id }), experimentExport: async () => item.response } };
      await assert.rejects(saveExperimentExport(context(root, input)), item.error);
      assert.deepEqual(await readdir(root), []);
    } finally { await cleanup(root); }
  }
});
