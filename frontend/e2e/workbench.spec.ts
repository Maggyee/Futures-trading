import { test, expect } from "@playwright/test";

test("login, empty trading desk, strategy publication, CSV import and missing-data backtest", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await page
    .getByLabel("密码", { exact: true })
    .fill("browser-test-password-123");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("heading", { name: "交易工作台" })).toBeVisible();
  await expect(page.getByText("暂无持仓")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "买入开多", exact: true }).last(),
  ).toBeDisabled();
  await page.screenshot({ path: "test-results/workbench.png", fullPage: true });
  await page.getByRole("button", { name: "策略", exact: true }).click();
  await expect(page.locator(".monaco-editor").first()).toBeVisible();
  await page.getByRole("button", { name: "检查语法" }).click();
  await expect(page.getByText("Python 语法检查通过")).toBeVisible();
  await page.getByRole("button", { name: "保存版本" }).click();
  await expect(page.getByText("新版本已保存")).toBeVisible();
  await page.getByRole("button", { name: "发布版本", exact: true }).click();
  await page.getByRole("button", { name: "发布", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "创建实例", exact: true }),
  ).toBeEnabled();
  await page.screenshot({ path: "test-results/strategy.png", fullPage: true });
  await page.getByRole("button", { name: "数据", exact: true }).click();
  await page.locator("input[type=file]").setInputFiles({
    name: "test.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "symbol,exchange,datetime,open,high,low,close,volume\nrb2610,SHFE,2026-09-28T09:00:00,3500,3505,3495,3501,10\n",
    ),
  });
  await expect(page.getByText("已导入 1 根 K 线")).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "rb2610", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "rb2610", exact: true }) })
    .getByRole("button", { name: "查看K线" })
    .click();
  await expect(page.getByText("1 根 · 已完成 K 线")).toBeVisible();
  await expect
    .poll(() =>
      page
        .locator(".chart-canvas")
        .evaluate(
          (el) =>
            [...el.querySelectorAll("canvas")].filter((c) => c.height > 40)
              .length,
        ),
    )
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: "分时", exact: true }).click();
  await page.getByLabel("分时日期").fill("2026-09-28");
  await expect(page.getByText("1 个分钟点 · 每 2 秒更新")).toBeVisible();
  await expect(page.getByText(/含分钟收盘价加权估算/)).toBeVisible();
  await page.getByLabel("分时日期").fill("2026-09-27");
  await expect(page.getByText(/当日暂无分时数据/)).toBeVisible();
  await page.getByRole("button", { name: "1m", exact: true }).click();
  await expect(page.getByText("1 根 · 已完成 K 线")).toBeVisible();
  await page.getByRole("button", { name: "回测", exact: true }).click();
  await page.getByLabel("策略版本").click();
  await page.locator(".ant-select-item-option").first().click();
  await page.locator("#symbol").fill("rb2610.SHFE");
  await page.getByLabel("开始日期").fill("2026-09-28");
  await page.getByLabel("结束日期").fill("2026-09-29");
  await page.getByRole("button", { name: "开始回测" }).click();
  await expect(page.getByText("回测任务已提交")).toBeVisible();
  await expect(
    page.getByText(
      "回测开始日期之前的 30 天数据不足以预热策略，请补充历史数据或后移开始日期",
      { exact: true },
    ),
  ).toBeVisible({ timeout: 20000 });
  expect(errors).toEqual([]);
});

test("chart indicators, stale quote limits, close intent and narrow viewport with explicitly synthetic API fixtures", async ({
  page,
}) => {
  // Fixtures only in this test; the product never inserts synthetic market/account data.
  const bars = Array.from({ length: 120 }, (_, i) => ({
    time: 1790643600 + i * 60,
    open: 3500 + Math.sin(i / 5) * 30,
    high: 3540,
    low: 3460,
    close: 3500 + Math.sin((i + 1) / 5) * 30,
    volume: 100 + i,
    open_interest: 10000 + i,
    complete: true,
  }));
  const snapshot = {
    environment: "SimNow",
    ready: true,
    connected: true,
    md: true,
    td: true,
    accounts: [{ balance: 20999983.76, available: 20999983.76, frozen: 10000 }],
    contracts: [
      {
        vt_symbol: "rb2610.SHFE",
        symbol: "rb2610",
        exchange: "SHFE",
        name: "测试合约",
        size: 10,
        pricetick: 1,
      },
    ],
    ticks: [
      {
        vt_symbol: "rb2610.SHFE",
        symbol: "rb2610",
        name: "测试合约",
        exchange: "SHFE",
        last_price: 3500,
        pre_close: 3490,
        ask_price_1: 3501,
        bid_price_1: 3499,
        volume: 1234,
        open_interest: 10000,
      },
    ],
    positions: [
      {
        vt_positionid: "CTP.rb2610.SHFE.LONG",
        vt_symbol: "rb2610.SHFE",
        direction: "LONG",
        volume: 5,
        yd_volume: 3,
        frozen: 0,
        price: 3490,
        pnl: 500,
      },
    ],
    orders: [],
    trades: [],
    strategies: [],
    logs: [],
    operations: [],
    tick_received_at: { "rb2610.SHFE": Date.now() / 1000 },
  };
  let closeBody: any;
  let openBody: any;
  await page.route("**/api/v1/session", (r) =>
    r.fulfill({ json: { csrf: "fixture" } }),
  );
  await page.route("**/api/v1/snapshot", (r) => r.fulfill({ json: snapshot }));
  await page.route("**/api/v1/bars/**", (r) =>
    r.fulfill({
      json: {
        bars,
        indicators: {
          MA: bars.map((b) => b.close - 4),
          BOLL_UP: bars.map(() => 3540),
          BOLL_MID: bars.map(() => 3500),
          BOLL_LOW: bars.map(() => 3460),
          MACD: bars.map((_, i) => Math.sin(i / 6)),
          SIGNAL: bars.map(() => 0),
          HIST: bars.map((_, i) => Math.sin(i / 6) / 2),
        },
      },
    }),
  );
  await page.route("**/api/v1/intraday/**", (r) =>
    r.fulfill({
      json: {
        date: "2026-09-29",
        estimated: false,
        bars: bars.map((b, i) => ({ ...b, average: 3500 + i / 10 })),
      },
    }),
  );
  await page.route("**/api/v1/close", (r) => {
    closeBody = r.request().postDataJSON();
    return r.fulfill({ json: { id: "fixture", state: "waiting_cancel" } });
  });
  await page.route("**/api/v1/orders", (r) => {
    openBody = r.request().postDataJSON();
    return r.fulfill({
      json: { id: "fixture", state: "submitted", orders: [] },
    });
  });
  await page.route("**/api/v1/commands/fixture", (r) =>
    r.fulfill({ json: { id: "fixture", state: "completed", filled: 2 } }),
  );
  await page.clock.install();
  await page.goto("/");
  await page
    .getByRole("button", { name: "查看行情 rb2610.SHFE", exact: true })
    .click();
  await expect(page.getByText("120 根", { exact: false })).toBeVisible();
  await expect
    .poll(() =>
      page.locator(".chart-canvas").evaluate((el) => {
        let colored = 0;
        for (const canvas of el.querySelectorAll("canvas")) {
          if (!canvas.width || !canvas.height) continue;
          const pixels = canvas
            .getContext("2d")!
            .getImageData(0, 0, canvas.width, canvas.height).data;
          for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i] > 150 && pixels[i] > pixels[i + 1] * 1.2) colored++;
          }
        }
        return colored;
      }),
    )
    .toBeGreaterThan(100);
  await expect(page.getByTestId("open-counterparty")).toContainText(
    "对手价（卖一）3,501",
  );
  await page
    .getByRole("button", { name: "卖出开空", exact: true })
    .first()
    .click();
  await expect(page.getByTestId("open-counterparty")).toContainText(
    "对手价（买一）3,499",
  );
  await page.getByRole("button", { name: "分时", exact: true }).click();
  await expect(page.getByText("120 个分钟点 · 每 2 秒更新")).toBeVisible();
  await page.screenshot({ path: "test-results/intraday.png", fullPage: true });
  await page.getByRole("button", { name: "平仓", exact: true }).click();
  await expect(page.getByTestId("close-counterparty")).toContainText(
    "对手价（买一）3,499",
  );
  await page.getByRole("dialog").getByRole("spinbutton").first().fill("2");
  await page.getByRole("button", { name: "确认停止策略并平仓" }).click();
  await expect
    .poll(() => closeBody)
    .toEqual({
      symbol: "rb2610.SHFE",
      direction: "LONG",
      volume: 2,
      price: null,
    });
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.clock.fastForward(31_000);
  await expect(page.getByTestId("open-quote-status")).toContainText(
    "行情未更新",
  );
  await expect(page.getByTestId("open-counterparty")).toContainText(
    "请填写限价",
  );
  const sell = page
    .getByRole("button", { name: "卖出开空", exact: true })
    .last();
  await expect(sell).toBeDisabled();
  await page
    .locator(".order-form .ant-input-number-input")
    .first()
    .fill("3500");
  await expect(sell).toBeEnabled();
  await sell.click();
  await expect(page.getByRole("dialog")).toContainText("行情未更新");
  await page.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect
    .poll(() => openBody)
    .toEqual({
      symbol: "rb2610.SHFE",
      direction: "SHORT",
      volume: 1,
      price: 3500,
    });
  await expect(page.getByRole("dialog")).toBeHidden();
  closeBody = null;
  await page.getByRole("button", { name: "平仓", exact: true }).click();
  const confirmClose = page.getByRole("button", { name: "确认停止策略并平仓" });
  await expect(confirmClose).toBeDisabled();
  await expect(page.getByTestId("close-quote-status")).toContainText(
    "行情未更新",
  );
  await page.getByRole("dialog").getByRole("spinbutton").first().fill("2");
  await page.getByRole("dialog").getByRole("spinbutton").last().fill("3500");
  await expect(confirmClose).toBeEnabled();
  await confirmClose.click();
  await expect
    .poll(() => closeBody)
    .toEqual({
      symbol: "rb2610.SHFE",
      direction: "LONG",
      volume: 2,
      price: 3500,
    });
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.locator(".order-form .ant-input-number-input").first().fill("");
  snapshot.ticks[0].ask_price_1 = 0;
  snapshot.ticks[0].bid_price_1 = 0;
  await expect(page.getByTestId("open-counterparty")).toContainText(
    "暂无对手报价",
    { timeout: 10000 },
  );
  await expect(
    page.getByRole("button", { name: "卖出开空", exact: true }).last(),
  ).toBeDisabled();
  await page
    .locator(".order-form .ant-input-number-input")
    .first()
    .fill("3500");
  await expect(page.getByTestId("open-counterparty")).toContainText(
    "手动限价3,500",
  );
  await expect(
    page.getByRole("button", { name: "卖出开空", exact: true }).last(),
  ).toBeEnabled();
  await page.screenshot({
    path: "test-results/chart-fixture.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".order-panel")).toBeVisible();
  await expect(sell).toBeEnabled();
  await page.screenshot({ path: "test-results/mobile.png", fullPage: true });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > innerWidth + 2,
  );
  expect(overflow).toBe(false);
});
