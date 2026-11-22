/** Native export boundary: only a save dialog can choose a filesystem target. */
import { constants, linkSync } from 'node:fs';
import { open, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { basename, dirname, join } from 'node:path';

// Read-only: saves exactly what the control API returned for one experiment.
// The file carries no connection credential and asserts nothing about validity.
export async function saveExperimentExport({ dialog, window, connection, id, experimentId }) {
  const valid = () => connection.status()?.id === id;
  if (!valid()) return { status: 'stale' };
  if (typeof experimentId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(experimentId)) {
    throw new Error('Select a valid experiment first.');
  }
  const choice = await dialog.showSaveDialog(window, {
    title: 'Save experiment export JSON', defaultPath: `tessera-experiment-${experimentId}.json`,
    filters: [{ name: 'JSON', extensions: ['json'] }],
  });
  if (choice.canceled || !choice.filePath) return { status: 'cancelled' };
  if (!valid()) return { status: 'stale' };
  const exported = await connection.experimentExport(experimentId);
  if (!valid()) return { status: 'stale' };
  if (!exported || typeof exported !== 'object' || Array.isArray(exported) ||
      exported.experiment?.id !== experimentId) {
    throw new Error('Control plane did not return the selected experiment export.');
  }
  const bytes = Buffer.from(JSON.stringify(exported) + '\n', 'utf8');
  if (bytes.length > 4 * 1024 * 1024) throw new Error('Experiment export exceeds the byte limit.');
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
