import asyncio
import json
import secrets
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Literal

import zmq
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import Auth
from .config import ORIGIN, ROOT, RPC_PUBLISH, RPC_REQUEST, RUNTIME, prepare_runtime
from .research_results import artifact, list_runs, run_summary
from .store import Store
from .strategies import TEMPLATES, save_version, validate_source


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Login(Input):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)


class Trade(Input):
    symbol: str = Field(pattern=r"^[a-zA-Z0-9]+\.[A-Z]+$")
    direction: Literal["LONG", "SHORT"]
    volume: int = Field(gt=0, le=100000, strict=True)
    price: float | None = Field(default=None, gt=0)


class Source(Input):
    name: str = Field(min_length=1, max_length=60)
    source: str = Field(max_length=100000)


class Instance(Input):
    name: str = Field(min_length=1, max_length=60)
    symbol: str = Field(pattern=r"^[a-zA-Z0-9]+\.[A-Z]+$")
    version: str = Field(pattern=r"^[a-f0-9]{32}$")
    parameters: dict = Field(default_factory=dict)


class BacktestInput(Input):
    version: str = Field(pattern=r"^[a-f0-9]{32}$")
    symbol: str = Field(pattern=r"^[a-zA-Z0-9]+\.[A-Z]+$")
    settings: dict = Field(default_factory=dict)
    start: datetime
    end: datetime
    rate: float = Field(default=0.0001, ge=0, le=1)
    slippage: float = Field(default=1, ge=0)
    size: float = Field(default=10, gt=0)
    pricetick: float = Field(default=1, gt=0)
    capital: int = Field(default=1000000, gt=0)


def rpc_call(action, payload=None, key=None):
    # Each request owns a socket: a timeout cannot poison the next REQ/REP exchange.
    with zmq.Context() as context:
        with context.socket(zmq.REQ) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.connect(RPC_REQUEST)
            socket.send_pyobj(["call", [action, payload, key], {}])
            if not socket.poll(18000):
                raise TimeoutError(
                    "交易服务未响应；写操作结果可能未知，请查询操作记录，不要重复下单"
                )
            success, result = socket.recv_pyobj()
            if not success:
                raise RuntimeError("交易服务处理异常，请检查服务日志")
            return result


def create_app(store=None, rpc=rpc_call):
    prepare_runtime()
    store = store or Store()
    auth = Auth(store)
    children = {}

    async def monitor():
        while True:
            for key, (child, began) in list(children.items()):
                if time.monotonic() - began > 600 and child.poll() is None:
                    child.terminate()
                if child.poll() is not None:
                    job = store.get("backtests", key)
                    if job and job["state"] in {"queued", "running"}:
                        job.update(
                            state="failed", error="回测进程退出或超过 10 分钟限制"
                        )
                        store.put("backtests", key, job)
                    del children[key]
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        for job in store.all("backtests"):
            if job["state"] in {"queued", "running"}:
                job.update(state="failed", error="接口服务已重启，请重新提交回测")
                store.put("backtests", job["id"], job)
        task = asyncio.create_task(monitor())
        yield
        task.cancel()
        for child, _ in children.values():
            child.terminate()
        for child, _ in children.values():
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    app = FastAPI(
        title="CTP Workbench",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.auth = auth

    @app.middleware("http")
    async def security(request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in {"/api/v1/login", "/api/v1/health"}:
            session = auth.session(request.cookies.get("session"))
            if not session:
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get(
                    "origin"
                ) != ORIGIN or not secrets.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf"]
                ):
                    return JSONResponse(
                        {"detail": "会话校验失败，请刷新页面"}, status_code=403
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; worker-src 'self' blob:; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    async def call(action, payload=None, key=None):
        try:
            result = await asyncio.to_thread(rpc, action, payload, key)
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from exc
        if isinstance(result, dict) and result.get("state") == "failed":
            raise HTTPException(400, result.get("error", "操作失败"))
        return result

    def request_key(request):
        key = request.headers.get("idempotency-key", "")
        try:
            return str(uuid.UUID(key))
        except ValueError as exc:
            raise HTTPException(400, "需要 UUID 格式的 Idempotency-Key") from exc

    @app.get("/api/v1/health")
    async def health():
        return {"ok": True, "environment": "SimNow"}

    @app.get("/api/v1/research/runs")
    async def research_runs():
        return await asyncio.to_thread(list_runs)

    @app.get("/api/v1/research/run")
    async def research_run(path: str):
        return await asyncio.to_thread(run_summary, path)

    @app.get("/api/v1/research/artifact")
    async def research_artifact(path: str, name: str):
        target = artifact(path, name)
        return FileResponse(target, filename=name, media_type="application/octet-stream")

    @app.post("/api/v1/login")
    async def login(data: Login, request: Request):
        if request.headers.get("origin") != ORIGIN:
            raise HTTPException(403, "来源地址不匹配，请检查 WORKBENCH_ORIGIN")
        try:
            token, csrf = await asyncio.to_thread(
                auth.login, data.username, data.password, request.client.host
            )
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        response = JSONResponse({"csrf": csrf, "username": data.username})
        response.set_cookie(
            "session",
            token,
            httponly=True,
            secure=ORIGIN.startswith("https://"),
            samesite="strict",
            max_age=8 * 3600,
        )
        return response

    @app.get("/api/v1/session")
    async def session(request: Request):
        return auth.session(request.cookies.get("session"))

    @app.post("/api/v1/logout")
    async def logout(request: Request):
        auth.logout(request.cookies.get("session", ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie("session")
        return response

    @app.get("/api/v1/snapshot")
    async def snapshot():
        return await call("snapshot")

    @app.post("/api/v1/connect")
    async def connect(request: Request):
        return await call("connect", {}, request_key(request))

    @app.post("/api/v1/subscribe/{symbol}")
    async def subscribe(symbol: str, request: Request):
        return await call("subscribe", {"symbol": symbol}, request_key(request))

    @app.post("/api/v1/watchlist/{symbol}/remove")
    async def remove_watchlist(symbol: str, request: Request):
        return await call("watchlist_remove", {"symbol": symbol}, request_key(request))

    @app.get("/api/v1/instances/{name}/chart")
    async def strategy_chart(name: str):
        return await call("strategy_chart", {"name": name})

    @app.get("/api/v1/intraday/{symbol}")
    async def intraday(symbol: str, day: date | None = None):
        return await call(
            "intraday", {"symbol": symbol, "date": day.isoformat() if day else None}
        )

    @app.get("/api/v1/bars/{symbol}")
    async def bars(
        symbol: str,
        minutes: int = 5,
        start: str | None = None,
        end: str | None = None,
        indicators: str = "{}",
    ):
        if minutes not in {1, 5, 15, 30, 60}:
            raise ValueError("不支持的 K 线周期")
        return await call(
            "bars",
            {
                "symbol": symbol,
                "minutes": minutes,
                "start": start,
                "end": end,
                "indicators": json.loads(indicators),
            },
        )

    @app.post("/api/v1/orders")
    async def orders(data: Trade, request: Request):
        return await call("order", data.model_dump(), request_key(request))

    @app.post("/api/v1/close")
    async def close(data: Trade, request: Request):
        return await call("close", data.model_dump(), request_key(request))

    @app.post("/api/v1/orders/{order_id}/cancel")
    async def cancel(order_id: str, request: Request):
        return await call("cancel", {"order_id": order_id}, request_key(request))

    @app.get("/api/v1/commands/{key}")
    async def command(key: str):
        return await call("command", {"id": key})

    @app.post("/api/v1/reconcile/{symbol}")
    async def reconcile(symbol: str, request: Request):
        return await call("reconcile", {"symbol": symbol}, request_key(request))

    @app.get("/api/v1/data")
    async def data():
        return await call("overview")

    @app.post("/api/v1/data/import")
    async def import_data(file: UploadFile, request: Request):
        raw = await file.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(413, "CSV 最大 20 MB")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "请使用 UTF-8 CSV") from exc
        return await call("import", {"csv": content}, request_key(request))

    @app.get("/api/v1/templates")
    async def templates():
        return TEMPLATES

    @app.get("/api/v1/versions")
    async def versions():
        return store.all("versions")

    @app.post("/api/v1/versions/check")
    async def check(data: Source):
        return {"class_name": validate_source(data.source)}

    @app.post("/api/v1/versions")
    async def save(data: Source):
        version = save_version(store, data.name, data.source)
        store.audit("save_version", {"id": version["id"], "name": version["name"]})
        return version

    @app.post("/api/v1/versions/{vid}/publish")
    async def publish(vid: str, request: Request):
        return await call("publish", {"id": vid}, request_key(request))

    @app.post("/api/v1/instances")
    async def instances(data: Instance, request: Request):
        return await call("add_instance", data.model_dump(), request_key(request))

    @app.post("/api/v1/instances/{name}/{action}")
    async def instance_action(
        name: str, action: Literal["init", "start", "stop", "remove"], request: Request
    ):
        return await call(
            "strategy", {"name": name, "action": action}, request_key(request)
        )

    @app.get("/api/v1/backtests")
    async def backtests():
        return store.all("backtests")

    @app.post("/api/v1/backtests")
    async def start_backtest(data: BacktestInput, request: Request):
        key = request_key(request)
        existing = store.get("backtests", key)
        p = data.model_dump(mode="json")
        if existing:
            if existing["parameters"] != p:
                raise HTTPException(409, "请求标识已用于其他回测")
            return existing
        if children:
            raise HTTPException(409, "已有回测运行，请等待完成")
        if data.end <= data.start:
            raise ValueError("结束时间必须晚于开始时间")
        version = store.get("versions", data.version)
        if not version or not version["published"]:
            raise ValueError("请选择已发布策略版本")
        job = {
            "id": key,
            "state": "queued",
            "parameters": p,
            "created": datetime.now().isoformat(),
        }
        store.put("backtests", key, job)
        log = open(RUNTIME / "backtest.log", "ab")
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "backend.backtest", key],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
        finally:
            log.close()
        children[key] = (child, time.monotonic())
        store.audit("backtest", {"id": key, "parameters": p})
        return job

    @app.websocket("/api/v1/ws")
    async def websocket(ws: WebSocket):
        token = ws.cookies.get("session")
        if ws.headers.get("origin") != ORIGIN or not auth.session(token):
            await ws.close(code=1008)
            return
        await ws.accept()
        import zmq.asyncio

        context = zmq.asyncio.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.setsockopt(zmq.RCVHWM, 2000)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(RPC_PUBLISH)
        try:
            await ws.send_json({"type": "resync"})
            while auth.session(token):
                if await socket.poll(1000):
                    topic, event = await socket.recv_pyobj()
                    if topic == "event":
                        await ws.send_json(event)
                else:
                    await ws.send_json({"type": "heartbeat"})
            await ws.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            socket.close()
            context.term()

    dist = ROOT / "frontend/dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        if (dist / "vendor").exists():
            app.mount("/vendor", StaticFiles(directory=dist / "vendor"), name="vendor")
        if (dist / "fonts").exists():
            app.mount("/fonts", StaticFiles(directory=dist / "fonts"), name="fonts")

        @app.get("/{path:path}")
        async def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")

    return app
