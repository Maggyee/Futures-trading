import argparse
import getpass
import signal
import subprocess
import sys

from .config import ROOT, prepare_runtime


def main():
    parser = argparse.ArgumentParser(description="CTP Workbench")
    parser.add_argument("command", choices=["init-admin", "serve", "worker", "api"])
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    prepare_runtime()
    if args.command == "init-admin":
        from .auth import HASHER
        from .store import Store

        password = getpass.getpass("管理员密码（至少 12 位）：")
        if len(password) < 12 or password != getpass.getpass("再次输入密码："):
            raise SystemExit("密码过短或两次输入不一致")
        store = Store()
        store.put(
            "config",
            "admin",
            {"username": args.username, "password_hash": HASHER.hash(password)},
        )
        with store.db() as db:
            db.execute("DELETE FROM sessions")
        print("管理员已设置，旧会话已失效。")
    elif args.command == "worker":
        from .worker import main as start_worker

        start_worker()
    elif args.command == "api":
        import uvicorn

        uvicorn.run(
            "backend.api:create_app",
            factory=True,
            host="127.0.0.1",
            port=8000,
            proxy_headers=False,
        )
    else:
        worker = subprocess.Popen(
            [sys.executable, "-m", "backend.cli", "worker"], cwd=ROOT
        )
        api = subprocess.Popen([sys.executable, "-m", "backend.cli", "api"], cwd=ROOT)

        def stop(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            import time

            while worker.poll() is None and api.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for proc in (api, worker):
                if proc.poll() is None:
                    proc.terminate()
            for proc in (api, worker):
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
