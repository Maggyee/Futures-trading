import { test, expect } from "@playwright/test";

test.use({ actionTimeout: 15000 });

test("full workflow with isolated paper gateway: trades, markers, strategy lifecycle and successful backtest", async ({
  page,
}) => {
  test.setTimeout(180000);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page
    .getByLabel("密码", { exact: true })
    .fill("browser-test-password-123");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await page.getByRole("button", { name: /连接 SimNow/ }).click();
  await expect(page.getByText("交易已就绪", { exact: false })).toBeVisible({
    timeout: 20000,
  });
  await page.locator(".watchlist .ant-select").click();
  await page
    .locator(".ant-select-item-option")
    .filter({ hasText: "rbTEST.SHFE" })
    .click();
  await page.getByRole("button", { name: "1m", exact: true }).click();
  await expect(page.getByTestId("open-counterparty")).toContainText("3,501");
  await page
    .getByRole("button", { name: "买入开多", exact: true })
    .last()
    .click();
  await page.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "平仓", exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "平仓", exact: true }).click();
  await expect(page.getByTestId("close-counterparty")).toContainText("3,499");
  await page.getByRole("button", { name: "确认停止策略并平仓" }).click();
  await expect(page.getByText("暂无持仓")).toBeVisible({ timeout: 20000 });
  await expect(page.locator(".chart-wrap")).toHaveAttribute(
    "data-marker-count",
    "2",
    { timeout: 15000 },
  );
  await page.getByText("成交点", { exact: true }).click();
  await expect(page.locator(".chart-wrap")).toHaveAttribute(
    "data-marker-count",
    "0",
  );
  await page.getByText("成交点", { exact: true }).click();
  await expect(page.locator(".chart-wrap")).toHaveAttribute(
    "data-marker-count",
    "2",
  );
  await expect
    .poll(() =>
      page.locator(".chart-canvas").evaluate((el) => {
        let buy = 0;
        let sell = 0;
        for (const canvas of el.querySelectorAll("canvas")) {
          const ctx = canvas.getContext("2d");
          if (!ctx || !canvas.width || !canvas.height) continue;
          const pixels = ctx.getImageData(
            0,
            0,
            canvas.width,
            canvas.height,
          ).data;
          for (let i = 0; i < pixels.length; i += 4) {
            if (
              Math.abs(pixels[i] - 241) < 5 &&
              Math.abs(pixels[i + 1] - 124) < 5 &&
              Math.abs(pixels[i + 2] - 134) < 5
            )
              buy++;
            if (
              Math.abs(pixels[i] - 53) < 5 &&
              Math.abs(pixels[i + 1] - 204) < 5 &&
              Math.abs(pixels[i + 2] - 172) < 5
            )
              sell++;
          }
        }
        return Math.min(buy, sell);
      }),
    )
    .toBeGreaterThan(10);
  await page.screenshot({
    path: "test-results/full-trades.png",
    fullPage: true,
  });
  const markerPoint = await page.locator(".chart-canvas").evaluate((el) => {
    for (const canvas of el.querySelectorAll("canvas")) {
      const ctx = canvas.getContext("2d");
      if (!ctx || !canvas.width || !canvas.height) continue;
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      for (let i = 0; i < pixels.length; i += 4) {
        if (
          pixels[i] === 241 &&
          pixels[i + 1] === 124 &&
          pixels[i + 2] === 134
        ) {
          const box = canvas.getBoundingClientRect();
          return {
            x: box.left + (((i / 4) % canvas.width) * box.width) / canvas.width,
            y:
              box.top +
              (Math.floor(i / 4 / canvas.width) * box.height) / canvas.height,
          };
        }
      }
    }
    throw new Error("Buy marker pixel not found");
  });
  await page.mouse.move(markerPoint.x, markerPoint.y);
  await expect(page.locator(".trade-tooltip")).toContainText("买入");
  await expect(page.locator(".trade-tooltip")).toContainText("3,501");
  await page.mouse.move(20, 20);
  await page.getByRole("button", { name: "分时", exact: true }).click();
  await expect(page.locator(".chart-wrap")).toHaveAttribute(
    "data-marker-count",
    "2",
  );

  const days = [3, 2].map((offset) =>
    new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(
      new Date(Date.now() - offset * 86400000),
    ),
  );
  const lines = ["symbol,exchange,datetime,open,high,low,close,volume"];
  for (const day of days)
    for (let i = 0; i < 180; i++) {
      const at = new Date(Date.parse(day + "T09:00:00+08:00") + i * 60000);
      const local = new Date(at.getTime() + 8 * 3600000)
        .toISOString()
        .slice(0, 19);
      const close = Math.round(3500 + Math.sin(i / 7) * 30);
      lines.push(
        `rbTEST,SHFE,${local}+08:00,${close},${close + 10},${close - 10},${close},100`,
      );
    }
  await page.getByRole("button", { name: "数据", exact: true }).click();
  await page.locator("input[type=file]").setInputFiles({
    name: "synthetic-e2e.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(lines.join("\n")),
  });
  await expect(page.getByText("已导入 360 根 K 线")).toBeVisible({
    timeout: 15000,
  });
  await page.getByRole("button", { name: "策略", exact: true }).click();
  await expect(page.locator(".monaco-editor").first()).toBeVisible();
  await expect(page.locator(".monaco-editor .view-lines")).toContainText(
    "DoubleMaStrategy",
  );
  await page
    .getByRole("textbox", { name: "Editor content", exact: true })
    .focus();
  await page.keyboard.press("Control+End");
  await page.keyboard.press("Enter");
  await page.keyboard.type("# Browser workflow validation");
  await page.getByLabel("策略名称").fill("全流程验证双均线");
  await page.getByRole("button", { name: "检查语法" }).click();
  await expect(page.getByText("Python 语法检查通过")).toBeVisible();
  const savedVersion = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" && r.url().endsWith("/api/v1/versions"),
  );
  await page.getByRole("button", { name: "保存版本" }).click();
  expect((await (await savedVersion).json()).source).toContain(
    "# Browser workflow validation",
  );
  await expect(page.getByText("新版本已保存")).toBeVisible();
  await page.getByRole("button", { name: "发布版本", exact: true }).click();
  await page.getByRole("button", { name: "发布", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "发布版本", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "创建实例", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("实例名称").fill("e2e_ma");
  await dialog.getByLabel("已发布版本").click();
  await page
    .locator(".ant-select-item-option")
    .filter({ hasText: "全流程验证双均线" })
    .click();
  await dialog.getByLabel("交易合约").click();
  await page
    .locator(".ant-select-item-option")
    .filter({ hasText: "rbTEST.SHFE" })
    .click();
  await dialog.getByLabel("参数（JSON）").fill('{"bar_minutes":1}');
  await dialog.getByRole("button", { name: "创建实例", exact: true }).click();
  await expect(dialog).toBeHidden();
  const instance = page
    .locator(".instances tbody tr")
    .filter({ hasText: "e2e_ma" });
  await instance.getByRole("button", { name: "初始化", exact: true }).click();
  await expect(instance.getByText("已就绪", { exact: true })).toBeVisible({
    timeout: 20000,
  });
  await expect(page.locator(".monitor-legend")).toContainText("SMA10", {
    timeout: 15000,
  });
  await expect(page.locator(".monitor-legend")).toContainText("SMA20");
  await expect(
    page.locator(".strategy-monitor .chart-canvas canvas").first(),
  ).toBeVisible();
  await expect(page.locator(".strategy-monitor .chart-wrap")).toHaveAttribute(
    "data-marker-count",
    "0",
  );
  await instance.getByRole("button", { name: /启动$/ }).click();
  await expect(instance.getByText("运行中", { exact: true })).toBeVisible({
    timeout: 15000,
  });
  await page.screenshot({
    path: "test-results/full-strategy.png",
    fullPage: true,
  });
  await instance.getByRole("button", { name: /停止$/ }).click();
  await expect(instance.getByText("已就绪", { exact: true })).toBeVisible({
    timeout: 15000,
  });
  await instance.getByRole("button", { name: "移除", exact: true }).click();
  await page.locator(".ant-modal-confirm .ant-btn-primary").click();
  await expect(instance).toHaveCount(0, { timeout: 15000 });

  await page.getByRole("button", { name: "回测", exact: true }).click();
  await page.getByLabel("策略版本").click();
  await page
    .locator(".ant-select-item-option")
    .filter({ hasText: "全流程验证双均线" })
    .click();
  await page.locator("#symbol").fill("rbTEST.SHFE");
  await page.getByLabel("开始日期").fill(days[1]);
  await page.getByLabel("结束日期").fill(days[1]);
  await page.getByLabel("策略参数（JSON，含周期）").fill('{"bar_minutes":1}');
  await page.getByRole("button", { name: "开始回测" }).click();
  await expect(
    page.locator(".result-panel .ant-tag").filter({ hasText: "已完成" }),
  ).toBeVisible({ timeout: 30000 });
  await expect
    .poll(async () =>
      Number(
        await page
          .getByText("成交笔数", { exact: true })
          .locator("..")
          .locator("strong")
          .innerText(),
      ),
    )
    .toBeGreaterThan(0);
  await expect
    .poll(async () =>
      Number(
        await page
          .locator(".result-panel .chart-wrap")
          .getAttribute("data-marker-count"),
      ),
    )
    .toBeGreaterThan(0);
  await page.screenshot({
    path: "test-results/full-backtest.png",
    fullPage: true,
  });
  await expect(page.locator(".result-panel .chart-wrap")).toHaveAttribute(
    "data-marker-label-count",
    "0",
  );
  await page.getByRole("checkbox", { name: "成交标签", exact: true }).check();
  await expect
    .poll(async () =>
      Number(
        await page
          .locator(".result-panel .chart-wrap")
          .getAttribute("data-marker-label-count"),
      ),
    )
    .toBeGreaterThan(0);
  await page.getByRole("checkbox", { name: "成交标签", exact: true }).uncheck();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
    )
    .toBeLessThanOrEqual(0);
  await expect
    .poll(async () =>
      Number(
        await page
          .locator(".result-panel .chart-wrap")
          .getAttribute("data-marker-count"),
      ),
    )
    .toBeGreaterThan(0);
  await page.screenshot({
    path: "test-results/full-backtest-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  expect(errors).toEqual([]);
});
