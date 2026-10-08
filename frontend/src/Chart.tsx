import { useEffect, useRef, useState } from "react";
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  type UTCTimestamp,
  type MouseEventParams,
} from "lightweight-charts";
import { Empty } from "antd";
import { num, labels, type Row } from "./api";
import { tradeMarkers } from "./tradeMarkers";

export function indicatorColor(key: string, index: number) {
  const colors: Record<string, string> = {
    MA: "#e8bd68",
    EMA: "#a293ed",
    BOLL_UP: "#6b9df5",
    BOLL_MID: "#5f7395",
    BOLL_LOW: "#6b9df5",
    MACD: "#e8bd68",
    SIGNAL: "#a293ed",
    RSI: "#a293ed",
    ATR: "#e8bd68",
  };
  return colors[key] || ["#e8bd68", "#a293ed", "#6b9df5"][index % 3];
}

export function MarketChart({
  data,
  overlays,
  oscillator,
  intraday = false,
  minutes = 1,
  showTrades = true,
  showTradeLabels = false,
  emptyText,
}: {
  data: Row;
  overlays: string[];
  oscillator: string;
  intraday?: boolean;
  minutes?: number;
  showTrades?: boolean;
  showTradeLabels?: boolean;
  emptyText?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<ReturnType<typeof createChart> | null>(null);
  const series = useRef<any[]>([]);
  const fit = useRef(true);
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    trades: Row[];
  } | null>(null);
  useEffect(() => {
    const c = createChart(container.current!, {
      autoSize: true,
      layout: {
        fontFamily: '"Workbench CJK", sans-serif',
        background: { type: ColorType.Solid, color: "#111a28" },
        textColor: "#8393ab",
        attributionLogo: true,
        panes: { separatorColor: "#26334a", separatorHoverColor: "#486076" },
      },
      grid: {
        vertLines: { color: "#1b2637" },
        horzLines: { color: "#1b2637" },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        rightOffsetPixels: 56,
        borderColor: "#26334a",
        tickMarkFormatter: (time: number) =>
          new Date(time * 1000).toLocaleString("zh-CN", {
            timeZone: "Asia/Shanghai",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          }),
      },
      localization: {
        timeFormatter: (time: number) =>
          new Date(time * 1000).toLocaleString("zh-CN", {
            timeZone: "Asia/Shanghai",
          }),
      },
      rightPriceScale: { borderColor: "#26334a" },
      crosshair: {
        vertLine: { color: "#60718d" },
        horzLine: { color: "#60718d" },
      },
    });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
    };
  }, []);
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    for (const s of series.current) c.removeSeries(s);
    series.current = [];
    const fillMarkers = showTrades
      ? tradeMarkers(data.bars || [], data.trades || [], intraday ? 1 : minutes)
      : [];
    const markers = fillMarkers.map((marker) => ({
      ...marker,
      text: showTradeLabels ? marker.text : undefined,
    }));
    const group = new Map<number, Row[]>();
    const markerTimes = new Set(fillMarkers.map((m) => Number(m.time)));
    for (const trade of data.trades || []) {
      const step = (intraday ? 1 : minutes) * 60;
      const time = Math.floor(Date.parse(trade.datetime) / 1000 / step) * step;
      if (!markerTimes.has(time)) continue;
      group.set(time, [...(group.get(time) || []), trade]);
    }
    const onHover = (event: MouseEventParams) => {
      const trades =
        typeof event.time === "number" ? group.get(event.time) : undefined;
      if (!event.point || !trades?.length || event.paneIndex !== 0) {
        setHover(null);
        return;
      }
      const el = container.current!;
      setHover({
        x: Math.max(8, Math.min(event.point.x + 16, el.clientWidth - 248)),
        y: Math.max(8, Math.min(event.point.y + 16, el.clientHeight - 170)),
        trades,
      });
    };
    c.subscribeCrosshairMove(onHover);
    c.subscribeClick(onHover);
    const cleanup = () => {
      c.unsubscribeCrosshairMove(onHover);
      c.unsubscribeClick(onHover);
    };
    if (intraday) {
      const bars: Row[] = data.bars || [];
      // Keep missing minutes on the time axis without manufacturing prices/volume.
      const timeline: Row[] = [];
      for (const b of bars) {
        const previous = timeline.at(-1);
        if (previous) {
          for (let time = previous.time + 60; time < b.time; time += 60)
            timeline.push({ time });
        }
        timeline.push(b);
      }
      for (const [field, color, title] of [
        ["close", "#63afff", "价格"],
        ["average", "#e8bd68", "均价"],
      ]) {
        const line = c.addSeries(LineSeries, {
          color,
          title,
          lineWidth: 2,
          priceLineVisible: false,
          crosshairMarkerVisible: true,
        });
        series.current.push(line);
        line.setData(
          timeline.map((b) =>
            b[field] == null
              ? { time: b.time as UTCTimestamp }
              : { time: b.time as UTCTimestamp, value: b[field] },
          ),
        );
        if (field === "close")
          createSeriesMarkers(line, markers, { zOrder: "aboveSeries" });
      }
      const volume = c.addSeries(
        HistogramSeries,
        {
          title: "成交量",
          priceFormat: { type: "volume" },
          priceLineVisible: false,
        },
        1,
      );
      series.current.push(volume);
      volume.setData(
        timeline.map((b) =>
          b.volume == null
            ? { time: b.time as UTCTimestamp }
            : {
                time: b.time as UTCTimestamp,
                value: b.volume,
                color: b.close >= b.open ? "#ed6d7888" : "#26c6a088",
              },
        ),
      );
      c.panes().forEach((pane, index) =>
        pane.setStretchFactor(index === 0 ? 3 : 1),
      );
      if (fit.current && bars.length) {
        c.timeScale().fitContent();
        fit.current = false;
      }
      return cleanup;
    }
    const candles = c.addSeries(CandlestickSeries, {
      upColor: "#ed6d78",
      downColor: "#26c6a0",
      borderVisible: false,
      wickUpColor: "#ed6d78",
      wickDownColor: "#26c6a0",
    });
    series.current.push(candles);
    const bars: Row[] = data.bars || [];
    candles.setData(
      bars.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    createSeriesMarkers(candles, markers, { zOrder: "aboveSeries" });
    const volume = c.addSeries(
      HistogramSeries,
      { priceFormat: { type: "volume" }, priceScaleId: "right" },
      1,
    );
    series.current.push(volume);
    volume.setData(
      bars.map((b) => ({
        time: b.time as UTCTimestamp,
        value: b.volume,
        color: b.close >= b.open ? "#ed6d7866" : "#26c6a066",
      })),
    );
    const keys = overlays
      .flatMap((x) =>
        x === "BOLL" ? ["BOLL_UP", "BOLL_MID", "BOLL_LOW"] : [x],
      )
      .concat(
        oscillator === "MACD"
          ? ["MACD", "SIGNAL"]
          : oscillator === "NONE"
            ? []
            : [oscillator],
      );
    for (const [index, key] of keys.entries()) {
      const pane = ["MACD", "SIGNAL", "RSI", "ATR", "OI"].includes(key) ? 2 : 0;
      const s = c.addSeries(
        LineSeries,
        {
          color: indicatorColor(key, index),
          lineWidth: 1,
          title: key,
          priceLineVisible: false,
          lastValueVisible: false,
        },
        pane,
      );
      series.current.push(s);
      s.setData(
        bars.flatMap((b, i) => {
          const v =
            key === "OI" ? b.open_interest : data.indicators?.[key]?.[i];
          return v == null ? [] : [{ time: b.time as UTCTimestamp, value: v }];
        }),
      );
    }
    if (oscillator === "MACD") {
      const s = c.addSeries(
        HistogramSeries,
        { title: "HIST", priceLineVisible: false, lastValueVisible: false },
        2,
      );
      series.current.push(s);
      s.setData(
        bars.flatMap((b, i) => {
          const v = data.indicators?.HIST?.[i];
          return v == null
            ? []
            : [
                {
                  time: b.time as UTCTimestamp,
                  value: v,
                  color: v >= 0 ? "#ed6d78" : "#26c6a0",
                },
              ];
        }),
      );
    }
    // Absolute pane heights divide by the chart's height before its first resize.
    // Positive proportions also work during that initial zero-height layout.
    c.panes().forEach((pane, index) =>
      pane.setStretchFactor([6, 2, 3][index] || 1),
    );
    if (fit.current && bars.length) {
      c.timeScale().fitContent();
      fit.current = false;
    }
    return cleanup;
  }, [
    data,
    overlays,
    oscillator,
    intraday,
    minutes,
    showTrades,
    showTradeLabels,
  ]);
  return (
    <div
      className="chart-wrap"
      onMouseLeave={() => setHover(null)}
      data-marker-label-count={
        showTrades && showTradeLabels
          ? tradeMarkers(
              data.bars || [],
              data.trades || [],
              intraday ? 1 : minutes,
            ).length
          : 0
      }
      data-marker-count={
        showTrades
          ? tradeMarkers(
              data.bars || [],
              data.trades || [],
              intraday ? 1 : minutes,
            ).length
          : 0
      }
    >
      <div ref={container} className="chart-canvas" />
      {showTrades && hover && (
        <div
          className="trade-tooltip"
          role="tooltip"
          style={{ left: hover.x, top: hover.y }}
        >
          <strong>
            {new Date(hover.trades[0].datetime).toLocaleDateString("zh-CN", {
              timeZone: "Asia/Shanghai",
            })}
          </strong>
          {hover.trades.slice(0, 5).map((t, index) => (
            <div className="trade-tooltip-row" key={t.vt_tradeid || index}>
              <span className={t.direction === "LONG" ? "up" : "down"}>
                {t.direction === "LONG" ? "买入" : "卖出"} ·{" "}
                {labels[t.offset] || t.offset}
                <small>
                  {new Date(t.datetime).toLocaleTimeString("zh-CN", {
                    timeZone: "Asia/Shanghai",
                    hour12: false,
                  })}
                </small>
              </span>
              <strong>{num(t.price)}</strong>
              <span>{num(t.volume, 0)} 手</span>
            </div>
          ))}
          {hover.trades.length > 5 && (
            <small>共 {hover.trades.length} 笔成交</small>
          )}
        </div>
      )}
      {!data.bars?.length && (
        <div className="chart-empty">
          <Empty
            description={
              emptyText ||
              (intraday
                ? "当日暂无分时数据 · 订阅行情或选择已有数据的日期"
                : "暂无 K 线 · 订阅实时行情或导入历史 CSV")
            }
          />
        </div>
      )}
    </div>
  );
}

export function EquityChart({ rows }: { rows: Row[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const c = createChart(ref.current!, {
      autoSize: true,
      layout: {
        fontFamily: '"Workbench CJK", sans-serif',
        background: { type: ColorType.Solid, color: "#111a28" },
        textColor: "#8393ab",
      },
      grid: {
        vertLines: { color: "#1b2637" },
        horzLines: { color: "#1b2637" },
      },
    });
    const s = c.addSeries(LineSeries, { color: "#42c6ae", lineWidth: 2 });
    s.setData(
      rows
        .filter((r) => r.balance != null)
        .map((r) => ({ time: r.date, value: r.balance })),
    );
    c.timeScale().fitContent();
    return () => c.remove();
  }, [rows]);
  return <div ref={ref} style={{ height: 260 }} />;
}
