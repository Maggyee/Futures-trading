import { test, expect } from "@playwright/test";
import { tradeMarkers } from "../src/tradeMarkers";

test("fill markers align to Shanghai bar starts and distinguish open/close directions", () => {
  const time = Date.parse("2026-09-29T22:25:00+08:00") / 1000;
  const markers = tradeMarkers(
    [{ time }],
    [
      {
        datetime: "2026-09-29T22:27:52+08:00",
        direction: "SHORT",
        offset: "CLOSETODAY",
        price: 3116,
        volume: 1,
      },
      {
        datetime: "2026-09-29T22:27:47+08:00",
        direction: "LONG",
        offset: "OPEN",
        price: 3117,
        volume: 1,
      },
    ],
    5,
  );
  expect(markers).toHaveLength(2);
  expect(markers.every((m) => m.time === time)).toBe(true);
  expect(markers.find((m) => m.shape === "arrowUp")?.text).toBe("买开 3117");
  expect(markers.find((m) => m.shape === "arrowDown")?.text).toBe("卖平 3116");
});

test("missing bars, other dates and malformed fills create no marker", () => {
  const time = Date.parse("2026-09-29T22:27:00+08:00") / 1000;
  const valid = {
    datetime: "2026-09-29T22:27:47+08:00",
    direction: "LONG",
    offset: "OPEN",
    price: 3117,
    volume: 1,
  };
  expect(tradeMarkers([], [valid], 1)).toEqual([]);
  expect(
    tradeMarkers(
      [{ time }],
      [
        { ...valid, datetime: "invalid" },
        { ...valid, datetime: "2026-09-30T22:27:47+08:00" },
        { ...valid, volume: 0 },
        { ...valid, price: Infinity },
      ],
      1,
    ),
  ).toEqual([]);
});
