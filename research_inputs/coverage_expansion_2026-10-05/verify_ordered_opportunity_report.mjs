import { chromium } from '/home/nishiki/vnpy-ctp/frontend/node_modules/playwright/index.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';

process.umask(0o077);
const root = '/home/nishiki/vnpy-ctp/research_outputs/coverage_expansion_2026-10-05/ordered_opportunity/trade_review';
const project = '/home/nishiki/vnpy-ctp';
const plan = project + '/research_inputs/coverage_expansion_2026-10-05/ordered_opportunity_plan.json';
const checkBudget = (path, reserve = 0) => execFileSync(project + '/.venv/bin/python', ['-c',
  'import sys,json; from pathlib import Path; from research.storage import SpaceBudget; SpaceBudget(json.loads(Path(sys.argv[1]).read_text())["budget"]).check(sys.argv[2],reserve=int(sys.argv[3]))',
  plan, path, String(reserve)], { cwd: project, stdio: 'pipe' });
const saveBytes = (path, value) => {
  const bytes = typeof value === 'string' ? Buffer.from(value, 'utf8') : value;
  checkBudget(path, bytes.length);
  writeFileSync(path, bytes);
};
const saveScreenshot = async (view, path) => saveBytes(path, await view.screenshot());
const require = (ok, message) => { if (!ok) throw Error(message); };
const browser = await chromium.launch({ headless: true, env: { ...process.env,
  LD_LIBRARY_PATH: '/home/nishiki/vnpy-ctp/.tools/browser-libs/usr/lib/x86_64-linux-gnu',
  FONTCONFIG_FILE: '/tmp/ctp_strategy_report_20261004/fonts.conf' } });
const page = await browser.newPage({ viewport: { width: 1600, height: 1250 }, deviceScaleFactor: 1 });
const errors = [], requests = [], checks = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => { if (/^https?:/.test(r.url())) requests.push(r.url()); });
await page.goto('file://' + root + '/ordered_opportunity_review.html');
await page.waitForFunction(() => window.chartReady === true);
// Keep the audit process small; full candles and indicators remain in the report.
const data = await page.evaluate(() => {
  const d = window.reviewData;
  return {
    selected: d.selected,
    verification: { checks: d.verification.checks.map(c => ({
      all_entry_filters: c.all_entry_filters, raw_candles: c.raw_candles,
      exit_rule_and_price: c.exit_rule_and_price,
    })) },
    assessment: { totals: Object.fromEntries(Object.entries(d.assessment.totals)
      .map(([v, t]) => [v, { trade_count: t.trade_count }])) },
    charts: d.charts.map(c => ({
      trade: Object.fromEntries(['uid', 'variant', 'contract', 'entry_price', 'exit_price',
        'entry_time', 'exit_time', 'entry_signal_time', 'blocked_reentry_uid', 'paired_uid',
        'pullback'].map(k => [k, c.trade[k]])),
      strategy: Object.fromEntries(['trend_entry', 'efficiency_min', 'breakeven',
        'ma40_exit_confirmation_bars', 'enable_oi_filter'].map(k => [k, c.strategy[k]])),
      views: Object.fromEntries(Object.entries(c.views).map(([k, v]) => [k, { bar_count: v.bar_count }])),
      path: { exit_kind: c.path.exit_kind },
      higher_latest_times: c.higher_figure.data.filter(t => t.type === 'candlestick')
        .map(t => t.x[t.x.length - 1]),
    })),
  };
});
console.log(JSON.stringify({ stage: 'loaded', trades: data.charts.length }));
require(data.verification.checks.every(c=>c.all_entry_filters&&c.raw_candles&&c.exit_rule_and_price), 'All source checks passed');
require(data.charts.length === Object.values(data.assessment.totals).reduce((n,t)=>n+t.trade_count,0), 'All audited version trades');
require(await page.locator('#trade option').count() === data.assessment.totals[data.selected].trade_count, 'Selected candidate is the default');
await saveScreenshot(page, root + '/report_overview.png');
for (const c of data.charts) {
  console.log(JSON.stringify({ stage: 'checking', uid: c.trade.uid }));
  await page.evaluate(uid => window.showTrade(uid), c.trade.uid);
  await page.waitForFunction(uid => window.chartReady && window.currentUid === uid, c.trade.uid);
  for (const scope of ['local', 'day']) {
    await page.selectOption('#scope', scope);
    await page.waitForFunction(() => window.chartReady);
    const plotted = await page.evaluate(() => {
      const p = document.getElementById('chart');
      return { traces: p.data.map(t => ({ name: t.name, x: t.x, y: t.y, type: t.type })), xRange: p._fullLayout.xaxis.range };
    });
    const candle = plotted.traces.find(t => t.type === 'candlestick');
    require(candle.x.length === c.views[scope].bar_count, 'Real chart candles: ' + c.trade.uid);
    for (const [name, field] of [['模拟入场', 'entry'], ['模拟退场', 'exit']]) {
      const t = plotted.traces.find(t => t.name === name);
      require(t && t.y.length === 1 && t.y[0] === c.trade[field + '_price'], 'Fill marker: ' + name);
      require(t.x[0] === c.trade[field + '_time'].slice(0, 19), 'Shanghai wall time: ' + name);
    }
    require(plotted.traces.some(t => t.name === '当时生效的保护线'), 'Actual protection line');
    require(!plotted.traces.some(t => t.name === '固定止盈参考'), 'Target must be activation, not exit');
    checks.push({ uid: c.trade.uid, scope, candles: candle.x.length, markers: true, protection: true });
  }
  const entryText=await page.locator('#entry-checks').textContent();
  const trend = !!c.strategy.trend_entry;
  require(entryText.includes(String(trend ? c.strategy.trend_entry.efficiency_min : c.strategy.efficiency_min)), 'Displayed actual efficiency threshold');
  const expectedR = c.strategy.breakeven.activation_r;
  const traces = await page.evaluate(() => document.getElementById('chart').data.map(t=>({name:t.name,x:t.x,y:t.y,customdata:t.customdata})));
  require(traces.some(t=>t.name===expectedR+'R保本启动价'), 'Actual break-even activation rule');
  const entryExplanation = await page.locator('#entry-text').textContent();
  if (trend) {
    require(entryText.includes('1分钟斜率（仅展示）') && entryText.includes('5分钟效率'), 'Higher quality and removed low-period gate correctly labeled');
    require(entryExplanation.includes('已完成MA10回踩') && entryExplanation.includes('有效至'), 'Qualified pullback window explained');
    const touch = traces.find(t=>t.name==='已完成MA10回踩');
    require(touch && touch.x[0]===c.trade.pullback.event.slice(0,19), 'Actual completed pullback marker');
    const quality = traces.find(t=>t.name==='5分钟方向效率（入场门槛）');
    require(quality && quality.x.every((t,i)=>quality.customdata[i]===null || quality.customdata[i].slice(0,19)<=t), 'Five-minute quality uses only completed bars');
  } else require(entryExplanation.includes('首次通过'), 'Direct first-passing trigger explained');
  const exitText = await page.locator('#exit-text').textContent();
  if(c.path.exit_kind==='opening_gap')require(exitText.includes('实际开盘行情价') && exitText.includes('已越过'), 'Opening-gap protection uses actual open and no fictitious completed-bar signal');
  require(exitText.includes('保本启动为'+expectedR+'R') && exitText.includes('MA40采用'+(c.strategy.ma40_exit_confirmation_bars||1)+'根'), 'Actual exit confirmation and activation displayed');
  require(!entryText.includes('undefined'), 'All thresholds defined');
  if (!c.strategy.enable_oi_filter) require(entryText.includes('本实验停用此过滤'), 'OI ablation identified');
  await page.locator('#higher-details').evaluate(e => e.open = true);
  const expectedHigher = c.higher_latest_times;
  await page.waitForFunction(expected => {
    const traces = document.getElementById('higher').data;
    return traces?.length === 10 && JSON.stringify(traces.filter(t => t.type === 'candlestick').map(t => t.x[t.x.length - 1])) === JSON.stringify(expected);
  }, expectedHigher);
  const latestTimes = await page.evaluate(() => document.getElementById('higher').data.filter(t => t.type === 'candlestick').map(t => t.x[t.x.length - 1]));
  require(latestTimes.every(t => t <= c.trade.entry_signal_time.slice(0, 19)), 'Higher timeframe avoids unfinished candle');
  await page.locator('#higher-details').evaluate(e => e.open = false);
  if (c.trade.blocked_reentry_uid) {
    await page.click('#blocked');
    await page.waitForFunction(uid => window.chartReady && window.currentUid === uid, c.trade.blocked_reentry_uid);
    await page.evaluate(uid => window.showTrade(uid), c.trade.uid);
  }
  if (c.trade.paired_uid) {
    await page.click('#paired');
    await page.waitForFunction(uid => window.chartReady && window.currentUid === uid, c.trade.paired_uid);
  }
  console.log(JSON.stringify({ stage: 'checked', uid: c.trade.uid }));
}
await page.selectOption('#variant', 'control');
await page.selectOption('#month', '2026-08');
await page.waitForFunction(() => window.chartReady && window.currentUid === null);
require(await page.locator('#empty').isVisible(), 'Empty August explicitly shown');
await page.selectOption('#variant', data.selected);
await page.selectOption('#month', 'all');
await page.waitForFunction(() => window.chartReady && window.currentUid !== null);
const downloadPromise = page.waitForEvent('download');
await page.click('#export');
const download = await downloadPromise;
checkBudget(root + '/selected_trades_export.csv', 64 * 1024);
await download.saveAs(root + '/selected_trades_export.csv');
checkBudget(root + '/selected_trades_export.csv');
const lines = readFileSync(root + '/selected_trades_export.csv', 'utf8').trim().split(/\r?\n/);
require(lines.length === data.assessment.totals[data.selected].trade_count + 1, 'CSV contains selected candidate actual trades');
require(lines[0].includes('net_pnl') && !lines[0].includes('\\r'), 'Real CSV newlines');
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  const overflow = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: innerWidth,
    cards: [...document.querySelectorAll('.card')].map(e => [e.id, e.scrollWidth, e.clientWidth]) }));
  require(overflow.page <= overflow.viewport, 'No page overflow at ' + width + ': ' + JSON.stringify(overflow));
  require(overflow.cards.every(c => c[1] <= c[2] + 1), 'Contained cards at ' + width + ': ' + JSON.stringify(overflow));
  await saveScreenshot(page, root + '/report_mobile_' + width + '.png');
}
await page.setViewportSize({ width: 1600, height: 1250 });
await page.goto('file://' + root + '/ordered_opportunity_review.html?preview=1');
await page.waitForFunction(() => window.chartReady);
for (const c of data.charts) {
  await page.evaluate(uid => window.showTrade(uid), c.trade.uid);
  await page.waitForFunction(uid => window.chartReady && window.currentUid === uid, c.trade.uid);
  await saveScreenshot(page.locator('#charts'), root + '/' + c.trade.uid + '.png');
  console.log(JSON.stringify({ stage: 'screenshot', uid: c.trade.uid }));
}
const example=data.charts.find(c=>c.trade.variant===data.selected&&c.trade.contract==='lc2609.GFEX'&&c.trade.entry_time.startsWith('2026-07-27'))||data.charts[0];
await page.evaluate(uid => location.hash = uid, example.trade.uid);
await page.waitForFunction(uid => window.chartReady && window.currentUid === uid, example.trade.uid);
await saveScreenshot(page.locator('#chart'), root + '/entry_exit_example.png');
const examples=[['dual_pullback_example.png',c=>!!c.strategy.trend_entry],['dual_breakout_example.png',c=>c.trade.variant==='dual'&&!c.strategy.trend_entry]];
for(const [filename,predicate] of examples){const example=data.charts.find(predicate);if(!example)continue;await page.evaluate(uid=>window.showTrade(uid),example.trade.uid);await page.waitForFunction(uid=>window.chartReady&&window.currentUid===uid,example.trade.uid);await saveScreenshot(page.locator('#charts'),root+'/'+filename);}
require(errors.length === 0, 'Browser errors: ' + errors.join(';'));
require(requests.length === 0, 'Offline report has no network requests');
await browser.close();
const proof = { status: 'passed', html_sha256: createHash('sha256').update(readFileSync(root + '/ordered_opportunity_review.html')).digest('hex'),
  checked_views: checks, higher_timeframes: data.charts.length, screenshot_trades: data.charts.length, mobile_widths: [390, 320], empty_august: true,
  latest_csv_rows: lines.length - 1, offline: true, browser_errors: errors };
saveBytes(root + '/browser_verification.json', JSON.stringify(proof, null, 2) + '\n');
console.log(JSON.stringify({ status: proof.status, chart_views: checks.length, higher_timeframes: data.charts.length, screenshots: data.charts.length, offline: true }));
