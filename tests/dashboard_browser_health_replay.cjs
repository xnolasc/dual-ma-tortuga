const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const {spawn}=require('node:child_process');const assert=require('node:assert/strict');
const root=require('node:path').resolve(__dirname,'..');const fs=require('node:fs');
const child=spawn(root+'/.venv/bin/python',['-u','-m','tests.dashboard_health_replay_fixture'],{cwd:root});
(async()=>{let browser;try{
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',d=>resolve(Number(d.toString().trim())));child.once('exit',c=>reject(Error('Server exit '+c)));});
 browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
 const page=await browser.newPage();let gets=0,navigations=0;const errors=[],consoleErrors=[],api=[],monitorPayloads=[];
 page.on('request',r=>{if(r.url().endsWith('/api/monitor'))gets++;});page.on('framenavigated',f=>{if(f===page.mainFrame())navigations++;});page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});page.on('response',async r=>{if(r.url().includes('/api/'))api.push({url:new URL(r.url()).pathname,status:r.status()});if(r.url().endsWith('/api/monitor')&&r.status()===200)monitorPayloads.push(await r.json());});
 await page.goto(`http://127.0.0.1:${port}`);
 try {await page.waitForFunction(()=>document.querySelector('#monitor-health')?.textContent.includes('DEGRADED'),{},{timeout:15000});}
 catch(e){throw new Error(`${e.message}; DOM=${JSON.stringify(await page.locator('#monitor').innerText())}; requests=${JSON.stringify(api)}; console=${JSON.stringify(consoleErrors)}; pageerrors=${JSON.stringify(errors)}`);}
 assert.match(await page.locator('#monitor-health').textContent(),/DEGRADED · CONNECTED · IEX · READ ONLY/);
 const rows=await page.locator('#monitor-signals tbody tr').evaluateAll(rs=>rs.map(r=>Array.from(r.querySelectorAll('td'),c=>c.textContent)));
 for(const ticker of ['SPCX','AAOI']){const row=rows.find(r=>r[0]===ticker);assert(row,`${ticker} appears in the signal panel`);assert.equal(row[2],'—');assert.equal(row[3],'BLOCKED');assert(!row.join(' ').match(/BUY|SELL/));}
 for(const ticker of ['LITE','MRVL','TSM','CIEN','COHR','MU','NVDA']){const tickerRows=rows.filter(r=>r[0].startsWith(ticker));assert.equal(tickerRows.length,3,`${ticker} has three strategy rows`);assert(tickerRows.every(r=>r[3]==='NO CROSS'));}
 const before=await page.locator('#monitor-time').textContent();await page.waitForTimeout(22000);const after=await page.locator('#monitor-time').textContent();
 assert(gets>=3);assert.notEqual(before,after);assert.equal(navigations,1);assert.deepEqual(errors,[]);assert.deepEqual(consoleErrors,[]);
 assert(monitorPayloads.length>=3&&monitorPayloads.every(m=>m.status==='DEGRADED'&&m.paper_execution_enabled===false));
 fs.writeFileSync(process.env.DUAL_MA_BROWSER_EVIDENCE||'docs/dual-ma-ai-phase5a-health-replay-browser.json',JSON.stringify({passed:true,monitor_gets:gets,navigations,errors,console_errors:consoleErrors,api,monitor_payloads:monitorPayloads.length,blocked_symbols_suppressed:['SPCX','AAOI'],monitor_time_before:before,monitor_time_after:after},null,2)+'\n');console.log('PASS isolated DEGRADED health rendered and updated automatically; blocked symbols suppressed; no page reload or JavaScript errors.');
}finally{if(browser)await browser.close();child.kill('SIGINT');}})().catch(e=>{console.error(e);process.exitCode=1;});
