/** Native-selected, connection-bound, digest-verified no-clobber publication. */
import { constants, linkSync } from 'node:fs';
import { open, unlink } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { basename, dirname, join } from 'node:path';

const filesystem = { open, unlink, linkSync };

export async function saveReleasedArtifact({ dialog, window, connection, manifestId, artifactId, fs = filesystem }) {
  const current = connection.captureOperationContext();
  if (!current()) return { status: 'stale' };
  if (![manifestId, artifactId].every(value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value))) {
    throw new Error('Select a valid released artifact.');
  }
  const choice = await dialog.showSaveDialog(window, { title: 'Save released artifact',
    defaultPath: `tessera-artifact-${artifactId}`, properties: ['showOverwriteConfirmation'] });
  if (choice.canceled || !choice.filePath) return { status: 'cancelled' };
  if (!current()) return { status: 'stale' };
  const artifact = await connection.retrieveReleasedArtifact(manifestId, artifactId);
  if (!current()) return { status: 'stale' };
  if (!Buffer.isBuffer(artifact?.content) || !/^[a-f0-9]{64}$/.test(artifact.sha256) ||
      createHash('sha256').update(artifact.content).digest('hex') !== artifact.sha256) {
    throw new Error('Released artifact retrieval integrity check failed.');
  }
  const directory = dirname(choice.filePath);
  const stage = join(directory, `.tessera-artifact-${randomUUID()}.tmp`);
  let created = false;
  try {
    const file = await fs.open(stage, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    created = true;
    try { await file.writeFile(artifact.content); await file.sync(); } finally { await file.close(); }
    if (!current()) return { status: 'stale' };
    // No asynchronous gap for a connection change before atomic no-clobber link.
    fs.linkSync(stage, choice.filePath);
    await fs.unlink(stage); created = false;
    const parent = await fs.open(directory, constants.O_RDONLY);
    try { await parent.sync(); } finally { await parent.close(); }
    return { status: 'saved', basename: basename(choice.filePath), sha256: artifact.sha256 };
  } catch (error) {
    if (error.code === 'EEXIST') throw new Error('Artifact destination already exists. Choose a new filename.');
    throw error;
  } finally {
    if (created) await fs.unlink(stage);
  }
}
