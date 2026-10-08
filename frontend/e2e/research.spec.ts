import { test, expect } from "@playwright/test";

test("offline research is labeled, displays results, and has no connect action", async ({
  page,
}) => {
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      /\/api\/v1\/(connect|orders|close|instances)/.test(request.url())
    ) {
      mutations.push(request.url());
    }
  });
  await page.route("**/api/v1/research/runs", (route) =>
    route.fulfill({
      json: [
        {
          path: "fixture/run_test",
          id: "SYNTHETIC_TEST_ONLY",
          synthetic: true,
          scope: "shared",
          k: 1,
          entry_mode: "direct",
          split: "validation",
          window: { start: "2026-01-08", end: "2026-01-08" },
        },
      ],
    }),
  );
  await page.route("**/api/v1/research/run?*", (route) =>
    route.fulfill({
      json: {
        coverage: [],
        artifacts: ["report.md"],
        result: {
          status: "completed",
          unflattened_risk: [],
          metrics: {
            net_profit: 0,
            max_drawdown: 0,
            trade_count: 0,
            risk_rejection_count: 0,
            sample_warning: "工程样例，证据不足",
            daily: [{ date: "2026-01-08", equity: 1000000, net_pnl: 0 }],
          },
        },
      },
    }),
  );
  await page.goto("/");
  await page
    .getByLabel("密码", { exact: true })
    .fill("browser-test-password-123");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await page.getByRole("button", { name: "研究", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "开盘强弱研究" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "SYNTHETIC_TEST_ONLY：以下仅用于工程计算核对，不能用于策略收益验证。",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "连接 SimNow" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("link", { name: "下载中文报告" })).toBeVisible();
  await expect(page.locator(".research-panel canvas").first()).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("heading", { name: "离线研究结果" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  expect(mutations).toEqual([]);
});
