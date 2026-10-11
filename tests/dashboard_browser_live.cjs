/* Optional read-only integration check against acquired REAL caches/reports.
 * Does not submit wallet forms. Fixture suites remain independent of these files. */
const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const {spawn}=require('node:child_process');
const {readFileSync}=require('node:fs');
const {createHash}=require('node:crypto');
const assert=require('node:assert/strict');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const wallet=path.join(root,'data/dual_ma_ai/wallet.sqlite3');
const hash=()=>createHash('sha256').update(readFileSync(wallet)).digest('hex');
const before=hash();
const child=spawn(path.join(root,'.venv/bin/python'),['-u','-c',`from dual_ma_ai.dashboard import Dashboard, make_server
from dual_ma_ai.wallet import Wallet
with make_server(Dashboard(Wallet.open_existing('data/dual_ma_ai/wallet.sqlite3')),0) as server:
 print(server.server_port,flush=True)
 try: server.serve_forever()
 except KeyboardInterrupt: pass
`],{cwd:root});
(async()=>{let browser;try{
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',d=>resolve(Number(d.toString().trim())));child.once('exit',c=>reject(Error('Server exit '+c)));});
 browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(60000);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);
 await page.waitForFunction(()=>document.querySelector('#report-label').textContent.includes('REAL'),{},{timeout:60000});
 // The page starts signal/coverage reads concurrently with report research.
 // Wait for those initial panel requests to finish before issuing direct API
 // assertions, so the probe does not compete with the exact same cold query.
 await page.waitForFunction(()=>document.querySelectorAll('#signal-table tbody tr').length===9,{},{timeout:60000});
 await page.waitForFunction(()=>document.querySelectorAll('#coverage tbody tr').length===9,{},{timeout:60000});
 assert.equal(await page.locator('#include-synthetic').isChecked(),false);
 const snapshot=await page.evaluate(async()=>{const r=await fetch('/api/wallet');return r.json();});
 assert.equal(await page.locator('#metrics .value').first().textContent(),'$'+Number(snapshot.equity).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}));
 const signals=await page.evaluate(async()=>{const r=await fetch('/api/signals?provider=alpaca&feed=iex');return r.json();});
 assert(signals.rows.some(r=>r.status==='REAL'&&r.feed==='iex'));
 assert(signals.rows.filter(r=>['SPCX','AAOI'].includes(r.ticker)).every(r=>r.status==='UNAVAILABLE'));
 assert.equal(await page.locator('#chart path').count(),3);
 await page.waitForFunction(()=>document.querySelectorAll('#coverage tbody tr').length===9,{},{timeout:60000});
 assert.match(await page.locator('#coverage').textContent(),/VALIDATED/);
 await page.screenshot({path:path.join(root,process.env.DUAL_MA_LIVE_SCREENSHOT || 'docs/dual-ma-ai-phase4.1-live-dashboard.png'),fullPage:true});
 assert.deepEqual(errors,[]);assert.equal(hash(),before);
 console.log('PASS: acquired REAL reports shown by default, IEX signal provenance, rejected tickers unavailable, 3 strategy curves, persistent wallet UI consistency and unchanged SHA256; GET requests only.');
}finally{if(browser)await browser.close();child.kill('SIGINT');assert.equal(hash(),before);}})().catch(e=>{console.error(e);process.exitCode=1;});
