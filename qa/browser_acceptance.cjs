/* Development-only acceptance. Uses installed Chrome, an isolated profile and temporary spool. */
const { chromium } = require(process.env.LANFILES_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const spool = fs.mkdtempSync(path.join(os.tmpdir(), 'lanfiles-browser-'));
const base = 'http://127.0.0.1:18764';
let server, browser;
const results = [];
const errors = [];
function check(name) { results.push(name); console.log('PASS', name); }
async function startServer() {
  server = spawn(process.env.LANFILES_PYTHON || 'python3', [path.join(root, 'transfer.py'), '--host', '127.0.0.1', '--port', '18764', '--dir', spool, '--min-free-space', '0'],
    { cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  server.stdout.on('data', b => log += b); server.stderr.on('data', b => log += b);
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    if (server.exitCode !== null) throw new Error('Server exited: ' + log);
    try { if ((await fetch(base)).ok) return; } catch (_) {}
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error('Server readiness timeout: ' + log);
}
async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const stopped = new Promise(resolve => server.once('exit', resolve));
  server.kill('SIGTERM'); await stopped;
}
async function opened(context, name) {
  const p = await context.newPage(); p.on('pageerror', e => errors.push(e.message));
  await p.goto(base); await p.locator('#connection-status').filter({hasText:'已连接'}).waitFor();
  if (name) { await p.locator('#my-name').fill(name); await p.locator('#my-name').press('Tab'); await p.waitForFunction(n => document.querySelector('#my-name').value === n, name); }
  return p;
}
(async () => {
  try {
    await startServer();
    browser = await chromium.launch({headless:true, executablePath:process.env.LANFILES_CHROME});
    const ca = await browser.newContext({acceptDownloads:true, viewport:{width:1000,height:850}});
    const cb = await browser.newContext({acceptDownloads:true, viewport:{width:390,height:844}});
    const cc = await browser.newContext({acceptDownloads:true});
    const a = await opened(ca, '发送端'); const b = await opened(cb, '接收端'); const c = await opened(cc, '另一个收件人');
    const cookies = await ca.cookies();
    assert(cookies.some(x => x.name.startsWith('lanfiles_session_') && x.httpOnly && x.sameSite==='Strict'));
    assert(!(await a.evaluate(() => document.cookie)).includes('lanfiles_session'));
    check('HttpOnly Cookie identity unavailable to page JavaScript');
    await a.locator('.device').filter({hasText:'接收端'}).waitFor();
    await a.locator('.device').filter({hasText:'接收端'}).click();
    const receiver = await cb.request.get(base+'/api/session').then(r=>r.json());
    let observed = [];
    let release;
    const gate = new Promise(resolve => release = resolve);
    await a.route('**/api/send?**', async route => {
      observed.push(route.request().url());
      if (observed.length===1) await gate;
      await route.continue();
    });
    await a.locator('#file-input').setInputFiles([
      {name:'中文一.txt',mimeType:'text/plain',buffer:Buffer.from('first 完整内容')},
      {name:'中文二.txt',mimeType:'text/plain',buffer:Buffer.from('second 完整内容')}
    ]);
    await a.waitForFunction(()=>document.querySelectorAll('.send-item').length===2);
    assert.equal(observed.length,1);
    await a.locator('.device').filter({hasText:'另一个收件人'}).click();
    release();
    await a.locator('.send-item .st.ok').nth(1).waitFor();
    assert.equal(observed.length,2);
    assert(observed.every(url=>new URL(url).searchParams.get('to')===receiver.device_id));
    await a.unroute('**/api/send?**');
    check('Uploads queued serially and queued recipient remains fixed');
    await b.locator('.inbox-item').filter({hasText:'中文一.txt'}).waitFor();
    await b.locator('.inbox-item').filter({hasText:'中文二.txt'}).waitFor();
    const downloadPromise = b.waitForEvent('download');
    await b.locator('.inbox-item').filter({hasText:'中文一.txt'}).getByText('下载',{exact:true}).click();
    const download = await downloadPromise;
    assert.equal(download.suggestedFilename(),'中文一.txt');
    assert.equal(fs.readFileSync(await download.path(),'utf8'),'first 完整内容');
    check('Browser download includes Cookie and preserves Chinese name/content');
    const before = (await cb.request.get(base+'/api/session').then(r=>r.json())).device_id;
    await stopServer();
    await b.locator('#connection-status').filter({hasText:'连接中断'}).waitFor();
    check('Connection loss visibly reported');
    await startServer();
    await b.locator('#connection-status').filter({hasText:'已连接'}).waitFor({timeout:25000});
    assert.equal((await cb.request.get(base+'/api/session').then(r=>r.json())).device_id,before);
    await b.locator('.inbox-item').filter({hasText:'中文二.txt'}).waitFor();
    check('Service restart restores same identity and outstanding inbox without reload');
    const sibling=await opened(cb);
    assert.equal((await cb.request.get(base+'/api/session').then(r=>r.json())).device_id,before);
    await sibling.locator('.inbox-item').filter({hasText:'中文二.txt'}).waitFor();
    check('Tabs share one persisted browser identity');
    // Hold recovery so draining the remaining queue with invalid identity is observable.
    await a.locator('.device').filter({hasText:'接收端'}).click();
    let queueRequests=0, recoverStarted=false, resumeRecovery;
    const recoverGate=new Promise(r=>resumeRecovery=r);
    await a.route('**/api/register',async route=>{recoverStarted=true;await recoverGate;await route.continue();});
    await a.route('**/api/send?**',async route=>{
      queueRequests++;
      if(queueRequests===1) await route.fulfill({status:401,contentType:'application/json',body:'{"error":"expired session"}'});
      else await route.continue();
    });
    await a.locator('#file-input').setInputFiles(['queue-a.txt','queue-b.txt','queue-c.txt'].map(name=>({name,mimeType:'text/plain',buffer:Buffer.from(name)})));
    const until=Date.now()+3000;
    while(!recoverStarted && Date.now()<until) await new Promise(r=>setTimeout(r,20));
    assert(recoverStarted);
    await new Promise(r=>setTimeout(r,200));
    const pausedCorrectly=queueRequests===1;
    resumeRecovery();
    assert(pausedCorrectly,'remaining uploads must pause during identity recovery');
    await a.locator('.send-item').filter({hasText:'queue-b.txt'}).locator('.st.ok').waitFor({timeout:15000});
    await a.locator('.send-item').filter({hasText:'queue-c.txt'}).locator('.st.ok').waitFor({timeout:15000});
    await a.unroute('**/api/register'); await a.unroute('**/api/send?**');
    check('Pending uploads pause until identity recovery; failed item is not retried');
    await b.locator('#domain-input').fill('9876');
    await new Promise(r=>setTimeout(r,1700));
    assert.equal(await b.locator('#domain-input').inputValue(),'9876');
    await b.locator('#my-name').focus();
    check('Polling does not overwrite an actively edited room code');
    for (const p of [a,b]) { await p.locator('#domain-input').fill('1234'); await p.locator('#domain-join').click(); await p.locator('#domain-status').filter({hasText:'#1234'}).waitFor(); }
    await a.locator('#bfile-input').setInputFiles({name:'广播.txt',mimeType:'text/plain',buffer:Buffer.from('room payload')});
    await b.locator('.inbox-item').filter({hasText:'广播.txt'}).waitFor();
    check('Room broadcast remains available to current members');
    await cb.clearCookies();
    // Another tab can establish the replacement Cookie before this tab observes a 401.
    await cb.request.post(base+'/api/register',{data:{domain:'1234'}});
    await b.locator('#connection-status').filter({hasText:'已连接'}).waitFor({timeout:20000});
    await b.waitForFunction(()=>!Array.from(document.querySelectorAll('.inbox-item')).some(x=>x.textContent.includes('中文二.txt')));
    const after=(await cb.request.get(base+'/api/session').then(r=>r.json())).device_id;
    assert.notEqual(after,before);
    await b.locator('#identity-notice').filter({hasText:'新身份'}).waitFor({timeout:4000});
    check('Lost credential creates new identity and clears stale private inbox');
    assert(await b.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await a.screenshot({path:path.join(__dirname,'desktop.png'),fullPage:true});
    await b.screenshot({path:path.join(__dirname,'mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    check('Desktop/mobile render without horizontal overflow or JavaScript errors');
    fs.writeFileSync(path.join(__dirname,'browser-results.json'),JSON.stringify({results,errors},null,2));
  } finally {
    if(browser) await browser.close(); await stopServer(); fs.rmSync(spool,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
