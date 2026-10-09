import assert from 'node:assert/strict';
import { test } from 'node:test';
import { MAX_CREDENTIAL_HEADER_BYTES, parseCredentialHeader, validControlToken } from '../control-credential.mjs';

test('header and connection share the API token length contract', () => {
  for (const length of [43, 256, 257, 4096]) {
    const token = 'a'.repeat(length);
    const header = `Authorization: Bearer ${token}\r\n`;
    assert.equal(validControlToken(token), true);
    assert.equal(parseCredentialHeader(header), token);
    assert.ok(Buffer.byteLength(header) <= MAX_CREDENTIAL_HEADER_BYTES);
  }
  assert.equal(validControlToken('a'.repeat(43) + '\n'), false);
  for (const token of ['a'.repeat(42), 'a'.repeat(4097), 'salad' + 'a'.repeat(43), 'sk_' + 'a'.repeat(43), 'a'.repeat(43) + '.']) {
    assert.equal(validControlToken(token), false);
    assert.throws(() => parseCredentialHeader(`Authorization: Bearer ${token}`), /control-plane credential/);
  }
});

test('headers reject extra lines, whitespace and unrelated authorization schemes', () => {
  for (const header of [`Authorization: Bearer ${'a'.repeat(43)}\n\n`, `Authorization: Bearer ${'a'.repeat(43)}\nX-Other: value`, `Authorization: Basic ${'a'.repeat(43)}`, ` Authorization: Bearer ${'a'.repeat(43)}`]) {
    assert.throws(() => parseCredentialHeader(header), /control-plane credential/);
  }
});
