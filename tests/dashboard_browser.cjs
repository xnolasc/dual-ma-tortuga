/* Optional browser integration QA. Uses a temporary wallet; no persistent funding. */
const {chromium}=require(process.env.DUAL_MA_PLAYWRIGHT || 'playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const child=spawn(path.join(root,'.venv/bin/python'),['-u','-c',`import tempfile
from pathlib import Path
from dual_ma_ai.dashboard import Dashboard, make_server
from dual_ma_ai.wallet import Wallet
from dual_ma_ai.market_data import strict_json
import shutil
with tempfile.TemporaryDirectory() as d:
 reports=Path(d)/'reports'
 reports.mkdir()
 for p in sorted(Path('data/dual_ma_ai/backtests').glob('*.json')):
  report=strict_json(p.read_bytes())
  if report.get('synthetic') and all(x['status']=='available' for x in report['comparisons']) and len(report['comparisons'])==4:
   shutil.copyfile(p,reports/p.name)
   break
 else:
  raise RuntimeError('Full-horizon synthetic browser fixture required')
 with make_server(Dashboard(Wallet(Path(d)/'wallet.sqlite3'), reports=reports, alpaca_cache=Path(d)/'alpaca'), 0) as s:
  print(s.server_port, flush=True)
  try: s.serve_forever()
  except KeyboardInterrupt: pass
`],{cwd:root});
(async()=>{let browser;try{const port=await new Promise((resolve,reject)=>{child.stdout.once('data',d=>resolve(Number(d.toString().trim())));child.once('exit',code=>reject(Error('Server exit '+code)));child.stderr.on('data',d=>process.stderr.write(d));});browser=await chromium.launch({...(process.env.DUAL_MA_CHROME?{executablePath:process.env.DUAL_MA_CHROME}:{}),headless:true,args:['--no-sandbox']});const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error'&&!m.text().includes('favicon'))errors.push(m.text());});await page.goto(`http://127.0.0.1:${port}`);await page.locator('#include-synthetic').check();await page.waitForFunction(()=>document.querySelector('#comparison tbody tr')&&document.querySelector('#signal-table tbody tr'));assert.equal(await page.locator('#metrics .value').first().textContent(),'$2,000.00');assert.match(await page.locator('#report-label').textContent(),/SYNTHETIC TEST DATA/);assert.equal(await page.locator('#chart path').count(),3);await page.locator('#chart-mode').selectOption('drawdown');await page.locator('#chart-window').selectOption('63');await page.locator('#chart').focus();await page.keyboard.press('ArrowRight');assert.match(await page.locator('#tooltip').textContent(),/SMA 50\/200/);await page.locator('#horizon').selectOption('3');assert.equal(await page.locator('#comparison tbody tr').count(),3);await page.locator('#amount').fill('125.00');await page.locator('#review').click();await page.locator('#confirm-dialog').waitFor({state:'visible'});assert.equal(await page.locator('#metrics .value').first().textContent(),'$2,000.00');await page.locator('#confirm').click();await page.waitForFunction(()=>document.querySelector('#funding-status').textContent.includes('recorded'));assert.equal(await page.locator('#metrics .value').nth(1).textContent(),'$2,125.00');assert.equal(await page.locator('#metrics .value').nth(3).textContent(),'$0.00');await page.locator('#operation').selectOption('withdraw');await page.locator('#amount').fill('25.00');await page.locator('#review').click();await page.locator('#confirm-dialog').waitFor({state:'visible'});await page.locator('#confirm').click();await page.waitForFunction(()=>document.querySelector('#metrics .value').textContent==='$2,100.00');await page.reload();await page.locator('#include-synthetic').check();await page.waitForFunction(()=>document.querySelector('#comparison tbody tr'));assert.equal(await page.locator('#metrics .value').first().textContent(),'$2,100.00');assert.equal(await page.locator('#history tbody tr').count(),3);await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(root,process.env.DUAL_MA_DESKTOP_SCREENSHOT || 'docs/dual-ma-ai-phase4.1-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(root,process.env.DUAL_MA_MOBILE_SCREENSHOT || 'docs/dual-ma-ai-phase4.1-mobile.png'),fullPage:true});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);assert.deepEqual(errors,[]);console.log('PASS: desktop/mobile rendering, synthetic labels, 3 strategy curves, drawdown, horizon/window selection, keyboard chart inspection, confirmed deposit/withdrawal, reload persistence, funding excluded from P&L, no JS/CSP errors. Temporary wallet only.');}finally{if(browser)await browser.close();child.kill('SIGINT');}})().catch(e=>{console.error(e);process.exitCode=1;});
