/** Native export boundary: only a save dialog can choose a filesystem target. */
import { constants, linkSync } from 'node:fs';
import { open, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { basename, dirname, join } from 'node:path';

export async function saveStudyExport({ dialog, window, connection, current, id, studyId, projectId, selection }) {
  const valid = () => connection.status()?.id === id && current(id, studyId, selection);
  if (!valid()) return { status: 'stale' };
  if (![studyId, projectId].every(value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value))) {
    throw new Error('Select a valid study first.');
  }
  const choice = await dialog.showSaveDialog(window, {
    title: 'Save study ledger JSON', defaultPath: `tessera-study-${studyId}.json`,
    filters: [{ name: 'JSON', extensions: ['json'] }],
  });
  if (choice.canceled || !choice.filePath) return { status: 'cancelled' };
  if (!valid()) return { status: 'stale' };
  const ledger = await connection.studyExport(studyId);
  if (!valid()) return { status: 'stale' };
  if (!ledger || ledger.completeness !== 'complete_in_scope_study_ledger' ||
      ledger.study?.id !== studyId || ledger.study?.project_id !== projectId ||
      !['variants', 'sources', 'findings', 'corrections', 'events'].every(
        key => Array.isArray(ledger[key]) && ledger[key].length <= 1000)) {
    throw new Error('Control plane did not return a complete selected study ledger.');
  }
  const bytes = Buffer.from(JSON.stringify(ledger) + '\n', 'utf8');
  if (bytes.length > 4 * 1024 * 1024) throw new Error('Study export exceeds the byte limit.');
  const target = choice.filePath;
  const directory = dirname(target);
  const stage = join(directory, `.tessera-export-${randomUUID()}.tmp`);
  let created = false;
  try {
    const file = await open(stage, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    created = true;
    try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
    if (!valid()) return { status: 'stale' };
    // No await between the final context check and no-clobber publication.
    // A queued context change cannot interleave those two operations.
    linkSync(stage, target);
    await unlink(stage); created = false;
    const parent = await open(directory, constants.O_RDONLY);
    try { await parent.sync(); } finally { await parent.close(); }
    return { status: 'saved', basename: basename(target) };
  } catch (error) {
    if (error.code === 'EEXIST') throw new Error('Export destination already exists. Choose a new filename.');
    throw error;
  } finally {
    if (created) await unlink(stage);
  }
}
