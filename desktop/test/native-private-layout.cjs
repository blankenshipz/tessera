'use strict';
const { resolve, relative, isAbsolute, parse } = require('node:path');
const { existsSync, lstatSync, realpathSync } = require('node:fs');
function contains(parent, child) {
  const rel = relative(parent, child);
  return rel === '' || (!rel.startsWith('..' + require('node:path').sep) && rel !== '..' && !isAbsolute(rel));
}
function validatePrivateLayout(output, runtime) {
  output = resolve(output); runtime = resolve(runtime);
  if (runtime.split(require('node:path').sep).includes('receipts')) throw new Error('Private runtime must be outside all receipt trees.');
  if (contains(output, runtime) || contains(runtime, output)) throw new Error('Private runtime and receipt output must not overlap.');
  for (const path of [output, runtime]) {
    let cursor = parse(path).root;
    for (const part of relative(cursor, path).split(require('node:path').sep)) {
      cursor = resolve(cursor, part);
      if (existsSync(cursor) && lstatSync(cursor).isSymbolicLink()) throw new Error('Symlinked runtime/receipt ancestors are not permitted.');
    }
  }
  if (existsSync(runtime) && (!lstatSync(runtime).isDirectory() || lstatSync(runtime).uid !== process.getuid() || realpathSync(runtime) !== runtime)) throw new Error('Private runtime must be an owned real directory.');
  return { output, runtime };
}
module.exports = { validatePrivateLayout };
