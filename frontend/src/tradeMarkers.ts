import type { SeriesMarker, UTCTimestamp } from "lightweight-charts";
import type { Row } from "./api";

export function tradeMarkers(
  bars: Row[],
  trades: Row[],
  minutes: number,
): SeriesMarker<UTCTimestamp>[] {
  const times = new Set(bars.map((bar) => bar.time));
  return trades
    .flatMap((trade) => {
      const timestamp = Date.parse(trade.datetime) / 1000;
      const time = Math.floor(timestamp / (minutes * 60)) * minutes * 60;
      if (
        !Number.isFinite(timestamp) ||
        !times.has(time) ||
        !Number.isFinite(trade.price) ||
        !(trade.volume > 0)
      )
        return [];
      const buy = trade.direction === "LONG";
      if (!buy && trade.direction !== "SHORT") return [];
      const label = `${buy ? "买" : "卖"}${trade.offset === "OPEN" ? "开" : "平"}`;
      return [
        {
          time: time as UTCTimestamp,
          position: buy ? ("belowBar" as const) : ("aboveBar" as const),
          color: buy ? "#f17c86" : "#35ccac",
          shape: buy ? ("arrowUp" as const) : ("arrowDown" as const),
          text: `${label} ${trade.price}`,
          id: trade.vt_tradeid,
        },
      ];
    })
    .sort((a, b) => a.time - b.time);
}
