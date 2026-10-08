import type { Row } from "./api";
import { useEffect, useState } from "react";

export function useQuoteFresh(receivedAt: number | undefined) {
  const [, refresh] = useState(0);
  useEffect(() => {
    if (!receivedAt) return;
    const remaining = receivedAt * 1000 + 30000 - Date.now();
    if (remaining <= 0) return;
    const timer = setTimeout(() => refresh((n) => n + 1), remaining);
    return () => clearTimeout(timer);
  }, [receivedAt]);
  return !!receivedAt && Date.now() < receivedAt * 1000 + 30000;
}

export function quoteTime(tick: Row | undefined) {
  if (!tick?.datetime) return "—";
  return new Date(tick.datetime).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function quotePrice(value: unknown): number | null {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    value > 0 &&
    value < 1e20
    ? value
    : null;
}

export function counterparty(
  tick: Row | undefined,
  direction: string,
  closing = false,
) {
  const buy = closing ? direction === "SHORT" : direction === "LONG";
  return {
    side: buy ? "卖一" : "买一",
    price: quotePrice(tick?.[buy ? "ask_price_1" : "bid_price_1"]),
  };
}
