// Opt-in official TypeScript SDK client, no model inference or provider calls.
// TESSERA_MCP_JS_SDK references an isolated @modelcontextprotocol/sdk@1.32.0 directory.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';

const expectedTools = [
  'tessera_status',
  'tessera_list_contexts',
  'tessera_read_context',
  'tessera_list_proposals',
  'tessera_read_proposal',
  'tessera_propose_task_plan',
  'tessera_request_authority',
  'tessera_list_authority_requests',
  'tessera_authority_status',
  'tessera_consume_grant',
  'tessera_propose_research',
  'tessera_record_observation',
  'tessera_request_review',
  'tessera_notebook',
  'tessera_export_notebook',
  'tessera_register_generated_output',
  'tessera_generated_output_metadata',
  'tessera_read_generated_output',
];

const root = process.env.TESSERA_MCP_JS_SDK;
assert(root, 'Missing isolated JS SDK reference');
const { Client } = await import(pathToFileURL(`${root}/dist/esm/client/index.js`));
const { StdioClientTransport } = await import(pathToFileURL(`${root}/dist/esm/client/stdio.js`));
const client = new Client({ name: 'tessera-js-acceptance', version: '1.0' });
const transport = new StdioClientTransport({
  command: process.env.TESSERA_MCP_ENTRYPOINT || process.env.TESSERA_MCP_PYTHON,
  args: process.env.TESSERA_MCP_ENTRYPOINT ? [] : ['-m', 'tessera_mcp.server'],
  env: { ...process.env },
  stderr: 'ignore',
});
try {
  await client.connect(transport);
  let args = JSON.parse(await readFile(process.argv[2], 'utf8'));
  if (args.mode === 'artifact-resource') {
    const read = client.readResource({ uri: `tessera://contexts/${args.context_id}` });
    if (args.denied) {
      await assert.rejects(read);
      console.log('Official TypeScript SDK: artifact resource denied');
    } else {
      const resource = await read;
      const text = resource.contents[0].text;
      assert(Buffer.byteLength(text) <= 128 * 1024);
      const context = JSON.parse(text);
      const bytes = Buffer.from(context.content_base64, 'base64');
      assert(bytes.length <= 65536);
      assert.equal(bytes.toString('base64'), args.content_base64);
      assert.equal(context.sha256, createHash('sha256').update(bytes).digest('hex'));
      const tool = await client.callTool({ name: 'tessera_read_context',
        arguments: { context_id: args.context_id } });
      assert(!tool.isError);
      assert.equal(tool.structuredContent.content_base64, args.content_base64);
      console.log('Official TypeScript SDK: bounded artifact resource/tool passed');
    }
  } else {
  if (args.mode === 'bench-proposal') {
    const resource = await client.readResource({ uri: `tessera://contexts/${args.context_id}` });
    const context = JSON.parse(resource.contents[0].text);
    assert.equal(context.content_base64, args.content_base64);
    assert(Buffer.from(context.content_base64, 'base64').length <= 65536);
    args = args.proposal_args;
  }
  const tools = await client.listTools();
   assert.deepEqual(tools.tools.map(tool => tool.name).sort(), [...expectedTools].sort());
  const status = await client.callTool({ name: 'tessera_status', arguments: {} });
  assert(!status.isError);
  const resources = await client.listResources();
  assert(resources.resources.some(r => r.uri === 'tessera://status'));
  const resource = await client.readResource({ uri: 'tessera://status' });
  assert(resource.contents.length);
  const result = await client.callTool({ name: 'tessera_propose_task_plan', arguments: args });
  assert(!result.isError);
  assert.equal(result.structuredContent.authority_effect, 'none');
  const proposals = await client.callTool({ name: 'tessera_list_proposals', arguments: {} });
  assert(!proposals.isError);
  assert(proposals.structuredContent.proposals.some(p => p.id === result.structuredContent.id));
  console.log('Official TypeScript SDK 1.32.0: tools/resources/proposal replay passed');
  }
} finally {
  await client.close();
}
