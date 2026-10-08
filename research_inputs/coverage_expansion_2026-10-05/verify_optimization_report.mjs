import { chromium } from '/home/nishiki/vnpy-ctp/frontend/node_modules/playwright/index.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const root='/home/nishiki/vnpy-ctp/research_outputs/coverage_expansion_2026-10-05';
const check=(value,message)=>{if(!value)throw new Error(message)};
const reportFile=root+'/optimization_review.html';
const reportHash=createHash('sha256').update(readFileSync(reportFile)).digest('hex');
const browser=await chromium.launch({headless:true,env:{...process.env,
 LD_LIBRARY_PATH:'/home/nishiki/vnpy-ctp/.tools/browser-libs/usr/lib/x86_64-linux-gnu',
 FONTCONFIG_FILE:'/tmp/ctp_strategy_report_20261004/fonts.conf'}});
const page=await browser.newPage({viewport:{width:1440,height:1100},deviceScaleFactor:1});
const errors=[],requests=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
await page.goto('file://'+root+'/optimization_review.html');
const D=await page.locator('#report-data').evaluate(el=>JSON.parse(el.textContent));
check(D.scenarios.length===(D.assessment.combined_eligible?18:15),'Complete portfolio experiments');
check(D.assessment.inputs.length===D.scenarios.length,'Fingerprints for every experiment');
check(await page.locator('#windows tr').count()===D.scenarios.length,'All window comparison rows');
check(D.scenarios.every(s=>s.audit_status==='passed'&&s.locked_test_read===false),'Audits and test lock');
check(await page.locator('#totals tr').count()===Object.keys(D.assessment.totals).length,'Totals rows');
const selections=[];
for(const s of D.scenarios){
 await page.selectOption('#month',s.month);
 await page.selectOption('#variant',s.variant);
 check(await page.locator('#equity-chart circle').count()===s.metrics.daily_count,'Actual daily equity observations');
 check(await page.locator('#trade-select option').count()===s.trades.length,'Actual trade choices');
 check(Number((await page.locator('#funnel td').last().innerText()).replaceAll(',',''))===s.trades.length,'Actual fills');
 for(let i=0;i<s.trades.length;i++){
  await page.selectOption('#trade-select',String(i));
  check((await page.locator('#trade-chart').innerHTML()).includes(s.trades[i].contract),'Trade protection rendering');
  check((await page.locator('#trade-note').innerText()).includes('净收益'),'Trade arithmetic annotation');
  check(await page.locator('#trade-chart .exit-fill').count()===1,'Modeled exit fill marked');
  check((await page.locator('#trade-chart .exit-fill title').textContent()).includes(String(s.trades[i].exit_time)),'Actual exit timestamp');
 }
 const pending=page.waitForEvent('download');await page.click('#export');
 const download=await pending;
 const destination=root+'/optimization_review/'+s.month+'_'+s.variant+'_browser_export.csv';
 await download.saveAs(destination);
 const lines=readFileSync(destination,'utf8').trim().split(/\r?\n/);
 check(lines.length===s.trades.length+1,'Export rows');
 check(lines[0].includes('net_pnl')&&!lines[0].includes('\\r'),'Real CSV newlines');
 for(let i=0;i<s.trades.length;i++){
  const cells=lines[i+1].split(',');
  check(cells[0]===s.trades[i].contract&&Number(cells[11])===s.trades[i].net_pnl,'Exported actual values');
 }
 selections.push({month:s.month,variant:s.variant,trades:s.trades.length,equity_points:s.metrics.daily_count,csv:true});
}
await page.selectOption('#month','2026-07');await page.selectOption('#variant','breakeven');
await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:root+'/optimization_desktop.png'});
await page.locator('#trade-chart').scrollIntoViewIfNeeded();await page.screenshot({path:root+'/optimization_trade_path.png'});
for(const width of [390,320]){
 await page.setViewportSize({width,height:844});await page.evaluate(()=>window.scrollTo(0,0));
 check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'No page overflow at '+width);
 check(await page.locator('.card').evaluateAll(nodes=>nodes.every(el=>el.scrollWidth<=el.clientWidth+1)),'No metric overflow at '+width);
 check(await page.locator('.value').evaluateAll(nodes=>nodes.every(el=>{const range=document.createRange();range.selectNodeContents(el);return range.getClientRects().length===1})),'Metric numbers stay on one line at '+width);
 await page.screenshot({path:root+'/optimization_mobile_'+width+'.png'});
 await page.locator('#trade-chart').scrollIntoViewIfNeeded();
 check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'Contained chart at '+width);
}
check(errors.length===0,'Browser errors: '+errors.join('; '));
check(requests.length===0,'Offline report');
check(createHash('sha256').update(readFileSync(reportFile)).digest('hex')===reportHash,'Report stable during browser validation');
await browser.close();
const proof={status:'passed',report_sha256:reportHash,desktop:[1440,1100],mobile:[390,320],selections,
 all_trade_paths_rendered:true,offline:true,browser_errors:errors,external_requests:requests};
writeFileSync(root+'/optimization_browser_verification.json',JSON.stringify(proof,null,2)+'\n');
console.log(JSON.stringify(proof));
