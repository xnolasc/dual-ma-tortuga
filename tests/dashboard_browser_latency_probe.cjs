const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const {spawn}=require('node:child_process');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const child=spawn(path.join(root,'.venv/bin/python'),['-u','-m','tests.dashboard_latency_browser_fixture'],{cwd:root});
(async()=>{let browser;try{
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',d=>resolve(Number(d.toString().trim())));child.once('exit',c=>reject(Error('fixture server exited '+c)));});
 browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
 const pageCount=Number(process.env.DUAL_MA_LATENCY_PAGES||2);
 const evidence={isolated_temporary_state:true,browser_pages:pageCount,requests:[],console_errors:[],page_errors:[]};
 const pages=await Promise.all(Array.from({length:pageCount},()=>browser.newPage()));
 for(const page of pages){page.on('request',r=>{if(new URL(r.url()).pathname.startsWith('/api/'))r._latencyStarted=Date.now();});page.on('response',r=>{if(new URL(r.url()).pathname.startsWith('/api/'))evidence.requests.push({path:new URL(r.url()).pathname,status:r.status(),elapsed_ms:Math.round(Date.now()-(r.request()._latencyStarted||Date.now()))});});page.on('console',m=>{if(m.type()==='error')evidence.console_errors.push(m.text());});page.on('pageerror',e=>evidence.page_errors.push(e.message));}
 const started=Date.now();
 await Promise.all(pages.map(p=>p.goto(`http://127.0.0.1:${port}`,{waitUntil:'domcontentloaded'})));
 const monitor=await pages[0].evaluate(async()=>{const t=performance.now();const r=await fetch('/api/monitor');const body=await r.json();return {status:r.status,elapsed_ms:Math.round(performance.now()-t),mode:body.mode,read_only:body.paper_execution_enabled===false};});
 evidence.monitor_direct=monitor;evidence.total_ms=Date.now()-started;
 evidence.monitor_poll_count=pages.length;
 assert.equal(monitor.status,200);assert.equal(monitor.mode,'READ_ONLY');assert.equal(monitor.read_only,true);
 assert(monitor.elapsed_ms<15000,`monitor API exceeded 15 seconds: ${monitor.elapsed_ms}ms`);
 assert.deepEqual(evidence.console_errors,[]);assert.deepEqual(evidence.page_errors,[]);
 console.log(JSON.stringify(evidence));
}finally{if(browser)await browser.close();child.kill('SIGINT');}}
)().catch(e=>{console.error(e);process.exitCode=1;});
