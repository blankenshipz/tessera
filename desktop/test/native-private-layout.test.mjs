import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, mkdirSync, symlinkSync, rmSync } from 'node:fs';
const { validatePrivateLayout } = createRequire(import.meta.url)('./native-private-layout.cjs');
test('native private runtime is separate from receipts and overlapping layouts fail closed', () => {
  assert.equal(validatePrivateLayout('/private/tmp/opencode/layout-test/receipts/run', '/private/tmp/opencode/layout-test/private/run').runtime, '/private/tmp/opencode/layout-test/private/run');
  for (const runtime of ['/private/tmp/opencode/layout-test/receipts/run', '/private/tmp/opencode/layout-test/receipts/run/profile', '/private/tmp/opencode/layout-test/receipts']) assert.throws(() => validatePrivateLayout('/private/tmp/opencode/layout-test/receipts/run', runtime));
  assert.throws(() => validatePrivateLayout('/private/tmp/opencode/layout-test/receipts/run', '/private/tmp/opencode/layout-test/receipts/other-run'));
});
test('native private layout rejects a symlinked runtime ancestor', () => {
  const dir = mkdtempSync('/private/tmp/opencode/native-layout-');
  try {
    mkdirSync(`${dir}/owned`); symlinkSync(`${dir}/owned`, `${dir}/alias`);
    assert.throws(() => validatePrivateLayout(`${dir}/receipt-output`, `${dir}/alias/private`));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
