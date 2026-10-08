/* Isolated browser acceptance for visual deletion. No production data or browser profile is used. */
const { chromium }=require(process.env.LANFILES_PLAYWRIGHT || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..'),spool=fs.mkdtempSync(path.join(os.tmpdir(),'lanfiles-delete-'));
const base='http://127.0.0.1:18766',python=process.env.LANFILES_PYTHON || 'python3';
const results=[],errors=[];let browser,server;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function check(name){results.push(name);console.log('PASS',name);}
async function waitUntil(test,timeout=5000){const until=Date.now()+timeout;while(Date.now()<until){if(await test())return;await sleep(50);}throw new Error('Condition timeout');}
async function start(){
 const code=`import sys,signal,transfer\ns=transfer.create_server('127.0.0.1',18766,sys.argv[1],transfer.Limits(min_free_space=0,register_per_ip=100,register_global=1000))\ndef stop(a,b): raise KeyboardInterrupt\nsignal.signal(signal.SIGTERM,stop)\ntry:s.serve_forever(poll_interval=.02)\nexcept KeyboardInterrupt:pass\nfinally:s.server_close()\n`;
 server=spawn(python,['-c',code,spool],{cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},stdio:['ignore','pipe','pipe']});
 let log='';server.stdout.on('data',b=>log+=b);server.stderr.on('data',b=>log+=b);
 await waitUntil(async()=>{if(server.exitCode!==null)throw new Error(log);try{return(await fetch(base)).ok;}catch(_){return false;}},10000);
}
async function stop(){if(!server||server.exitCode!==null)return;const ended=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await ended;}
async function open(context){const p=await context.newPage();p.on('pageerror',e=>errors.push(e.message));await p.goto(base);await p.locator('#connection-status').filter({hasText:'已连接'}).waitFor();return p;}
async function profile(context){return(await context.request.get(base+'/api/session')).json();}
async function files(context){return(await context.request.get(base+'/api/files')).json();}
async function send(context,name,target,domain){const params=new URLSearchParams({name});params.set(domain?'domain':'to',domain||target);const r=await context.request.post(base+'/api/send?'+params,{data:Buffer.from(name),headers:{'Content-Type':'application/octet-stream'}});assert.equal(r.status(),200);return(await r.json()).transfer_id;}
function row(page,name){return page.locator('.file-item').filter({hasText:name});}
async function readyRow(page,name){await row(page,name).waitFor();return row(page,name);}
async function tick(){await sleep(1800);}
(async()=>{
 try{
  await start();browser=await chromium.launch({headless:true,executablePath:process.env.LANFILES_CHROME});
  const ca=await browser.newContext({viewport:{width:1000,height:900}}),cb=await browser.newContext({viewport:{width:390,height:844}}),cc=await browser.newContext();
  const a=await open(ca),b=await open(cb),c=await open(cc);
  const receiver=await profile(cb);
  await send(ca,'取消删除.txt',receiver.device_id);
  await readyRow(b,'取消删除.txt');
  let confirmText='';b.once('dialog',async d=>{confirmText=d.message();await d.dismiss();});
  await row(b,'取消删除.txt').getByRole('button',{name:'删除',exact:true}).click();
  assert(confirmText.includes('取消删除.txt'));assert(confirmText.includes('已下载'));
  assert((await files(cb)).inbox.some(x=>x.filename==='取消删除.txt'));
  check('Single deletion confirmation can be cancelled without deleting data');
  b.once('dialog',d=>d.accept());
  await row(b,'取消删除.txt').getByRole('button',{name:'删除',exact:true}).click();
  await waitUntil(async()=>!(await files(cb)).inbox.some(x=>x.filename==='取消删除.txt'));
  await row(b,'取消删除.txt').waitFor({state:'detached'});
  check('Single deletion removes both file listing and persisted transfer');
  for(const p of [a,b,c]){await p.locator('#domain-input').fill('1234');await p.locator('#domain-join').click();await p.locator('#domain-status').filter({hasText:'#1234'}).waitFor();}
  const shared=await send(ca,'发送者共享.txt',null,'1234');
  await readyRow(a,'发送者共享.txt');await readyRow(b,'发送者共享.txt');
  assert.equal(await row(b,'发送者共享.txt').getByRole('button',{name:'删除',exact:true}).count(),0);
  assert.equal(await row(b,'发送者共享.txt').getByRole('checkbox').count(),0);
  assert.equal((await cb.request.post(base+'/api/ack/'+shared)).status(),403);
  await a.locator('#domain-leave').click();await a.locator('#domain-status').filter({hasText:'未加入域'}).waitFor();
  await readyRow(a,'发送者共享.txt');
  assert(await row(a,'发送者共享.txt').innerText().then(t=>t.includes('1234')));
  a.once('dialog',d=>d.accept());await row(a,'发送者共享.txt').getByRole('button',{name:'删除',exact:true}).click();
  await waitUntil(async()=>!(await files(ca)).outbox.some(x=>x.filename==='发送者共享.txt'));
  check('Only sender can delete broadcast, including after leaving its room');
  // Receiver also owns a broadcast: selection spans both groups.
  await send(cc,'批量私发一.txt',receiver.device_id);await send(cc,'批量私发二.txt',receiver.device_id);
  await send(cb,'批量自己广播.txt',null,'1234');
  for(const n of ['批量私发一.txt','批量私发二.txt','批量自己广播.txt'])await readyRow(b,n);
  await b.getByRole('checkbox',{name:'选择全部可删除文件'}).check();
  assert((await b.locator('#delete-selected').innerText()).includes('3'));
  let dialogs=0,acks=0;let release;const gate=new Promise(r=>release=r);
  b.on('dialog',async d=>{dialogs++;await d.accept();});
  await b.route('**/api/ack/**',async route=>{acks++;if(acks===1)await gate;await route.continue();});
  await b.locator('#delete-selected').click();await waitUntil(()=>acks===1);
  assert(await b.locator('#delete-selected').isDisabled());
  await tick();assert.equal(acks,1);assert.equal(dialogs,1);
  await send(cc,'操作后新到文件.txt',receiver.device_id);
  release();
  await waitUntil(()=>acks===3);await b.locator('#delete-result').filter({hasText:'已删除 3'}).waitFor();
  await b.unroute('**/api/ack/**');b.removeAllListeners('dialog');
  assert((await files(cb)).inbox.some(x=>x.filename==='操作后新到文件.txt'));
  check('Cross-group batch confirms once, serializes requests, survives polling and keeps new arrivals');
  const first=await send(cc,'部分成功.txt',receiver.device_id),failed=await send(cc,'部分失败.txt',receiver.device_id);
  await readyRow(b,'部分成功.txt');await readyRow(b,'部分失败.txt');
  for(const n of ['部分成功.txt','部分失败.txt'])await row(b,n).getByRole('checkbox').check();
  await b.route('**/api/ack/'+failed+'**',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"测试磁盘暂不可用"}'}));
  b.once('dialog',d=>d.accept());await b.locator('#delete-selected').click();
  await b.locator('#delete-result').filter({hasText:'失败 1'}).waitFor();
  assert(await row(b,'部分失败.txt').innerText().then(t=>t.includes('测试磁盘暂不可用')));
  assert(await row(b,'部分失败.txt').getByRole('checkbox').isChecked());
  await b.unroute('**/api/ack/'+failed+'**');
  b.once('dialog',d=>d.accept());await row(b,'部分失败.txt').getByRole('button',{name:'删除',exact:true}).click();
  await waitUntil(async()=>!(await files(cb)).inbox.some(x=>x.transfer_id===failed));
  check('Partial failure is visible and retained for an explicit retry');
  const gone=await send(cc,'已被其他标签删除.txt',receiver.device_id);
  await readyRow(b,'已被其他标签删除.txt');
  await cb.request.post(base+'/api/ack/'+gone);
  b.once('dialog',d=>d.accept());await row(b,'已被其他标签删除.txt').getByRole('button',{name:'删除',exact:true}).click();
  await b.locator('#delete-result').filter({hasText:'已删除 1'}).waitFor();
  check('Already removed transfer is treated as complete');
  const stop1=await send(cc,'失效第一项.txt',receiver.device_id),stop2=await send(cc,'失效第二项.txt',receiver.device_id);
  await readyRow(b,'失效第一项.txt');await readyRow(b,'失效第二项.txt');
  for(const n of ['失效第一项.txt','失效第二项.txt'])await row(b,n).getByRole('checkbox').check();
  let sent=0;b.on('request',r=>{if(r.url().includes('/api/ack/'))sent++;});
  await b.route('**/api/ack/**',route=>route.fulfill({status:401,contentType:'application/json',body:'{"error":"身份已失效"}'}));
  b.once('dialog',d=>d.accept());await b.locator('#delete-selected').click();
  await b.locator('#delete-result').filter({hasText:'未删除 1'}).waitFor();
  await sleep(250);assert.equal(sent,1);
  await b.unroute('**/api/ack/**');
  check('401 stops remaining deletions without retrying them');
  await waitUntil(async()=>(await files(cb)).inbox.length>=2);
  await tick();assert.equal(await b.getByRole('checkbox',{name:'选择全部可删除文件'}).isChecked(),false);
  check('Identity recovery clears file selection');
  // Persisted pending delete with actual filesystem unlink failure, then restart.
  await send(cc,'等待删除共享.txt',null,'1234');await readyRow(c,'等待删除共享.txt');
  let pendingTid=(await files(cc)).outbox.find(x=>x.filename==='等待删除共享.txt').transfer_id;
  await stop();
  execFileSync(python,['-c',"import sqlite3,sys;d=sqlite3.connect(sys.argv[1]);d.execute(\"UPDATE transfers SET state='deleting' WHERE transfer_id=?\",(sys.argv[2],));d.commit();d.close()",path.join(spool,'.lanfiles','state.sqlite3'),pendingTid]);
  // Keep the blob as a directory to force unlink failure, preserving retry metadata.
  const blob=path.join(spool,'.lanfiles','files',pendingTid);fs.unlinkSync(blob);fs.mkdirSync(blob);
  await start();
  await readyRow(c,'等待删除共享.txt');
  await row(c,'等待删除共享.txt').getByText('等待删除',{exact:true}).waitFor({timeout:20000});
  assert(await row(c,'等待删除共享.txt').getByRole('button',{name:'删除',exact:true}).isDisabled());
  assert.equal(await row(c,'等待删除共享.txt').getByText('下载',{exact:true}).count(),0);
  fs.rmdirSync(blob);
  await waitUntil(async()=>!(await files(cc)).outbox.some(x=>x.transfer_id===pendingTid));
  check('Pending delete survives restart, disables actions and disappears after successful retry');
  // Delay an old profile response while a newer identity is established by an explicit profile update.
  const cx=await browser.newContext({viewport:{width:1000,height:900}}),x=await open(cx);
  await cx.request.post(base+'/api/register',{data:{domain:'1234',name:'旧身份'}});await tick();
  let profileHeld=false,releaseProfile;
  const profileGate=new Promise(r=>releaseProfile=r);
  await x.route('**/api/session',async route=>{
    if(!profileHeld){const response=await route.fetch();profileHeld=true;await profileGate;await route.fulfill({response});}
    else await route.continue();
  });
  await waitUntil(()=>profileHeld);
  await cx.clearCookies();
  const replacement=await cx.request.post(base+'/api/register',{data:{domain:'1234',name:'新身份'}}).then(r=>r.json());
  await send(cc,'新身份保留选择.txt',replacement.device_id);
  await x.locator('#my-name').fill('新身份页面');await x.locator('#my-name').press('Tab');
  await readyRow(x,'新身份保留选择.txt');await row(x,'新身份保留选择.txt').getByRole('checkbox').check();
  releaseProfile();await tick();
  const kept=await row(x,'新身份保留选择.txt').getByRole('checkbox').isChecked();
  await x.unroute('**/api/session');
  if(kept)check('Late old-session response does not overwrite new identity or selection');
  // Reset the UI to isolate the second race, even if the first assertion failed.
  await x.reload();await x.locator('#connection-status').filter({hasText:'已连接'}).waitFor();
  const owner=(await profile(cx)).device_id;
  for(const name of ['切换首项.txt','切换第二项.txt','切换第三项.txt']){await send(cc,name,owner);await readyRow(x,name);await row(x,name).getByRole('checkbox').check();}
  let releaseAck,releaseSessions,requestCount=0;
  const ackGate=new Promise(r=>releaseAck=r),sessionGate=new Promise(r=>releaseSessions=r);
  await x.route('**/api/session',async route=>{await sessionGate;await route.continue();});
  await x.route('**/api/ack/**',async route=>{
    requestCount++;
    if(requestCount===1){
      const response=await route.fetch();
      assert.equal(response.status(),200);
      await ackGate;await route.fulfill({response});
    }else await route.continue();
  });
  x.once('dialog',d=>d.accept());await x.locator('#delete-selected').click();await waitUntil(()=>requestCount===1);
  await cx.clearCookies();await cx.request.post(base+'/api/register',{data:{domain:'1234'}});
  releaseAck();
  await waitUntil(async()=>(await x.locator('#delete-selected').innerText())!=='正在删除…');
  const stoppedAtMismatch=requestCount===2;
  releaseSessions();await x.unroute('**/api/ack/**');await x.unroute('**/api/session');
  if(stoppedAtMismatch)check('Cookie identity change stops batch before submitting remaining old-identity items');
  assert(kept,'late old profile response cleared new identity selection');
  assert(stoppedAtMismatch,'batch continued after Cookie identity mismatch: requests='+requestCount);
  assert(await b.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await b.setViewportSize({width:1000,height:900});
  await b.screenshot({path:path.join(__dirname,'delete-desktop.png'),fullPage:true});
  await b.setViewportSize({width:390,height:844});
  await b.screenshot({path:path.join(__dirname,'delete-mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);check('Deletion controls render on mobile/desktop without overflow or script errors');
  fs.writeFileSync(path.join(__dirname,'delete-results.json'),JSON.stringify({results,errors},null,2));
 }finally{if(browser)await browser.close();await stop();fs.rmSync(spool,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
