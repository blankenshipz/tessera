import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const { safeError } = createRequire(import.meta.url)('./native-safe-diagnostics.cjs');
test('native diagnostics retain stage/type/status/location but never arbitrary message or body secrets', () => {
  const secret = 'sensitive-private-value-DO-NOT-LOG';
  const error = new TypeError(`Control request failed (403). Authorization: Bearer ${secret} body=${secret}`);
  error.stack = `Error ${secret}\n at /Users/user/desktop/connection.mjs:125:4\n ${secret}`;
  const result = safeError(error, 'main-readback-connect');
  assert.equal(result.name, 'TypeError'); assert.equal(result.httpStatus, 403);
  assert.deepEqual(result.locations, ['connection.mjs:125:4']);
  assert.equal(JSON.stringify(result).includes(secret), false);
  assert.equal(JSON.stringify(safeError(new Error(secret), 'native-load')).includes(secret), false);
});
test('native production main import precedes authenticated network awaits for privileged scheme startup', () => {
  const source = readFileSync(new URL('./electron-bench-execution.cjs', import.meta.url), 'utf8');
  const main = source.slice(source.indexOf('async function main()'));
  assert.ok(main.indexOf("await import('../main.mjs')") < main.indexOf('await verify.connect('));
  assert.match(safeError(new Error('protocol.registerSchemesAsPrivileged must be called before app is ready'), 'startup').summary, /before any network await/);
});
