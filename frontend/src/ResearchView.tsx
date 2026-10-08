import { useEffect, useState } from "react";
import { Alert, Button, Empty, Select, Space, Table, Tag } from "antd";
import { api, num, type Row } from "./api";
import { EquityChart } from "./Chart";

export function ResearchView() {
  const [runs, setRuns] = useState<Row[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Row>();
  const [error, setError] = useState("");
  const reload = async () => {
    try {
      const result = await api<Row[]>("/research/runs");
      setRuns(result);
      setSelected((value) => value || result[0]?.path || "");
      setError("");
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    void reload();
  }, []);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    setDetail(undefined);
    api<Row>(`/research/run?path=${encodeURIComponent(selected)}`)
      .then((result) => {
        if (active) {
          setDetail(result);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, [selected]);
  const run = runs.find((r) => r.path === selected);
  const result = detail?.result;
  const stats = result?.metrics;
  const names: Record<string, string> = {
    "report.md": "中文报告",
    "trades.csv": "成交明细",
    "signals.csv": "信号审计",
    "daily_candidates.csv": "每日候选",
    "trades.csv.gz": "成交明细（压缩）",
    "signals.csv.gz": "信号审计（压缩）",
    "daily_candidates.csv.gz": "每日候选（压缩）",
    "data_quality.json": "数据质量",
    "equity.html": "权益图",
    "drawdown.html": "回撤图",
    "monthly.html": "月度图",
    "case_profit.html": "盈利案例",
    "case_loss.html": "亏损案例",
    "case_rejected.html": "未入场案例",
  };
  return (
    <section className="panel research-panel">
      <Space wrap>
        <h2>离线研究结果</h2>
        <Button onClick={reload}>刷新实验</Button>
      </Space>
      {error && <Alert type="error" message={error} showIcon />}
      {!runs.length ? (
        <Empty description="尚无离线研究结果" />
      ) : (
        <>
          <Select
            aria-label="选择研究实验"
            className="research-select"
            value={selected}
            onChange={setSelected}
            options={runs.map((r) => ({
              value: r.path,
              label: `${r.synthetic ? "工程样例" : "历史研究"} · ${r.scope} · K=${r.k} · ${r.entry_mode} · ${r.id}`,
            }))}
          />
          <Alert
            type={run?.synthetic ? "warning" : "info"}
            showIcon
            message={
              run?.synthetic
                ? "SYNTHETIC_TEST_ONLY：以下仅用于工程计算核对，不能用于策略收益验证。"
                : "分钟级历史研究：尚未完成 tick、模拟交易或实盘执行验证。"
            }
          />
          <p>
            {run?.window.start} 至 {run?.window.end} · {run?.split} ·{" "}
            <Tag>{result?.status || "读取中"}</Tag>
          </p>
          {result?.error && (
            <Alert type="error" message={result.error} showIcon />
          )}
          {(!!result?.unflattened_risk?.length ||
            !!result?.break_unflattened_risk?.length) && (
            <Alert
              type="error"
              showIcon
              message="存在未平仓风险，已发送平仓请求不代表持仓已归零。"
            />
          )}
          {stats && (
            <>
              <div className="monitor-metrics">
                <div>
                  <span>净盈亏</span>
                  <strong>{num(stats.net_profit)}</strong>
                </div>
                <div>
                  <span>最大回撤</span>
                  <strong>{num(stats.max_drawdown)}</strong>
                </div>
                <div>
                  <span>交易笔数</span>
                  <strong>{num(stats.trade_count, 0)}</strong>
                </div>
                <div>
                  <span>风控拒绝</span>
                  <strong>{num(stats.risk_rejection_count, 0)}</strong>
                </div>
              </div>
              {stats.sample_warning && (
                <Alert type="info" message={stats.sample_warning} showIcon />
              )}
              <EquityChart
                rows={stats.daily.map((r: Row) => ({
                  date: r.date,
                  balance: r.equity,
                  net_pnl: r.net_pnl,
                }))}
              />
            </>
          )}
          <Table
            size="small"
            rowKey="contract"
            dataSource={detail?.coverage || []}
            pagination={false}
            scroll={{ x: 640 }}
            columns={[
              { title: "实际合约", dataIndex: "contract" },
              { title: "分钟数", dataIndex: "rows" },
              { title: "开始", dataIndex: "start" },
              { title: "结束", dataIndex: "end" },
            ]}
          />
          <Space wrap>
            {(detail?.artifacts || []).map((name: string) => (
              <Button
                key={name}
                href={`/api/v1/research/artifact?path=${encodeURIComponent(selected)}&name=${encodeURIComponent(name)}`}
              >
                下载{names[name] || name}
              </Button>
            ))}
          </Space>
        </>
      )}
    </section>
  );
}
