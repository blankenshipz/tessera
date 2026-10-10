import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
const html=readFileSync(new URL('../index.html',import.meta.url),'utf8');
const css=readFileSync(new URL('../style.css',import.meta.url),'utf8');
test('workspace has five native navigation destinations and unique control IDs',()=>{
  for(const view of ['experiments','plans','ledger','conversation','operations']) {
    assert.match(html,new RegExp(`id="view-${view}" type="button"`));
    assert.match(html,new RegExp(`data-workspace-view="${view}"`));
  }
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(ids.length,new Set(ids).size);
  assert.match(html,/id="admin-return"/);
  assert.doesNotMatch(html,/role="tab"/);
});
test('responsive layout contains JSON, supports selects and disclosures focus, and reduced motion',()=>{
  assert.match(css,/@media \(max-width: 1100px\)/);
  assert.match(css,/grid-template-columns: 240px minmax\(0, 1fr\)/);
  assert.match(css,/button, input, textarea, select, summary/);
  assert.match(css,/prefers-reduced-motion: reduce/);
  assert.match(css,/max-height: calc\(100vh - 48px\)/);
  assert.match(css,/pre \{ white-space: pre-wrap; overflow-wrap: anywhere/);
});
