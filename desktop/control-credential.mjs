// Match the control API's opaque bearer contract; never return it to the renderer.
export const MAX_CREDENTIAL_HEADER_BYTES = 4120;
export function validControlToken(token) {
  return typeof token === 'string' && /^[A-Za-z0-9_-]{43,4096}$/.test(token) &&
    !/^(salad|sld[_-]|sk[_-]|ai_gateway)/i.test(token);
}
export function parseCredentialHeader(header) {
  const match = typeof header === 'string' && /^Authorization: Bearer ([A-Za-z0-9_-]{43,4096})\r?\n?$/.exec(header);
  if (!match || !validControlToken(match[1])) {
    throw new Error('Select a control-plane credential header (43–4096 URL-safe token characters), not a provider key.');
  }
  return match[1];
}

// Explicit token-only file mode; never guess a file's format or accept provider keys.
export function parseControlTokenFile(contents) {
  if (typeof contents !== 'string' || !/^[A-Za-z0-9_-]{43,4096}\n?$/.test(contents)) {
    throw new Error('Select a private control-token file, not a provider key or header.');
  }
  const token = contents.endsWith('\n') ? contents.slice(0, -1) : contents;
  if (!validControlToken(token)) throw new Error('Select a control-plane token, not a provider key.');
  return token;
}
