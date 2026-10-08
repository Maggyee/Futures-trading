# 交付验收报告：开盘强弱选品 + 多周期平稳波段

交付日期：2026-10-03。工作目录：`/home/nishiki/vnpy-ctp`。

已交付离线数据导入、真实合约池、固定开盘候选、共享因果信号、组合撮合、实验、审计报告及只读网页。真实策略收益验证尚未完成，原因是数据和执行元数据不足；没有连接账户、发送真实委托或重启现有交易服务。

## 实际文件

新增研究代码：

- `research/__init__.py`、`__main__.py`：包及命令行。
- `research/config.py`、`calendar.py`：配置、生效口径、历史日历及独立时间事件。
- `research/data.py`：CSV/gzip CSV、Parquet、只读 vn.py SQLite、质量审计及前日 OI 选合约。
- `research/signals.py`：SMA、Wilder ATR、完整5/15分钟聚合、冻结排名、四种入场和独立拒绝原因。
- `research/execution.py`：固定风险预算、预占额度、六状态、下一开盘成交、固定保护、费用、强平和未平风险。
- `research/experiments.py`：Top-K三资金场景、入场比较、单因素敏感性/消融、训练校准、滚动验证、冻结与锁定测试。
- `research/reporting.py`：JSON/CSV、中文报告、独立HTML图、排名贡献和可复现案例。
- `research/fixtures.py`：逐行标记 `SYNTHETIC_TEST_ONLY` 的工程数据。
- `research/vnpy_adapter.py`：实际安装版组合模板及共享信号桥；委托路由显式禁用。

新增配置和文档：`research/examples/{config.json,metadata.json,calendar.json,data_template.csv}`、`research/README.md`、`research/vnpy_engine_review.md`、`research/source_audit.json`、`research/ENVIRONMENT.json`、`research/requirements.research.lock`、本报告及 `validation_results.json`，根目录 `assumptions.md`、`known_limitations.md`。

网页/API：新增 `backend/research_results.py`、`frontend/src/ResearchView.tsx`；修改 `backend/api.py`、`frontend/src/main.tsx`、`frontend/src/style.css`。登录后“研究”页只读结果、曲线和下载，页内无连接交易动作。新增 `frontend/e2e/research.spec.ts`。

测试：新增 `tests/test_research.py`、`tests/test_research_integration.py`。其他集成改动：根目录 `README.md`、`pyproject.toml`、`.gitignore`。前端已构建；现有服务需按原有部署流程重载 API 后显示新增接口。

完整路径、源码文件 SHA256、执行结果和产物位置见 [validation_results.json](validation_results.json)。生成结果保留于 `research_outputs/`，已加入忽略规则；历史尝试没有删除。

## 环境与 API 核对

Python 3.12.3、vn.py 4.4.0、vnpy_ctp 6.7.11.4、vnpy_ctastrategy 1.4.1、vnpy_sqlite 1.1.3；本次安装 vnpy_portfoliostrategy 1.3.0 和 pyarrow 24.0.0。numpy 2.5.3、pandas 3.0.6、TA-Lib 0.8.1、Plotly 7.1.0。`pip check` 通过。

已读取用户指定四处官方资料，并对照本机源码核实 `on_bars`、`update_trade`、`update_order`、`send_order`。签名、来源和哈希见 [source_audit.json](source_audit.json)。原生组合回测包含缺行情时的旧价补齐撮合，且不满足本项目日期费用/固定保护/独立定时要求，因此使用隔离研究执行模块，未修改第三方库。差异说明见 [vnpy_engine_review.md](vnpy_engine_review.md)。

当前目录没有 Git 根仓库。最终研究 Python 源码指纹为 `4a46e096b247f8f232926753376d11a5f6630f780c5a2282ebc55bcee5d0fc20`；每个实验另存配置、行情、逐文件哈希、源码归档、依赖版本和随机种子 20261002。

## 实际测试及命令结果

全量测试：`104 passed`，81.42秒。之后仅收紧滚动报告的样本资格，并新增一项测试；最终研究回归：`46 passed`，40.90秒。二者合并为105个不同用例：原有59项、研究46项。最后一项修改不改变交易信号或撮合。原始机器结果分别保存在 `research_outputs/validation_python.xml`、`validation_research_final.xml`。

```bash
.venv/bin/python -m pytest tests -q --junitxml=research_outputs/validation_python.xml
.venv/bin/python -m pytest tests/test_research.py tests/test_research_integration.py -q --junitxml=research_outputs/validation_research_final.xml
.venv/bin/ruff check backend research tests
```

Ruff通过。前端 TypeScript/Vite 构建和 Prettier 检查通过，七个浏览器用例已分批通过，包括研究页、下载/曲线、无交易动作及手机宽度。早先重负载并发执行时，原有 locale 子进程和原有完整浏览器流程各出现一次超时；顺序重跑均通过，最新全量 Python 测试亦通过。唯一保留警告是 FastAPI/Starlette TestClient 对旧 httpx 接口的弃用提示，不影响此次通过结果。

本次实际运行的研究命令如下；所有合成命令均只用于工程验收：

```bash
.venv/bin/python -m research backtest --config research_outputs/acceptance_fixture/config.json --synthetic --output research_outputs/final_baseline
.venv/bin/python -m research sweep-topk --config research_outputs/acceptance_fixture/config.json --synthetic --budget 24 --output research_outputs/acceptance_topk
.venv/bin/python -m research compare-entry --config research_outputs/acceptance_fixture/config.json --synthetic --budget 4 --output research_outputs/acceptance_entry
.venv/bin/python -m research walk-forward --config research_outputs/acceptance_fixture/config.json --synthetic --budget 8 --output research_outputs/acceptance_walk
.venv/bin/python -m research calibrate-ticks --config research_outputs/acceptance_fixture/config.json --synthetic --output research_outputs/acceptance_calibration
.venv/bin/python -m research freeze --run research_outputs/final_baseline/run_0003_eb6308332b00 --frozen research_outputs/acceptance_frozen_final.json
.venv/bin/python -m research backtest --config research_outputs/final_baseline/run_0003_eb6308332b00/config_snapshot.json --frozen research_outputs/acceptance_frozen_final.json --split test --scope shared --synthetic --output research_outputs/acceptance_final_test
.venv/bin/python -m research report --run research_outputs/acceptance_final_test/run_0002_ed767290d3fd
.venv/bin/python -m research validate-data --config research/examples/config.json --output research_outputs/real_data_audit
.venv/bin/python -m research backtest --config research/examples/config.json --output research_outputs/real_baseline
```

| 操作 | 实际结果 | 产物 |
| --- | --- | --- |
| 真实数据审计 | 2,829根日盘、0条解析错误、31项配置缺口；不表示数据完整 | real_data_audit/report.md、data_quality.json、daily_pool.json、pool_exclusions.json、normalized_bars.csv.gz |
| 真实正式回测 | 退出码1，明确失败，无收益结果 | real_baseline 中的失败实验、配置、源码/行情快照和中文报告 |
| 合成单次回测 | 成功、12笔完整成交、未平风险0 | final_baseline/run_0003_eb6308332b00 |
| Top-K | 24/24成功；八个K×商品/金融/共享三场景 | acceptance_topk/top_k_comparison.json、csv、md、html |
| 四种入场 | 4/4成功；direct有成交，三个回踩版本均0笔 | acceptance_entry/entry_comparison.json、csv、md、html |
| 滚动训练/验证 | 一窗、八个K；不读取锁定测试；合成样例择参证据资格为0 | acceptance_walk/walk_forward.json、fold_00训练候选与结果 |
| 训练跳数校准 | 成功；只读取训练截止日之前的数值 | acceptance_calibration/training_tick_candidates.json |
| 冻结及最终测试 | 成功；核验代码/配置/源文件指纹；仍只是合成验收 | acceptance_frozen_final.json、acceptance_final_test/run_0002_ed767290d3fd |
| 报告重建 | 成功；使用保存的 normalized_data 快照 | 最终实验 report.md、equity/drawdown/monthly及三类案例HTML |

## 自动验收覆盖

| 用户要求 | 对应验收 |
| --- | --- |
| 09:08/09:38独立、八分钟前禁止、不足K | group_cutoffs_eighth_minute_and_insufficient_k |
| 并列、冻结候选、不补选 | ties_candidate_lock_and_no_replacement |
| 主力不读当日OI、缺前日不借更早日 | real_contract_choice_uses_previous_day_not_future_oi |
| 完整5/15分钟、不跨休息 | no_partial_higher_bars_or_cross_break_aggregation |
| Wilder ATR、当前量不进放量基准 | atr_is_talib_wilder_and_current_volume_excluded |
| 做空增仓、当前15分钟 | short_requires_increasing_oi_and_current_15m |
| 午休不是缺分钟、真实缺分钟不补VWAP | session_vwap_continuity_excludes_recess_and_missing_actual_minute |
| MA10/MA20回踩分别有效、短侧对称 | both_pullback_references_and_short_symmetry、pullback_can_generate_real_unmocked_strategy_fill |
| 双触碰一次、事件不重复 | dual_touch_produces_one_event、next_open_fill_and_actual_fixed_ticks_and_single_pullback_event |
| MA10/20、OI、VWAP不作持仓退出 | ma10_ma20_vwap_oi_are_not_holding_exits |
| MA40接近定义对称 | ma40_approach_is_explicit_and_symmetric |
| 双触及止损优先、跳空实际价、入场前波幅无效 | ambiguous_stops_gaps_and_pre_entry_prices、gap_fill_uses_open_with_slippage_and_ambiguous_counter |
| 下一根open成交、禁开撤单、无新K线强平 | next_open_fill_and_actual_fixed_ticks_and_single_pullback_event、cutoff_cancels_pending_and_force_timer_without_bar_preserves_risk |
| 无法成交保留风险、禁止跨休息及时请求 | full_session_missing_tail_never_fakes_flatten、no_hold_across_break_exits_before_recess_and_records_unfilled_risk |
| 零量、休市、缺失、提前收市、换月 | zero_volume_missing_and_break_and_early_close、roll_uses_new_contract_history_only |
| 固定费/比例费、平今平昨、不重复扣滑点 | fee_modes_close_today_close_yesterday_no_double_slippage |
| K不扩预算、一手超额跳过 | top_k_never_increases_budget_and_one_lot_too_expensive_skipped |
| direct只在完整条件转换触发、额度恢复和跳空撤单后可恢复 | direct_transition_not_repeated_each_minute、direct_entry_becomes_eligible_when_portfolio_capacity_frees、direct_retries_after_gap_margin_recheck_cancels_entry |
| 改未来不改变过去候选/指标/信号/成交 | future_changes_do_not_change_past_candidates_features_signals_or_fills、simultaneous_open_fills_do_not_read_other_contract_intraminute_stop |
| 优化禁止读锁定集、未来坏价格不解析 | optimizer_cannot_access_locked_test_and_calibration_is_training_only、locked_prices_are_not_parsed_during_training |
| 缓存/格式/费用配置、重复/OI缺失/累计重置 | finished_indicator_cache_matches_uncached_causal_values、parquet_and_normalized_csv_roundtrip、csv_duplicate_oi_missing_end_timestamp_and_cumulative_reset、missing_fixed_ticks_blocks_formal_backtest |
| 只读数据库、vn.py同源信号、禁止委托、API认证与路径范围 | read_only_database_adapter、vnpy_bridge_parity_no_orders_and_completion_guard、research_api_authenticated_readonly_and_path_confined |
| 预算0不扩成默认、合成不能成为择参证据 | invalid_experiment_budget_never_expands_to_default、walk_forward_never_marks_synthetic_runs_as_selection_evidence |

## 真实数据覆盖与尚缺信息

原文件位于 `runtime/history/20260930-125229/`，五个CSV共5,115行，自然日范围2026-09-23至09-30。日盘实际保留：rb2701.SHFE 783、m2701.DCE 777、cu2611.SHFE 543、au2612.SHFE 363、sc2612.INE 363。各自起止及逐日缺失见 [真实审计报告](../research_outputs/real_data_audit/report.md)。

全部保留记录缺少 turnover；无金融组数据、同品种完整交割合约池、充足连续预热及样本外长度。夜盘无 trading_day 的记录被剔除；日盘自然日仅作明确代理。分钟起止/成交量口径及成交额换算须核实供应商；未核实的元数据和日历禁止正式回测。还缺上市/到期、保证金、实际生效的开仓/平今/平昨费用、逐品种固定止损止盈配置。

合成样例是四个工程代码，2026-01-05至01-09，训练前三日、验证01-08、锁定01-09。金额仅作软件算术核对：基准12笔、已实现净金额637.8、费用108；这不是实际市场收益。Top-K各K相同，因为各方向只有排名1。回踩零交易是因为没有突破上一根高/低的确认，不能据此评价回踩策略优劣。没有最佳K、稳定参数区间或有效真实样本外收益结论。

## 实现差异和验证边界

- 保留 vn.py组合数据/模板接口，使用隔离分钟研究撮合；没有修改原生库、假装原生支持CTA停止单，或实现实盘委托。
- 默认日盘；夜盘开盘选品接口预留，启用会明确拒绝。有夜盘映射时，可参与前日完整性和历史指标配置。
- OHLC固定保护、全量成交、固定不利滑点、分钟volume可交易代理均为明确近似；没有tick路径、盘口排队、部分成交或柜台确认。
- 保证金为名义金额比例，未建逐日结算或组合保证金优惠。不可成交时保留待平和风险，权益标记不视为成交。
- 固定止损止盈无真实默认跳数，只支持明确配置或训练候选冻结。MA10/20不承担退出，未加20分钟最长持仓或最近5分钟增仓。
- 报告给出实际排名贡献及旧排名因资金竞争发生的变化；未做完整无限资金反事实和交易日分块重采样。滚动结果不自动按最高收益选参；合成和少于配置交易日/交易次数的样本无择参证据资格。
- 宽松MA20变体不替换严格基准；本阶段尚未实现该额外实验。没有tick、模拟交易、实盘验收。

## 下一步真实接入命令

下面创建独立输入，不改工程样例；按真实资料编辑三个JSON、写入真实bars.csv。

```bash
mkdir -p research_inputs
cp research/examples/config.json research_inputs/config.json
cp research/examples/metadata.json research_inputs/metadata.json
cp research/examples/calendar.json research_inputs/calendar.json
cp research/examples/data_template.csv research_inputs/bars.csv
```

将 `data.sources` 改为 `[{"format":"csv","path":"bars.csv","provenance":"REAL_UNVERIFIED"}]`。CSV至少提供 datetime、trading_day、exchange、symbol、product、OHLC、volume、open_interest；优先补 turnover。用Parquet时改 format/path，或使用已说明的只读 vnpy_sqlite。补齐历史日历、全交割合约元数据和实际费用；核实后才置 verified=true。按真实覆盖编辑 splits，保留最后测试段。无成交额时须显式开启 approximate_vwap，报告会标为近似均价。

```bash
.venv/bin/python -m research validate-data --config research_inputs/config.json --output research_outputs/real_audit
.venv/bin/python -m research calibrate-ticks --config research_inputs/config.json --output research_outputs/calibration
.venv/bin/python -m research backtest --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --output research_outputs/baseline
.venv/bin/python -m research sweep-topk --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --budget 24 --output research_outputs/topk
.venv/bin/python -m research compare-entry --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --budget 4 --output research_outputs/entry
.venv/bin/python -m research walk-forward --config research_inputs/config.json --budget 32 --output research_outputs/walk
```

索引0只是预先声明的候选，不是最佳参数；已自行配置有效固定跳数时省略 calibration/tick-index。敏感性、消融、冻结最终测试、报告和网页部署命令详见 [README.md](README.md)。正式数据命令不加 synthetic。
