import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ControlConnection } from '../connection.mjs';

test('remote control disconnect/reconnect never sends a workload, secret or implicit cancel', async () => {
  const calls = [];
  const token = 'x'.repeat(43);
  const connection = new ControlConnection(async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(url.endsWith('/me') ? { principal_id: 'human', org_id: 'org' } : { routes: [] }));
  });
  const settings = { id: 'remote', token, name: 'Remote', endpoint: 'https://control.example.test' };
  await connection.connect(settings);
  await connection.remoteExecutions('task_1');
  const body = { expected_revision: 1, proposal_digest: 'a'.repeat(64), command: 'start' };
  await connection.remoteExecutionCommand('route_1', body, 'start-1');
  connection.disconnect();
  const count = calls.length;
  assert.throws(() => connection.remoteExecutionCommand('route_1', body, 'retry'));
  assert.equal(calls.length, count);
  await connection.connect(settings);
  await connection.remoteExecution('route_1');
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 1);
  assert.deepEqual(JSON.parse(calls[2].options.body), body);
  assert.ok(!calls[2].options.body.includes(token));
  assert.ok(!calls[2].options.body.includes('control.example.test'));
  assert.match(calls.at(-1).url, /\/remote-executions\/route_1$/);
  assert.throws(() => connection.remoteExecution('../route'));
});
