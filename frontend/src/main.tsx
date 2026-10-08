import React, { useState, useEffect, useCallback, useRef } from "react";
import { createRoot } from "react-dom/client";
import {
  App as AntApp,
  ConfigProvider,
  theme,
  Button,
  Input,
  InputNumber,
  Select,
  Table,
  Tag,
  Tabs,
  Space,
  Form,
  Modal,
  Alert,
  Empty,
  Spin,
  Upload,
  Checkbox,
  Tooltip,
} from "antd";
import ApiOutlined from "@ant-design/icons/es/icons/ApiOutlined";
import LineChartOutlined from "@ant-design/icons/es/icons/LineChartOutlined";
import CodeOutlined from "@ant-design/icons/es/icons/CodeOutlined";
import ExperimentOutlined from "@ant-design/icons/es/icons/ExperimentOutlined";
import DatabaseOutlined from "@ant-design/icons/es/icons/DatabaseOutlined";
import LogoutOutlined from "@ant-design/icons/es/icons/LogoutOutlined";
import PlayCircleOutlined from "@ant-design/icons/es/icons/PlayCircleOutlined";
import PauseCircleOutlined from "@ant-design/icons/es/icons/PauseCircleOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import UploadOutlined from "@ant-design/icons/es/icons/UploadOutlined";
import SettingOutlined from "@ant-design/icons/es/icons/SettingOutlined";
import CloseOutlined from "@ant-design/icons/es/icons/CloseOutlined";
import Editor, { loader } from "@monaco-editor/react";
import { api, setCsrf, num, labels, type Row } from "./api";
import { MarketChart, EquityChart } from "./Chart";
import { counterparty, quotePrice, quoteTime, useQuoteFresh } from "./quotes";
import { StrategyMonitor } from "./StrategyMonitor";
import { ResearchView } from "./ResearchView";
import "./style.css";

loader.config({ paths: { vs: "/vendor/monaco/vs" } });
const initial: Row = {
  contracts: [],
  ticks: [],
  positions: [],
  orders: [],
  trades: [],
  accounts: [],
  strategies: [],
  logs: [],
  operations: [],
  subscriptions: [],
  tick_received_at: {},
};
const activeStates = ["SUBMITTING", "NOTTRADED", "PARTTRADED"];
const cell = (value: unknown) => num(value);
const tagged = (value: string) => (
  <Tag
    color={value === "LONG" ? "red" : value === "SHORT" ? "green" : undefined}
  >
    {labels[value] || value}
  </Tag>
);

function LoginView({ onLogin }: { onLogin: () => void }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  return (
    <div className="login-shell">
      <div className="login-story">
        <div className="brand">
          <span className="brand-mark">W</span> CTP WORKBENCH
        </div>
        <h1>
          看清市场。
          <br />
          <span>掌握每一次决策。</span>
        </h1>
        <p>
          行情、交易、策略与回测，
          <br />
          在一个工作台中连接你的研究与执行。
        </p>
        <div className="login-lines">
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
        </div>
        <small>POWERED BY VN.PY · SIMNOW</small>
      </div>
      <div className="login-card">
        <Tag color="cyan">个人模拟交易工作台</Tag>
        <h2>欢迎回来</h2>
        <p className="muted">登录以查看行情和管理策略</p>
        {error && <Alert type="error" message={error} showIcon />}
        <Form
          layout="vertical"
          onFinish={async (values) => {
            setLoading(true);
            setError("");
            try {
              const r = await api("/login", "POST", values);
              setCsrf(r.csrf);
              onLogin();
            } catch (e) {
              setError(String(e));
            } finally {
              setLoading(false);
            }
          }}
        >
          <Form.Item
            label="管理员账号"
            name="username"
            initialValue="admin"
            rules={[{ required: true }]}
          >
            <Input size="large" autoComplete="username" />
          </Form.Item>
          <Form.Item label="密码" name="password" rules={[{ required: true }]}>
            <Input.Password size="large" autoComplete="current-password" />
          </Form.Item>
          <Button
            block
            size="large"
            type="primary"
            htmlType="submit"
            loading={loading}
          >
            进入工作台
          </Button>
        </Form>
        <p className="login-help">
          首次使用？请在服务器运行
          <br />
          <code>.venv/bin/python -m backend.cli init-admin</code>
        </p>
      </div>
    </div>
  );
}

function Workbench() {
  const { message, modal } = AntApp.useApp();
  const [logged, setLogged] = useState(false),
    [checked, setChecked] = useState(false),
    [page, setPage] = useState("trade");
  const [snapshot, setSnapshot] = useState<Row>(initial),
    [error, setError] = useState(""),
    [wsOk, setWsOk] = useState(false);
  const [symbol, setSymbol] = useState(""),
    [minutes, setMinutes] = useState(5),
    [chart, setChart] = useState<Row>({ bars: [] });
  const [overlays, setOverlays] = useState(["MA", "BOLL"]),
    [oscillator, setOscillator] = useState("MACD");
  const [historyStart, setHistoryStart] = useState(""),
    [historyEnd, setHistoryEnd] = useState("");
  const [chartMode, setChartMode] = useState("candles"),
    [intradayDate, setIntradayDate] = useState("");
  const [showTrades, setShowTrades] = useState(true);
  const [indicatorParams, setIndicatorParams] = useState({
      ma: 20,
      ema: 20,
      boll: 20,
      dev: 2,
      rsi: 14,
      atr: 14,
      fast: 12,
      slow: 26,
      signal: 9,
    }),
    [indicatorOpen, setIndicatorOpen] = useState(false);
  const [closing, setClosing] = useState<Row | null>(null),
    [busy, setBusy] = useState(false),
    [operation, setOperation] = useState<Row | null>(null);
  const [direction, setDirection] = useState("LONG"),
    [volume, setVolume] = useState(1),
    [price, setPrice] = useState<number | null>(null);
  const [closeVolume, setCloseVolume] = useState(1),
    [closePrice, setClosePrice] = useState<number | null>(null);
  const refreshRunning = useRef(false);
  const refresh = useCallback(async () => {
    if (refreshRunning.current) return;
    refreshRunning.current = true;
    try {
      setSnapshot(await api("/snapshot"));
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      refreshRunning.current = false;
    }
  }, []);
  useEffect(() => {
    api("/session")
      .then((r) => {
        setCsrf(r.csrf);
        setLogged(true);
      })
      .catch(() => {})
      .finally(() => setChecked(true));
    const expired = () => setLogged(false);
    window.addEventListener("session-expired", expired);
    return () => window.removeEventListener("session-expired", expired);
  }, []);
  useEffect(() => {
    if (!logged) return;
    void refresh();
    const interval = setInterval(refresh, 4000);
    let socket: WebSocket;
    let retry: ReturnType<typeof setTimeout>;
    let stopped = false;
    const connect = () => {
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/ws`,
      );
      socket.onopen = () => {
        setWsOk(true);
        void refresh();
      };
      socket.onmessage = (e) => {
        const event = JSON.parse(e.data);
        if (event.type === "eTick.") {
          setSnapshot((s) => ({
            ...s,
            ticks: [
              ...(s.ticks.some((t: Row) => t.vt_symbol === event.data.vt_symbol)
                ? s.ticks.map((t: Row) =>
                    t.vt_symbol === event.data.vt_symbol ? event.data : t,
                  )
                : [...s.ticks, event.data]),
            ],
            tick_received_at: {
              ...s.tick_received_at,
              ...(typeof event.received_at === "number"
                ? { [event.data.vt_symbol]: event.received_at }
                : {}),
            },
          }));
        }
      };
      socket.onclose = () => {
        setWsOk(false);
        if (!stopped) retry = setTimeout(connect, 3000);
      };
      socket.onerror = () => socket.close();
    };
    connect();
    return () => {
      stopped = true;
      clearInterval(interval);
      clearTimeout(retry);
      socket?.close();
    };
  }, [logged, refresh]);
  useEffect(() => {
    if (!logged || !symbol) return;
    let live = true;
    let running = false;
    const load = async () => {
      if (running) return;
      running = true;
      try {
        const r = await api(
          chartMode === "intraday"
            ? `/intraday/${symbol}${intradayDate ? "?day=" + intradayDate : ""}`
            : `/bars/${symbol}?minutes=${minutes}&indicators=${encodeURIComponent(JSON.stringify(indicatorParams))}${historyStart ? "&start=" + historyStart + "T00:00:00" : ""}${historyEnd ? "&end=" + historyEnd + "T23:59:59" : ""}`,
        );
        if (live) setChart(r);
      } catch (e) {
        if (live) setError(String(e));
      } finally {
        running = false;
      }
    };
    setChart({ bars: [] });
    void load();
    const interval = setInterval(load, chartMode === "intraday" ? 2000 : 5000);
    return () => {
      live = false;
      clearInterval(interval);
    };
  }, [
    logged,
    symbol,
    minutes,
    indicatorParams,
    historyStart,
    historyEnd,
    chartMode,
    intradayDate,
  ]);
  useEffect(() => {
    if (
      !operation?.id ||
      ["completed", "failed", "unknown"].includes(operation.state)
    )
      return;
    const timer = setInterval(async () => {
      try {
        const r = await api(`/commands/${operation.id}`);
        if (r) setOperation(r);
      } catch {}
    }, 2000);
    return () => clearInterval(timer);
  }, [operation]);
  async function action(path: string, body?: unknown) {
    setBusy(true);
    const key = crypto.randomUUID();
    try {
      const r = await api(path, "POST", body, key);
      if (r.id) setOperation(r);
      message.success(r.result?.message || r.message || "操作已受理");
      await refresh();
      return r;
    } catch (e) {
      message.error(String(e));
      if (["/close", "/orders"].includes(path))
        setOperation({
          id: key,
          state: "unknown",
          error: "请查询操作状态和柜台委托，勿直接重复下单",
        });
    } finally {
      setBusy(false);
    }
  }
  function selectSymbol(s: string) {
    setSymbol(s);
    setPrice(null);
    void action(`/subscribe/${s}`);
  }
  function confirmOrder() {
    modal.confirm({
      title: `确认${direction === "LONG" ? "买入开多" : "卖出开空"} ${symbol}`,
      content: `${volume} 手 · ${price === null ? `对手价（${opposite.side}）参考 ${num(opposite.price)}，提交时以最新报价为准` : `限价 ${price}`}。这是 SimNow 模拟账户。${price !== null && !openQuoteFresh ? "行情未更新，当前报价仅供参考；委托是否受理由柜台确认。" : ""}`,
      okText: "确认提交",
      cancelText: "取消",
      onOk: () => action("/orders", { symbol, direction, volume, price }),
    });
  }
  const contract = snapshot.contracts.find((c: Row) => c.vt_symbol === symbol);
  const tick = snapshot.ticks.find((t: Row) => t.vt_symbol === symbol);
  const watchSymbols: string[] =
    snapshot.watchlist ??
    snapshot.subscriptions ??
    snapshot.ticks.map((t: Row) => t.vt_symbol).sort();
  const opposite = counterparty(tick, direction);
  const openQuoteFresh = useQuoteFresh(snapshot.tick_received_at?.[symbol]);
  const closeTick = snapshot.ticks.find(
    (t: Row) => t.vt_symbol === closing?.vt_symbol,
  );
  const closeOpposite = counterparty(closeTick, closing?.direction, true);
  const closeQuoteFresh = useQuoteFresh(
    snapshot.tick_received_at?.[closing?.vt_symbol],
  );
  const account = snapshot.accounts[0];
  const columns = {
    positions: [
      { title: "合约", dataIndex: "vt_symbol" },
      { title: "方向", dataIndex: "direction", render: tagged },
      { title: "持仓", dataIndex: "volume" },
      { title: "昨仓", dataIndex: "yd_volume" },
      { title: "冻结", dataIndex: "frozen" },
      { title: "持仓均价", dataIndex: "price", render: cell },
      {
        title: "浮动盈亏",
        dataIndex: "pnl",
        render: (v: number) => (
          <span className={v >= 0 ? "up" : "down"}>{num(v)}</span>
        ),
      },
      {
        title: "操作",
        render: (_: unknown, p: Row) => (
          <Button
            size="small"
            danger
            disabled={!snapshot.ready || !p.volume || busy}
            onClick={() => {
              setClosing(p);
              setCloseVolume(Math.max(1, p.volume - p.frozen));
              setClosePrice(null);
            }}
          >
            平仓
          </Button>
        ),
      },
    ],
    orders: [
      { title: "委托号", dataIndex: "vt_orderid" },
      { title: "合约", dataIndex: "vt_symbol" },
      { title: "方向", dataIndex: "direction", render: tagged },
      { title: "开平", dataIndex: "offset", render: tagged },
      { title: "价格", dataIndex: "price", render: cell },
      { title: "手数", dataIndex: "volume" },
      { title: "已成交", dataIndex: "traded" },
      { title: "状态", dataIndex: "status", render: tagged },
      {
        title: "操作",
        render: (_: unknown, o: Row) =>
          activeStates.includes(o.status) ? (
            <Button
              size="small"
              disabled={busy}
              onClick={() => action(`/orders/${o.vt_orderid}/cancel`)}
            >
              撤单
            </Button>
          ) : null,
      },
    ],
    trades: [
      { title: "成交号", dataIndex: "vt_tradeid" },
      { title: "合约", dataIndex: "vt_symbol" },
      { title: "方向", dataIndex: "direction", render: tagged },
      { title: "开平", dataIndex: "offset", render: tagged },
      { title: "价格", dataIndex: "price", render: cell },
      { title: "数量", dataIndex: "volume" },
      { title: "时间", dataIndex: "datetime" },
    ],
  };
  if (!checked)
    return (
      <div className="loading">
        <Spin size="large" />
      </div>
    );
  if (!logged) return <LoginView onLogin={() => setLogged(true)} />;
  return (
    <div className="shell">
      <aside className="rail">
        <a className="brand-mark" onClick={() => setPage("trade")}>
          W
        </a>
        {[
          { key: "trade", icon: <LineChartOutlined />, label: "交易" },
          { key: "strategy", icon: <CodeOutlined />, label: "策略" },
          { key: "backtest", icon: <ExperimentOutlined />, label: "回测" },
          { key: "research", icon: <ExperimentOutlined />, label: "研究" },
          { key: "data", icon: <DatabaseOutlined />, label: "数据" },
        ].map((n) => (
          <button
            key={n.key}
            aria-label={n.label}
            className={page === n.key ? "nav-item active" : "nav-item"}
            onClick={() => setPage(n.key)}
          >
            {n.icon}
            <span>{n.label}</span>
          </button>
        ))}
        <Tooltip title="退出登录">
          <button
            className="nav-item logout"
            aria-label="退出登录"
            onClick={async () => {
              await api("/logout", "POST");
              setLogged(false);
            }}
          >
            <LogoutOutlined />
          </button>
        </Tooltip>
      </aside>
      <div className="main">
        <header>
          <div>
            <span className="wordmark">
              CTP <strong>WORKBENCH</strong>
            </span>
            <Tag color="cyan">
              {page === "research" ? "离线研究" : "SIMNOW 模拟"}
            </Tag>
          </div>
          <Space>
            <span className="connection">
              <i className={snapshot.ready && wsOk ? "dot online" : "dot"} />
              {snapshot.ready ? "交易已就绪" : "等待连接"} ·{" "}
              {wsOk ? "推送在线" : "推送离线"}
            </span>
            {page !== "research" && (
              <Button
                icon={<ApiOutlined />}
                onClick={() => action("/connect")}
                loading={busy}
                disabled={snapshot.connected}
              >
                连接 SimNow
              </Button>
            )}
            <span className="avatar">N</span>
          </Space>
        </header>
        <div className="page-heading">
          <div>
            <div className="eyebrow">YOUR PERSONAL TRADING DESK</div>
            <h1>
              {
                {
                  trade: "交易工作台",
                  strategy: "策略中心",
                  backtest: "历史回测",
                  research: "开盘强弱研究",
                  data: "行情数据",
                }[page]
              }
            </h1>
          </div>
          <span className="muted">
            {new Date().toLocaleDateString("zh-CN", {
              year: "numeric",
              month: "long",
              day: "numeric",
            })}{" "}
            · 上海时间
          </span>
        </div>
        {error && (
          <Alert
            className="global-alert"
            type="warning"
            showIcon
            message={error}
            action={
              <Button size="small" onClick={refresh}>
                重试
              </Button>
            }
          />
        )}
        {page === "trade" && (
          <>
            <div className="account-strip">
              {[
                { label: "账户总权益", value: account?.balance },
                { label: "可用资金", value: account?.available },
                { label: "冻结资金", value: account?.frozen },
                {
                  label: "持仓浮动盈亏",
                  value: account
                    ? snapshot.positions.reduce(
                        (n: number, p: Row) => n + p.pnl,
                        0,
                      )
                    : null,
                },
              ].map((m, i) => (
                <div className="metric" key={m.label}>
                  <span>{m.label}</span>
                  <strong
                    className={
                      i === 3 && m.value != null
                        ? m.value >= 0
                          ? "up"
                          : "down"
                        : ""
                    }
                  >
                    {num(m.value)}
                    <small>CNY</small>
                  </strong>
                </div>
              ))}
              <div className="metric last">
                <span>策略运行</span>
                <strong>
                  {snapshot.strategies.filter((s: Row) => s.trading).length}
                  <small>/ {snapshot.strategies.length} 个实例</small>
                </strong>
              </div>
            </div>
            <div className="trading-grid">
              <section className="panel watchlist">
                <div className="panel-title">
                  自选行情 <span>{watchSymbols.length}</span>
                </div>
                <Select
                  showSearch
                  aria-label="添加自选合约"
                  placeholder="添加合约"
                  value={undefined}
                  onChange={selectSymbol}
                  disabled={busy || !snapshot.contracts.length}
                  options={snapshot.contracts.map((c: Row) => ({
                    value: c.vt_symbol,
                    label: `${c.vt_symbol} ${c.name}`,
                    disabled: watchSymbols.includes(c.vt_symbol),
                  }))}
                  filterOption={(input, option) =>
                    String(option?.label)
                      .toLowerCase()
                      .includes(input.toLowerCase())
                  }
                />
                <div className="watch-head">
                  <span>合约 / 最新价</span>
                  <span>涨跌幅</span>
                </div>
                {watchSymbols.length ? (
                  watchSymbols.map((s) => {
                    const t =
                      snapshot.ticks.find((t: Row) => t.vt_symbol === s) || {};
                    const c =
                      snapshot.contracts.find((c: Row) => c.vt_symbol === s) ||
                      {};
                    return (
                      <div
                        className={`watch-item ${s === symbol ? "selected" : ""}`}
                        key={s}
                        data-symbol={s}
                      >
                        <button
                          className="watch-open"
                          aria-label={`查看行情 ${s}`}
                          onClick={() => {
                            setSymbol(s);
                            setPrice(null);
                          }}
                        >
                          <div>
                            <strong>{s.split(".")[0]}</strong>
                            <small title={c.name || t.name || s}>
                              {c.name || t.name || s.split(".")[1]}
                            </small>
                          </div>
                          <div
                            className={
                              t.last_price == null
                                ? "muted"
                                : t.last_price >= t.pre_close
                                  ? "up"
                                  : "down"
                            }
                          >
                            <strong>{num(t.last_price)}</strong>
                            <small>
                              {t.pre_close
                                ? `${((t.last_price / t.pre_close - 1) * 100).toFixed(2)}%`
                                : "—"}
                            </small>
                          </div>
                        </button>
                        <Tooltip title="移出自选">
                          <button
                            className="watch-remove"
                            aria-label={`移出自选 ${s}`}
                            disabled={busy}
                            onClick={async () => {
                              const r = await action(`/watchlist/${s}/remove`);
                              if (r && symbol === s) {
                                setSymbol(r.result?.symbols?.[0] || "");
                                setPrice(null);
                                setChart({ bars: [] });
                              }
                            }}
                          >
                            <CloseOutlined />
                          </button>
                        </Tooltip>
                      </div>
                    );
                  })
                ) : (
                  <div className="watch-empty">
                    <ApiOutlined />
                    <p>暂无自选合约</p>
                  </div>
                )}
                <div className="watch-note">
                  <i className="dot online" />
                  实时行情来自 CTP
                  <br />
                  <span>休市期间可能没有新报价</span>
                </div>
              </section>
              <section className="panel chart-panel">
                <div className="chart-top">
                  <div>
                    <h2>
                      {symbol || "选择一个合约"} <small>{contract?.name}</small>
                    </h2>
                    <span className="muted">
                      {tick
                        ? `最新 ${num(tick.last_price)} · 持仓量 ${num(tick.open_interest, 0)}`
                        : "订阅行情，开始观察市场"}
                    </span>
                  </div>
                  <Tag>
                    {chartMode === "intraday" ? "分时" : `${minutes} 分钟`}
                  </Tag>
                </div>
                <div className="chart-tools">
                  <Space size={2}>
                    <button
                      className={`period ${chartMode === "intraday" ? "selected" : ""}`}
                      onClick={() => setChartMode("intraday")}
                    >
                      分时
                    </button>
                    {[1, 5, 15, 30, 60].map((m) => (
                      <button
                        key={m}
                        className={`period ${chartMode === "candles" && minutes === m ? "selected" : ""}`}
                        onClick={() => {
                          setChartMode("candles");
                          setMinutes(m);
                        }}
                      >
                        {m}m
                      </button>
                    ))}
                  </Space>
                  {chartMode === "candles" && (
                    <>
                      <Checkbox.Group
                        options={["MA", "EMA", "BOLL"]}
                        value={overlays}
                        onChange={(v) => setOverlays(v as string[])}
                      />
                      <Select
                        size="small"
                        value={oscillator}
                        onChange={setOscillator}
                        options={["MACD", "RSI", "ATR", "OI", "NONE"].map(
                          (v) => ({
                            value: v,
                            label:
                              v === "OI"
                                ? "持仓量"
                                : v === "NONE"
                                  ? "无副图"
                                  : v,
                          }),
                        )}
                      />
                      <Button
                        size="small"
                        type="text"
                        icon={<SettingOutlined />}
                        aria-label="指标参数"
                        onClick={() => setIndicatorOpen(true)}
                      />
                    </>
                  )}
                  {chartMode === "intraday" && (
                    <span className="intraday-legend">
                      价格 <b>━━</b> · 均价 <em>━━</em> · 成交量
                    </span>
                  )}
                  <Checkbox
                    checked={showTrades}
                    onChange={(e) => setShowTrades(e.target.checked)}
                  >
                    成交点
                  </Checkbox>
                </div>
                {chartMode === "intraday" ? (
                  <div className="chart-dates">
                    <Input
                      type="date"
                      size="small"
                      aria-label="分时日期"
                      value={intradayDate || chart.date || ""}
                      onChange={(e) => setIntradayDate(e.target.value)}
                    />
                    <Button
                      size="small"
                      type="text"
                      onClick={() => setIntradayDate("")}
                    >
                      今天
                    </Button>
                    <span className="muted">
                      上海时间 · 自然日（夜盘按日期分开）
                    </span>
                  </div>
                ) : (
                  <div className="chart-dates">
                    <Input
                      type="date"
                      size="small"
                      aria-label="图表起始日期"
                      value={historyStart}
                      onChange={(e) => setHistoryStart(e.target.value)}
                    />
                    <span className="muted">至</span>
                    <Input
                      type="date"
                      size="small"
                      aria-label="图表结束日期"
                      value={historyEnd}
                      onChange={(e) => setHistoryEnd(e.target.value)}
                    />
                    <Button
                      size="small"
                      type="text"
                      onClick={() => {
                        setHistoryStart("");
                        setHistoryEnd("");
                      }}
                    >
                      最近30天
                    </Button>
                  </div>
                )}
                <MarketChart
                  key={`${symbol}-${chartMode}-${minutes}-${intradayDate}-${historyStart}-${historyEnd}-${chartMode === "intraday" ? chart.date : ""}`}
                  data={chart}
                  overlays={overlays}
                  oscillator={oscillator}
                  intraday={chartMode === "intraday"}
                  minutes={minutes}
                  showTrades={showTrades}
                />
                <div className="chart-footer">
                  <span>
                    {chartMode === "intraday"
                      ? `均价：本地累计${chart.estimated ? "（含分钟收盘价加权估算）" : "成交额加权"} · 缺失时段不补齐`
                      : "数据源：本地录制 / CSV"}
                  </span>
                  {chartMode === "intraday" ? (
                    <span>
                      {chart.bars?.length || 0} 个分钟点 · 每 2 秒更新
                    </span>
                  ) : (
                    <span>
                      {chart.bars?.length || 0} 根 ·{" "}
                      {chart.bars?.at(-1)?.complete === false
                        ? "最新 K 线未完成"
                        : "已完成 K 线"}
                    </span>
                  )}
                </div>
              </section>
              <section className="panel order-panel">
                <div className="panel-title">
                  手动交易<Tag>限价</Tag>
                </div>
                <div className="quote-box">
                  <div>
                    <span>卖一</span>
                    <b className="down">{num(quotePrice(tick?.ask_price_1))}</b>
                    <small>{num(tick?.ask_volume_1, 0)}</small>
                  </div>
                  <div>
                    <span>买一</span>
                    <b className="up">{num(quotePrice(tick?.bid_price_1))}</b>
                    <small>{num(tick?.bid_volume_1, 0)}</small>
                  </div>
                </div>
                <div className="quote-status" data-testid="open-quote-status">
                  <Tag color={openQuoteFresh ? "green" : "default"}>
                    {!tick
                      ? "等待行情"
                      : openQuoteFresh
                        ? "行情更新中"
                        : "行情未更新"}
                  </Tag>
                  <span>行情时间 {quoteTime(tick)}</span>
                </div>
                <div className="order-form">
                  <label>交易方向</label>
                  <div className="direction">
                    <button
                      className={direction === "LONG" ? "buy selected" : "buy"}
                      onClick={() => setDirection("LONG")}
                    >
                      买入开多
                    </button>
                    <button
                      className={
                        direction === "SHORT" ? "sell selected" : "sell"
                      }
                      onClick={() => setDirection("SHORT")}
                    >
                      卖出开空
                    </button>
                  </div>
                  <label>委托价格</label>
                  <InputNumber
                    style={{ width: "100%" }}
                    min={contract?.pricetick || 0.01}
                    step={contract?.pricetick || 1}
                    value={price}
                    onChange={setPrice}
                    placeholder="留空使用对手价"
                  />
                  <div
                    className="counterparty-quote"
                    data-testid="open-counterparty"
                    aria-live="polite"
                  >
                    {price !== null
                      ? "手动限价"
                      : `${openQuoteFresh ? "" : "参考"}对手价（${opposite.side}）`}
                    <strong>{num(price ?? opposite.price)}</strong>
                    {price === null && opposite.price === null && (
                      <span>暂无对手报价，等待买卖盘更新或填写限价</span>
                    )}
                    {price === null &&
                      opposite.price !== null &&
                      !openQuoteFresh && (
                        <span>
                          行情超过 30 秒未更新，请填写限价或等待新行情
                        </span>
                      )}
                  </div>
                  <label>委托数量（手）</label>
                  <InputNumber
                    style={{ width: "100%" }}
                    min={1}
                    precision={0}
                    value={volume}
                    onChange={(v) => setVolume(v || 1)}
                  />
                  <div className="order-details">
                    <span>
                      合约乘数 <b>{num(contract?.size, 0)}</b>
                    </span>
                    <span>
                      最小变动 <b>{num(contract?.pricetick)}</b>
                    </span>
                  </div>
                  <Button
                    block
                    type="primary"
                    size="large"
                    className={
                      direction === "LONG" ? "buy-button" : "sell-button"
                    }
                    disabled={
                      !snapshot.ready ||
                      !symbol ||
                      busy ||
                      (price === null &&
                        (!openQuoteFresh || opposite.price === null))
                    }
                    onClick={confirmOrder}
                  >
                    {direction === "LONG" ? "买入开多" : "卖出开空"}
                  </Button>
                  <p className="muted order-tip">
                    限价委托不保证成交。
                    <br />
                    平仓请在下方持仓列表操作。
                  </p>
                </div>
              </section>
            </div>
            {operation && (
              <Alert
                className="operation"
                type={
                  operation.state === "unknown" || operation.state === "failed"
                    ? "warning"
                    : "info"
                }
                closable
                onClose={() => setOperation(null)}
                message={`${labels[operation.state] || operation.state} · ${operation.error || operation.message || operation.result?.message || operation.id}`}
                description={
                  <Space>
                    <span>{operation.orders?.join(" · ")}</span>
                    {operation.filled !== undefined && (
                      <span>
                        实际成交 {operation.filled} / {operation.requested} 手
                      </span>
                    )}
                    <Button
                      size="small"
                      onClick={async () => {
                        const r = await api(`/commands/${operation.id}`);
                        if (r) setOperation(r);
                        else message.info("尚无记录，请核对柜台订单");
                      }}
                    >
                      查询结果
                    </Button>
                  </Space>
                }
              />
            )}
            <section className="panel blotter">
              <Tabs
                items={[
                  {
                    key: "positions",
                    label: `持仓 · ${snapshot.positions.filter((p: Row) => p.volume).length}`,
                    children: (
                      <Table
                        size="small"
                        rowKey="vt_positionid"
                        columns={columns.positions}
                        dataSource={snapshot.positions.filter(
                          (p: Row) => p.volume,
                        )}
                        pagination={false}
                        scroll={{ x: 700 }}
                        locale={{ emptyText: "暂无持仓" }}
                      />
                    ),
                  },
                  {
                    key: "orders",
                    label: `委托 · ${snapshot.orders.length}`,
                    children: (
                      <Table
                        size="small"
                        rowKey="vt_orderid"
                        columns={columns.orders}
                        dataSource={[...snapshot.orders].reverse()}
                        pagination={{ pageSize: 8 }}
                        scroll={{ x: 800 }}
                      />
                    ),
                  },
                  {
                    key: "trades",
                    label: `成交 · ${snapshot.trades.length}`,
                    children: (
                      <Table
                        size="small"
                        rowKey="vt_tradeid"
                        columns={columns.trades}
                        dataSource={[...snapshot.trades].reverse()}
                        pagination={{ pageSize: 8 }}
                        scroll={{ x: 700 }}
                      />
                    ),
                  },
                  {
                    key: "operations",
                    label: "操作记录",
                    children: (
                      <Table
                        size="small"
                        rowKey="id"
                        dataSource={snapshot.operations || []}
                        pagination={{ pageSize: 8 }}
                        scroll={{ x: 800 }}
                        columns={[
                          {
                            title: "操作编号",
                            dataIndex: "id",
                            render: (v: string) => (
                              <code>{v.slice(0, 12)}</code>
                            ),
                          },
                          { title: "操作", dataIndex: "action" },
                          { title: "合约", dataIndex: "symbol" },
                          { title: "状态", dataIndex: "state", render: tagged },
                          {
                            title: "结果",
                            render: (_: unknown, r: Row) =>
                              r.error ||
                              r.submission_error ||
                              r.message ||
                              r.result?.message ||
                              r.orders?.join(", ") ||
                              "已受理",
                          },
                          {
                            title: "核对",
                            render: (_: unknown, r: Row) => (
                              <Space>
                                <Button
                                  size="small"
                                  onClick={() => setOperation(r)}
                                >
                                  查看
                                </Button>
                                {r.symbol && r.state === "unknown" && (
                                  <Button
                                    size="small"
                                    onClick={() =>
                                      modal.confirm({
                                        title: "确认已核对柜台委托和持仓？",
                                        content:
                                          "确认活动委托已结束后解除操作限制。仓位不一致的策略继续停止，可通过持仓列表平仓；不会重新提交订单。",
                                        onOk: () =>
                                          action(`/reconcile/${r.symbol}`),
                                      })
                                    }
                                  >
                                    核对状态
                                  </Button>
                                )}
                              </Space>
                            ),
                          },
                        ]}
                      />
                    ),
                  },
                  {
                    key: "logs",
                    label: "运行日志",
                    children: (
                      <div className="logs">
                        {snapshot.logs.map((l: Row, i: number) => (
                          <div key={i}>
                            <time>{l.time?.slice(11, 19) || "—"}</time>
                            {l.msg}
                          </div>
                        ))}
                      </div>
                    ),
                  },
                ]}
              />
            </section>
          </>
        )}
        {page === "strategy" && (
          <StrategyView snapshot={snapshot} action={action} />
        )}
        {page === "backtest" && <BacktestView snapshot={snapshot} />}
        {page === "research" && <ResearchView />}
        {page === "data" && (
          <DataView
            onView={(row) => {
              setChartMode("candles");
              setSymbol(`${row.symbol}.${row.exchange}`);
              const end = new Date(row.end);
              const start = new Date(
                Math.max(
                  new Date(row.start).getTime(),
                  end.getTime() - 30 * 86400000,
                ),
              );
              setHistoryStart(start.toISOString().slice(0, 10));
              setHistoryEnd(row.end.slice(0, 10));
              setMinutes(1);
              setPage("trade");
            }}
          />
        )}
        <footer>
          <span>
            <i className="dot online" /> SIMNOW · 模拟环境
          </span>
          <span>vn.py 4.4 / CTP 6.7 · Workbench 0.1</span>
        </footer>
      </div>
      <Modal
        title={`平仓 · ${closing?.vt_symbol || ""}`}
        open={!!closing}
        onCancel={() => setClosing(null)}
        confirmLoading={busy}
        okButtonProps={{
          disabled:
            !snapshot.ready ||
            (closePrice === null &&
              (!closeQuoteFresh || closeOpposite.price === null)),
        }}
        okText="确认停止策略并平仓"
        cancelText="取消"
        onOk={async () => {
          const r = await action("/close", {
            symbol: closing?.vt_symbol,
            direction: closing?.direction,
            volume: closeVolume,
            price: closePrice,
          });
          if (r) setClosing(null);
        }}
      >
        <Alert
          type="info"
          showIcon
          message="会先停止该合约策略并撤销活动委托，核对持仓后提交平仓。成交后策略需重新初始化。"
        />
        <div className="modal-fields">
          <p>
            方向：{labels[closing?.direction]} · 持仓：{closing?.volume} ·
            冻结：{closing?.frozen}
          </p>
          <label>平仓手数</label>
          <InputNumber
            min={1}
            max={closing?.volume}
            precision={0}
            value={closeVolume}
            onChange={(v) => setCloseVolume(v || 1)}
          />
          <label>平仓限价（留空使用对手价）</label>
          <InputNumber min={0.01} value={closePrice} onChange={setClosePrice} />
          <div className="quote-status" data-testid="close-quote-status">
            <Tag color={closeQuoteFresh ? "green" : "default"}>
              {!closeTick
                ? "等待行情"
                : closeQuoteFresh
                  ? "行情更新中"
                  : "行情未更新"}
            </Tag>
            <span>行情时间 {quoteTime(closeTick)}</span>
          </div>
          <div className="counterparty-quote" data-testid="close-counterparty">
            {closePrice !== null
              ? "手动限价"
              : `${closeQuoteFresh ? "" : "参考"}对手价（${closeOpposite.side}）`}
            <strong>{num(closePrice ?? closeOpposite.price)}</strong>
            {closePrice === null && closeOpposite.price === null && (
              <span>暂无对手报价，等待买卖盘更新或填写限价</span>
            )}
            {closePrice === null &&
              closeOpposite.price !== null &&
              !closeQuoteFresh && (
                <span>行情超过 30 秒未更新，请填写限价或等待新行情</span>
              )}
          </div>
        </div>
      </Modal>
      <Modal
        title="指标参数"
        open={indicatorOpen}
        footer={null}
        onCancel={() => setIndicatorOpen(false)}
      >
        <Form
          layout="vertical"
          initialValues={indicatorParams}
          onFinish={(v) => {
            setIndicatorParams(v);
            setIndicatorOpen(false);
          }}
        >
          <div className="parameter-grid">
            {Object.keys(indicatorParams).map((k) => (
              <Form.Item key={k} name={k} label={k.toUpperCase()}>
                <InputNumber
                  min={k === "dev" ? 0.1 : 2}
                  max={k === "dev" ? 10 : 500}
                />
              </Form.Item>
            ))}
          </div>
          <Button type="primary" htmlType="submit">
            应用参数
          </Button>
        </Form>
      </Modal>
    </div>
  );
}

function StrategyView({
  snapshot,
  action,
}: {
  snapshot: Row;
  action: (path: string, body?: unknown) => Promise<any>;
}) {
  const { message, modal } = AntApp.useApp();
  const [templates, setTemplates] = useState<Row>({}),
    [versions, setVersions] = useState<Row[]>([]),
    [source, setSource] = useState(""),
    [editorEpoch, setEditorEpoch] = useState(0),
    [name, setName] = useState("我的双均线策略"),
    [selected, setSelected] = useState<string>(),
    [create, setCreate] = useState(false),
    [form] = Form.useForm();
  const reload = async () => setVersions(await api("/versions"));
  useEffect(() => {
    api("/templates").then((t) => {
      setTemplates(t);
      setSource(Object.values(t)[0] as string);
      setEditorEpoch((n) => n + 1);
    });
    void reload();
  }, []);
  const current = versions.find((v) => v.id === selected);
  const published = versions.filter((v) => v.published);
  const run = async (fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e) {
      message.error(String(e));
    }
  };
  return (
    <>
      <div className="strategy-grid">
        <section className="panel editor-panel">
          <div className="panel-title">
            策略编辑器 <Tag>Python</Tag>
          </div>
          <div className="editor-toolbar">
            <Input
              aria-label="策略名称"
              value={name}
              onChange={(e) => setName(e.target.value)}
              style={{ width: 190 }}
            />
            <Select
              placeholder="从模板创建"
              style={{ width: 150 }}
              options={Object.keys(templates).map((k) => ({
                value: k,
                label: k,
              }))}
              onChange={(v) => {
                setSource(templates[v]);
                setEditorEpoch((n) => n + 1);
                setName(`我的${v}策略`);
                setSelected(undefined);
              }}
            />
            <Select
              placeholder="打开已保存版本"
              style={{ width: 210 }}
              value={selected}
              options={versions.map((v) => ({
                value: v.id,
                label: `${v.name} · ${v.id.slice(0, 6)}${v.published ? " 已发布" : ""}`,
              }))}
              onChange={(v) => {
                const doc = versions.find((x) => x.id === v)!;
                setSource(doc.source);
                setEditorEpoch((n) => n + 1);
                setName(doc.name);
                setSelected(v);
              }}
            />
          </div>
          <Editor
            key={editorEpoch}
            height="440px"
            language="python"
            theme="vs-dark"
            defaultValue={source}
            onChange={(v) => setSource(v || "")}
            options={{
              minimap: { enabled: false },
              fontSize: 13,
              padding: { top: 16 },
              scrollBeyondLastLine: false,
              automaticLayout: true,
            }}
          />
          <div className="editor-actions">
            <span className="muted">版本发布后不可修改 · 保存将创建新版本</span>
            <Space>
              <Button
                onClick={() =>
                  run(async () => {
                    await api("/versions/check", "POST", { name, source });
                    message.success("Python 语法检查通过");
                  })
                }
              >
                检查语法
              </Button>
              <Button
                onClick={() =>
                  run(async () => {
                    const v = await api("/versions", "POST", { name, source });
                    await reload();
                    setSelected(v.id);
                    message.success("新版本已保存");
                  })
                }
              >
                保存版本
              </Button>
              <Button
                type="primary"
                disabled={
                  !current || current.published || current.source !== source
                }
                onClick={() =>
                  modal.confirm({
                    title: "发布这个策略版本？",
                    content:
                      "发布会加载你的 Python 代码；现有实例仍使用原版本。",
                    okText: "发布",
                    cancelText: "取消",
                    onOk: async () => {
                      await action(`/versions/${selected}/publish`);
                      await reload();
                    },
                  })
                }
              >
                发布版本
              </Button>
            </Space>
          </div>
        </section>
        <section className="panel guide-panel">
          <div className="eyebrow">STRATEGY WORKFLOW</div>
          <h2>从想法到执行</h2>
          {[
            [
              "01",
              "编写与检查",
              "继承 WorkbenchStrategy，在 on_signal 中编写信号逻辑。",
            ],
            ["02", "保存与发布", "保存代码版本，再发布以供实例和回测选择。"],
            [
              "03",
              "初始化与启动",
              "载入历史数据预热；核对仓位后启动模拟交易。",
            ],
          ].map(([n, title, body]) => (
            <div className="step" key={n}>
              <span>{n}</span>
              <div>
                <h3>{title}</h3>
                <p>{body}</p>
              </div>
            </div>
          ))}
          <Alert
            type="info"
            message="一个合约，一个策略"
            description="手动平仓会停止策略。更新代码时，停止并移除空仓实例，再选择新版本创建。"
          />
        </section>
      </div>
      <section className="panel instances">
        <div className="panel-title">
          策略实例{" "}
          <Button
            icon={<PlayCircleOutlined />}
            aria-label="创建实例"
            type="primary"
            onClick={() => {
              form.resetFields();
              setCreate(true);
            }}
            disabled={!published.length}
          >
            创建实例
          </Button>
        </div>
        <Table
          rowKey="name"
          dataSource={snapshot.strategies}
          pagination={false}
          scroll={{ x: 850 }}
          columns={[
            { title: "实例", dataIndex: "name" },
            { title: "合约", dataIndex: "symbol" },
            {
              title: "版本",
              dataIndex: "version",
              render: (v: string) => v.slice(0, 8),
            },
            { title: "持仓", dataIndex: "pos" },
            {
              title: "状态",
              render: (_: unknown, s: Row) => (
                <Tooltip title={s.error || JSON.stringify(s.variables)}>
                  <Tag
                    color={s.trading ? "green" : s.error ? "red" : "default"}
                  >
                    {s.trading
                      ? "运行中"
                      : s.initializing
                        ? "初始化中"
                        : s.needs_init
                          ? "待初始化"
                          : s.ready
                            ? "已就绪"
                            : "已停止"}
                  </Tag>
                </Tooltip>
              ),
            },
            {
              title: "操作",
              render: (_: unknown, s: Row) => (
                <Space>
                  <Button
                    size="small"
                    disabled={s.trading || s.initializing}
                    onClick={() =>
                      action(`/instances/${encodeURIComponent(s.name)}/init`)
                    }
                  >
                    初始化
                  </Button>
                  <Button
                    size="small"
                    icon={
                      s.trading ? (
                        <PauseCircleOutlined />
                      ) : (
                        <PlayCircleOutlined />
                      )
                    }
                    disabled={!s.trading && (!s.ready || s.needs_init)}
                    onClick={() =>
                      action(
                        `/instances/${encodeURIComponent(s.name)}/${s.trading ? "stop" : "start"}`,
                      )
                    }
                  >
                    {s.trading ? "停止" : "启动"}
                  </Button>
                  <Button
                    size="small"
                    danger
                    disabled={s.trading || !!s.pos}
                    onClick={() =>
                      modal.confirm({
                        title: `移除实例 ${s.name}？`,
                        onOk: () =>
                          action(
                            `/instances/${encodeURIComponent(s.name)}/remove`,
                          ),
                      })
                    }
                  >
                    移除
                  </Button>
                </Space>
              ),
            },
          ]}
          expandable={{
            expandedRowRender: (s) => (
              <pre>
                {JSON.stringify(
                  { 参数: s.parameters, 变量: s.variables, 错误: s.error },
                  null,
                  2,
                )}
              </pre>
            ),
          }}
        />
      </section>
      <StrategyMonitor snapshot={snapshot} />
      <Modal
        title="创建策略实例"
        open={create}
        onCancel={() => setCreate(false)}
        footer={null}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (v) => {
            try {
              v.parameters = JSON.parse(v.parameters || "{}");
              const r = await action("/instances", v);
              if (r) setCreate(false);
            } catch (e) {
              message.error(String(e));
            }
          }}
        >
          <Form.Item name="name" label="实例名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="version"
            label="已发布版本"
            rules={[{ required: true }]}
          >
            <Select
              options={published.map((v) => ({
                value: v.id,
                label: `${v.name} · ${v.id.slice(0, 8)}`,
              }))}
              onChange={(id) =>
                form.setFieldValue(
                  "parameters",
                  JSON.stringify(
                    versions.find((v) => v.id === id)?.parameters,
                    null,
                    2,
                  ),
                )
              }
            />
          </Form.Item>
          <Form.Item
            name="symbol"
            label="交易合约"
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              options={snapshot.contracts.map((c: Row) => ({
                value: c.vt_symbol,
                label: c.vt_symbol,
              }))}
            />
          </Form.Item>
          <Form.Item name="parameters" label="参数（JSON）">
            <Input.TextArea rows={6} />
          </Form.Item>
          <Button block type="primary" htmlType="submit">
            创建实例
          </Button>
        </Form>
      </Modal>
    </>
  );
}

function BacktestView({ snapshot }: { snapshot: Row }) {
  const { message } = AntApp.useApp();
  const [versions, setVersions] = useState<Row[]>([]),
    [jobs, setJobs] = useState<Row[]>([]),
    [selected, setSelected] = useState<string>(),
    [form] = Form.useForm(),
    [loading, setLoading] = useState(false);
  const [showTradeLabels, setShowTradeLabels] = useState(false);
  useEffect(() => {
    api("/versions").then((v) =>
      setVersions(v.filter((x: Row) => x.published)),
    );
    let live = true;
    const poll = () =>
      api("/backtests")
        .then((r) => {
          if (live) setJobs(r);
        })
        .catch(() => {});
    void poll();
    const t = setInterval(poll, 3000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);
  const job = jobs.find((j) => j.id === selected) || jobs[0];
  return (
    <div className="backtest-grid">
      <section className="panel backtest-form">
        <div className="panel-title">回测配置</div>
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            start: "",
            end: "",
            rate: 0.0001,
            slippage: 1,
            size: 10,
            pricetick: 1,
            capital: 1000000,
            settings: "{}",
          }}
          onFinish={async (v) => {
            setLoading(true);
            try {
              v.settings = JSON.parse(v.settings);
              v.start += "T00:00:00+08:00";
              v.end += "T23:59:59+08:00";
              const r = await api("/backtests", "POST", v);
              setSelected(r.id);
              setJobs((j) => [r, ...j]);
              message.success("回测任务已提交");
            } catch (e) {
              message.error(String(e));
            } finally {
              setLoading(false);
            }
          }}
        >
          <Form.Item
            name="version"
            label="策略版本"
            rules={[{ required: true }]}
          >
            <Select
              placeholder="选择已发布版本"
              options={versions.map((v) => ({
                value: v.id,
                label: `${v.name} · ${v.id.slice(0, 6)}`,
              }))}
              onChange={(id) =>
                form.setFieldValue(
                  "settings",
                  JSON.stringify(
                    versions.find((v) => v.id === id)?.parameters,
                    null,
                    2,
                  ),
                )
              }
            />
          </Form.Item>
          <Form.Item
            name="symbol"
            label="合约代码（如 rb2610.SHFE）"
            rules={[{ required: true }]}
          >
            <Input list="contracts" />
          </Form.Item>
          <datalist id="contracts">
            {snapshot.contracts.map((c: Row) => (
              <option key={c.vt_symbol} value={c.vt_symbol} />
            ))}
          </datalist>
          <Form.Item name="start" label="开始日期" rules={[{ required: true }]}>
            <Input type="date" />
          </Form.Item>
          <Form.Item name="end" label="结束日期" rules={[{ required: true }]}>
            <Input type="date" />
          </Form.Item>
          <div className="parameter-grid">
            {[
              ["rate", "手续费率"],
              ["slippage", "滑点（价格）"],
              ["size", "合约乘数"],
              ["pricetick", "价格步长"],
              ["capital", "初始资金"],
            ].map(([k, l]) => (
              <Form.Item key={k} name={k} label={l}>
                <InputNumber
                  min={k === "rate" || k === "slippage" ? 0 : 0.000001}
                />
              </Form.Item>
            ))}
          </div>
          <Form.Item name="settings" label="策略参数（JSON，含周期）">
            <Input.TextArea rows={6} />
          </Form.Item>
          <Button
            block
            type="primary"
            htmlType="submit"
            loading={loading}
            disabled={jobs.some((j) => ["queued", "running"].includes(j.state))}
            icon={<ExperimentOutlined />}
          >
            开始回测
          </Button>
          <p className="muted">
            需要开始日期之前的预热数据。每次只运行一个任务，不影响模拟交易。
          </p>
        </Form>
      </section>
      <div>
        <section className="panel result-panel">
          <div className="panel-title">
            回测结果
            <Select
              style={{ width: 270 }}
              placeholder="选择回测记录"
              value={job?.id}
              onChange={setSelected}
              options={jobs.map((j) => ({
                value: j.id,
                label: `${j.parameters.symbol} · ${j.created.slice(0, 16)} · ${labels[j.state]}`,
              }))}
            />
          </div>
          {!job ? (
            <Empty description="配置策略和历史区间，开始第一次回测" />
          ) : (
            <>
              <Tag
                color={
                  job.state === "completed"
                    ? "green"
                    : job.state === "failed"
                      ? "red"
                      : "blue"
                }
              >
                {labels[job.state]}
              </Tag>
              {job.error && <Alert type="error" message={job.error} />}
              <div className="result-metrics">
                {[
                  ["total_return", "总收益 %"],
                  ["max_ddpercent", "最大回撤 %"],
                  ["sharpe_ratio", "夏普比率"],
                  ["total_trade_count", "成交笔数"],
                ].map(([k, l]) => (
                  <div className="metric" key={k}>
                    <span>{l}</span>
                    <strong>{num(job.statistics?.[k])}</strong>
                  </div>
                ))}
              </div>
              {job.equity?.length ? (
                <EquityChart rows={job.equity} />
              ) : (
                <Empty
                  description={
                    job.state === "running"
                      ? `运行中 ${Math.round((job.progress || 0) * 100)}%`
                      : "尚无资金曲线"
                  }
                />
              )}
              {!!job.chart?.bars?.length && (
                <>
                  <div className="chart-replay-heading">
                    <h3>成交回放</h3>
                    <Checkbox
                      checked={showTradeLabels}
                      onChange={(e) => setShowTradeLabels(e.target.checked)}
                    >
                      成交标签
                    </Checkbox>
                  </div>
                  <MarketChart
                    key={job.id}
                    data={{ ...job.chart, trades: job.trades }}
                    overlays={[]}
                    oscillator="NONE"
                    minutes={job.chart.minutes}
                    showTradeLabels={showTradeLabels}
                  />
                  <p className="muted">
                    买入 ↑ · 卖出 ↓ · 最近 {job.chart.bars.length} 根 K 线
                  </p>
                </>
              )}
              <p className="muted">
                {job.code_hash &&
                  `代码版本 ${job.code_hash.slice(0, 12)} · ${job.bar_count} 根回测数据 · ${job.warmup_count} 根预热数据`}
              </p>
            </>
          )}
        </section>
        {job && (
          <section className="panel blotter">
            <Tabs
              items={[
                {
                  key: "trades",
                  label: "成交明细",
                  children: (
                    <Table
                      rowKey="vt_tradeid"
                      size="small"
                      dataSource={job.trades || []}
                      pagination={{ pageSize: 10 }}
                      scroll={{ x: 650 }}
                      columns={[
                        { title: "时间", dataIndex: "datetime" },
                        {
                          title: "方向",
                          dataIndex: "direction",
                          render: tagged,
                        },
                        { title: "开平", dataIndex: "offset", render: tagged },
                        { title: "价格", dataIndex: "price", render: cell },
                        { title: "数量", dataIndex: "volume" },
                      ]}
                    />
                  ),
                },
                {
                  key: "logs",
                  label: "任务日志",
                  children: (
                    <pre className="logs">
                      {job.logs?.join("\n") || "等待任务输出"}
                    </pre>
                  ),
                },
              ]}
            />
          </section>
        )}
      </div>
    </div>
  );
}

function DataView({ onView }: { onView: (row: Row) => void }) {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState<Row[]>([]),
    [busy, setBusy] = useState(false);
  const refresh = () =>
    api("/data")
      .then(setRows)
      .catch((e) => message.error(String(e)));
  useEffect(() => {
    void refresh();
  }, []);
  function template() {
    const blob = new Blob(
      ["symbol,exchange,datetime,open,high,low,close,volume,open_interest\n"],
      { type: "text/csv" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bars-template.csv";
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <>
      <div className="data-intro">
        <div>
          <div className="eyebrow">BUILD YOUR MARKET HISTORY</div>
          <h2>让每一根 K 线成为研究的起点</h2>
          <p>
            CTP 提供实时行情；订阅后自动录制一分钟 K
            线。导入已有历史数据，即可用于指标预热和策略回测。
          </p>
        </div>
        <DatabaseOutlined />
      </div>
      <section className="panel">
        <div className="panel-title">
          导入历史行情
          <Space>
            <Button onClick={template}>下载 CSV 模板</Button>
            <Upload
              accept=".csv"
              showUploadList={false}
              beforeUpload={(file) => {
                setBusy(true);
                const data = new FormData();
                data.append("file", file);
                api("/data/import", "POST", data)
                  .then((r) => {
                    message.success(`已导入 ${r.result.count} 根 K 线`);
                    void refresh();
                  })
                  .catch((e) => message.error(String(e)))
                  .finally(() => setBusy(false));
                return false;
              }}
            >
              <Button type="primary" icon={<UploadOutlined />} loading={busy}>
                导入 CSV
              </Button>
            </Upload>
          </Space>
        </div>
        <div className="data-help">
          <p>
            UTF-8 编码，一行一分钟，时间为 K
            线开始时间，默认上海时区。相同合约和时间会覆盖；不自动填补缺失行情。
          </p>
          <code>
            symbol, exchange, datetime, open, high, low, close, volume,
            open_interest
          </code>
          <p className="muted">
            示例时间：2026-09-28T21:00:00+08:00 · 支持
            SHFE、DCE、CZCE、CFFEX、INE、GFEX · 单次最多 20 MB / 20 万行
          </p>
        </div>
      </section>
      <section className="panel data-table">
        <div className="panel-title">
          本地数据概览
          <Button icon={<ReloadOutlined />} onClick={refresh}>
            刷新
          </Button>
        </div>
        <Table
          rowKey={(r) => `${r.symbol}.${r.exchange}.${r.interval}`}
          dataSource={rows}
          columns={[
            { title: "合约", dataIndex: "symbol" },
            { title: "交易所", dataIndex: "exchange" },
            {
              title: "查看",
              render: (_: unknown, row: Row) => (
                <Button size="small" onClick={() => onView(row)}>
                  查看K线
                </Button>
              ),
            },
            { title: "周期", dataIndex: "interval" },
            { title: "记录数", dataIndex: "count", render: cell },
            { title: "开始时间", dataIndex: "start" },
            { title: "结束时间", dataIndex: "end" },
          ]}
          locale={{ emptyText: "暂无历史数据，开始录制或导入 CSV" }}
        />
      </section>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      button={{ autoInsertSpace: false }}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: "#47c7b0",
          colorBgBase: "#0b1220",
          colorBgContainer: "#111a28",
          colorBorder: "#2a374c",
          colorText: "#dce5f2",
          colorTextSecondary: "#8292aa",
          borderRadius: 7,
          fontFamily:
            'Inter, "Workbench CJK", "Noto Sans SC", system-ui, sans-serif',
        },
        components: {
          Table: { headerBg: "#152031", rowHoverBg: "#19283b" },
          Tabs: { inkBarColor: "#47c7b0" },
        },
      }}
    >
      <AntApp>
        <Workbench />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
