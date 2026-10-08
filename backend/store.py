import hashlib
import json
import math
import sqlite3
import time
from dataclasses import is_dataclass
from datetime import date, datetime
from enum import Enum

from .config import RUNTIME


def wire(value):
    if is_dataclass(value):
        return wire(vars(value))
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    if hasattr(value, "tolist"):
        return wire(value.tolist())
    if hasattr(value, "item"):
        return wire(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class Store:
    def __init__(self, path=None):
        self.path = path or RUNTIME / "workbench.db"
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents(kind TEXT, id TEXT, body TEXT, updated REAL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, fingerprint TEXT, body TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, csrf TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, action TEXT, body TEXT);
            """)

    def db(self):
        return sqlite3.connect(self.path, timeout=15)

    def put(self, kind, key, body):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO documents VALUES(?,?,?,?)",
                (kind, key, json.dumps(wire(body), ensure_ascii=False), time.time()),
            )
        return wire(body)

    def get(self, kind, key):
        with self.db() as db:
            row = db.execute(
                "SELECT body FROM documents WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.db() as db:
            rows = db.execute(
                "SELECT body FROM documents WHERE kind=? ORDER BY updated DESC", (kind,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def audit(self, action, body):
        with self.db() as db:
            db.execute(
                "INSERT INTO audit(at,action,body) VALUES(?,?,?)",
                (time.time(), action, json.dumps(wire(body), ensure_ascii=False)),
            )

    def claim(self, key, payload):
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        initial = {
            "id": key,
            "state": "pending",
            "action": payload.get("action"),
            "symbol": payload.get("payload", {}).get("symbol"),
        }
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO commands VALUES(?,?,?,?)",
                (key, fingerprint, json.dumps(initial), time.time()),
            )
            inserted = db.execute("SELECT changes()").fetchone()[0]
            found = db.execute(
                "SELECT fingerprint,body FROM commands WHERE id=?", (key,)
            ).fetchone()
        if found[0] != fingerprint:
            raise ValueError("请求标识已被其他操作使用")
        return bool(inserted), json.loads(found[1])

    def finish(self, key, body):
        previous = self.command(key) or {}
        body = {
            "id": key,
            "action": previous.get("action"),
            "symbol": previous.get("symbol"),
            **wire(body),
        }
        with self.db() as db:
            db.execute(
                "UPDATE commands SET body=?,updated=? WHERE id=?",
                (json.dumps(body, ensure_ascii=False), time.time(), key),
            )
        return body

    def command(self, key):
        with self.db() as db:
            row = db.execute("SELECT body FROM commands WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def commands(self, limit=30):
        with self.db() as db:
            rows = db.execute(
                "SELECT body FROM commands ORDER BY updated DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover(self):
        # Never replay a command across a worker crash: submission may have happened.
        with self.db() as db:
            rows = db.execute("SELECT id,body FROM commands").fetchall()
        for key, raw in rows:
            body = json.loads(raw)
            if body.get("state") in {"pending", "waiting_cancel", "submitting"}:
                self.finish(
                    key,
                    {
                        **body,
                        "state": "unknown",
                        "error": "交易进程重启，需核对柜台委托后再操作；未自动重发",
                    },
                )
