import assert from 'node:assert/strict';
import { linkSync, writeFileSync } from 'node:fs';
import { mkdtemp, readFile, readdir, stat, rm, writeFile, symlink, open, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { saveReleasedArtifact } from '../artifact-save.mjs';
import { ControlConnection } from '../connection.mjs';

const content = Buffer.from('exact\0released\xffbytes');
const sha256 = createHash('sha256').update(content).digest('hex');
async function fixture(run) {
  const root = await mkdtemp('/private/tmp/opencode/tessera-artifact-save-');
  let live = true; let fetches = 0;
  const options = { manifestId: 'manifest_1', artifactId: 'artifact_1',
    dialog: { showSaveDialog: async () => ({ filePath: join(root, 'artifact.bin') }) },
    connection: { captureOperationContext: () => () => live,
      retrieveReleasedArtifact: async () => { fetches++; return { content, sha256 }; } } };
  try { await run({ root, options, stale: () => { live = false; }, fetches: () => fetches }); }
  finally { await rm(root, { recursive: true, force: true }); }
}

test('artifact save publishes synced exact private bytes and basename-only receipt', () => fixture(async ({ root, options }) => {
  assert.deepEqual(await saveReleasedArtifact(options), { status: 'saved', basename: 'artifact.bin', sha256 });
  assert.deepEqual(await readFile(join(root, 'artifact.bin')), content);
  assert.equal((await stat(join(root, 'artifact.bin'))).mode & 0o777, 0o600);
  assert.deepEqual(await readdir(root), ['artifact.bin']);
}));

test('cancel and connection change during dialog never fetch or create output', () => fixture(async ({ root, options, stale, fetches }) => {
  options.dialog.showSaveDialog = async () => ({ canceled: true });
  assert.deepEqual(await saveReleasedArtifact(options), { status: 'cancelled' });
  options.dialog.showSaveDialog = async () => { stale(); return { filePath: join(root, 'stale') }; };
  assert.deepEqual(await saveReleasedArtifact(options), { status: 'stale' });
  assert.equal(fetches(), 0); assert.deepEqual(await readdir(root), []);
}));

test('connection change during retrieval discards bytes', () => fixture(async ({ root, options, stale }) => {
  options.connection.retrieveReleasedArtifact = async () => { stale(); return { content, sha256 }; };
  assert.deepEqual(await saveReleasedArtifact(options), { status: 'stale' });
  assert.deepEqual(await readdir(root), []);
}));

for (const failure of ['write', 'sync', 'stale']) {
  test(`staged ${failure} failure leaves no partial destination or temporary file`, () => fixture(async ({ root, options, stale }) => {
    options.fs = { unlink, linkSync, open: async (...args) => {
      const file = await open(...args);
      return { close: () => file.close(),
        writeFile: async bytes => { await file.writeFile(bytes.subarray(0, 2)); if (failure === 'write') throw new Error('write failed'); await file.writeFile(bytes); },
        sync: async () => { if (failure === 'sync') throw new Error('sync failed'); await file.sync(); stale(); } };
    } };
    if (failure === 'stale') assert.deepEqual(await saveReleasedArtifact(options), { status: 'stale' });
    else await assert.rejects(saveReleasedArtifact(options), /failed/);
    assert.deepEqual(await readdir(root), []);
  }));
}

test('digest mismatch fails before staging', () => fixture(async ({ root, options }) => {
  options.connection.retrieveReleasedArtifact = async () => ({ content, sha256: '0'.repeat(64) });
  await assert.rejects(saveReleasedArtifact(options), /integrity/);
  assert.deepEqual(await readdir(root), []);
}));

test('retrieval denial creates no filesystem output', () => fixture(async ({ root, options }) => {
  options.connection.retrieveReleasedArtifact = async () => { throw new Error('denied'); };
  await assert.rejects(saveReleasedArtifact(options), /denied/);
  assert.deepEqual(await readdir(root), []);
}));

test('unsupported atomic publication fails closed and cleans stage', () => fixture(async ({ root, options }) => {
  options.fs = { open, unlink, linkSync: () => { throw new Error('unsupported link'); } };
  await assert.rejects(saveReleasedArtifact(options), /unsupported link/);
  assert.deepEqual(await readdir(root), []);
}));

for (const kind of ['file', 'symlink', 'publication-race']) {
  test(`no-clobber preserves destination on ${kind}`, () => fixture(async ({ root, options }) => {
    const target = join(root, 'artifact.bin');
    if (kind === 'file') await writeFile(target, 'keep');
    if (kind === 'symlink') { await writeFile(join(root, 'source'), 'keep'); await symlink('source', target); }
    if (kind === 'publication-race') {
      options.fs = { open, unlink, linkSync: (stage, destination) => {
        writeFileSync(destination, 'keep'); linkSync(stage, destination);
      } };
    }
    await assert.rejects(saveReleasedArtifact(options), /already exists/);
    assert.deepEqual(await readFile(target), Buffer.from('keep'));
    assert.equal((await readdir(root)).some(name => name.endsWith('.tmp')), false);
  }));
}

test('operation context cannot revive after same-ID reconnection', async () => {
  const connection = new ControlConnection(async () => new Response(JSON.stringify({ principal_id: 'owner', org_id: 'org' }),
    { headers: { 'content-type': 'application/json' } }));
  const settings = { id: 'same', name: 'test', endpoint: 'http://127.0.0.1:8000', token: 'a'.repeat(64) };
  await connection.connect(settings);
  const current = connection.captureOperationContext();
  assert.equal(current(), true);
  connection.disconnect(); assert.equal(current(), false);
  await connection.connect(settings); assert.equal(current(), false);
  assert.equal(connection.captureOperationContext()(), true);
});
