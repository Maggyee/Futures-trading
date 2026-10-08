from collections import deque
from copy import copy

from vnpy_ctastrategy import ArrayManager, CtaTemplate

from .market import MinuteRecorder, aggregate, bucket


class WorkbenchStrategy(CtaTemplate):
    """Strategy contract: on_signal(completed_bar), ready, and persisted pos only.

    on_bar receives 1-minute input in both live trading and backtesting.
    Chart and strategy use the same aggregation and TA-Lib implementation.
    """

    author = "Workbench"
    bar_minutes = 5
    warmup_bars = 100
    parameters = ["bar_minutes", "warmup_bars"]
    variables = ["ready"]
    ready = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if (
            self.bar_minutes not in {1, 5, 15, 30, 60}
            or not 100 <= self.warmup_bars <= 2000
        ):
            raise ValueError(
                "bar_minutes 需为 1/5/15/30/60，warmup_bars 需为 100～2000"
            )
        self.am = ArrayManager(self.warmup_bars)
        self.monitor_bars = deque(maxlen=self.warmup_bars)
        self.window = []
        self.recorder = MinuteRecorder(self.on_bar)
        self.ready = False

    def on_init(self):
        self.load_bar(30, use_database=True)

    def on_tick(self, tick):
        self.recorder.update(tick)

    def on_bar(self, bar):
        if self.bar_minutes == 1:
            self._complete(bar)
            return
        if self.window and bucket(bar.datetime, self.bar_minutes) != bucket(
            self.window[0].datetime, self.bar_minutes
        ):
            self._complete(aggregate(self.window, self.bar_minutes)[0])
            self.window = []
        self.window.append(bar)

    def _complete(self, bar):
        self.monitor_bars.append(copy(bar))
        self.am.update_bar(bar)
        self.ready = self.am.inited
        if self.ready:
            self.on_signal(bar)
        self.put_event()

    def on_signal(self, bar):
        raise NotImplementedError

    def on_trade(self, trade):
        self.put_event()
