import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

HASHER = PasswordHasher()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store):
        self.store = store
        self.failures = {}

    def login(self, username, password, peer):
        now = time.time()
        attempts = self.failures.get(peer, [])
        attempts = [t for t in attempts if now - t < 300]
        self.failures[peer] = attempts
        if len(attempts) >= 5:
            raise ValueError("登录失败次数过多，请 5 分钟后再试")
        user = self.store.get("config", "admin")
        valid = False
        if user:
            try:
                valid = HASHER.verify(
                    user["password_hash"], password
                ) and secrets.compare_digest(user["username"], username)
            except VerificationError:
                pass
        if not valid:
            attempts.append(now)
            raise ValueError("用户名或密码错误，或管理员尚未初始化")
        self.failures.pop(peer, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.db() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (digest(token), csrf, now + 8 * 3600),
            )
        self.store.audit("login", {"username": username, "peer": peer})
        return token, csrf

    def session(self, token):
        if not token:
            return None
        with self.store.db() as db:
            row = db.execute(
                "SELECT csrf,expires FROM sessions WHERE token=?", (digest(token),)
            ).fetchone()
        return {"csrf": row[0]} if row and row[1] > time.time() else None

    def logout(self, token):
        with self.store.db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))
