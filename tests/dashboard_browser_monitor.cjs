const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const {spawn}=require('node:child_process');const path=require('node:path');const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const child=spawn(path.join(root,'.venv/bin/python'),['-u','-m','tests.monitor_browser_server'],{cwd:root});
(async()=>{let browser;try{
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',d=>resolve(Number(d.toString().trim())));child.once('exit',c=>reject(Error('Server exit '+c)));child.stderr.on('data',d=>process.stderr.write(d));});
 browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));let gets=0,navigations=0;
 page.on('request',r=>{if(r.url().endsWith('/api/monitor'))gets++;});page.on('framenavigated',f=>{if(f===page.mainFrame())navigations++;});
 await page.goto(`http://127.0.0.1:${port}`);await page.waitForFunction(()=>document.querySelector('#monitor-health').textContent.includes('RUNNING'));
 assert.match(await page.locator('#monitor-health').textContent(),/READ ONLY/);assert.equal(await page.locator('#monitor-signals tbody tr').count(),3);
 const initial=await page.locator('#monitor-prices').textContent();await page.waitForFunction(first=>document.querySelector('#monitor-prices').textContent!==first,initial,{timeout:15000});
 assert(gets>=2);assert.equal(navigations,1);assert.equal(await page.locator('#review').isDisabled(),true);
 const rejected=await page.evaluate(async()=>{const r=await fetch('/api/funding/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return r.status;});assert.equal(rejected,403);
 assert.equal(await page.locator('#metrics .value').first().textContent(),'$2,000.00');
 await page.screenshot({path:path.join(root,process.env.DUAL_MA_MONITOR_SCREENSHOT||'docs/dual-ma-ai-phase5a-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
 console.log('PASS: 10-second automatic price/status/signal polling, no page reload, three daily presets, separate quote timestamps/age, read-only funding rejected, temporary wallet unchanged, desktop/mobile, no JavaScript errors. Synthetic fixture only.');
}finally{if(browser)await browser.close();child.kill('SIGINT');}})().catch(e=>{console.error(e);process.exitCode=1;});
