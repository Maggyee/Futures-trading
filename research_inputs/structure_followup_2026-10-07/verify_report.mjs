import { chromium } from '/home/nishiki/vnpy-ctp/frontend/node_modules/playwright/index.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';

process.umask(0o077);
const project='/home/nishiki/vnpy-ctp';
const root=project+'/research_outputs/structure_followup_2026-10-07/trade_review';
const plan=project+'/research_inputs/structure_followup_2026-10-07/plan.json';
const save=(path,value)=>{const bytes=typeof value==='string'?Buffer.from(value):value;execFileSync(project+'/.venv/bin/python',['-c','import json,sys; from pathlib import Path; from research.storage import SpaceBudget; SpaceBudget(json.loads(Path(sys.argv[1]).read_text())["budget"]).check(sys.argv[2],reserve=int(sys.argv[3]))',plan,path,String(bytes.length)],{cwd:project,stdio:'pipe'});writeFileSync(path,bytes)};
const require=(v,m)=>{if(!v)throw Error(m)};
const browser=await chromium.launch({headless:true,env:{...process.env,LD_LIBRARY_PATH:project+'/.tools/browser-libs/usr/lib/x86_64-linux-gnu',FONTCONFIG_FILE:'/tmp/ctp_strategy_report_20261004/fonts.conf'}});
const page=await browser.newPage({viewport:{width:1600,height:1250},deviceScaleFactor:1});
const errors=[],requests=[],checks=[];
page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
await page.goto('file://'+root+'/structure_followup_review.html');
await page.waitForFunction(()=>window.chartReady===true&&window.caseReady===true);
const data=await page.evaluate(()=>{const d=window.reviewData;return {selected:d.selected,totals:d.assessment.totals,checks:d.verification.checks,charts:d.charts.map(c=>({uid:c.trade.uid,variant:c.trade.variant,entry_price:c.trade.entry_price,exit_price:c.trade.exit_price,entry_time:c.trade.entry_time,exit_time:c.trade.exit_time,entry_signal_time:c.trade.entry_signal_time,paired_uid:c.trade.paired_uid,confirmed:Boolean(c.trade.entry_snapshot.price_confirmation),structural:c.trade.entry_protection.structure_anchor!==undefined,local:c.views.local.bar_count,day:c.views.day.bar_count}))}});
require(data.charts.length===Object.values(data.totals).reduce((n,t)=>n+t.trade_count,0),'Every version trade is delivered');
require(data.checks.every(c=>c.all_entry_filters&&c.raw_candles&&c.exit_rule_and_price),'All source checks passed');
for(const c of data.charts){
 await page.selectOption('#variant',c.variant);await page.selectOption('#tradeSelect',c.uid);
 await page.waitForFunction(()=>window.chartReady===true);
 for(const view of ['local','day']){
  await page.click(`[data-view="${view}"]`);await page.waitForFunction(()=>window.chartReady===true);
  const plot=await page.evaluate(()=>({selected:document.querySelector('#tradeSelect').value,candles:document.querySelector('#chart').data.find(t=>t.type==='candlestick').x.length,traces:document.querySelector('#chart').data.map(t=>({name:t.name,x:t.x,y:t.y})),higher:document.querySelector('#higher').data.filter(t=>t.type==='candlestick').map(t=>t.x.at(-1))}));
  require(plot.selected===c.uid&&plot.candles===c[view],'Correct trade and chart extent');
  for(const [name,expected] of [['模拟入场',c.entry_price],['模拟退场',c.exit_price]])require(Math.abs(plot.traces.find(t=>t.name===name).y[0]-expected)<1e-7,'Fill markers preserve prices');
  require(plot.traces.some(t=>t.name==='当时生效的保护线'),'Causal protection path visible');
  require(plot.higher.every(t=>t<=c.entry_signal_time.slice(0,19)),'Only completed higher candles are shown');
  if(c.confirmed)require(plot.traces.some(t=>t.name==='已完成的形态建立K线')&&plot.traces.some(t=>t.name==='5分钟方向效率（资格门槛）'),'Explicit confirmation visible');
  if(c.structural)require(plot.traces.some(t=>t.name==='信号时冻结的5分钟结构边界'),'Frozen structure visible');
  checks.push({uid:c.uid,view,passed:true});
 }
}
const rejected=await page.evaluate(()=>window.reviewData.unfilled_confirmations.map(c=>({uid:c.uid,filled:c.filled})));
for(const c of rejected){require(c.filled===false,'Rejected confirmation is not counted as a fill');await page.selectOption('#caseSelect',c.uid);await page.waitForFunction(()=>window.caseReady===true);const traces=await page.evaluate(()=>document.querySelector('#caseChart').data.map(t=>t.name));require(traces.includes('已完成的价格延续确认（未成交）')&&!traces.includes('模拟入场'),'Unfilled confirmation is visibly distinguished from executed trades')}
const paired=data.charts.find(c=>c.paired_uid);
if(paired){await page.selectOption('#variant',paired.variant);await page.selectOption('#tradeSelect',paired.uid);await page.waitForFunction(()=>window.chartReady===true);await page.click('#pair');await page.waitForFunction(()=>window.chartReady===true);require(await page.inputValue('#tradeSelect')===paired.paired_uid,'Baseline pair navigation works')}
await page.selectOption('#variant',data.selected);await page.waitForFunction(()=>window.chartReady===true);
await page.evaluate(()=>window.scrollTo(0,0));save(root+'/report_overview.png',await page.screenshot());
const inspected=data.charts.find(c=>c.confirmed)||data.charts.find(c=>c.structural)||data.charts[0];
await page.selectOption('#variant',inspected.variant);await page.selectOption('#tradeSelect',inspected.uid);await page.click('[data-view="local"]');await page.waitForFunction(()=>window.chartReady===true);await page.locator('#tradeTitle').scrollIntoViewIfNeeded();save(root+'/trade_chart.png',await page.screenshot());
await page.setViewportSize({width:390,height:950});await page.evaluate(()=>window.scrollTo(0,0));
await page.waitForFunction(()=>document.body.scrollWidth<=window.innerWidth+1,undefined,{timeout:10000});
require(await page.evaluate(()=>document.body.scrollWidth<=window.innerWidth+1),'Mobile page stays inside viewport after chart resize');
save(root+'/report_mobile.png',await page.screenshot());
await page.reload();await page.waitForFunction(()=>window.chartReady===true&&window.caseReady===true);
require(await page.evaluate(()=>document.body.scrollWidth<=window.innerWidth+1),'Mobile initial load stays inside viewport');
require(errors.length===0,'No browser errors: '+errors.join('; '));require(requests.length===0,'Report works offline');await browser.close();
const sha=p=>createHash('sha256').update(readFileSync(p)).digest('hex');
save(root+'/delivery_verification.json',JSON.stringify({status:'passed',chart_checks:checks,trade_count:data.charts.length,selected:data.selected,browser_errors:errors,network_requests:requests,mobile_layout:true,mobile_initial_load:true,unfilled_cases_checked:rejected.length,files:Object.fromEntries(['structure_followup_review.html','review_data.json.gz','verification.json','stage_evidence.json','report_sources.json','trade_review.csv','report_overview.png','trade_chart.png','report_mobile.png'].map(n=>[n,{sha256:sha(root+'/'+n)}]))},null,2));
console.log(JSON.stringify({status:'passed',trades:data.charts.length,charts_checked:checks.length,network_requests:requests.length}));
