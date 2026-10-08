# vn.py 组合策略引擎核对

核对时间：2026-10-02。原环境 Python3.12.3、vn.py4.4.0、vnpy_ctp6.7.11.4、vnpy_ctastrategy1.4.1、vnpy_sqlite1.1.3；组合扩展原未安装，本次安装 vnpy_portfoliostrategy1.3.0（要求 vnpy>=4.4.0），未替换原依赖、未修改第三方源码。

已查阅用户指定的官方资料：

- https://www.vnpy.com/docs/cn/community/app/portfolio_strategy.html
- https://github.com/vnpy/vnpy_portfoliostrategy
- https://github.com/vnpy/vnpy_ctp
- https://www.vnpy.com/docs/cn/community/app/data_manager.html

另外读取组合仓库 main 的 template.py、backtesting.py、engine.py，并与本机已安装1.3.0源码核对；实际适配以已安装版本为准，不把远程main当作当前版本。官方网页的部分API描述可能早于本机版本，尤其委托/成交更新以本机源码为准。

本机路径 `.venv/lib/python3.12/site-packages/vnpy_portfoliostrategy/`：

- StrategyTemplate 初始化签名 `(strategy_engine, strategy_name, vt_symbols, setting)`。
- `on_bars(self, bars: dict[str, BarData])` 是组合切片回调；不是 CTA 的逐个 on_bar。
- `update_trade(self, trade)` 更新 pos_data，`update_order(self, order)` 更新 orders 和 active_orderids；不能照搬 CTA on_trade/on_order。
- `send_order(self, vt_symbol, direction, offset, price, volume, lock=False, net=False)` 不含本地 stop 参数。官方说明组合策略不提供 CTA 本地停止单。
- `BacktestingEngine.new_bars` 先构造本时间切片，再 cross_limit_order，最后 strategy.on_bars。因此上一分钟产生的订单可在后续切片撮合；不能把本切片收盘信号追溯成交到其开盘。
- new_bars 在某合约本分钟缺失时，会把前一 close 构造为 OHLC 并放进撮合缓存；本次要求禁止这种数据变成可成交K线。
- 原生 cross_limit_order 用 low/high 对限价是否触及撮合，一次全成交，以 open 与限价更优者定价；没有本次固定保护路径和保守双触及顺序。
- 原生日盈亏按固定各合约 rate 收取成交额比例费，slippage 单独计费用；本次需要按手/按比例、生效日期、开/平今/平昨，以及在成交价内计滑点，不能直接照用。

因此保留 vn.py 数据容器与组合 StrategyTemplate 接口，信号统一放入 research.signals；研究执行使用 research.execution 的隔离事件钟/资金预算/状态机/OHLC保护扩展。它与原生撮合的不同点均可见上述代码及 known_limitations.md，不声称原生引擎支持了这些功能。

CTP 不提供完整历史K线，安装 vnpy_ctp 不代表已有历史库。DataManager支持独立CSV导入/数据库查看，本项目已有Web导入和vnpy_sqlite；研究适配以只读数据库事务复用已有分钟数据，不通过DataManager GUI或CTP自动下载。
