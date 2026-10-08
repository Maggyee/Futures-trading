import { test, expect } from "@playwright/test";

test("watchlist stays ordered through ticks, supports add/remove and retains an empty list", async ({
  page,
}) => {
  const names = ["rb2610.SHFE", "cu2611.SHFE", "m2701.DCE"];
  let watched = names.slice(0, 2);
  let updates = 0;
  const ticks = names.map((s) => ({
    vt_symbol: s,
    symbol: s.split(".")[0],
    last_price: 3500,
    pre_close: 3490,
  }));
  await page.route("**/api/v1/session", (r) =>
    r.fulfill({ json: { csrf: "fixture" } }),
  );
  await page.route("**/api/v1/snapshot", (r) =>
    r.fulfill({
      json: {
        connected: true,
        ready: true,
        accounts: [],
        positions: [],
        orders: [],
        trades: [],
        strategies: [],
        logs: [],
        operations: [],
        watchlist: watched,
        subscriptions: names,
        contracts: names.map((s) => ({
          vt_symbol: s,
          symbol: s.split(".")[0],
          name: "测试合约",
          exchange: s.split(".")[1],
          size: 10,
          pricetick: 1,
        })),
        ticks: ticks.slice().reverse(),
      },
    }),
  );
  await page.route("**/api/v1/bars/**", (r) =>
    r.fulfill({ json: { bars: [], indicators: {}, trades: [] } }),
  );
  await page.route("**/api/v1/watchlist/*/remove", (r) => {
    const symbol = decodeURIComponent(
      new URL(r.request().url()).pathname.split("/").at(-2)!,
    );
    watched = watched.filter((s) => s !== symbol);
    return r.fulfill({
      json: { id: "fixture", state: "completed", result: { symbols: watched } },
    });
  });
  await page.route("**/api/v1/subscribe/*", (r) => {
    const symbol = decodeURIComponent(
      new URL(r.request().url()).pathname.split("/").at(-1)!,
    );
    if (!watched.includes(symbol)) watched.push(symbol);
    return r.fulfill({
      json: { id: "fixture", state: "completed", result: { symbol } },
    });
  });
  await page.routeWebSocket("**/api/v1/ws", (socket) => {
    const timer = setInterval(() => {
      const tick = ticks[updates++ % ticks.length];
      socket.send(
        JSON.stringify({
          type: "eTick.",
          data: { ...tick, last_price: 3500 + updates },
        }),
      );
    }, 120);
    socket.onClose(() => clearInterval(timer));
  });
  const order = () =>
    page
      .locator(".watch-item")
      .evaluateAll((rows) =>
        rows.map((r) => (r as HTMLElement).dataset.symbol),
      );
  await page.goto("/");
  await expect.poll(order).toEqual(names.slice(0, 2));
  await expect.poll(() => updates).toBeGreaterThan(6);
  expect(await order()).toEqual(names.slice(0, 2));
  await page
    .getByRole("button", { name: "查看行情 rb2610.SHFE", exact: true })
    .click();
  await page
    .getByRole("button", { name: "移出自选 rb2610.SHFE", exact: true })
    .click();
  await expect.poll(order).toEqual([names[1]]);
  await page.locator(".watchlist .ant-select").click();
  await page
    .locator(".ant-select-item-option")
    .filter({ hasText: names[2] })
    .click();
  await expect.poll(order).toEqual([names[1], names[2]]);
  await page.reload();
  await expect.poll(order).toEqual([names[1], names[2]]);
  await page
    .getByRole("button", { name: `移出自选 ${names[1]}`, exact: true })
    .click();
  await page
    .getByRole("button", { name: `移出自选 ${names[2]}`, exact: true })
    .click();
  await expect(page.getByText("暂无自选合约")).toBeVisible();
  await expect.poll(order).toEqual([]);
  await page.reload();
  await expect.poll(order).toEqual([]);
  await expect(page.getByText("暂无自选合约")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});
