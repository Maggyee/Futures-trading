# 开盘强弱选品 + 多周期平稳波段研究

这是现有 CTP Workbench 的独立离线研究包。核心不导入 CTP、不需要 Qt、不登录账户，不复用原 CTA 引擎的撮合假设。数据、信号、资金风控、撮合、实验和报告各有独立模块。现有网页增加只读“研究”页，研究命令不会调用交易 RPC。

本阶段交付分钟级研究工具和工程验收。真实数据不足时明确阻止正式回测；合成结果不能用于策略收益结论。

## 安装与工程闭环

在项目根目录执行。当前环境已具备研究依赖，并已安装可选组合扩展 1.3.0。

```bash
.venv/bin/python -m pip install -e '.[test,research,research-vnpy]'
# 只有使用 Parquet 才需要：
.venv/bin/python -m pip install -e '.[research-parquet]'

.venv/bin/python -m pytest tests -q
.venv/bin/ruff check backend research tests

# 每行都有 SYNTHETIC_TEST_ONLY；仅证明工程闭环
.venv/bin/python -m research make-fixture --synthetic --output research_outputs/demo_data
.venv/bin/python -m research validate-data --config research_outputs/demo_data/config.json --synthetic --output research_outputs/demo_audit
.venv/bin/python -m research backtest --config research_outputs/demo_data/config.json --synthetic --output research_outputs/demo_baseline
.venv/bin/python -m research sweep-topk --config research_outputs/demo_data/config.json --synthetic --budget 24 --output research_outputs/demo_topk
.venv/bin/python -m research compare-entry --config research_outputs/demo_data/config.json --synthetic --budget 4 --output research_outputs/demo_entry
.venv/bin/python -m research walk-forward --config research_outputs/demo_data/config.json --synthetic --budget 8 --output research_outputs/demo_walk
```

Top-K 默认为每个 K 分别做商品、金融、共享资金三场景，8×3=24 项；入场比较固定其他参数，仅四个版本。预算硬限制来自 `experiments.max_runs`，命令行只能缩小它。未执行项记录为 `skipped_budget`，失败尝试也保留；不只保存收益好的结果。`sensitivity` 是单因素实验与消融，不做大规模全组合。

命令返回 0 表示执行成功，1 表示校验/实验失败，2 表示调用或研究配置无效。`execution_risk` 仍会写出完整报告，必须另行检查其未平仓记录，不能当成已清仓结果。

## 保持原排名的部分执行诊断

`backtest` 默认仍是正式模式，保留全局数据、固定保护、元数据、费用和历史日历检查。
另设 `prepare-diagnostic` / `diagnostic-backtest`，用于正式资料尚不完整时复核已具备条件的候选。
诊断没有改变合约池、R8、Top-K、任何入场/退出过滤或资金预算。
例如原第1名不可执行，K=2仍是原第1、2名；第1名记录拒绝，不引入第3名。

本月诊断只运行已锁定的9月14—23日验证段及shared资金场景。
训练截止9月11日的v2校准决定产品资格，PM/WH等不足产品不补造跳数。
资料准备会校验真实训练数据指纹，逐交易时刻核对官方合约规范的跳动、价值及可获得/适用日期，不要求规范早于训练截止，
并将上一完整交易日收盘后公开的SHFE/INE一般持仓费用/保证金装入独立执行层。
原元数据、历史合约池未核实标记及`calendar.verified=false`保持在正式配置内。
同日日内交易表只作保证金生效时点对账，不在其发布前输入决策。
收盘结算公告使用收盘时段边界作生效代理；秒级实际结算起点没有被验证。

```bash
# 第一次可串行归档公开的合约规范和日内参数；无账户登录或付费接口
.venv/bin/python -m research prepare-diagnostic \
  --config research_inputs/2026-09/research_config_k2.json \
  --calibration research_outputs/2026-09/calibration_v2/training_tick_candidates.json \
  --tick-index 0 --fetch-official \
  --output research_outputs/2026-09/diagnostic_preparation_k2

.venv/bin/python -m research diagnostic-backtest \
  --config research_outputs/2026-09/diagnostic_preparation_k2/diagnostic_config.json \
  --split validation --scope shared \
  --output research_outputs/2026-09/diagnostic_baseline_k2

# 离线重新准备：第一条去掉--fetch-official，读取有URL/SHA256凭据的归档
# 重新生成报告：填入实际实验目录；校验并复用共享行情对象
.venv/bin/python -m research report --run research_outputs/2026-09/diagnostic_baseline_k2/<实验编号>
```

`execution.mode` 默认为`formal`；准备命令生成`diagnostic`及嵌入的`qualification`快照。
资格包含训练窗口/校准哈希/固定跳数哈希、准入交易所、逐真实合约时段规则及全部研究假设。
规则记录`source_date`、官方`update_date`对应的`available_at`、`margin_effective_at`、
`effective_from/to`、官方上市/最后交易日、fee/margin/tick/value及原始资料路径/哈希。
`effective_to`是排他边界，包含收盘分钟OHLC的结束时刻；没有新行情不制造成交。
规则缺少精确合约或日期时拒绝；信号时和实际成交时分别重查，退出规则缺失会保留未平风险。

输出增加`candidate_execution.csv(.gz)`：包括所有原方向排名、是否进入Top-K、执行通过与独立拒绝原因。
`signals`保留原过滤快照，另加`execution_pass/rejections`；执行资格拒绝与资金拒绝分别统计。
逐笔成交保存开平时使用的参数来源与适用时段。
`fee_arithmetic_checks.json`是四个真实官方参数案例在声明的示例价格下的算术核对，**不是行情成交或回测结果**。

诊断显式采用典型价近似VWAP及仅交易所费用，账户加收未知。
当前阶段禁止诊断配置参加`sweep-topk/compare-entry/sensitivity/walk-forward/freeze`或锁定测试。
诊断报告不能用于完整策略收益或最佳K结论；待正式费用、日历及合约池缺口解决后，使用原正式配置运行Top-K。

指标缓存使用v3逐合约/周期的gzip JSONL，压缩级别6、mtime=0；逐个frame读取，避免另建全市场指标副本。
只有完整结束标记及gzip尾部成功落盘才发布缓存，失败的`.partial`文件不会被复用。
旧v2缓存保留，由源码指纹区分；不删除历史实验或覆盖旧缓存。
时段分钟表和已排序的本日K线索引按合约/日期复用，VWAP的求和及所有过滤公式保持原口径。
撮合仅保留已从完整历史算出的窗口指标及此前40行；不会重算短历史SMA/ATR或在开盘归零。
相同配置复用已有Dataset索引，不随shared资金场景重复构造；参数变体另有配置视图，互不污染。

## 已触发合约的公开执行参数扩展

`extend-diagnostic`从已冻结的schema=1诊断版本建立schema=2版本，仅增加执行资格。
本轮公开归档接入FG701、PX611、AP701、lc2701及IC2612、IM2612、TF2612、TL2612；
仍使用原全池、K=2/direct、训练候选索引0及9/8/4时间边界。
郑商所读取官方静态`FutureDataClearParams.txt`前日结算表；广期所历史查询必须回显所请求日期，
响应`time`只表示查询时间。中金所按明确日期收费表连续适用的诊断假设建立逐日规则。
固定费为元/手，比例费按万分数转换；中金所1000%平今收取率为基础费的10倍。

LC9/22交易时起使用9/18预先公告的万分之0.8费率、最小开仓2手和每日开仓800手限制。
更早最小手数依据未确认的LC日期保留拒绝。开仓手数不会为了满足最小数量而突破风险预算；
实际成交累计日开仓量，平仓不会恢复额度，成交时再次检查。
CZCE/LC规格的历史连续适用、日期表当日末已可获得及标准最小1手均明确标为诊断假设；
补查资料在观察验证信号之后完成，不构成独立样本外验证，正式模式的缺口仍保留。

```bash
.venv/bin/python -m research extend-diagnostic \
  --config research_outputs/2026-09/diagnostic_preparation_k2/diagnostic_config.json \
  --source-dir research_inputs/2026-09/source_docs/triggered_contracts \
  --output research_outputs/2026-09/exchange_preparation_k2

.venv/bin/python -m research diagnostic-backtest \
  --config research_outputs/2026-09/exchange_preparation_k2/diagnostic_config.json \
  --data-run research_outputs/2026-09/diagnostic_baseline/run_0003_56e490fe2309 \
  --split validation --scope shared \
  --output research_outputs/2026-09/exchange_baseline_k2
```

准备输出保存原始参数行、58个示例价格费用核对、规则覆盖及配置不变部分的指纹。
示例费用核对不是回测成交；实际收益以实验目录的成交表和限制说明为准。

## 独立规则价格回放

本轮明确固定K=2、direct。`research_config_k2.json`另立版本，旧K=1输入及实验不改写；
`baseline_expectation.strategy`必须与实际策略一致，读配置、准备、引擎和实验快照均校验。
`k2_configuration_trace.json`保存旧输入→准备→运行K=1的证据，未发现运行时静默覆盖。

`prepare-price-replay`只装入训练冻结的规则；`price-replay`复用信号、撮合时钟、下一可成交分钟开盘、
固定保护、放量/MA40/时间退出与冷却。逐合约最多一个假设单位，不模拟账户组合风险预算或手数。
未知费用不设为0，不生成权益、保证金、货币盈亏、资金收益率或夏普。
输出`trades.csv.gz`中的price_points、计划止损价格距离及条件r_multiple；
只有采用的已复核官方规范适用假设有证据时输出ticks_pnl，其余为空。
供应商最新目录步长是历史适用假设，price_basis完整记录该限制；不会因此宣称历史规格已核实。

```bash
.venv/bin/python -m research prepare-price-replay \
  --config research_inputs/2026-09/research_config_k2.json \
  --calibration research_outputs/2026-09/calibration_v2/training_tick_candidates.json \
  --tick-index 0 \
  --execution-review research_outputs/2026-09/diagnostic_preparation_k2/diagnostic_config.json \
  --output research_outputs/2026-09/price_replay_preparation_k2

.venv/bin/python -m research price-replay \
  --config research_outputs/2026-09/price_replay_preparation_k2/price_replay_config.json \
  --data-run research_outputs/2026-09/diagnostic_baseline/run_0003_56e490fe2309 \
  --split validation --scope shared \
  --output research_outputs/2026-09/price_replay_k2
```

`--execution-review`可省略，此时没有历史报价步长核实依据而不输出跳数盈亏。
`--data-run`可省略以重读原数据文件；使用时先校验旧快照窗口、物理SHA256及语义指纹，
复用其数据审计，并保存审计来源；不重新下载。`storage.indicator_cache_root`可引用同一内容指纹指标缓存。
规则价格回放只允许validation/shared，禁止参数搜索、freeze和锁定测试。
报告和案例逐条标注假设进出，未知成本不能解释为0；闭合路径数量不能替代真实成交数量。
报告复用已校验的行情对象，案例只计算确定性选出的合约自身全部历史。
同次回测生成报告时直接复用已完成结果；独立`report`命令仍验证共享行情并读取结果一次。

## 现有真实数据审计

```bash
.venv/bin/python -m research validate-data --config research/examples/config.json --output research_outputs/real_data_audit
.venv/bin/python -m research backtest --config research/examples/config.json --output research_outputs/real_baseline
```

第二条目前会失败，并保留 manifest、配置、数据快照与失败报告：缺少固定止损止盈、实际开平手续费、保证金、上市到期日期、核实后的历史日历。模板中的合约数值是待核实信息，不是费用或上市日期的真实断言。

现有五个新浪近期 CSV 有 5,115 行；默认日盘审计实际保留 2,829 行，覆盖 rb2701.SHFE、m2701.DCE、cu2611.SHFE、au2612.SHFE、sc2612.INE，原始自然日范围 2026-09-23 至 2026-09-30。夜盘 CSV 没有 trading_day，明确排除，不按自然日猜交易日。无成交额；金融数据为空。短样本和缺失分钟不足以完成严谨 Top-K 样本外比较。当前目录没有 Git 根仓库，使用逐文件 SHA256、源码归档代替不存在的 Git commit。

## 接入真实 CSV / Parquet / vn.py 数据库

复制并编辑模板；配置相对路径始终以配置文件位置为基准，而不是当前工作目录。

```bash
mkdir -p research_inputs
cp research/examples/config.json research_inputs/config.json
cp research/examples/metadata.json research_inputs/metadata.json
cp research/examples/calendar.json research_inputs/calendar.json
cp research/examples/data_template.csv research_inputs/bars.csv
```

将真实记录写入 bars.csv；仅有表头不会生成收益结果。把 config.json 的 `data.sources` 改为：

```json
[{"format": "csv", "path": "bars.csv", "provenance": "REAL_UNVERIFIED"}]
```

也可使用 `{"format":"parquet","path":"bars.parquet"}`；安装 Parquet extra 后按相同字段读取。gzip CSV 支持 `.csv.gz`。不能混用真实与合成标签。

已有数据库适配不改数据库、不读取账户配置、不导入 vn.py 的全局设置：

```json
[{"format":"vnpy_sqlite","path":"../runtime/.vntrader/database.db"}]
```

使用只读 SQLite 事务对 `dbbardata` 的 `interval='1m'` 做一致性快照。当前项目数据库时间配置为 Asia/Shanghai；其他数据库必须先明确时间口径。夜盘没有 trading_day 的数据库记录不会被猜测归属。不得把 CSV 和同一数据库重叠分钟作为两个数据源重复导入。

标准字段为 `datetime,trading_day,exchange,symbol,product,open,high,low,close,volume,open_interest`，可选 `turnover,session_open_oi,tradable,limit_up,limit_down,provenance`。OHLC 必须为真实交割合约；拒绝连续和期权代码。缺失 OI 不替换为零。`tradable=false` 和一字涨跌停不能成交。

- `datetime` 标准化为 Asia/Shanghai 分钟开始；源为结束标记时显式设置 `data.timestamp="end"`。
- `volume/turnover` 默认为本分钟增量。累计模式必须设置 `counter_mode="cumulative"`；未知初始基线、计数重置和断档后的第一根被排除。只有明确从零开始、且第一根就在本次开盘时，才可设置 `cumulative_first_is_zero_based=true`。
- `open_interest` 是时点，不累计。没有开盘快照时使用第一分钟末值作为代理。
- 日盘没有 trading_day 时，可按已提供日历和自然日生成日盘代理；夜盘必须给交易日和 `calendar.night_dates` 映射。代理和排除记录都会输出。
- 缺失分钟不填充；零量且确有记录的分钟保留但不可成交。只有分钟数据时，缺失记录无法区分无成交还是漏采，需要供应商状态或 tick 复核。

## 元数据与日历字段

`metadata.contracts` 每条必须包含真实 symbol/exchange、product、group、session_profile、time_profile、effective_from、listed、expiry、tick_size、value_per_price、turnover_factor、margin_rate、fees、verified。一个合约可按 effective_from 有多条历史版本，禁止重复生效日。

`value_per_price` 是每手价格变化 1 单位对应金额；`turnover_factor` 独立控制 VWAP 的成交额换算，不能默认相等。保证金当前支持按名义金额的比例，不支持固定金额或组合优惠。

手续费示例结构（以下金额仅演示，必须替换为实际生效规则）：

```json
{"effective_from":"2026-01-01","effective_to":"2026-12-31",
 "open":{"mode":"fixed","value":1},
 "close_today":{"mode":"rate","value":0.0001},
 "close_yesterday":{"mode":"fixed","value":2}}
```

`fixed` 单位为元/手；`rate` 乘实际成交价 × value_per_price × 手数。开仓和平今/平昨分别计费。滑点已反映在成交价，只另外记录诊断金额，不重复扣款。历史规则缺少生效覆盖时明确失败。

日历必须给出明确 `trading_days`，包括中间交易日，即使数据文件该日完全缺失。不得删掉缺失日期来假装上一日完整。`profiles` 给自然日日盘小节，区间左闭右开。`overrides[日期][profile]` 可覆盖 sessions 和 deadlines；提前收市会把研究强平时间裁剪到最后一个交易分钟，并同步裁剪禁开时间。

有夜盘的合约应在元数据给 `night_sessions`，如 `[["21:00","23:00"]]` 或 `[["21:00","02:30"]]`，并提供 `night_dates[交易日]` 的实际夜盘开始自然日。上一完整交易日资格检查包含这些时段，即使当轮只选日盘；缺少夜盘映射时剔除。夜盘参与指标可用 `include_night_indicators` 开关，但夜盘开盘选品本阶段不开放。

产品与组映射由元数据维护，不内置声称完整的全市场合约清单。每产品每日按上一完整交易日收市 OI 最大选一个已上市合约，先按 OI、再前日真实量、再代码打破并列；当天不换。换月使用新合约自己的历史指标。

## 策略与研究配置

默认八分钟选品，15分钟当前时段第一根完成后入场；SMA 跨实际交易时段，完整5/15分钟聚合不跨休息，缺一分钟不产生该长周期。ATR 为 TA-Lib 的 Wilder 14，异常波幅和乖离使用 ATR[t-1]。

`entry_mode` 支持 direct、pullback_ma10、pullback_ma20、pullback_either。direct 由完整入场条件 false→true 触发；组合额度拒绝也属于未满足，额度释放后重新符合可触发。价格过滤和含风险准入分别记录。回踩取最近符合事件，双触碰只记一个事件，事件发出开仓请求时消费。订单取消后不会重复消费同一回踩。平仓冷却按交易日历实际交易分钟累计，不计午休。

`approximate_vwap=false` 要求每个有效成交分钟有成交额，否则 VWAP 过滤失败；true 全程使用典型价量权近似，不能把近似与精确分钟偷偷混用。做空增仓仍须正值。`enable_oi_filter/enable_smooth_filter/enable_multicycle_filter` 仅用于单独消融；`enable_volume_exit/enable_ma40_exit` 可消融退出，但固定保护和时间退出始终保留。

风险初值：100万元、单笔0.2%、组合1%、保证金30%、最多5合约、每合约最多100手、分组各50%预留。这些不是实盘授权或优化参数。按止损金额加开平成本与滑点缓冲取整手数，活动开仓请求预占额度；成交开盘跳空后再检查，数量可减少而不增加。并发信号依次按排名、abs(R8)、组名、代码分配额度，拒绝原因保留。商品先开盘不会占用金融预留额度。

禁止开仓和强平的初值见 config.json；这些是研究时间，不是交易所规定。时间事件由离线钟逐分钟触发，与新行情回调无关。禁止跨休息持仓时在休息前最后可交易分钟请求平仓，无法成交则记录未平风险。缺失、零量或一字涨跌停无法成交时保持 EXIT_PENDING、持仓和未平风险，不按最后价格虚构清仓；发生未平日盘或休息风险后默认禁止继续开新仓。所有合约先完成同一时刻开盘撮合，再检查该分钟内固定保护，避免跨合约使用尚未发生的止损结果。

固定保护先判断开盘价格：已越过止损或止盈时，按该开盘价加不利滑点退出，原因和触发标记只记录开盘已经发生的一侧。之后同一分钟的反向高低价不影响已完成的退出。开盘仍在保护区间内时，才用该分钟OHLC判断触及；两侧同时触及且无法确定顺序时，保守取止损，并保留双触及标记。

## 训练校准、实验与最终测试

配置 `splits` 按时间分离 train/validation/test，不随机打乱；每个窗口从空仓开始，过去数据只用于指标预热。优化获得的 Dataset 已物理截断到验证截止日；校准器会拒绝训练截止之后的数据。最后测试禁止 sweep 和 walk-forward 进入。

2026年9月14—23日窗口已被反复用于图表和入退场诊断，现在属于开发样本；`validation`仍是冻结配置中的窗口名称，不能据此将后续改规则的同段结果称为独立样本外验证。原9/8/4边界不移动，锁定测试保持未运行。附件问题复核和分离行情报价/滑点模拟价的图见 `research_outputs/2026-09/entry_exit_audit_review/report.md`。

CLI 在导入时执行截止过滤：CSV 在数值解析前按 trading_day 排除未来记录，Parquet 使用交易日谓词下推，SQLite 使用日期条件；完整 `validate-data` 审计仍单独检查所有源记录。源文件哈希只用于完整性核对，不作为学习或择参信息。报告命令使用保存的快照，无需重新读取源行情。

```bash
.venv/bin/python -m research calibrate-ticks --config research_inputs/config.json --output research_outputs/calibration
# 查看候选，选择预先声明的索引；并不是已验证最优值
.venv/bin/python -m research backtest --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --output research_outputs/baseline
.venv/bin/python -m research sweep-topk --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --budget 24 --output research_outputs/topk
.venv/bin/python -m research compare-entry --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --budget 4 --output research_outputs/entry
.venv/bin/python -m research sensitivity --config research_inputs/config.json --calibration research_outputs/calibration/training_tick_candidates.json --tick-index 0 --budget 16 --output research_outputs/sensitivity
.venv/bin/python -m research walk-forward --config research_inputs/config.json --budget 32 --output research_outputs/walk
```

训练校准汇总各产品训练窗口的 ATR/跳动中位数，按显式倍数向上取整生成固定跳数候选。仅输出候选，不用验证未来波动调整距离。walk-forward 为每个明确窗口独立校准，默认候选索引0为未确认假设；训练样本或 tick 不足会报错，不能用全样本波动代替。

人工审阅验证窗口的多指标与稳定区间后，可冻结某个成功验证实验。下面 RUN 替换为已选验证实验的实际目录；使用该实验的内嵌 config_snapshot（包含所选 K、止损与其他参数），避免原模板与冻结方案不一致。

```bash
.venv/bin/python -m research freeze --run RUN --frozen research_outputs/frozen.json
.venv/bin/python -m research backtest --config RUN/config_snapshot.json --frozen research_outputs/frozen.json --split test --scope shared --output research_outputs/final_test
.venv/bin/python -m research report --config RUN/config_snapshot.json --run FINAL_RUN
```

合成样例命令另外加 `--synthetic`。最终测试只读取冻结方案；配置或研究源码变化都会拒绝。CSV/Parquet 源文件哈希也必须与冻结时一致。SQLite 没有整文件哈希，须先导出静态研究数据以获得更强锁定。原生数据库继续录制会产生新快照，不宜用于锁定测试。

## 可审计结果与网页

每项新实验保存 manifest.json、config_snapshot.json、source_snapshot.tar.gz、data_reference.json、data_quality.json、result.json 或 result.json.gz、summary.json，以及 pool/candidates/signals/trades/orders/events/equity/rank_contribution 和漏斗。行情按内容指纹共享存一次，同一截止窗口的 K、入场和资金场景引用同一基础对象；文件SHA256及语义指纹均核对，损坏时拒绝重建。旧实验的 normalized_data.json.gz 仍可读取。无 Git 时 code_hash 仍可核对。指标缓存使用数据、源码、库、日历、元数据和周期指纹，不能跨未来窗口共享混入。

报告输出中文 Markdown、独立可分享的 Plotly HTML 图和按最早盈利/亏损/未入场确定的案例，缺某类案例就不造。`report` 优先使用该实验不可变数据快照。金额报告区分已实现成交盈亏和未平持仓的最后价格标记权益。

Top-K 汇总给出各场景指标、风险拒绝、资金利用率和排名贡献；新增排名贡献与旧排名贡献变化分别输出，便于检查资金竞争。它没有自动按最高收益选 K。手续费比例分母为各笔绝对毛盈亏；夏普为日收市简单收益/样本标准差 × sqrt(240)，至少20日和20笔才显示。少样本与无交易返回 null，不制造稳定性结论。

前端“研究”页只读这些结果，可下载中文报告、明细和图表。默认读取项目 `research_outputs`，其他路径通过 `WORKBENCH_RESEARCH_ROOT` 配置。更新前端用 `bash scripts/build-frontend.sh`；研究本身无需启动交易服务。现有 Web 服务需要按原部署方式重载 API 才能显示新增接口，研究交付不会主动重启它。

## vn.py 适配

`vnpy_adapter.py` 的 BarData 转换和 CompletedBarBridge 只接受显式完成时间的分钟切片。`portfolio_strategy_class()` 返回基于已安装1.3.0 StrategyTemplate 的只输出审计模板，复用同一个 SignalLogic；send_order 被显式禁止。初始化可导入截止已知时点的历史，后续每个切片设置 observed_at，再调用 on_bars。这个桥不是实盘执行器；不调用原生 rebalance_portfolio，也不假定 CTA 的 on_trade 或本地 stop 参数存在。

官方资料核对和撮合差异见 [vnpy_engine_review.md](vnpy_engine_review.md)。本阶段没有 tick 撮合、柜台委托、模拟账户或实盘验证。

## 2026年9月公开真实数据接入

新增 `acquire-month`，直接复用现有信号、排名、回测和报告。公开来源为[信易EDB官方分钟/日线接口](https://doc.shinnytech.com/edb/latest/md_server.html)。按其文档，匿名可获取最近一年分钟及历史日线；接口实际返回时间仍须审计，不因为文档声明而假定所有合约都有数据。本适配器不读取Token、交易账户配置或密码。

本轮配置为 `research_inputs/2026-09/acquire.json`：September 2026、日盘、最多5个前交易日预热、串行、最多4GiB新增空间、至少保留2GiB空闲；包括原始文件、未完成响应、临时Parquet、行情对象与本月输出。产品 `products=null` 表示尝试供应商目录中的全部真实期货；可在新目录的独立任务中设为 `["SHFE.rb", "CFFEX.IF"]` 等明确范围，不能覆盖已开始任务的范围来伪装续传。

```bash
.venv/bin/python -m research acquire-month --config research_inputs/2026-09/acquire.json --phase catalogue
.venv/bin/python -m research acquire-month --config research_inputs/2026-09/acquire.json --phase daily
.venv/bin/python -m research acquire-month --config research_inputs/2026-09/acquire.json --phase minute
# 或串行完成全部阶段；--requests 200 可限制本次新增请求，再次运行继续
.venv/bin/python -m research acquire-month --config research_inputs/2026-09/acquire.json --phase all
.venv/bin/python -m research validate-data --config research_inputs/2026-09/config.json --output research_outputs/2026-09/audit
.venv/bin/python -m research report-acquisition --config research_inputs/2026-09/acquire.json --output research_outputs/2026-09/audit
```

`raw/*receipt.json` 保存请求网址、时间、SHA256及字节；续传逐文件验证网址与哈希，已完成响应不重新下载，未完成 `.partial` 不能作为数据。大目录下载设有总超时，失败的单个请求从头重试；这是请求级续传，不声称字节级断点能力。`progress.json` 保留进度/错误/空间。`--phase minute` 会读取和检查已缓存的目录及日线，保证代表合约可追溯。`config.json` 在全部分钟尝试完毕后生成，部分失败会明确记录；不能把暂停状态当作完成。

下载完成时记录 config.json、metadata.json、calendar.json 的哈希。使用者随后填写费用、修改策略参数或核实日历后，再次运行 acquisition 会停止自动再生成并保留修改；直接用 validate-data/backtest 执行研究。不要通过重下行情重置已经填写的研究配置。

最新合约目录包含过期和期权元数据；过滤 `class=FUTURE`、六家期货交易所、实际交割代码及供应商到期时间后，仅请求真实期货日线。不会下载期权行情，也不会请求连续合约价格。供应商完整历史合约池尚未证明，尤其郑商所三位代码的十年复用通过到期年份核对，不能拿2016年的同名合约作2026年数据。

`data.daily_sources` 新增独立日线输入，CSV/gzip CSV/Parquet均可。必填字段：`trading_day,exchange,symbol,product,volume,open_interest,complete,provenance`。其中 OI 为前日收市时点值，`complete` 必须显式给出；不完整、缺少紧邻前交易日或OI无效时剔除，不回退到更早日期或分钟资料。前日OI并列按前日量和合约代码固定排序，选中的合约缺分钟时不换成次名。旧配置没有 daily_sources 时保留原完整分钟检查。

本源日线实测为上海交易日零点，分钟为开始标记，volume为增量手数，close_oi为分钟末快照；第一根日盘 `open_oi` 保存在 `session_open_oi`。按分钟任务清单补同一真实合约预热，不跨合约拼均线。区间响应附带夜盘时只归档原始响应，标准化只保留请求的实际日盘分钟，未启用夜盘指标。`data.expected_contract_days` 限定有意请求的代表合约/预热日期，避免把没有下载的次要合约分钟误报为漏采；选中合约真正缺失仍逐分钟报告，不填充。

**没有成交额。** 自动生成配置显式开启 `strategy.approximate_vwap=true`，只能称为典型价近似均价研究。最新时段、到期、跳动及乘数有公开来源，但历史上市日未提供，窗口内首个日线观察日只是保守代理；手续费/平今费/保证金和成交额换算系数留空，verified=false。`research_blockers` 另外记录全历史合约池未核实，不能用替换标记的方式冒充核实。

`storage.shared_root` 指定不可变共享快照；`storage.compact_results=true` 只保存一份压缩完整结果和压缩CSV审计，不再重复一份JSON明细。全部独立过滤拒绝原因仍保存。`storage.audit_export_normalized=false` 避免审计额外复制全部分钟；原始响应和标准化Parquet仍在共享输入目录。重建报告必须同时保留实验目录和引用的共享对象。压缩CSV可直接下载后解压，原有网页接口同时兼容未压缩文件。

完成历史元数据核实、日历核实并解决真实数据缺口后，按训练段生成固定保护候选，再冻结用于验证：

```bash
.venv/bin/python -m research calibrate-ticks --config research_inputs/2026-09/config.json --output research_outputs/2026-09/calibration
.venv/bin/python -m research backtest --config research_inputs/2026-09/config.json --calibration research_outputs/2026-09/calibration/training_tick_candidates.json --tick-index 0 --output research_outputs/2026-09/baseline
.venv/bin/python -m research sweep-topk --config research_inputs/2026-09/config.json --calibration research_outputs/2026-09/calibration/training_tick_candidates.json --tick-index 0 --budget 6 --output research_outputs/2026-09/topk
```

本月默认最多6项实验，完整8个K×3资金场景需24项，可在空间验收后显式改 `experiments.max_runs`。默认顺序按K依次比较商品/金融/共享，6项仅覆盖K=1、2，不声称已比较所有K。索引0是未确认训练候选；9/1—9/11训练、9/14—9/23验证、9/24—9/30锁定测试，是一个月研究接线初值，样本不足以宣布稳定或最佳参数。缺关键执行配置时 backtest 返回失败并保存原因，暂停收益Top-K比较。

首次训练校准因WH不足而整批失败，这是旧执行记录。现在校准逐品种保存成功候选和失败诊断：返回码1表示候选不完整，文件仍保留；不能将“不完整”解释为正式回测已具备保护参数。缺失品种不会自动获得统一跳数，也不会自动从排名范围剔除。

## 9月数据复核、时间划分锁与费用参考

`audit-month`从真实日线和分钟重新计算品种日，不把合同级排除数量误当成品种数。每个品种日有一个互斥阶段，并保留全部原因。原87品种×21日=1,827，其中74个因无正前日持仓量被池前排除，1,753个选定真实合同日中117个排名排除、1,636个有效方向排名。覆盖84品种但少11个品种日，均为PM在9/1—9/15没有合格前日正OI。JR、RI、ZC没有分钟；指数目录LR没有相应真实合约目录记录，仍是历史池缺口。

用途取minute_plan.json，不按自然月份判断：新换月合同在月内补取的旧分钟属于预热。零量明细同时给出品种、合约、日期、研究/预热、日历切分；保留零量行而禁止成交，量大于零仍只是分钟成交代理。

```bash
.venv/bin/python -m research audit-month --config research_inputs/2026-09/acquire.json --output research_outputs/2026-09/readiness
.venv/bin/python -m research calibrate-ticks --config research_outputs/2026-09/readiness/locked_config.json --output research_outputs/2026-09/calibration_v2
```

`split_lock.json`和内嵌于`locked_config.json`的时间锁保留现有9/8/4日期，不改成另一个比例。改动日期或对应交易日会拒绝；预先声明且在原开发范围内的滚动窗口允许使用，原锁定测试边界保持一致。时间锁不是策略freeze，不能据此打开测试收益。已有locked_config被使用者修改时，重复审计不会覆盖，需另用输出目录立新版本。

校准v2只统计训练期按前日OI选定的真实合同、有成交量且ATR有效的1分钟观察；ATR仍由同合同自身历史计算，预热行仅供指标。不会统计为后续换月补取的未选合同，也不以无成交旧价衰减出的ATR代表可交易波动。输出82个成功品种，JR/RI/ZC/PM没有训练期选定样本、WH没有训练期有量样本；另外保留元数据、缺ATR、样本数诊断。范围取预先配置的合同品种，训练期未出现的品种也明确列为不足。

候选仍是未确认研究参数。现在的候选文件可以传给基准命令，但PM/WH及历史执行配置尚缺，正式基准仍会失败并保存原因。更改ATR或候选生成倍数须重新校准，不能沿用旧候选；代码和依赖、训练数据、选合同清单及生成设置的指纹均记录。

`review-costs`是隔离的官方费用参考适配。端点从上期所官方api.js核对，请求其发布的真实期货ContractBaseInfo、Settlement，不请求期权、Tick或另一研究月。报表包含SHFE和INE；能源品种的EXCHANGEID也返回SHFE，必须按项目产品交易所映射并保留原字段，不能直接作真实交易所。允许额外取得月初前一交易日参数。逐日归档网址、SHA256、report_date和行日期，缺一天就记录，不跨日填费率。首次明确加`--fetch-official`，之后去掉该开关只用已归档资料：

```bash
.venv/bin/python -m research review-costs --config research_outputs/2026-09/readiness/locked_config.json --source-dir research_inputs/2026-09/source_docs/shfe_parameters --fetch-official --output research_outputs/2026-09/cost_review
```

比例费用原值是小数；页面乘1000显示‰。平今参考费乘DISCOUNTRATE，普通仓位采用SPEC_*保证金；保留多空比例差异及复合收费的引擎缺口。OPENDATE用作官方上市日期、EXPIREDATE为最后交易日。这些参考含收市结算价，完全不进入行情适配、信号或跳数统计。只取得SHFE/INE的历史费用参考不能自动把全市场metadata/calendar设为verified，也不代表期货公司实际加收费用已核实。

已准备可编辑的`research_inputs/2026-09/research_config.json`，引用原metadata.json/calendar.json及时间锁。锁定输出配置是本轮审计快照；下一步编辑研究工作配置和它引用的元数据、日历，保留未经证实字段的缺失状态。按实际生效日期核实费用、保证金、时段和上市资格后，重新训练校准；PM/WH没有训练候选，需要明确有效的保护参数来源，不能用测试段倒推。再执行：

```bash
.venv/bin/python -m research validate-data --config research_inputs/2026-09/research_config.json --output research_outputs/2026-09/reviewed_audit
.venv/bin/python -m research calibrate-ticks --config research_inputs/2026-09/research_config.json --output research_outputs/2026-09/reviewed_calibration
.venv/bin/python -m research backtest --config research_inputs/2026-09/research_config.json --calibration research_outputs/2026-09/reviewed_calibration/training_tick_candidates.json --tick-index 0 --output research_outputs/2026-09/reviewed_baseline
.venv/bin/python -m research sweep-topk --config research_inputs/2026-09/research_config.json --calibration research_outputs/2026-09/reviewed_calibration/training_tick_candidates.json --tick-index 0 --budget 6 --output research_outputs/2026-09/reviewed_topk
```

这些命令不会自动核实缺少的费用。当前尚不应运行收益Top-K搜索；真实基准通过后再运行。6项只覆盖K=1、2的三资金场景，不能声称比较了全部K。

## 入退场审计后的顺序研究版本

`research_inputs/2026-09/ordered_refinement_plan.json`在各阶段收益运行之前声明四项累计修改：入场价格复查、趋势位移及活跃度、逐笔保护尺度、放量走弱退出。原K=2/direct、原排名、执行资格和资金预算保留；每一步只相对上一步增加一个模块。已经反复查看的9/14—23是开发样本，仍沿用9/8/4日期锁，不再称这些新规则获得独立样本外验证。

新增配置均可省略，省略时保留原行为：

- `strategy.recheck_entry_price=true`：冻结信号MA20±原1.5倍前值ATR形成跳价边界，预留原不利滑点；下一可成交开盘超限则取消，资金预占释放，direct须等一次新的不通过→通过后再触发。不利用后续分钟高低价制造限价成交。
- `strategy.trend_quality`：最近11个已完成观察的10次收盘变化至少3次非零；同向净位移至少1倍已知ATR且至少2跳。MA20近5根方向与MA40距离另作诊断，不增加均线排列硬过滤。
- `strategy.protection_scale`：止损取原训练跳数、1倍信号前值ATR、2倍估计往返交易所费用及滑点折算跳数中的最大值。止盈按原品种的止盈/止损比例同步缩放。信号和开盘均按原风险预算重算，成交数量不超过预占数量；开盘不得缩小信号计划止损，入场后不扩大距离。
- `strategy.volume_exit_mode="weakness"`：原量比阈值满足后，持仓仅在已完成K线反向且收盘比上一根走弱时退出。原新入场放量否决、MA40、固定保护和时间退出保留。默认`threshold`沿用仅按量比退出。

低内存复核入口验证原共享行情SHA256、完整训练数据指纹、保存的指标算法及缓存键，复用完整历史生成的因果滚动指标；只裁剪原始预热行，不在短窗口重新启动ATR/SMA。先运行并核对control，随后price→trend→protection→volume；上一阶段复核未通过则拒绝继续。例如：

```bash
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/run_ordered_refinement.py control
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/audit_ordered_refinement.py control
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/run_ordered_refinement.py price
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/audit_ordered_refinement.py price
```

其余三个阶段分别将`price`换成`trend`、`protection`、`volume`，依次执行和审计。阶段输出保存在`research_outputs/2026-09/ordered_refinements`，独立核对原池、所有指标快照、逐笔行情价、手续费及保护算术。完整对比与更新的入退场指标图见该目录的交付报告。压缩写入按1MiB聚合，每次实际写盘及最后flush仍检查完整空间预算；不降低4GiB总上限或2GiB磁盘余量。

## 原候选内的入场频率研究

`research_inputs/2026-09/entry_frequency_plan.json`声明保持原K=2每日候选、排名、执行资格和资金预算，仅将`strategy.efficiency_min`从0.45调至0.35。效率为方向调整后的10次收盘变化净位移除以绝对变化总和；1m/5m斜率区间、位移、活跃度、冲击、价格边界与原追踪退出均保持原样。候选扩展方案已按用户要求暂缓，没有执行扩候选回测。

```bash
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/run_entry_frequency.py control
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/audit_entry_frequency.py control
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/run_entry_frequency.py efficiency
PYTHONPATH=. .venv/bin/python research_inputs/2026-09/audit_entry_frequency.py efficiency
```

`PreparedEntries(..., allow_efficiency_change=True)`仅在来源代码、数据指纹及配置核对通过后，复用已完成的因果指标，并逐行重算效率过滤和当前持仓状态。它拒绝改变K或其他入场规则；资金、交易触发、成交和退出全部重新回放。对照必须与原版10份CSV逐字节一致，独立审计从11个完成收盘价重算所有受影响原候选观察及实际成交的效率，再核对斜率与交易路径。原入场被提前替代须与真正新增的交易日/合约/方向机会分开归因。此实验使用反复查看的开发窗口，不读取锁定测试，不自动改变实盘或默认策略配置。

## 成本与保本组合版的频率跟进

`research_inputs/coverage_expansion_2026-10-05/frequency_followup_plan.json`固定在上一轮成本/ATR≤0.5、1R含成本保本和目标后2ATR追踪的组合版上研究。保留K=2/direct、原池排名、资料资格、资金与成本：效率下限0.35、仅5分钟斜率Q10—Q90、关闭净增仓过滤、初始止损后同日同方向禁入，分别单项对照。5分钟范围沿用原9/1—11训练总体，先冻结分位数意图，再核对原总体数量与哈希并计算；不根据7、8月或验证段收益选择范围。

可选布尔值`strategy.block_same_day_reentry_after_stop`缺省为`false`。启用后，只有已实际成交且净亏损的`fixed_stop`才记录该交易日、合约、方向；当日其后同方向入场被`stop_reentry`拒绝。另一方向、其他合约、次交易日不受此历史限制；未成交退出、保本、追踪、指标退出均不触发。过滤记录与事件`same_day_reentry_blocked`保留触发交易编号。

```bash
# 串行完成3窗口×（对照+4单项），每一组独立审计后才继续。
.venv/bin/python -m research.frequency_followup --all
.venv/bin/python -m research.frequency_assessment

# 仅当assessment声明组合eligible且待完成时，按其中conditional_combination运行：
# .venv/bin/python -m research.frequency_followup --month <月> --variant <组合>
# .venv/bin/python -m research.frequency_audit --directory <该月组合运行目录>
# 完成三个窗口后再次运行frequency_assessment。
PYTHONPATH=. .venv/bin/python research_inputs/coverage_expansion_2026-10-05/build_frequency_report.py
```

三个控制窗口须分别精确复现上一轮10份CSV；所有变体须保留相同候选池和排名。独立审计逐观察核对修改过滤、执行资格、费用与止损历史，并从原完成收盘价复核效率和变动斜率，重建全部实际退出路径。所有场景包括未通过筛选的结果保留在`research_outputs/coverage_expansion_2026-10-05/frequency_followup`。评估按每个窗口的收益和回撤、剔除最大盈利单、交易数及逐笔入场变化进行；相邻分钟和同日改时点不算新的独立机会。只有通过全部预声明条件的第一项入场变体与独立通过的禁入规则可组合，不事后重调阈值。

这些仍是已查看样本上的开发回放：7、8月是9月参数冻结后的历史回放，9/14—23已经用于开发，9/24—30不读取。新开关及研究运行不会自动切换默认策略或实盘。图表使用保存的真实分钟行情和当时指标，门槛随实验显示；包含局部/全天、5分钟/15分钟入场上下文和上轮对应交易跳转。
