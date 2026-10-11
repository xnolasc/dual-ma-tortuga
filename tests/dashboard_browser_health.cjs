const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const {spawn}=require('node:child_process');const assert=require('node:assert/strict');
const root=require('node:path').resolve(__dirname,'..');const fs=require('node:fs');
const child=spawn(root+'/.venv/bin/python',['-u','-m','tests.dashboard_health_fixture'],{cwd:root,env:{...process.env,DASHBOARD_FIXTURE_DELAY_WALLET:'6'}});
(async()=>{let browser;try{
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',d=>resolve(Number(d.toString().trim())));child.once('exit',c=>reject(Error('Server exit '+c)));});
 browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
 const page=await browser.newPage();let gets=0,navigations=0;const errors=[],api=[];
 page.on('request',r=>{if(r.url().endsWith('/api/monitor'))gets++;});page.on('framenavigated',f=>{if(f===page.mainFrame())navigations++;});page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.url().includes('/api/'))api.push({url:new URL(r.url()).pathname,status:r.status()});});
 await page.goto(`http://127.0.0.1:${port}`);await page.waitForFunction(()=>document.querySelector('#monitor-health').textContent.includes('DEGRADED'),{},{timeout:4000});
 assert.match(await page.locator('#monitor-health').textContent(),/DEGRADED · CONNECTED · IEX · READ ONLY/);
 const before=await page.locator('#monitor-time').textContent();await page.waitForTimeout(12000);const after=await page.locator('#monitor-time').textContent();
 assert(gets>=2);assert.notEqual(before,after);assert.equal(navigations,1);assert.deepEqual(errors,[]);
 fs.writeFileSync(process.env.DUAL_MA_BROWSER_EVIDENCE||'docs/dual-ma-ai-phase5a-health-sync-browser.json',JSON.stringify({passed:true,delayed_wallet_seconds:6,degraded_rendered_before_wallet_response:true,monitor_gets:gets,navigations,errors,api,monitor_time_before:before,monitor_time_after:after},null,2)+'\n');console.log('PASS isolated DEGRADED health rendered before delayed wallet response and updated automatically; no page reload; synthetic health-only fixture.');
}finally{if(browser)await browser.close();child.kill('SIGINT');}})().catch(e=>{console.error(e);process.exitCode=1;});
