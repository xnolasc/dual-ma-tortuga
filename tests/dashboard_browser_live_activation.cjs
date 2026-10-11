const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.DUAL_MA_BASE_URL||'http://127.0.0.1:8895';
const out=process.env.DUAL_MA_BROWSER_EVIDENCE||'docs/dual-ma-ai-phase5a-final-browser.json';
const expectedWallet=process.env.DUAL_MA_EXPECT_WALLET;
if(!expectedWallet)throw new Error('Set DUAL_MA_EXPECT_WALLET for the explicitly authorized live acceptance run.');
(async()=>{
 let browser,page;const evidence={started_at:new Date().toISOString(),base_url:base,api:[],monitor_poll_requests:[],failed_requests:[],console_errors:[],page_errors:[],monitor_payloads:[],navigations:0};
 try {
  browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
  page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('console',m=>{if(m.type()==='error')evidence.console_errors.push(m.text());});
  page.on('pageerror',e=>evidence.page_errors.push(e.message));
  page.on('requestfailed',r=>evidence.failed_requests.push({url:new URL(r.url()).pathname,error:r.failure()?.errorText}));
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/monitor')evidence.monitor_poll_requests.push({at_ms:Date.now()});});
  page.on('framenavigated',f=>{if(f===page.mainFrame())evidence.navigations++;});
  page.on('response',async r=>{if(r.url().includes('/api/')){const item={path:new URL(r.url()).pathname,status:r.status()};evidence.api.push(item);if(item.path==='/api/monitor'&&r.status()===200){try{evidence.monitor_payloads.push(await r.json());}catch(e){item.parse_error=e.message;}}}});
  await page.goto(base,{waitUntil:'domcontentloaded',timeout:15000});
  try {await page.waitForFunction(()=>document.querySelector('#monitor-health')?.textContent.includes('DEGRADED'),{},{timeout:15000});}
  catch(e){evidence.dom_at_health_timeout=await page.locator('#monitor').innerText().catch(()=>'<monitor DOM unavailable>');throw e;}
  evidence.initial_health=await page.locator('#monitor-health').textContent();
  evidence.initial_dom=await page.locator('#monitor').innerText();
  evidence.initial_heartbeat=(await page.locator('#monitor-time').textContent()).match(/Worker heartbeat: ([^ ]+)/)?.[1]||null;
  evidence.session=await (await page.request.get(base+'/api/session')).json();
  evidence.wallet=(await (await page.request.get(base+'/api/wallet')).json());
  evidence.monitor_initial=await (await page.request.get(base+'/api/monitor')).json();
  assert.equal(evidence.session.read_only,true);
  assert.equal(evidence.monitor_initial.mode,'READ_ONLY');assert.equal(evidence.monitor_initial.paper_execution_enabled,false);
  assert.equal(evidence.monitor_initial.status,'DEGRADED');assert.equal(evidence.monitor_initial.feed,'iex');
  assert.equal(evidence.wallet.cash,expectedWallet);assert.equal(evidence.wallet.equity,expectedWallet);assert.equal(evidence.wallet.trading_pnl,'0.00');
  const signals=()=>page.locator('#monitor-signals tbody tr').evaluateAll(rs=>rs.map(r=>Array.from(r.querySelectorAll('td'),c=>c.textContent)));
  const checkRows=rows=>{
    for(const ticker of ['SPCX','AAOI']){const row=rows.find(r=>r[0]===ticker);assert(row,`${ticker} is visible as blocked`);assert.equal(row[3],'BLOCKED');assert(!row.join(' ').match(/BUY|SELL/));}
    for(const ticker of ['LITE','MRVL','TSM','CIEN','COHR','MU','NVDA']){
      const matching=rows.filter(r=>r[0].startsWith(ticker));assert(matching.length>0,`${ticker} remains visible`);
      for(const row of matching){if(row[3]==='STALE')assert(!row.join(' ').match(/BUY|SELL/),`${ticker} stale signals are suppressed`);
        else assert(['BUY','SELL','NO CROSS'].includes(row[3]),`${ticker} decision uses completed-candle vocabulary`);}
    }
  };
  checkRows(await signals());
  // Poll cadence and worker heartbeat are independent acceptance signals.
  // Never end this interval early when a worker-stage heartbeat changes.
  const observationStarted=Date.now(),until=observationStarted+80000;
  while(Date.now()<until)await page.waitForTimeout(Math.min(1000,until-Date.now()));
  evidence.final_health=await page.locator('#monitor-health').textContent();evidence.final_dom=await page.locator('#monitor').innerText();
  evidence.final_heartbeat=(await page.locator('#monitor-time').textContent()).match(/Worker heartbeat: ([^ ]+)/)?.[1]||null;
  evidence.signal_rows=await signals();checkRows(evidence.signal_rows);
  evidence.monitor_requests=evidence.monitor_poll_requests.length;
  evidence.observation_elapsed_ms=Date.now()-observationStarted;
  evidence.poll_intervals_ms=evidence.monitor_poll_requests.slice(1).map((x,i)=>x.at_ms-evidence.monitor_poll_requests[i].at_ms);
  evidence.finished_at=new Date().toISOString();
  assert.match(evidence.final_health,/^DEGRADED · CONNECTED · IEX · READ ONLY/);
  assert.notEqual(evidence.initial_heartbeat,evidence.final_heartbeat,'visible worker heartbeat advances automatically');
  evidence.heartbeat_changed=evidence.initial_heartbeat!==evidence.final_heartbeat;
  assert(evidence.observation_elapsed_ms>=79000,'full polling observation window elapsed');
  assert(evidence.monitor_requests>=7,'monitor request count proves automatic polling over the observation window');
  assert(evidence.poll_intervals_ms.every(ms=>ms>=7500&&ms<=12500),'automatic polling cadence is approximately 10 seconds');
  assert.equal(evidence.navigations,1,'dashboard did not reload');
  assert(evidence.api.filter(x=>x.path.startsWith('/api/')).every(x=>x.status===200),'all API responses succeed');
  assert.deepEqual(evidence.failed_requests,[]);assert.deepEqual(evidence.console_errors,[]);assert.deepEqual(evidence.page_errors,[]);
  assert.equal(evidence.session.read_only,true);
  evidence.passed=true;fs.writeFileSync(out,JSON.stringify(evidence,null,2)+'\n');
  console.log(JSON.stringify({passed:true,monitor_requests:evidence.monitor_requests,initial_heartbeat:evidence.initial_heartbeat,final_heartbeat:evidence.final_heartbeat,navigations:evidence.navigations,failed_requests:evidence.failed_requests,console_errors:evidence.console_errors}));
 } catch(e) {
  evidence.passed=false;evidence.failure={name:e.name,message:e.message};
  if(page){evidence.failure_dom=await page.locator('#monitor').innerText().catch(()=>'<monitor DOM unavailable>');await page.screenshot({path:'docs/dual-ma-ai-phase5a-final-browser-failure.png',fullPage:true}).catch(()=>{});}
  evidence.finished_at=new Date().toISOString();fs.writeFileSync(out,JSON.stringify(evidence,null,2)+'\n');console.error(JSON.stringify({failure:evidence.failure,dom:evidence.failure_dom,api:evidence.api,failed_requests:evidence.failed_requests,console_errors:evidence.console_errors,page_errors:evidence.page_errors}));process.exitCode=1;
 } finally {if(browser)await browser.close();}
})();
