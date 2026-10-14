'use strict';
// Opt-in real application + isolated HTTP backend. Picker substitution only.
const { app, dialog, nativeTheme } = require('electron');
const { readFileSync, writeFileSync, mkdtempSync, rmSync } = require('node:fs');
const { join } = require('node:path');
const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const profile = mkdtempSync(join(fixture.output, 'profile-'));
app.setPath('userData', profile);
dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [fixture.header] });
let window;
const errors = [];
let loaded;
const ready = new Promise(resolve => { loaded = resolve; });
app.on('browser-window-created', (_event, candidate) => {
  window = candidate;
  candidate.webContents.once('did-finish-load', loaded);
  candidate.webContents.on('console-message', details => { if (details.level === 'error') errors.push(details.message); });
});
const js = async code => {
  try { return await window.webContents.executeJavaScript(`{${code}}`); }
  catch (error) { throw new Error(`${error.message}; URL: ${window.webContents.getURL()}; operation: ${code.slice(0, 240)}; renderer: ${errors.join('; ')}`); }
};
async function settle() { await js('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))'); }
async function capture(name) {
  // Resize back to the same content bounds to invalidate native cached surfaces
  // after scrolling/focus. This does not alter the captured application state.
  const [width,height]=window.getContentSize();
  window.setContentSize(width+1,height); await settle();
  window.setContentSize(width,height);
  await settle();
  // Allow native compositor rasterization after focus/scroll, not only DOM layout.
  await new Promise(resolve => setTimeout(resolve, 150));
  writeFileSync(join(fixture.output, name), (await window.webContents.capturePage()).toPNG(), { mode: 0o600 });
}
async function wait(expression) {
  await js(`(async () => { const end = Date.now()+12000; while (!(${expression})) { if(Date.now()>end) throw new Error('Timed out: '+${JSON.stringify(expression)}+'; '+document.querySelector('#notice').textContent); await new Promise(r=>setTimeout(r,25)); } })()`);
}
async function click(id) { await js(`document.getElementById(${JSON.stringify(id)}).click()`); }
async function connect(projectName) {
  await click('connection-open');
  await js(`const f=document.querySelector('#connection-form'); f.elements.name.value='UI test · isolated local service'; f.elements.endpoint.value=${JSON.stringify(fixture.endpoint)}; f.requestSubmit();`);
  await wait("document.querySelectorAll('#projects button').length > 0");
  await js(`[...document.querySelectorAll('#projects button')].find(b=>b.textContent===${JSON.stringify(projectName)}).click()`);
  await wait(`document.querySelector('#project-title').textContent===${JSON.stringify(projectName)} && !document.querySelector('#workspace').hidden`);
}
async function appearance(mode, palette) {
  await click('appearance-open');
  await js(`for (const [id,value] of [['appearance-mode',${JSON.stringify(mode)}],['appearance-palette',${JSON.stringify(palette)}]]) {const n=document.getElementById(id);n.value=value;n.dispatchEvent(new Event('change',{bubbles:true}));}`);
  await settle();
}
async function closeAppearance() {
  window.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'Escape' });
  window.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'Escape' });
  await wait("!document.querySelector('#appearance-dialog').open && document.activeElement.id==='appearance-open'");
}
async function geometry() {
  return js(`(() => { const rect=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}}; return {width:innerWidth,height:innerHeight,sidebar:rect(document.querySelector('aside')),main:rect(document.querySelector('main')),overflow:document.documentElement.scrollWidth>innerWidth,theme:document.documentElement.dataset.theme,palette:document.documentElement.dataset.palette}; })()`);
}
async function main() {
  await import('../main.mjs'); await ready;
  window.setContentSize(1280,850); nativeTheme.themeSource='light';
  await capture('disconnected.png');
  if (!await js("document.querySelector('#workspace-navigation').hidden")) throw new Error('Disconnected destinations exposed.');
  await connect('UI test · research workspace');
  await click('project-new');
  await js("const f=document.querySelector('#project-form');f.elements.name.value='UI test · record-only journey';f.requestSubmit()");
  await wait("document.querySelector('#project-title').textContent==='UI test · record-only journey'");
  await click('view-plans'); await click('task-new');
  await js("const f=document.querySelector('#task-form');f.elements.title.value='UI test · zero-budget draft';f.elements.purpose.value='Record-only interface check. No execution or model calls; scientific validity not assessed.';f.elements.budget.value='0';f.elements.duration.value='30';f.requestSubmit()");
  await wait("document.querySelector('#tasks').textContent.includes('UI test · zero-budget draft') && !document.querySelector('#task-dialog').open");
  await capture('draft.png');
  const appearances=[];
  for (const mode of ['light','dark']) for (const palette of ['plum','lagoon','graphite']) {
    await appearance(mode,palette);
    appearances.push(await geometry()); await capture(`${mode}-${palette}.png`); await closeAppearance();
  }
  await appearance('system','plum'); await closeAppearance();
  nativeTheme.themeSource='dark'; await wait("document.documentElement.dataset.theme==='dark'");
  nativeTheme.themeSource='light'; await wait("document.documentElement.dataset.theme==='light'");
  await appearance('dark','lagoon'); await closeAppearance();
  await new Promise(resolve=>{window.webContents.once('did-finish-load',resolve);window.webContents.reload();});
  await wait("document.documentElement.dataset.theme==='dark' && document.documentElement.dataset.palette==='lagoon'");
  await connect('UI test · record-only journey'); await click('view-plans');
  await wait("document.querySelector('#tasks').textContent.includes('UI test · zero-budget draft')");
  await click('disconnect'); await wait("document.querySelector('#workspace-navigation').hidden");
  if (!await js("document.documentElement.dataset.theme==='dark' && document.documentElement.dataset.palette==='lagoon'")) throw new Error('Disconnect lost appearance.');
  await connect('UI test · record-only journey'); await click('view-plans');
  await wait("document.querySelector('#tasks').textContent.includes('UI test · zero-budget draft')");
  if (await js("[...document.querySelectorAll('#tasks article')].filter(n=>n.textContent.includes('UI test · zero-budget draft')).length")!==1) throw new Error('Draft replayed.');
  await click('admin-open'); await wait("!document.querySelector('#admin-workspace').hidden");
  await click('admin-return'); await wait("!document.querySelector('#workspace').hidden && document.querySelector('#view-plans').getAttribute('aria-current')==='page'");
  await appearance('light','plum'); await closeAppearance();
  const wide=await geometry();
  window.setContentSize(900,850); await settle();
  const narrow=await geometry(); if(narrow.overflow || narrow.sidebar.width!==216) throw new Error('Narrow shell geometry failed.');
  await click('task-new');
  await js("document.querySelector('#task-form').elements.title.value='UI test · long dialog title '.repeat(5)");
  await capture('900-draft-dialog.png');
  const footer=await js("(() => {const r=document.querySelector('#task-dialog .actions').getBoundingClientRect();return {bottom:r.bottom,right:r.right};})()");
  if(footer.bottom>850 || footer.right>900) throw new Error('Dialog footer clipped.');
  await js("document.querySelector('#task-dialog').close()");
  await appearance('dark','graphite'); await capture('900-appearance.png'); await closeAppearance();
  await js("document.querySelector('.skip-link').focus()");
  window.webContents.sendInputEvent({type:'keyDown',keyCode:'Return'}); window.webContents.sendInputEvent({type:'keyUp',keyCode:'Return'});
  await wait("document.activeElement.id==='main-content'");
  await appearance('light','plum'); await closeAppearance();
  window.setContentSize(1280,850);
  await click('disconnect'); await wait("document.querySelector('#workspace-navigation').hidden");
  await connect('UI test · research workspace');
  await wait(`document.querySelector('[data-experiment-id="${fixture.experimentId}"]')`);
  await js(`document.querySelector('[data-experiment-id="${fixture.experimentId}"]').click()`);
  await wait("document.querySelector('#experiment-detail').getAttribute('aria-busy')==='false' && document.querySelector('#experiment-detail').textContent.includes('Current design revision')");
  await capture('alpha-design.png');
  const designGeometry=await geometry();
  await js("[...document.querySelectorAll('#experiment-detail h4')].find(n=>/attempt/i.test(n.textContent)).scrollIntoView({block:'start'})");
  await capture('alpha-results.png');
  const resultsGeometry=await geometry();
  await click('disconnect'); await wait("document.querySelector('#workspace-navigation').hidden");
  await connect('UI test · record-only journey'); await click('view-plans');
  await wait("document.querySelector('#tasks').textContent.includes('UI test · zero-budget draft')");
  await js('scrollTo(0,0)'); await capture('alpha-review.png');
  if(errors.length) throw new Error('Renderer errors: '+errors.join('; '));
  const result={visible:window.isVisible(),wide,narrow,footer,appearances,designGeometry,resultsGeometry,systemUpdates:true,reloadPersistence:true,escapeFocus:true,skipLink:true,reconnectOneDraft:true,administrationReturn:true,picker:'test-only substitution',records:'synthetic human observations; no execution',errors};
  writeFileSync(join(fixture.output,'workspace-state.json'),JSON.stringify(result,null,2),{mode:0o600});
  console.log(JSON.stringify(result));
}
let timer;
Promise.race([main(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Workspace check timed out')),75000)})])
  .catch(error=>{console.error(error.stack);process.exitCode=1;})
  .finally(()=>{clearTimeout(timer);if(window&&!window.isDestroyed())window.destroy();rmSync(profile,{recursive:true,force:true});app.exit(process.exitCode||0);});
