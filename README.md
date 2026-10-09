# CTP Workbench

## 当前研究结果

**本版入场假设已[正式收口](docs/research-closeout.md)：完整规则及7／8／10笔回测对照封存为研究基准，等待研究完成，当前不支持实盘晋级。**保留数据与工程工具，停止本版参数修补；新假设研究尚未启动。

截至2026-10-09，已完成有限的“等待是否改善入场”研究，按分钟对齐首次基础环境、首次原严格行情条件和下一开盘执行资格。52个已查看开发日期中，前两名551个基础段只有23段首次严格达标，7段通过空仓约束，恰好对应原7笔回测模拟成交。8月没有可执行样本，7月去掉最佳日期后净标签转负，9月仅有2个LC样本，尚未建立跨窗口、日期和品种重复的可执行等待优势。

- [决策时刻研究报告](docs/decision-timing-results.md)：最新结论，区分筛选、共同终点与各自入场后相同期限，保留永未达标、拒绝及未知。
- [决策时刻完整评估](research_outputs/decision_timing_2026-10-09/assessment.json)与[发布证据](docs/decision-timing-evidence.json)：233,430个分钟时刻、5,736个机会段、376对阶段匹配；补齐12个郑商所合约／日期执行资料，历史规格仍沿用明确的连续性假设。标签不能当作策略收益。

- [上一轮机会质量研究报告](docs/opportunity-quality-results.md)：开盘排名、单项过滤及MA10／MA20回踩，固定15分钟为主、5/30分钟为对照。
- [机会质量完整评估](research_outputs/opportunity_quality_2026-10-09/assessment.json)与[发布证据](docs/opportunity-quality-evidence.json)：含636对匹配、逐项过滤、形态生命周期和独立核验。

上一轮四层规则复核的24组回放已交付形态有效期、配置与过滤缓存、成本及资金诊断、初始止损与盈利保护解耦的工程修改。7笔、8笔和10笔版本已封存为研究对照，不作为已验证策略；下表均为回测模拟结果：

| 方案 | 回测模拟成交 | 模型净额 | 非LC净额 |
| --- | ---: | ---: | ---: |
| 原7笔基准 | 7笔 | +7,540.05元 | -84.67元 |
| 形态自身有效期 | 8笔 | +7,752.28元 | +127.56元 |
| 回踩恢复即确认 | 10笔 | +7,486.96元 | -137.77元 |
| 仅改结构止损 | 8笔 | +2,404.51元 | -20.51元 |

- [四层规则复核报告](docs/rule-layer-results.md)：上一轮工程与账本结论，含新增／丢失形态、账户拒绝、通道贡献、成本压力及仓位／价格路径归因。
- [完整窗口评估](research_outputs/rule_layers_2026-10-08/assessment.json)与[汇总及来源证据](docs/rule-layer-evidence.json)。
- [上一轮顺序实验逐笔交互报告](research_outputs/structure_followup_2026-10-07/trade_review/structure_followup_review.html)：旧21条版本成交及3处确认案例，下载或克隆后在浏览器打开。
- [回测记录与复现范围](docs/backtest-results.md)

24组回放全部通过独立核验；另保留1次回放前失败初始化，共25次操作尝试。62条版本成交只对应12个入场时间键、9个合约／日／方向组合及8个日期。三个窗口分别重置账户，总额不能当作连续实盘账户收益。

决策时刻研究在 `34a11db` 中按[预声明](docs/decision-timing-plan.json)完成三次扫描，新增完整策略回测为0。497项Python测试通过，独立核对18,258个原资格原始期限标签、11,297个扣费标签及5,990次入场资格判断；此前1,424份跟踪结果和57份研究Python源码保持逐字节一致。新增文件Ruff检查通过，全仓库另有67条既有问题，已记录检查范围。本次正式收口仅更新文档与发布指纹，没有重新运行这些测试、扫描或原始行情核验。

上一轮机会质量研究的三次月度扫描、476项测试、46,823个原始价格标签及29,553个扣费标签继续保留。原始行情与指标缓存仍不随Git发布，复核与复现范围见[研究说明](research/README.md)。

9月24—30日锁定测试继续封存，任何研究版本均未升级为实盘候选。这些窗口反复参与开发，运行前冻结不能消除研究选择偏差。

## 获取代码

```bash
git clone --recurse-submodules https://github.com/Maggyee/Futures-trading.git
cd Futures-trading
```

`src/vnpy`和`src/vnpy_ctp`保留为官方上游的固定版本子模块；项目自己的源码位于`backend`、`frontend`和`research`。下文安装和运行命令仍适用。

新增离线“开盘强弱选品 + 多周期平稳波段”研究包，入口为 `.venv/bin/python -m research`。
代码、数据接入、Top-K实验、冻结测试和报告命令见 [research/README.md](research/README.md)，假设见 [assumptions.md](assumptions.md)，验证边界见 [known_limitations.md](known_limitations.md)。网页新增只读“研究”页；研究核心无需连接账户。

本次实际文件、命令、测试及数据阻塞项见 [交付验收报告](research/validation_report.md)。

基于 vn.py 的个人 SimNow 模拟交易工作台。浏览器内查看行情和指标、手动开平仓、编辑 Python 策略、管理策略实例并执行历史回测。不需要图形桌面。

## 当前目录与运行方式

- `src/vnpy`、`src/vnpy_ctp`：原有上游源码，未修改。
- `backend`：Web API、独立交易进程、CTA 扩展、独立回测进程。
- `frontend`：React / TypeScript / Ant Design / Lightweight Charts / Monaco。
- `runtime`：自动创建的配置、数据库、代码版本和回测数据快照，不应加入版本控制。
- `deploy`：公网部署模板，尚未安装为系统服务。

原来的 Qt 桌面入口仍为 `.venv/bin/python src/vnpy_ctp/script/run.py`，仅能在图形桌面运行。

## 首次启动 Web 版

在 `/home/nishiki/vnpy-ctp` 执行：

```bash
# 当前工作区已安装依赖；重新安装时执行
.venv/bin/python -m pip install -c requirements.lock -e '.[test]'

# 构建前端（本工作区已下载 Node 22 到 .tools；其他环境需先安装 Node 22）
bash scripts/build-frontend.sh

# 交互式设置管理员账号和密码，不提供默认密码
.venv/bin/python -m backend.cli init-admin --username admin

# 同时启动交易进程和 Web API；Ctrl+C 关闭
bash scripts/start.sh
```

浏览器打开 **http://127.0.0.1:8000**。服务仅监听本机。远程开发可以用 `ssh -L 8000:127.0.0.1:8000 用户名@服务器`，然后访问本机同一 URL。不要直接用服务器 IP:8000，因为服务不绑定公网，且登录会校验来源。

未配置账户也可以登录、编辑策略和导入历史数据；行情与账户显示空态。初始化管理员会撤销所有旧登录会话。

Linux 上 CTP 的中文回调依赖 `zh_CN.GB18030` locale。交易进程会在启动时校验，缺少时自动生成到 `runtime/locales` 并通过 `LOCPATH` 加载，无需更改系统默认语言。如果提示生成失败，请安装发行版的 `locales` 包（包含 `localedef`、`zh_CN` 和 `GB18030` 数据）后重试。缺少此 locale 会导致原生扩展抛出 `locale::facet::_S_create_c_locale name not valid` 并退出。

### 配置 SimNow

```bash
cp deploy/simnow.example.json runtime/simnow.json
chmod 600 runtime/simnow.json
```

在服务器上编辑这个文件，填写你的 SimNow 用户名、密码、经纪商代码、交易/行情前置、产品名称和授权编码。不要把密码提交到 Git 或发送到聊天中。

**请按 SimNow 当前官方连接信息填写地址与柜台环境**。`柜台环境` 是 CTP 原生 API 的接口环境选择，不能据此判断账户是模拟还是实盘；有些 SimNow 环境使用生产版 API，配置值为“实盘”，但交易账户仍是模拟账户。应用仅接受经纪商 `9999` 和服务端 SimNow 地址白名单，网页没有实盘切换开关。新地址由管理员核实后加入 `SIMNOW_ALLOWED_HOSTS` 环境变量。

在网页点击“连接 SimNow”，等待行情、交易登录、合约下载和持仓查询完成，再搜索合约订阅。休市或 SimNow 维护期间可能没有行情。修改连接配置后需重启交易服务；网关断线时自动尝试重连，策略保持停止且需重新初始化。

左侧“自选行情”按添加顺序排列，实时价格更新不会改变顺序。搜索合约即可添加，行末叉号可移出自选；刷新网页和重启服务均保留列表，删除全部后保持空列表。移出自选仅改变列表，保留底层行情订阅供策略和本地数据录制使用；重启并连接成功后自动订阅保留的自选合约。

### 行情与 CSV

CTP 不提供历史 K 线。订阅后，实时 Tick 自动合成并保存一分钟 K 线；5/15/30/60 分钟按上海时区整点分桶合成，跨夜盘保留自然日时间。休市不填造 K 线；断线期间无法补齐 Tick，首条 Tick 之前的成交量也无法恢复。

在“数据”页下载模板并导入 UTF-8 CSV：

```csv
symbol,exchange,datetime,open,high,low,close,volume,open_interest
rb2610,SHFE,2026-09-28T21:00:00+08:00,3500,3505,3498,3502,100,123456
```

此行仅演示文件格式，不是真实行情。`datetime` 必须为一分钟起始时间；无时区时视为上海时间，`open_interest` 可省略。最多 20 MB / 20 万行。先校验全文件再写入；重复合约与时间以后导入值覆盖。页面最多展示 5000 根，单次查询最长一年。数据时间缺口按缺口展示。

导入后，在数据列表点击“查看 K 线”即可查看对应时间范围，无需连接 SimNow。交易页也可选择历史起止日期、K 线周期，以及 MA、EMA、BOLL、MACD、RSI、ATR 和持仓量指标。

点击图表上方“分时”可查看价格折线、均价线和分钟成交量，每 2 秒更新当前分钟。默认显示今天，可选择历史日期；按上海时间自然日划分，夜盘跨午夜分属两个日期，不等同于交易所交易日。缺失时段不补造行情。均价按本地已录制区间的累计成交额 ÷ 合约乘数 ÷ 累计成交量计算；CSV 缺少成交额或无法获取合约乘数时，以分钟收盘价乘成交量估算，并在图表底部标注。连接前未录制的数据不会自动补齐，均价可能与行情软件的全交易日均价不同。

K 线和分时默认显示“成交点”，可用复选框切换。红色向上箭头代表买入，绿色向下箭头代表卖出，默认不附文字。悬停或点击成交所在 K 线可查看实际成交时间、买卖方向、开平、价格和手数。成交按时间归入所选周期的 K 线；只在查询范围内且有对应行情的时间点展示。成交回报保存在本地数据库，服务重启后仍能查看；尚未成交的委托不会生成成交点。2026-09-29 的两笔 SimNow 成交已补入记录。

### 免费历史行情

已接入新浪公开的近期一分钟期货行情。先启动服务并连接 SimNow，停止运行中的策略后执行：

```bash
# 默认获取螺纹钢、豆粕、沪铜、黄金、原油的具体交割合约
.venv/bin/python -m backend.history

# 也可指定当前柜台可用的具体合约，合约到期后需更换代码
.venv/bin/python -m backend.history rb2701.SHFE m2701.DCE
```

默认合约为 `rb2701.SHFE`、`m2701.DCE`、`cu2611.SHFE`、`au2612.SHFE`、`sc2612.INE`。导入工具仅补充缺失时间点，保留本地已有数据；不会订阅行情或执行交易。公开服务每次通常返回约 1,023 根近期分钟线，可用长度由供应商决定，并非完整历史库。源时间为分钟结束时间，入库时减一分钟转换为项目约定的起始时间；未完成分钟会被排除。

下载的原始响应、转换后的 CSV、来源 URL、哈希、时间范围和导入结果保存在 `runtime/history/<时间>/`。公开数据不含成交额，因此相关分时均价标记为估算。导入后刷新“数据”页，点击对应合约的“查看 K 线”；分时可选择日期查看。2026-09-30 已导入五个合约，各获取 1,023 根，合计新增 4,701 根；数据最晚到当日 11:29，缺失时段不会补造。

### 手动交易与策略

- 开仓采用指定限价或对手价限价，不保证成交。前端受理成功不代表成交成功，需看委托/成交回报。
- 手填限价开仓和平仓不受“30 秒无新行情”限制，仍校验连接、合约、最小价位、涨跌停和可平仓量。留空使用对手价时，必须有最近 30 秒收到的 Tick；页面显示行情时间和更新状态，旧报价仅供参考。休市时 SimNow 可能拒绝委托，能提交不代表能成交，应用不会延迟到开市后自动重发。
- 手机交易页同样保留下单面板，填写限价和下单确认均可操作。
- 下单面板及平仓弹窗显示当前对手价：买入使用卖一，卖出使用买一（平多为卖出，平空为买入）。柜台返回零或无效报价时显示“暂无对手报价”，禁用自动对手价提交；仍可填写有效限价。显示价格仅供参考，实际提交使用交易进程收到的最新报价。
- 持仓行可部分或全部平仓：先停同合约策略，撤销该合约全部活动委托，等回报、查询持仓，再按交易所规则拆平今/平昨。
- 一个合约最多一个策略实例。合约已有策略实例时禁止手动开仓；手动平仓后校准剩余仓位并要求重新初始化。
- 超时不要直接再次下单：保留操作编号，查询操作记录并核对柜台订单。进程重启不会重放写请求或自动启动策略。
- 等待撤单期间断线会终止尚未发出的平仓指令，重连后需核对委托和持仓；已经提交的订单继续等待实际回报。准备超过 30 秒不会提交新平仓委托；行情过期时禁止对手价平仓，已指定限价的平仓仍需完成撤单及持仓查询。
- 策略继承 `WorkbenchStrategy`，实现 `on_signal(bar)`；基础类统一分钟聚合、TA-Lib 指标及预热。`parameters` 声明可配置参数，`variables` 声明界面变量。
- 代码检查只检查 Python 语法。发布时会执行导入代码；这是本人管理员的可信代码运行环境，不是不可信代码沙箱。
- 保存始终创建新版本，实例固定版本。更新时先停止、平仓、移除实例，再选择新版本创建。
- 初始化默认读取最近 30 天本地数据，至少预热 `warmup_bars` 根所选周期 K 线；不足时禁止启动。重启只恢复实例与记录的仓位，不恢复策略自动运行。

“策略”页的“运行监控”可选择实例，查看其状态、周期、版本、策略持仓、合约浮动盈亏和运行变量。图表每三秒更新，显示实例实际接收的最近 `warmup_bars` 根已完成 K 线及指标，包含初始化预热数据，不包含尚未完成的聚合周期。双均线参数 `fast_window` / `slow_window` 显示 SMA，布林参数 `boll_window` / `boll_dev` 显示 BOLL；指标读取该实例的 ArrayManager，与策略计算一致。其他自定义指标目前需扩展绘图接口；通过 `variables` 声明的数值仍可查看。成交点只显示归属于当前实例名称和代码版本的成交，不混入手动成交或其他实例成交。

### 回测

先保存并发布策略。选择合约、时间、策略参数（`bar_minutes` 为周期）、手续费率、滑点（价格单位）、乘数、价格步长和初始资金。开始日期之前的 30 天须有足够预热数据。

回测复用 vn.py CTA 回测引擎，每个任务单独子进程，最多一个任务并发，最长约 10 分钟 / 300 秒 CPU。运行时历史数据固定在内存，同时保存 `runtime/<任务ID>.bars.json.gz`、代码哈希、数据哈希和参数。失败明确显示错误。服务重启中断的任务标记失败，不自动重新运行。

完成后可查看资金曲线、统计、成交明细和“成交回放”。回放使用该任务固定的数据快照，并按策略周期展示最近 5,000 根 K 线及实际回测成交点。默认只显示买卖箭头，悬停或点击查看详情；勾选“成交标签”可额外显示开平和价格文字，密集成交时可关闭。

## 开发与测试

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check backend tests
.venv/bin/python -m pip check
export PATH="$PWD/.tools/node-v22.22.0-linux-x64/bin:$PATH"
export NODE_OPTIONS=--max-old-space-size=640
cd frontend
npm run build
npx prettier --check src e2e playwright.config.ts
npm audit
npx playwright install chromium
npm run test:e2e
```

浏览器测试使用独立临时数据库、测试管理员、8001 端口和 20240/20241 本机 RPC 端口，不连接 CTP。测试服务先生成所需 locale 并确认交易进程响应，再启动网页。完整流程采用 `rbTEST.SHFE` 合成行情和测试柜台回报，策略初始化、CTA 引擎和独立回测进程使用项目实际实现。覆盖登录、自选顺序/增删/刷新/空列表、开仓、平仓、K 线/分时成交点切换及画布像素和悬停详情、CSV 导入、编辑器连续键盘输入与代码检查/保存/发布、实例初始化/启停/移除及运行指标图表、成功回测及成交标签切换和退出登录；另有缺失预热数据及手机布局检查。新机器如缺少 Chromium 系统库，可使用 `npx playwright install --with-deps chromium` 安装；当前工作区已有 `.tools/browser-libs`，测试配置会自动加载。

本地验证结果：59 项 Python 测试、6 项 Playwright 测试通过；TypeScript/Vite 生产构建、Ruff、Prettier 和 `pip check` 通过，`npm audit` 未发现漏洞。测试覆盖历史源时间转换与保留已有数据、CTP locale 加载、平今/平昨拆单、完整/部分成交及拒单、断线与过期平仓、过期行情下禁止对手价但允许指定限价开平仓、会话校验、策略版本、CSV/指标、成交持久化与去重及策略归属、独立回测进程、自选持久化与策略监控图表，以及浏览器操作和窄屏布局。浏览器图表和平仓测试使用明确标注的合成数据。

前端开发可使用 `npm run dev`，API 仍运行在 8000；同时把后端 `WORKBENCH_ORIGIN` 设为 `http://127.0.0.1:5173`，确保登录来源校验与开发服务器一致。

## 公网部署

1. 准备指向服务器的域名，安装 Caddy。公网只暴露 80（证书申请/HTTPS 跳转）和 443，8000、20140、20141 保持本机监听。
2. 复制 `deploy/workbench.env.example` 为 `deploy/workbench.env`，设置 `WORKBENCH_ORIGIN=https://你的域名` 和运行目录。配置文件权限设为 600。
3. 将 `deploy/Caddyfile` 中域名替换为实际域名，安装到 Caddy 配置；使用 `caddy validate --config ...` 验证后重载。
4. 核对两个 systemd 模板中的 `User`、项目路径和运行目录，复制到 `/etc/systemd/system/` 后运行 `systemctl daemon-reload`，启用 `workbench-worker` 与 `workbench-api`。
5. 使用 `journalctl -u workbench-worker -u workbench-api -f` 查看服务日志。`/api/v1/health` 表示 Web 存活，登录后的工作台展示真实网关就绪状态。
6. 停止服务后备份整个 `runtime`，包含 SQLite 数据库、策略版本、账户配置和回测快照；备份需按含凭据的数据保管。

不要用 Uvicorn 多 worker 运行本项目，回测管理器为单进程；交易 RPC 使用 vn.py 本机通信，不可暴露公网。公网模板提供 HTTPS、HttpOnly 会话、CSRF 和 WebSocket 来源校验；无开放注册功能。

## API 概览

统一 `/api/v1`，除登录和存活检查外需要会话 Cookie。写请求需匹配 `Origin` 和 `X-CSRF-Token`；交易、连接、策略控制和回测还需 UUID `Idempotency-Key`。

| 能力 | 接口 |
| --- | --- |
| 会话 | `POST /login`、`GET /session`、`POST /logout` |
| 状态/推送 | `GET /snapshot`、`WS /ws` |
| 网关/自选 | `POST /connect`、`POST /subscribe/{symbol}`、`POST /watchlist/{symbol}/remove` |
| 行情 | `GET /bars/{symbol}?minutes=5`、`GET /intraday/{symbol}?day=2026-09-29`、`GET /data`、`POST /data/import` |
| 交易 | `POST /orders`、`POST /close`、`POST /orders/{id}/cancel` |
| 操作结果 | `GET /commands/{id}`、`POST /reconcile/{symbol}` |
| 策略代码 | `GET /templates`、`GET/POST /versions`、`POST /versions/check`、`POST /versions/{id}/publish` |
| 策略实例 | `POST /instances`、`POST /instances/{name}/{init,start,stop,remove}`、`GET /instances/{name}/chart` |
| 回测 | `GET/POST /backtests` |

手动交易请求：`{symbol, direction: "LONG"|"SHORT", volume: 整数, price: 数字|null}`。平仓的 `direction` 指持仓方向。实时事件使用 `{type, data}`；重连先重新获取完整快照，再消费增量，界面同时周期核对完整状态。

2026-09-29 已完成真实 SimNow 柜台登录及开平仓联调：`rb2701.SHFE` 买入开仓 1 手，实际成交价 3117；随后卖出平今 1 手，实际成交价 3116；柜台持仓归零且无遗留活动委托。限价委托价格与实际成交价格分别保存在 `runtime/validation/simnow-roundtrip.json`。该结果验证一次模拟账户交易往返，不代表全部交易所及异常场景均已柜台验收。

2026-09-30 已用导入的 `rb2701.SHFE` 真实历史行情验证双均线和布林带模板。回测周期为 1 分钟，区间为 09-29 00:00 至 09-30 12:00（上海时间），两者均使用 476 根回测数据和 549 根预热数据，含手续费及滑点：

| 策略 | 状态 | 成交笔数 | 净盈亏 |
| --- | --- | --- | --- |
| 双均线 | 已完成 | 27 | -294.06 |
| 布林带 | 已完成 | 42 | -730.77 |

两个验证版本增加了 Tick/分钟信号计数变量，交易逻辑保持模板实现。实际 SimNow 实例均完成初始化、启动、实时 Tick 和分钟信号处理、停止和移除。测试结束时持仓为零、活动委托为零、运行策略为零。完整参数、代码/数据哈希、统计和启停记录见 `runtime/validation/strategy-backtests.json`。回测结果仅用于功能验证，所用近期历史区间较短。

Playwright 另已检查实际运行的 8000 页面：自选删除、重新添加和刷新保留正常，K 线和分时均显示两笔历史 SimNow 成交及悬停详情，回测回放包含 42 笔成交，默认无文字且标签开关正常。临时双均线实例初始化后显示 100 根已完成 K 线和 SMA10/SMA20，检查后已移除。1512px 桌面与 390px 手机视图的画布均正常，交易、策略和回测页无横向溢出，没有浏览器脚本或 API 错误。本轮实际页面验收在收盘后进行，未收到新 Tick；行情推送时自选顺序保持稳定由隔离自动化测试验证。记录见 `runtime/validation/markers-browser.json`，截图为同目录的 `markers-*.png` 和 `strategy-monitor*.png`。完整自动化流程的截图保存在 `frontend/test-results/`。

自动化测试仍使用隔离运行目录、模拟网关回报和专用合成数据。公网 HTTPS 尚需实际域名部署后验收。

2026-09-30 收盘后再次用 Playwright 验证实际 8000 页面：过期对手价按钮禁用，填写限价后可打开下单确认弹窗；390px 手机下单面板可见且没有横向溢出。确认弹窗检查后取消，本轮实际页面验收没有发送柜台订单。报告为 `runtime/validation/stale-quote-browser.json`，截图为 `stale-quote.png`、`manual-limit.png`、`manual-limit-confirmation.png` 和 `manual-limit-mobile.png`。
