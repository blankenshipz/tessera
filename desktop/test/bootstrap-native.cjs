'use strict';
// Real macOS native dialogs, isolated private files, synthetic issuer (no HTTP).
const { app, BrowserWindow, dialog } = require('electron');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = path.resolve(process.argv[2]);
fs.mkdirSync(output, { recursive:true, mode:0o700 });
const manual = process.argv.includes('--manual');
const observedAt = Date.now();
const expiresAt = observedAt + (manual ? 20 * 60 * 1000 : 45000);
function record(event) {
  const fd = fs.openSync(path.join(output, 'native-events.jsonl'), 'a', 0o600);
  try { fs.writeSync(fd, JSON.stringify({ at:Date.now(),pid:process.pid,...event })+'\n');fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
record({phase:'starting',manual,observedAt,expiresAt,syntheticIssuer:true,noHTTP:true});
const heartbeat=setInterval(()=>fs.writeFileSync(path.join(output,'recorder-health.json'),JSON.stringify({pid:process.pid,at:Date.now(),expiresAt}),{mode:0o600}),5000);
heartbeat.unref();
app.setPath('userData', fs.mkdtempSync(path.join(output, 'profile-')));
app.on('window-all-closed', () => {}); // Retain failure until the recorder writes it.
let window;
const run = promisify(execFile);
async function automate(action) {
  // Scope interaction to this test process's native sheet, never another application.
  const script = `tell application "System Events"
tell (first application process whose unix id is ${process.pid})
set frontmost to true
repeat 80 times
if (count of windows) > 0 then exit repeat
delay 0.1
end repeat
if (count of windows) = 0 then error "Test native window unavailable"
delay 1
${action === 'issue' ? `if exists button "Issue control credential" of sheet 1 of window 1 then
click button "Issue control credential" of sheet 1 of window 1
else if exists button "Issue control credential" of window 1 then
click button "Issue control credential" of window 1
else
error "Native confirmation button not found in observed window"
end if` : action === 'save' ? 'key code 36' : 'key code 53'}
end tell
end tell`;
  await run('/usr/bin/osascript', ['-e', script], { timeout:15000 });
}
async function main() {
  const { runControlIssuance, nativeIssuanceDialogs } = await import('../control-issuance.mjs');
  window = new BrowserWindow({width:850,height:650,show:true,webPreferences:{sandbox:true,nodeIntegration:false,contextIsolation:true}});
  await window.loadURL('data:text/html,<h1>Isolated native credential test</h1><p>Synthetic issuer only. No production credentials or API calls.</p>');
  window.webContents.session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_request,callback)=>callback({cancel:true}));
  record({phase:'window-visible',visible:window.isVisible()});
  let posts=0;
  const issuer={binding:{origin:'http://127.0.0.1:1',principal_id:'isolated-root',credential_id:'isolated-root-credential',org_id:'isolated',project_id:'isolated-project'},current:()=>{},issue:async({expires_at})=>{posts++;return {principal_id:'isolated-operator',credential_id:'isolated-child',expires_at}}};
  const results=[];
  for(const scenario of ['cancel-save','cancel-confirm','confirm']) {
    const bundle=path.join(output,scenario+'-bundle');
    const native=nativeIssuanceDialogs(dialog,window,bundle);
    const interact=async(action,show)=>{
      const attempt={scenario,action,syntheticPostCount:posts,receiverReady:true,manual,pid:process.pid,observedAt,expiresAt};
      fs.writeFileSync(path.join(output,'native-attempt.json'),JSON.stringify(attempt,null,2),{mode:0o600});
      record({phase:'dialog-requested',...attempt});
      if(manual) {const result=await show();record({phase:'dialog-returned',scenario,action});return result;}
      let failure;
      const input=automate(action).catch(error=>{failure=error;fs.writeFileSync(path.join(output,'automation-failure.json'),JSON.stringify({action,message:error.message,code:error.code,killed:error.killed,signal:error.signal,stderr:error.stderr},null,2),{mode:0o600});window.destroy()});
      const result=await show();await input;if(failure)throw failure;return result;
    };
    const result=await runControlIssuance({issuer,mode:'new',minutes:60,
      choose:async mode=>{
        const selected=await interact(scenario==='cancel-save'?'cancel':'save',()=>native.choose(mode));
        if(scenario==='cancel-save')return selected;
        const parent=fs.realpathSync(path.dirname(selected));
        assert.equal(parent,fs.realpathSync(path.dirname(bundle)));
        assert.equal(path.basename(selected),path.basename(bundle));
        // macOS Save may return /tmp for /private/tmp. Canonicalize only this
        // verified test fixture, leaving production symlink guards unchanged.
        return path.join(parent,path.basename(selected));
      },
      confirm:async binding=>interact(scenario==='confirm'?'issue':'cancel',()=>native.confirm(binding))});
    if(scenario!=='confirm'){assert.equal(result.canceled,true);assert.equal(posts,0);assert.equal(fs.existsSync(bundle),false)}
    else {assert.equal(posts,1);assert.equal(fs.statSync(bundle).mode&0o777,0o700);assert.equal(fs.statSync(result.token_file).mode&0o777,0o600)}
    results.push({scenario,canceled:!!result.canceled,syntheticPostCount:posts,realNativeDialogs:true});
  }
  fs.writeFileSync(path.join(output,'native-result.json'),JSON.stringify({results,coverage:'real native save/confirm/cancel interaction and private destination; synthetic issuer, NOT production authorization'},null,2),{mode:0o600});
  record({phase:'captured',results});
  console.log('Real native save/cancel, confirm/cancel, explicit confirm and private export passed with synthetic issuer.');
}
const timeout=setTimeout(()=>{record({phase:'expired'});if(window)window.destroy();app.exit(2)},expiresAt-Date.now());
app.whenReady().then(main).then(()=>{clearTimeout(timeout);window.destroy();app.exit(0)}).catch(error=>{fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({message:error.message},null,2),{mode:0o600});console.error(error.message);clearTimeout(timeout);if(window)window.destroy();app.exit(1)});
