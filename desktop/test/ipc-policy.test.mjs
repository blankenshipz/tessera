import assert from 'node:assert/strict';
import { test } from 'node:test';
import { validateSender } from '../ipc-policy.mjs';

test('only the main frame of the owned application window can invoke privileged methods', () => {
  const frame = { url: 'tessera://app/index.html' };
  const contents = { mainFrame: frame };
  const window = { webContents: contents };
  validateSender({ sender: contents, senderFrame: frame }, window);
  assert.throws(() => validateSender({ sender: {}, senderFrame: frame }, window));
  assert.throws(() => validateSender({ sender: contents, senderFrame: { url: frame.url } }, window));
  frame.url = 'https://example.test/';
  assert.throws(() => validateSender({ sender: contents, senderFrame: frame }, window));
  assert.throws(() => validateSender({ sender: contents, senderFrame: frame }, null));
});
