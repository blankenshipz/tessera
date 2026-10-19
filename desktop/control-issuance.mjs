// Native-only custody. No Electron, renderer, network or ambient credential access.
import { constants } from 'node:fs';
import { lstat, mkdir, open, realpath } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { randomBytes, randomUUID } from 'node:crypto';
import { parseControlTokenFile } from './control-credential.mjs';

function fail() { throw new Error('Private issuance bundle unavailable; preserve it and inspect before retrying.'); }
async function directory(path) {
  if (await realpath(path) !== resolve(path)) fail();
  const stat = await lstat(path);
  if (!stat.isDirectory() || (stat.mode & 0o077) || stat.uid !== process.getuid()) fail();
  return stat;
}
async function write(path, value) {
  const file = await open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { await file.writeFile(value); await file.sync(); } finally { await file.close(); }
}
async function read(path) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || (stat.mode & 0o077) || stat.uid !== process.getuid() || stat.size > 8192) fail();
    const bytes = Buffer.alloc(8193);
    try {
      const { bytesRead } = await file.read(bytes, 0, bytes.length, 0);
      if (bytesRead > 8192) fail();
      return bytes.subarray(0, bytesRead).toString('utf8');
    } finally { bytes.fill(0); }
  } finally { await file.close(); }
}
async function readIntent(path) {
  // Keep permission/I/O failures outside this catch. A JSON SyntaxError may quote
  // private bytes: replace it here, without preserving its message, stack or cause.
  const contents = await read(join(path, 'intent.json'));
  try { return JSON.parse(contents); } catch { fail(); }
}
async function syncDirectory(path) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try { await file.sync(); } finally { await file.close(); }
}
export async function createIssuanceBundle(path, issuer, minutes, expiresAt = new Date(Date.now() + minutes * 60000).toISOString()) {
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) fail();
  if (!Number.isFinite(Date.parse(expiresAt)) || Date.parse(expiresAt) <= Date.now() || Date.parse(expiresAt) > Date.now() + minutes * 60000) fail();
  issuer.current();
  await mkdir(path, { mode: 0o700 }); // Exact new directory, never adopt/overwrite.
  await directory(path);
  const intent = { version: 1, binding: issuer.binding, key: randomUUID(),
    expires_at: expiresAt };
  await write(join(path, 'operator.control-token'), randomBytes(32).toString('base64url') + '\n');
  await write(join(path, 'intent.json'), JSON.stringify(intent));
  await syncDirectory(path);
  issuer.current(); // A switch leaves an unsubmitted private bundle, never a POST.
  return { expires_at: intent.expires_at };
}
export async function submitIssuanceBundle(path, issuer) {
  const before = await directory(path);
  const intent = await readIntent(path);
  if (intent.version !== 1 || Object.keys(intent).sort().join() !== 'binding,expires_at,key,version' ||
      JSON.stringify(intent.binding) !== JSON.stringify(issuer.binding)) fail();
  const token = parseControlTokenFile(await read(join(path, 'operator.control-token')));
  const after = await directory(path);
  if (before.ino !== after.ino || before.dev !== after.dev) fail();
  issuer.current();
  // Network uncertainty preserves the original private token/body/key. No regeneration.
  const metadata = await issuer.issue({ token, expires_at: intent.expires_at, key: intent.key });
  // Metadata only; the token file/intent remain the durable exact retry authority.
  return { ...metadata, token_file: join(resolve(path), 'operator.control-token') };
}

export async function runControlIssuance({ issuer, mode, minutes, choose, confirm }) {
  if (!['new', 'resume'].includes(mode) || !Number.isInteger(minutes) || minutes < 1 || minutes > 1440) fail();
  issuer.current();
  const path = await choose(mode);
  issuer.current();
  if (!path) return { canceled: true };
  let expiry = new Date(Date.now() + minutes * 60000).toISOString();
  if (mode === 'resume') {
    await directory(path);
    const intent = await readIntent(path);
    if (JSON.stringify(intent.binding) !== JSON.stringify(issuer.binding)) fail();
    expiry = intent.expires_at;
  }
  if (!await confirm({ ...issuer.binding, expires_at: expiry, mode })) return { canceled: true };
  issuer.current();
  if (mode === 'new') await createIssuanceBundle(path, issuer, minutes, expiry);
  return submitIssuanceBundle(path, issuer);
}

// Same real native dialog callbacks in production and the opt-in native screen test.
// The default destination is native-owned; never supplied by renderer IPC.
export function nativeIssuanceDialogs(dialog, window, defaultPath = 'tessera-operator-credential') {
  return {
    choose: async mode => {
      if (mode === 'resume') {
        const picked = await dialog.showOpenDialog(window, { title: 'Resume the exact private issuance bundle', properties: ['openDirectory'] });
        return picked.canceled ? null : picked.filePaths[0];
      }
      const picked = await dialog.showSaveDialog(window, { title: 'Choose a NEW private issuance bundle directory', defaultPath });
      return picked.canceled ? null : picked.filePath;
    },
    confirm: async binding => {
      const answer = await dialog.showMessageBox(window, { type: 'warning', buttons: ['Cancel', 'Issue control credential'], defaultId: 0, cancelId: 0,
        title: 'Human-only control credential issuance',
        message: `${binding.mode === 'resume' ? 'Resume exact request' : 'Issue separate operator credential'} for ${binding.project_id}?`,
        detail: `Endpoint: ${binding.origin}\nRoot principal: ${binding.principal_id}\nExpires: ${binding.expires_at}\nThis project-scoped CONTROL AGENT credential has existing broader control-agent permissions. Exact administration delegation limits only administration routes; it is not a universal action sandbox. No proposal or approval is automatic. Keep the new private bundle outside the repository.\nAfter an uncertain outcome, resume this exact bundle with this same root identity; never create a replacement blindly.` });
      return answer.response === 1;
    },
  };
}
