import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("WORKBENCH_RUNTIME", ROOT / "runtime")).resolve()
ORIGIN = os.environ.get("WORKBENCH_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
RPC_PORT = int(os.environ.get("WORKBENCH_RPC_PORT", "20140"))
RPC_REQUEST = f"tcp://127.0.0.1:{RPC_PORT}"
RPC_PUBLISH = f"tcp://127.0.0.1:{RPC_PORT + 1}"


def prepare_runtime():
    """Must run before importing vn.py (which chooses cwd at import time)."""
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    (RUNTIME / ".vntrader").mkdir(exist_ok=True, mode=0o700)
    (RUNTIME / "versions").mkdir(exist_ok=True, mode=0o700)
    os.chdir(RUNTIME)
    settings = RUNTIME / ".vntrader/vt_setting.json"
    if not settings.exists():
        settings.write_text(
            json.dumps(
                {
                    "database.name": "sqlite",
                    "database.timezone": "Asia/Shanghai",
                    "log.console": False,
                }
            )
        )


def read_ctp_config():
    path = RUNTIME / "simnow.json"
    if not path.exists():
        raise ValueError("请先在服务器配置 runtime/simnow.json，参见部署文档")
    data = json.loads(path.read_text())
    required = (
        "用户名",
        "密码",
        "经纪商代码",
        "交易服务器",
        "行情服务器",
        "产品名称",
        "授权编码",
        "柜台环境",
    )
    if any(not data.get(k) for k in required):
        raise ValueError("SimNow 配置缺少必填字段")
    if data["柜台环境"] not in {"实盘", "测试"}:
        raise ValueError("柜台环境必须为 CTP 支持的实盘或测试模式")
    # A backend allowlist, never browser-controlled endpoints or broker IDs.
    if data["经纪商代码"] != "9999":
        raise ValueError("首版仅允许 SimNow 经纪商 9999")
    from urllib.parse import urlparse

    allowed = {"180.168.146.187", "180.168.146.182", "182.254.243.31"}
    allowed.update(os.environ.get("SIMNOW_ALLOWED_HOSTS", "").split(","))
    allowed.discard("")
    for key in ("交易服务器", "行情服务器"):
        address = data[key] if "://" in data[key] else "tcp://" + data[key]
        url = urlparse(address)
        if url.scheme != "tcp" or url.hostname not in allowed or not url.port:
            raise ValueError(
                "服务器不在 SimNow 白名单中；管理员可核实地址后设置 SIMNOW_ALLOWED_HOSTS"
            )
    return {key: data[key] for key in required}
