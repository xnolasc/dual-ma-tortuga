/* Phase 5B.1 browser regression against an isolated temporary alert DB/wallet. */
const {spawn}=require('node:child_process');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const child=spawn(path.join(root,'.venv/bin/python'),['-u',path.join(root,'tests/alert_dashboard_fixture.py')],{cwd:root,stdio:['ignore','pipe','pipe']});
let stderr='';child.stderr.on('data',chunk=>{stderr+=chunk.toString();});
const playwrightName=process.env.DUAL_MA_PLAYWRIGHT||'playwright';
const {chromium}=require(playwrightName);
(async()=>{
 let browser;
 try{
  const port=await new Promise((resolve,reject)=>{
   let output='';child.stdout.on('data',chunk=>{output+=chunk.toString();const match=output.match(/(?:^|\n)(\d+)(?:\n|$)/);if(match)resolve(Number(match[1]));});
   child.once('exit',code=>reject(Error(`Temporary alert server exited (${code}): ${stderr}`)));
  });
  browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[],failed=[],responses=[];let monitorGets=0,alertGets=0,navigations=0;
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error'&&!message.text().includes('favicon'))errors.push(message.text());});
  page.on('request',request=>{if(request.url().endsWith('/api/monitor'))monitorGets++;if(request.url().includes('/api/alerts'))alertGets++;});
  page.on('requestfailed',request=>failed.push({url:request.url(),error:request.failure()?.errorText}));
  page.on('response',response=>{if(response.url().includes('/api/alerts'))responses.push({status:response.status(),url:response.url()});});
  page.on('framenavigated',frame=>{if(frame===page.mainFrame())navigations++;});
  await page.goto(`http://127.0.0.1:${port}`);
  try{await page.waitForFunction(()=>[...document.querySelectorAll('#alerts-table tbody tr')].some(row=>row.textContent.includes('MU')));}
  catch(error){console.error(JSON.stringify({alert_text:await page.locator('#alert-status').textContent().catch(()=>''),table_html:await page.locator('#alerts-table').innerHTML().catch(()=>''),responses,errors,failed,server_stderr:stderr}));throw error;}
  const response=await page.request.get(`http://127.0.0.1:${port}/api/session`);const session=await response.json();
  assert.equal(session.read_only,true);assert.equal(await page.locator('#review').isDisabled(),true);
  await page.locator('#alert-state').selectOption('ENTRY_REVIEW');
  await page.waitForFunction(()=>document.querySelectorAll('#alerts-table tbody tr').length===1);
  const row=page.locator('#alerts-table tbody tr').filter({hasText:'MU'});assert.match(await row.textContent(),/ENTRY_REVIEW/);
  const before=await (await page.request.get(`http://127.0.0.1:${port}/api/wallet`)).json();
  await row.getByRole('button',{name:'Continue review'}).click();
  await page.locator('#alert-state').selectOption('');
  await page.waitForFunction(()=>[...document.querySelectorAll('#alerts-table tbody tr')].some(r=>r.textContent.includes('HOLD_REVIEW')));
  assert.match(await page.locator('#alert-status').textContent(),/local alert record/);
  await page.waitForTimeout(10500);
  assert.ok(monitorGets>=2,`Expected monitor polling to continue; got ${monitorGets}`);
  assert.ok(alertGets>=2,`Expected alert polling to continue; got ${alertGets}`);
  assert.equal(navigations,1,'Polling must not reload or navigate the page');
  const after=await (await page.request.get(`http://127.0.0.1:${port}/api/wallet`)).json();
  assert.equal(after.cash,before.cash);assert.equal(after.equity,before.equity);assert.equal(after.events.length,before.events.length);
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);
  console.log(`PASS Phase 5B.1 isolated browser alerts; monitor polls=${monitorGets}, alert polls=${alertGets}, navigations=${navigations}, no JS/network errors; advisory HOLD_REVIEW recorded; wallet unchanged.`);
 }finally{
  if(browser)await browser.close();
  if(child.exitCode===null){child.kill('SIGTERM');await new Promise(resolve=>child.once('exit',resolve));}
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
