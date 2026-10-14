import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const source = readFileSync(new URL('../appearance.js', import.meta.url), 'utf8');
function boot(saved, denied = false) {
  const dataset = {};
  const scheme = { matches: false, addEventListener(_name, fn) { this.update = fn; } };
  runInNewContext(source, {
    document: { documentElement: { dataset }, addEventListener() {} },
    localStorage: { getItem() { if (denied) throw new Error('Denied'); return saved; } },
    matchMedia: () => scheme,
  });
  return { dataset, scheme };
}
test('prepaint restores enum-only appearance and follows system scheme', () => {
  const { dataset, scheme } = boot(null);
  assert.equal(dataset.theme, 'light'); assert.equal(dataset.palette, 'plum');
  scheme.matches = true; scheme.update(); assert.equal(dataset.theme, 'dark');
  const explicit = boot('{"mode":"light","palette":"lagoon","credential":"ignored"}');
  explicit.scheme.matches = true; explicit.scheme.update();
  assert.equal(explicit.dataset.theme, 'light'); assert.equal(explicit.dataset.palette, 'lagoon');
});
test('invalid, malformed and inaccessible storage falls back safely', () => {
  for (const saved of ['{"mode":"secret","palette":"plum"}', '{', null]) {
    assert.equal(boot(saved).dataset.palette, 'plum');
  }
  assert.equal(boot(null, true).dataset.theme, 'light');
});
