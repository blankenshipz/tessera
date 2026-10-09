'use strict';
// Never preserve arbitrary error text, headers, bodies, IPC arguments or tokens.
function safeError(error, stage) {
  const message = typeof error?.message === 'string' ? error.message : '';
  const name = ['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError'].includes(error?.name) ? error.name : 'Error';
  const codes = ['ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ERR_CERT_AUTHORITY_INVALID', 'ERR_MODULE_NOT_FOUND', 'ENOENT'];
  const code = codes.includes(error?.code) ? error.code : undefined;
  const http = message.match(/Control request failed \((\d{3})\)/);
  let summary = http ? `Control request failed (${http[1]}).` : 'Unrecognised error text withheld; inspect this stage locally without logging credentials.';
  for (const [pattern, text] of [
    [/registerSchemesAsPrivileged.*before.*ready/, 'Production privileged scheme registration occurred after app.ready; import production main before any network await.'],
    [/Script failed to execute/, 'Renderer JavaScript execution failed; inspect safe renderer diagnostic events.'],
    [/Cannot read properties of (undefined|null)/, 'Renderer accessed an undefined/null property.'],
    [/is not a function/, 'A required runtime method is unavailable.'],
    [/is not defined/, 'A required JavaScript symbol is undefined.'],
    [/Native execution condition timed out/, 'Native condition timed out; inspect committed effects before retry.'],
    [/Committed execution spec differs/, 'Committed specification differs; approval refused.'],
    [/Committed duration\/budget differs/, 'Committed duration/budget differs; approval refused.'],
    [/Operator grant does not match/, 'Operator grant basis differs from the native reviewed plan.'],
    [/Native design registration not committed/, 'Native experiment registration was not committed.'],
  ]) if (pattern.test(message)) { summary = text; break; }
  const locations = [...String(error?.stack || '').matchAll(/(?:\/desktop\/|\b)([a-z-]+\.(?:mjs|cjs)):(\d+):(\d+)/g)]
    .slice(0, 4).map(match => `${match[1]}:${match[2]}:${match[3]}`);
  return { stage, name, ...(code ? { code } : {}), ...(http ? { httpStatus: Number(http[1]) } : {}), summary, locations };
}
module.exports = { safeError };
