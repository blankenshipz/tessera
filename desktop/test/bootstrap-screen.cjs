'use strict';
// Opt-in actual Chromium rendering with isolated synthetic identities; never production auth.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const output = path.resolve(process.argv[2]);
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
app.setPath('userData', fs.mkdtempSync(path.join(output, 'profile-')));
const fixture = fs.readFileSync(path.join(__dirname, 'renderer-state.test.mjs'), 'utf8');
const start = fixture.indexOf('const api = { status:');
const end = fixture.indexOf('\n  const context =', start);
const api = fixture.slice(start, end).trim();
let window;
async function run() {
  const results = [];
  for (const root of [true, false]) {
    const preload = path.join(output, `preload-${root}.cjs`);
    fs.writeFileSync(preload, `const {contextBridge}=require('electron'); let connected=false; const identity={id:'connection',name:'Isolated screen',endpoint:'http://127.0.0.1:1',organizationId:'isolated-org',principalId:'fixture',role:'human',health:'online'};
const overrides={status:async()=>connected?identity:null,connect:async()=>{connected=true;return identity},disconnect:async()=>{connected=false},secretContext:async()=>{},projects:async()=>({projects:[{id:'p1',name:'Selected isolated project'}]}),adminWorkspace:async()=>({records:[],next_offset:null,held:false,root_admin:${root},control_status:'ready'}),adminAudit:async()=>({events:[]})};
${api}\ncontextBridge.exposeInMainWorld('tessera',api);`, { mode: 0o600 });
    window = new BrowserWindow({ width: 1100, height: 850, show: true, webPreferences: { preload, sandbox: true, contextIsolation: true, nodeIntegration: false } });
    await window.loadFile(path.join(__dirname, '..', 'index.html'));
    const evaluate = code => window.webContents.executeJavaScript(code);
    await new Promise(r => setTimeout(r, 300));
    await evaluate(`document.querySelector('#connection-open').click();document.querySelector('#connection-form').elements.name.value='Isolated';document.querySelector('#connection-form').elements.endpoint.value='http://127.0.0.1:1';document.querySelector('#connection-form').requestSubmit()`);
    await new Promise(r => setTimeout(r, 300));
    await evaluate(`document.querySelector('#projects button').click()`);
    await new Promise(r => setTimeout(r, 200));
    await evaluate(`document.querySelector('#admin-open').click()`);
    await new Promise(r => setTimeout(r, 200));
    const visible = await evaluate(`!document.querySelector('#admin-issuance').hidden`);
    if (visible !== root) throw new Error('Root/nonroot issuance visibility mismatch.');
    await evaluate(`document.querySelector('#admin-delegations').click();document.querySelector('#admin-delegation-editor').open=true; document.querySelectorAll('[name=action]').forEach(n=>n.checked=true)`);
    for (const width of [1100, 900]) {
      window.setSize(width, 850);
      await new Promise(r => setTimeout(r, 100));
      const geometry = await evaluate(`(()=>{const form=document.querySelector('#admin-delegation-form');form.scrollIntoView();const inputs=[...form.querySelectorAll('[name=action]')];inputs[0].focus();return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,selected:inputs.filter(n=>n.checked).map(n=>n.value),focus:document.activeElement.value,boxes:inputs.map(n=>{const r=n.parentElement.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})}})()`);
      if (geometry.overflow || geometry.selected.length !== 3 || geometry.boxes.some(b => b.width <= 0 || b.height <= 0 || b.x < 0 || b.x + b.width > geometry.width)) throw new Error('Delegation geometry mismatch.');
      window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'Space' });
      window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'Space' });
      await new Promise(r => setTimeout(r, 50));
      const keyboardSelected = await evaluate(`document.querySelector('[name=action]').checked`);
      if (keyboardSelected) throw new Error('Keyboard checkbox did not toggle.');
      await evaluate(`document.querySelector('[name=action]').checked=true`);
      fs.writeFileSync(path.join(output, `root-${root}-${width}.png`), (await window.webContents.capturePage()).toPNG(), { mode: 0o600 });
      results.push({ root, visible, geometry, keyboardToggle: true });
    }
    await evaluate(`document.querySelector('#disconnect').click()`);
    await new Promise(r => setTimeout(r, 100));
    const disconnected = await evaluate(`({hidden:document.querySelector('#admin-issuance').hidden,status:document.querySelector('#connection-status').textContent,selected:[...document.querySelectorAll('[name=action]:checked')].map(n=>n.value)})`);
    fs.writeFileSync(path.join(output, `disconnected-${root}.json`), JSON.stringify(disconnected,null,2), {mode:0o600});
    if (!disconnected.hidden || disconnected.status !== 'Not connected' || disconnected.selected.join() !== 'configuration.read') throw new Error('Disconnect retained stale authority or action selection.');
    await evaluate(`document.querySelector('#connection-open').click();document.querySelector('#connection-form').elements.name.value='Returned isolated';document.querySelector('#connection-form').elements.endpoint.value='http://127.0.0.1:1';document.querySelector('#connection-form').requestSubmit()`);
    await new Promise(r => setTimeout(r, 200));
    await evaluate(`document.querySelector('#projects button').click()`);
    await new Promise(r => setTimeout(r, 100));
    await evaluate(`document.querySelector('#admin-open').click()`);
    await new Promise(r => setTimeout(r, 100));
    const returned = await evaluate(`({visible:!document.querySelector('#admin-issuance').hidden,project:document.querySelector('#admin-status').textContent,selected:[...document.querySelectorAll('[name=action]:checked')].map(n=>n.value),tokenField:!!document.querySelector('input[name=token]')})`);
    if (returned.visible !== root || !returned.project.includes('p1') || returned.tokenField || returned.selected.join() !== 'configuration.read') throw new Error('Return context mismatch.');
    fs.writeFileSync(path.join(output, `returned-${root}.png`), (await window.webContents.capturePage()).toPNG(), { mode: 0o600 });
    results.push({root,disconnected,returned});
    window.destroy(); window = null;
  }
  fs.writeFileSync(path.join(output, 'geometry.json'), JSON.stringify({ mode:'actual Chromium with synthetic isolated identity projection',results }, null, 2), { mode: 0o600 });
  console.log('Rendered root/nonroot selected-action, keyboard and 900/1100px geometry checks passed.');
}
app.whenReady().then(run).then(()=>app.exit(0)).catch(error=>{console.error(error.message);if(window)window.destroy();app.exit(1)});
