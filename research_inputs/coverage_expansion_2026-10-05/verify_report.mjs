import { chromium } from '/home/nishiki/vnpy-ctp/frontend/node_modules/playwright/index.mjs';
import { writeFileSync, readFileSync } from 'node:fs';
const root='/home/nishiki/vnpy-ctp/research_outputs/coverage_expansion_2026-10-05';
const check=(value,message)=>{if(!value)throw new Error(message)};
const browser=await chromium.launch({headless:true,env:{...process.env,LD_LIBRARY_PATH:'/home/nishiki/vnpy-ctp/.tools/browser-libs/usr/lib/x86_64-linux-gnu',FONTCONFIG_FILE:'/tmp/ctp_strategy_report_20261004/fonts.conf'}});
const page=await browser.newPage({viewport:{width:1440,height:1100},deviceScaleFactor:1});
const errors=[],remote=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(/^https?:/.test(r.url()))remote.push(r.url())});
await page.goto('file://'+root+'/strategy_coverage_review.html');
const data=await page.locator('#report-data').evaluate(el=>JSON.parse(el.textContent));
check(data.scenarios.length===6,'Six actual comparisons required');
check(await page.locator('#comparison tr').count()===6,'Six comparison rows');
check(data.scenarios.every(s=>s.status==='completed'&&s.independent_review==='passed'&&s.locked_test_read===false),'Audit and split status');
const selections=[];
for(const s of data.scenarios){
 await page.selectOption('#month',s.month);await page.click('#k'+s.k);
 check(await page.locator('#k'+s.k).getAttribute('aria-pressed')==='true','Selected K state');
 check(await page.locator('#equityChart circle').count()===s.metrics.daily_count,'Real daily equity points');
 check(await page.locator('#trade-select option').count()===s.trades.length,'Trade selector size');
 check(Number((await page.locator('#funnel td').last().innerText()).replaceAll(',',''))===s.metrics.trade_count,'Actual fill count');
 for(let i=0;i<s.trades.length;i++){
  await page.selectOption('#trade-select',String(i));
  check((await page.locator('#tradeChart').innerHTML()).includes(s.trades[i].contract),'Trade path rendering');
  check((await page.locator('#tradePathNote').innerText()).includes('净收益'),'Trade path arithmetic annotation');
 }
 const request=page.waitForEvent('download');await page.click('#export');const download=await request;
 const destination=root+'/'+s.month+'_k'+s.k+'_browser_export.csv';await download.saveAs(destination);
 const lines=readFileSync(destination,'utf8').trim().split(/\r?\n/);
 check(lines.length===s.trades.length+1,'CSV real row count');
 check(lines[0].includes('net_pnl')&&!lines[0].includes('\\r'),'CSV actual line breaks');
 selections.push({month:s.month,k:s.k,trades:s.trades.length,equity_points:s.metrics.daily_count,csv:true});
}
await page.selectOption('#month','2026-07');await page.click('#k5');await page.selectOption('#trade-select','0');
await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:root+'/report_desktop.png'});
await page.locator('#tradeChart').scrollIntoViewIfNeeded();await page.screenshot({path:root+'/report_trade_path.png'});
for(const width of [390,320]){
 await page.setViewportSize({width,height:844});await page.evaluate(()=>window.scrollTo(0,0));
 check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'Document overflow at '+width);
 check(await page.locator('.card').evaluateAll(nodes=>nodes.every(el=>el.scrollWidth<=el.clientWidth+1)),'Metric overflow at '+width);
 await page.screenshot({path:root+'/report_mobile_'+width+'.png'});
 await page.locator('#tradeChart').scrollIntoViewIfNeeded();
 check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'Trade chart overflow at '+width);
}
check(!errors.length,'Browser errors: '+errors.join(';'));check(!remote.length,'Offline report made external requests');
await browser.close();
const proof={status:'passed',desktop:[1440,1100],mobile:[390,320],all_six_selections:selections,all_trade_paths_rendered:true,offline:true,browser_errors:errors,external_requests:remote};
writeFileSync(root+'/browser_verification.json',JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
