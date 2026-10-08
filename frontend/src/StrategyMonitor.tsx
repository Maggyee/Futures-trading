import { useEffect, useState } from "react";
import { Alert, Empty, Select, Tag, Checkbox } from "antd";
import { api, num, type Row } from "./api";
import { MarketChart, indicatorColor } from "./Chart";

export function StrategyMonitor({ snapshot }: { snapshot: Row }) {
  const [selected, setSelected] = useState("");
  const [data, setData] = useState<Row>({ bars: [] });
  const [error, setError] = useState("");
  const [showTrades, setShowTrades] = useState(true);
  const current =
    snapshot.strategies.find((s: Row) => s.name === selected) ||
    snapshot.strategies[0];
  useEffect(() => {
    setData({ bars: [] });
    setError("");
    if (!current) return;
    let active = true;
    let loading = false;
    const load = async () => {
      if (loading) return;
      loading = true;
      try {
        const result = await api(
          `/instances/${encodeURIComponent(current.name)}/chart`,
        );
        if (active) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (active) setError(String(e));
      } finally {
        loading = false;
      }
    };
    void load();
    const timer = setInterval(load, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [current?.name, current?.version, current?.initializing, current?.inited]);
  const pnl = current
    ? snapshot.positions
        .filter((p: Row) => p.vt_symbol === current.symbol)
        .reduce((total: number, p: Row) => total + (p.pnl || 0), 0)
    : 0;
  return (
    <section className="strategy-monitor">
      <div className="monitor-heading">
        <h2>运行监控</h2>
        <Select
          aria-label="监控实例"
          placeholder="选择实例"
          value={current?.name}
          onChange={setSelected}
          options={snapshot.strategies.map((s: Row) => ({
            value: s.name,
            label: `${s.name} · ${s.symbol}`,
          }))}
        />
      </div>
      {!current ? (
        <Empty description="暂无策略实例" />
      ) : (
        <>
          <div className="monitor-status">
            <strong>{current.symbol}</strong>
            <Tag
              color={
                current.trading ? "green" : current.error ? "red" : "default"
              }
            >
              {current.trading
                ? "运行中"
                : current.initializing
                  ? "初始化中"
                  : current.needs_init
                    ? "待初始化"
                    : current.ready
                      ? "已就绪"
                      : "已停止"}
            </Tag>
            <span>{current.parameters.bar_minutes} 分钟</span>
            <Checkbox
              checked={showTrades}
              onChange={(e) => setShowTrades(e.target.checked)}
            >
              成交点
            </Checkbox>
          </div>
          <div className="monitor-metrics">
            <div>
              <span>策略持仓</span>
              <strong>{num(current.pos, 0)} 手</strong>
            </div>
            <div>
              <span>合约浮动盈亏</span>
              <strong className={pnl >= 0 ? "up" : "down"}>{num(pnl)}</strong>
            </div>
            <div>
              <span>版本</span>
              <strong>{current.version.slice(0, 8)}</strong>
            </div>
            <div>
              <span>已完成 K 线</span>
              <strong>{data.bars?.length || 0}</strong>
            </div>
          </div>
          {(error || current.error) && (
            <Alert type="error" message={error || current.error} />
          )}
          <div className="monitor-legend">
            {Object.keys(data.indicators || {}).map((key, index) => (
              <span key={key}>
                <i
                  style={{
                    background: indicatorColor(key, index),
                  }}
                />
                {key}
                <strong>{num(data.indicators[key].at(-1))}</strong>
              </span>
            ))}
          </div>
          <MarketChart
            key={`${current.name}-${current.version}`}
            data={data}
            overlays={data.overlays || []}
            oscillator="NONE"
            minutes={data.minutes || current.parameters.bar_minutes}
            showTrades={showTrades}
            emptyText={
              current.initializing ? "正在初始化" : "等待初始化与已完成 K 线"
            }
          />
          <div className="monitor-variables">
            {Object.entries(current.variables || {})
              .filter(
                ([key]) => !["inited", "trading", "ready", "pos"].includes(key),
              )
              .map(([key, value]) => (
                <span key={key}>
                  {key}
                  <strong>
                    {typeof value === "number" ? num(value) : String(value)}
                  </strong>
                </span>
              ))}
          </div>
        </>
      )}
    </section>
  );
}
