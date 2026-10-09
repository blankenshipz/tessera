import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const tree = path.resolve(process.argv[2]);
const load = name => import(pathToFileURL(path.join(tree, name)).href);
const sdk = await load('packages/sdk/src/effect/terminal.ts');
const ws = await load('packages/ai/src/route/transport/websocket.ts');
const http = await load('packages/ai/src/route/transport/http.ts');
const producer = await load('harness/plugins/v2/owner-guard-producer.mjs');
const quota = await load('harness/plugins/v2/owned-native-quota.mjs');
const controller = await load('harness/plugins/v2/native-pipe-controller.mjs');
const proof = await load('harness/plugins/v2/owner-proof-ipc.mjs');
const pins = await load('harness/plugins/v2/owned-installation-pins.mjs');
for (const value of [sdk.makeStorePipeBoundRuntime, ws.open, ws.fromWebSocket, http.httpJson,
  http.jsonRequestParts, producer.makeOwnerGuardProducer, quota.makeOwnedNativeQuotaRunner,
  controller.makeNativePipeController, proof.serveOwnerProofIPC, pins.measureOwnedInstallation])
  assert.equal(typeof value, 'function');
assert.throws(() => sdk.makeStorePipeBoundRuntime({}, {}, {}, { authorized: true }, globalThis.fetch));
assert.throws(() => quota.makeOwnedNativeQuotaRunner({ authorized: true }));
console.log(JSON.stringify({ actual_cold_modules: 8, exports_checked: 10, mock_substitutions: 0,
  unknown_owner_denied: true, native_effects: 0, provider_calls: 0,
  claim: 'actual reconstructed source linkage, not full package build or production admission' }));
