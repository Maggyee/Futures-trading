import ast
import hashlib
import importlib.util
import re
import sys
import uuid
from datetime import datetime

from .config import RUNTIME

TEMPLATES = {
    "双均线": """from backend.strategy_base import WorkbenchStrategy

class DoubleMaStrategy(WorkbenchStrategy):
    fast_window = 10
    slow_window = 20
    parameters = WorkbenchStrategy.parameters + ["fast_window", "slow_window"]

    def on_signal(self, bar):
        fast = self.am.sma(self.fast_window, array=True)
        slow = self.am.sma(self.slow_window, array=True)
        self.cancel_all()
        if fast[-1] > slow[-1] and fast[-2] <= slow[-2]:
            if self.pos < 0:
                self.cover(bar.close_price, abs(self.pos))
            elif self.pos == 0:
                self.buy(bar.close_price, 1)
        elif fast[-1] < slow[-1] and fast[-2] >= slow[-2]:
            if self.pos > 0:
                self.sell(bar.close_price, self.pos)
            elif self.pos == 0:
                self.short(bar.close_price, 1)
""",
    "布林带": """from backend.strategy_base import WorkbenchStrategy

class BollStrategy(WorkbenchStrategy):
    boll_window = 20
    boll_dev = 2.0
    parameters = WorkbenchStrategy.parameters + ["boll_window", "boll_dev"]

    def on_signal(self, bar):
        upper, lower = self.am.boll(self.boll_window, self.boll_dev)
        mid = self.am.sma(self.boll_window)
        self.cancel_all()
        if self.pos == 0:
            if bar.close_price > upper:
                self.buy(bar.close_price, 1)
            elif bar.close_price < lower:
                self.short(bar.close_price, 1)
        elif self.pos > 0 and bar.close_price < mid:
            self.sell(bar.close_price, self.pos)
        elif self.pos < 0 and bar.close_price > mid:
            self.cover(bar.close_price, abs(self.pos))
""",
}


def validate_source(source):
    if len(source.encode()) > 100_000:
        raise ValueError("策略文件不能超过 100 KB")
    try:
        tree = ast.parse(source)
        compile(tree, "<strategy>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"第 {exc.lineno} 行：{exc.msg}") from exc
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    if len(classes) != 1:
        raise ValueError("每个版本必须定义一个策略类，继承 WorkbenchStrategy")
    return classes[0]


def save_version(store, name, source):
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff ]{1,60}", name):
        raise ValueError("策略名称格式无效")
    cls = validate_source(source)
    vid = uuid.uuid4().hex
    version = {
        "id": vid,
        "name": name,
        "class_name": cls,
        "source": source,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "published": False,
        "created": datetime.now().isoformat(),
    }
    (RUNTIME / "versions" / f"{vid}.py").write_text(source)
    return store.put("versions", vid, version)


def load_class(version):
    from .strategy_base import WorkbenchStrategy

    path = RUNTIME / "versions" / (version["id"] + ".py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != version["sha256"]:
        raise ValueError("策略版本文件哈希不符")
    module_name = "workbench_version_" + version["id"]
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, version["class_name"])
    if not issubclass(cls, WorkbenchStrategy):
        raise ValueError("策略必须继承 WorkbenchStrategy")
    # Distinct engine class keys for immutable versions with the same original name.
    return type(version["class_name"] + "_" + version["id"][:12], (cls,), {})


def validate_parameters(cls, params):
    if any(k not in cls.parameters for k in params):
        raise ValueError("存在策略未声明的参数")
    result = cls.get_class_parameters()
    for key, value in params.items():
        default = getattr(cls, key)
        if type(value) is not type(default):
            if isinstance(default, float) and type(value) is int:
                value = float(value)
            else:
                raise ValueError(f"{key} 类型应为 {type(default).__name__}")
        result[key] = value
    if not 100 <= result["warmup_bars"] <= 2000 or result["bar_minutes"] not in {
        1,
        5,
        15,
        30,
        60,
    }:
        raise ValueError("预热数量或 K 线周期无效")
    for name, value in result.items():
        if name.endswith("window") and (
            type(value) is not int or not 2 <= value < result["warmup_bars"]
        ):
            raise ValueError(f"{name} 必须大于等于 2 且小于 warmup_bars")
    if "fast_window" in result and result["fast_window"] >= result.get(
        "slow_window", 10000
    ):
        raise ValueError("快均线周期必须小于慢均线")
    return result
