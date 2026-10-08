"""Isolated browser-test server. Never uses the user's runtime or CTP credentials."""

import os
import subprocess
import sys
import tempfile
import time

os.environ["WORKBENCH_RUNTIME"] = tempfile.mkdtemp(prefix="workbench-browser-")
os.environ["WORKBENCH_ORIGIN"] = "http://127.0.0.1:8001"
os.environ["WORKBENCH_RPC_PORT"] = "20240"

from backend.config import ROOT, prepare_runtime

prepare_runtime()
from backend.native_locale import prepare_ctp_locale

prepare_ctp_locale()
from backend.auth import HASHER
from backend.store import Store

store = Store()
store.put(
    "config",
    "admin",
    {"username": "admin", "password_hash": HASHER.hash("browser-test-password-123")},
)

if __name__ == "__main__":
    import uvicorn

    worker = subprocess.Popen(
        [sys.executable, str(ROOT / "tests/paper_worker.py")], cwd=ROOT
    )
    try:
        from backend.api import rpc_call

        deadline = time.monotonic() + 40
        while True:
            if worker.poll() is not None:
                raise RuntimeError("Browser-test worker exited during startup")
            try:
                rpc_call("snapshot")
                break
            except TimeoutError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Browser-test worker did not become ready"
                    ) from exc
        uvicorn.run(
            "backend.api:create_app",
            factory=True,
            host="127.0.0.1",
            port=8001,
            log_level="warning",
        )
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
