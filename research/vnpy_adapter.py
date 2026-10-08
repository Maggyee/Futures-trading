"""Optional completed-bar bridge. No CTP import, account configuration or order routing."""

from .calendar import MINUTE, stamp
from .config import ResearchError
from .data import Bar, Dataset
from .signals import Features, SignalLogic, rank_candidates


def from_vnpy(bar, trading_day, product, provenance="REAL_UNVERIFIED"):
    return Bar(
        stamp(bar.datetime),
        trading_day,
        bar.exchange.value,
        bar.symbol,
        product,
        bar.open_price,
        bar.high_price,
        bar.low_price,
        bar.close_price,
        bar.volume,
        bar.open_interest,
        bar.turnover if bar.turnover > 0 or bar.volume == 0 else None,
        provenance=provenance,
    )


def to_vnpy(bar):
    # Lazy optional imports; core research never imports this stack.
    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.object import BarData

    return BarData(
        symbol=bar.symbol,
        exchange=Exchange(bar.exchange),
        datetime=bar.datetime,
        interval=Interval.MINUTE,
        open_price=bar.open,
        high_price=bar.high,
        low_price=bar.low,
        close_price=bar.close,
        volume=bar.volume,
        turnover=bar.turnover or 0,
        open_interest=bar.open_interest or 0,
        gateway_name="OFFLINE_RESEARCH",
    )


class CompletedBarBridge:
    """Explicit completion clock; receiver gets identical SignalLogic audit records.

    Previous data must be complete only up to `known_until`. Ranked pools are frozen.
    This is a signal bridge, not a production order executor or native matcher replacement.
    """

    def __init__(self, data, known_until):
        self.data = Dataset(
            [b for b in data.bars if b.end <= known_until],
            data.cfg,
            daily=[
                d for d in data.daily if d.trading_day < known_until.date().isoformat()
            ],
        )
        self.clock = known_until
        self.candidates = {}
        self.ranked = set()
        self.records = []

    def completed_daily(self, observations, observed_at):
        """Feed confirmed daily close OI explicitly; never infer it from current-day minutes."""
        observed_at = stamp(observed_at)
        if observed_at < self.clock:
            raise ResearchError("日线完成时间不能倒退")
        for row in observations:
            meta = self.data.metadata.get(row.key, row.trading_day)
            if (
                not meta
                or not row.complete
                or observed_at < self.data.calendar.bounds(row.trading_day, meta)[1]
            ):
                raise ResearchError("日线未完成或属于未来交易日")
        self.data = Dataset(
            self.data.bars,
            self.data.cfg,
            daily=list(self.data.daily) + list(observations),
        )
        self.clock = observed_at

    def completed(self, bars, observed_at):
        observed_at = stamp(observed_at)
        if observed_at < self.clock or any(b.end != observed_at for b in bars):
            raise ResearchError("适配层只接受已经完成的分钟，不接受未来/未完成K线")
        keys = {(b.key, b.datetime) for b in self.data.bars}
        if any((b.key, b.datetime) in keys for b in bars):
            raise ResearchError("适配层拒绝重复完成分钟")
        self.data = Dataset(
            list(self.data.bars) + list(bars), self.data.cfg, daily=self.data.daily
        )
        features = Features(self.data)
        logic = SignalLogic(self.data, features)
        days = {b.trading_day for b in bars}
        current_day = self.data.calendar.infer_day(observed_at)
        if current_day:
            days.add(current_day)
        for day in sorted(days):
            pool, _ = self.data.pool(day)
            for entry in pool:
                opening, _ = self.data.calendar.bounds(day, entry["meta"])
                rank_key = day, entry["group"], opening
                if observed_at >= opening + 8 * MINUTE and rank_key not in self.ranked:
                    group = [
                        p
                        for p in pool
                        if p["group"] == entry["group"]
                        and self.data.calendar.bounds(day, p["meta"])[0] == opening
                    ]
                    rows, _ = rank_candidates(
                        self.data,
                        day,
                        group,
                        opening + 8 * MINUTE,
                        self.data.cfg["strategy"]["k"],
                    )
                    self.candidates.update({(day, r["contract"]): r for r in rows})
                    self.ranked.add(rank_key)
        records = []
        for bar in sorted(bars, key=lambda b: b.key):
            candidate = self.candidates.get((bar.trading_day, bar.key))
            if candidate:
                cutoff, _, _ = self.data.calendar.deadlines(
                    bar.trading_day,
                    self.data.metadata.get(bar.key, bar.trading_day),
                    self.data.cfg["strategy"]["times"],
                )
                records.append(
                    {
                        "time": bar.end.isoformat(),
                        "contract": bar.key,
                        **logic.evaluate(
                            bar, candidate, before_cutoff=bar.end < cutoff
                        ),
                    }
                )
        self.clock = observed_at
        self.records.extend(records)
        return records


def portfolio_strategy_class():
    """Use installed 1.3.0 StrategyTemplate callbacks, explicitly with routing disabled."""
    from vnpy_portfoliostrategy import StrategyTemplate

    class ResearchPortfolioStrategy(StrategyTemplate):
        author = "Offline research"

        def on_init(self):
            self.bridge = None
            self.observed_at = None
            self.audit_records = []

        def bind(self, bridge):
            self.bridge = bridge

        def on_bars(self, bars):
            if self.bridge is None or self.observed_at is None:
                raise ResearchError(
                    "请 bind(CompletedBarBridge)，并显式设置 observed_at 完成时钟"
                )
            converted = []
            for key, bar in bars.items():
                dt = stamp(bar.datetime)
                day = self.bridge.data.calendar.infer_day(dt)
                if not day:
                    raise ResearchError("夜盘需要明确交易日；此日盘模板不推断夜盘归属")
                meta = self.bridge.data.metadata.get(key, day)
                if not meta:
                    raise ResearchError("缺少适配合约元数据")
                value = from_vnpy(
                    bar,
                    day,
                    meta["product"],
                    "SYNTHETIC_TEST_ONLY"
                    if self.bridge.data.cfg.get("synthetic")
                    else "REAL_UNVERIFIED",
                )
                converted.append(value)
            self.audit_records.extend(
                self.bridge.completed(converted, self.observed_at)
            )

        def send_order(self, *args, **kwargs):
            raise ResearchError("本阶段 vn.py 适配层禁止委托路由；仅输出信号审计")

    return ResearchPortfolioStrategy
