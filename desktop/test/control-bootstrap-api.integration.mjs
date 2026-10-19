// Opt-in explicit integration file (outside npm's *.test.mjs): real HTTP/API/SQLite.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { once } from 'node:events';
import { ControlConnection } from '../connection.mjs';
import { runControlIssuance } from '../control-issuance.mjs';
import { parseCredentialHeader, parseControlTokenFile } from '../control-credential.mjs';

test('native bundle -> real API issuance/replay -> child proposal/root review -> delegated reconnect/revocation', { timeout: 40000 }, async t => {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'tessera-bootstrap-api-')));
  const code = `import socket,sys,uvicorn
from pathlib import Path
from tessera_control.store import initialize_state
from tessera_control.api import create_app
state=Path(sys.argv[1]);initialize_state(state)
sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
print(sock.getsockname()[1],flush=True)
uvicorn.Server(uvicorn.Config(create_app(state),log_level='critical',access_log=False)).run(sockets=[sock])`;
  const child = spawn(resolve('.venv/bin/python'), ['-c', code, join(home, 'state')], { stdio: ['ignore', 'pipe', 'pipe'] });
  let serverDiagnostic = '';
  child.stderr.on('data', data => { serverDiagnostic += data.toString(); });
  t.after(async () => { child.kill('SIGTERM'); if (child.exitCode === null) await once(child, 'exit'); await rm(home, { recursive: true, force: true }); });
  const port = await new Promise((yes, no) => {
    child.stdout.once('data', data => yes(Number(data.toString().trim())));
    child.once('exit', () => no(new Error('Isolated API startup failed.')));
  });
  assert.ok(Number.isInteger(port));
  const origin = `http://127.0.0.1:${port}`;
  let online = false;
  for (let n = 0; n < 100; n++) {
    try { online = (await fetch(origin + '/health/ready')).status === 200; } catch { /* server binding only */ }
    if (online) break;
    await new Promise(r => setTimeout(r, 30));
  }
  assert.ok(online, 'isolated API ready');
  const owner = parseCredentialHeader(await readFile(join(home, 'state', 'owner.header'), 'utf8'));
  const request = async (token, path, body, key) => {
    const response = await fetch(origin + path, { method: body ? 'POST' : 'GET',
      headers: { Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json', 'Idempotency-Key': key } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, body: await response.json() };
  };
  const project = await request(owner, '/projects', { name: 'Isolated bootstrap' }, 'project');
  assert.equal(project.status, 201);
  const projectId = project.body.id;
  const connection = new ControlConnection();
  await connection.connect({ id: 'isolated-root', name: 'Isolated', endpoint: origin, token: owner });
  const issuer = await connection.prepareControlIssuance(projectId);
  const bundle = join(home, 'bundle');
  const issue = mode => runControlIssuance({ issuer, mode, minutes: 60, choose: async () => bundle, confirm: async () => true });
  const issued = await issue('new');
  assert.equal((await issue('resume')).credential_id, issued.credential_id);
  const token = parseControlTokenFile(await readFile(issued.token_file, 'utf8'));
  const operator = new ControlConnection();
  const reconnect = () => operator.connect({ id: 'isolated-operator', name: 'Operator', endpoint: origin, token });
  await reconnect();
  const target = await operator.adminCreate(projectId, { expected_revision: 0, spec: { kind: 'configuration', component: 'execution_target', provider: 'salad' } }, 'target');
  const path = `/admin/projects/${projectId}/records/${target.id}/salad-setup`;
  assert.equal((await request(token, path)).status, 403);
  const expires = new Date(Date.now() + 15 * 60000).toISOString();
  const grantBody = { expected_revision: 0, spec: { kind: 'delegation', subject_id: issued.principal_id,
    subject_role: 'agent', target_id: target.id, provider: 'salad', resource: 'configuration',
    actions: ['configuration.read', 'configuration.propose', 'readiness.read'], expires_at: expires } };
  const grant = await operator.adminCreate(projectId, grantBody, 'grant');
  const commandPath = `/admin/projects/${projectId}/records/${grant.id}/commands`;
  assert.equal((await request(token, commandPath, { expected_revision: grant.revision, action: 'approve' }, 'self')).status, 403);
  assert.equal((await request(owner, commandPath, { expected_revision: grant.revision, action: 'approve' }, 'review')).status, 200);
  assert.equal((await request(token, path)).status, 200);
  operator.disconnect(); await reconnect();
  assert.equal((await request(token, path)).body.authority, 'delegated');
  const longGrant = structuredClone(grantBody); longGrant.spec.expires_at = new Date(Date.now() + 2 * 3600000).toISOString();
  assert.equal((await request(token, `/admin/projects/${projectId}/records`, longGrant, 'outlives')).status, 403);
  const expiredGrant = structuredClone(grantBody); expiredGrant.spec.expires_at = '2000-01-01T00:00:00Z';
  assert.equal((await request(token, `/admin/projects/${projectId}/records`, expiredGrant, 'expired')).status, 409);
  assert.equal((await request(owner, `/credentials/${issued.credential_id}/revoke`, {}, 'revoke')).status, 200);
  assert.equal((await request(token, path)).status, 401);
  assert.equal(serverDiagnostic, '');
});
