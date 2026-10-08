import { chromium } from '/home/nishiki/vnpy-ctp/frontend/node_modules/playwright/index.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const root = '/home/nishiki/vnpy-ctp/research_outputs/coverage_expansion_2026-10-05/latest_trade_review';
const require = (ok, message) => { if (!ok) throw Error(message); };
const browser = await chromium.launch({ headless: true, env: { ...process.env,
  LD_LIBRARY_PATH: '/home/nishiki/vnpy-ctp/.tools/browser-libs/usr/lib/x86_64-linux-gnu',
  FONTCONFIG_FILE: '/tmp/ctp_strategy_report_20261004/fonts.conf' } });
const page = await browser.newPage({ viewport: { width: 1600, height: 1250 }, deviceScaleFactor: 1 });
const errors = [], requests = [], checks = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => { if (/^https?:/.test(r.url())) requests.push(r.url()); });
await page.goto('file://' + root + '/latest_entry_exit_review.html');
await page.waitForFunction(() => window.chartReady === true);
const data = await page.evaluate(() => window.reviewData);
require(data.charts.length === 20, '20 version trades');
require(await page.locator('#trade option').count() === 8, 'Latest 8 trades default');
await page.screenshot({ path: root + '/report_overview.png' });
for (const c of data.charts) {
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
  await page.locator('#higher-details').evaluate(e => e.open = true);
  await page.waitForFunction(() => document.getElementById('higher').data?.length === 10);
  const latestTimes = await page.evaluate(() => document.getElementById('higher').data.filter(t => t.type === 'candlestick').map(t => t.x[t.x.length - 1]));
  require(latestTimes.every(t => t <= c.trade.entry_signal_time.slice(0, 19)), 'Higher timeframe avoids unfinished candle');
  await page.locator('#higher-details').evaluate(e => e.open = false);
}
await page.selectOption('#variant', 'combined');
await page.selectOption('#month', '2026-08');
await page.waitForFunction(() => window.chartReady && window.currentUid === null);
require(await page.locator('#empty').isVisible(), 'Empty August explicitly shown');
await page.selectOption('#month', 'all');
await page.waitForFunction(() => window.chartReady && window.currentUid !== null);
const downloadPromise = page.waitForEvent('download');
await page.click('#export');
const download = await downloadPromise;
await download.saveAs(root + '/latest_8_trades_export.csv');
const lines = readFileSync(root + '/latest_8_trades_export.csv', 'utf8').trim().split(/\r?\n/);
require(lines.length === 9, 'CSV contains eight actual trades');
require(lines[0].includes('net_pnl') && !lines[0].includes('\\r'), 'Real CSV newlines');
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  const overflow = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: innerWidth,
    cards: [...document.querySelectorAll('.card')].map(e => [e.id, e.scrollWidth, e.clientWidth]) }));
  require(overflow.page <= overflow.viewport, 'No page overflow at ' + width + ': ' + JSON.stringify(overflow));
  require(overflow.cards.every(c => c[1] <= c[2] + 1), 'Contained cards at ' + width + ': ' + JSON.stringify(overflow));
  await page.screenshot({ path: root + '/report_mobile_' + width + '.png' });
}
await page.setViewportSize({ width: 1600, height: 1250 });
await page.goto('file://' + root + '/latest_entry_exit_review.html?preview=1#combined-2026-07-1');
await page.waitForFunction(() => window.chartReady);
for (const c of data.charts.filter(c => c.trade.variant === 'combined')) {
  await page.evaluate(uid => window.showTrade(uid), c.trade.uid);
  await page.waitForFunction(uid => window.chartReady && window.currentUid === uid, c.trade.uid);
  await page.locator('#charts').screenshot({ path: root + '/' + c.trade.uid + '.png' });
}
await page.evaluate(() => location.hash = 'combined-2026-09-1');
await page.waitForFunction(() => window.chartReady && window.currentUid === 'combined-2026-09-1');
await page.locator('#chart').screenshot({ path: root + '/entry_exit_example.png' });
require(errors.length === 0, 'Browser errors: ' + errors.join(';'));
require(requests.length === 0, 'Offline report has no network requests');
await browser.close();
const proof = { status: 'passed', html_sha256: createHash('sha256').update(readFileSync(root + '/latest_entry_exit_review.html')).digest('hex'),
  checked_views: checks, higher_timeframes: 20, screenshot_trades: 8, mobile_widths: [390, 320], empty_august: true,
  latest_csv_rows: lines.length - 1, offline: true, browser_errors: errors };
writeFileSync(root + '/browser_verification.json', JSON.stringify(proof, null, 2) + '\n');
console.log(JSON.stringify({ status: proof.status, chart_views: checks.length, higher_timeframes: 20, screenshots: 8, offline: true }));
