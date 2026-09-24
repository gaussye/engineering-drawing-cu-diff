"""Opt-in, single-process demo login. Never a replacement for production identity."""

from collections import deque
from dataclasses import dataclass
import re
import secrets
import threading
import time

from werkzeug.security import check_password_hash


COOKIE = "__Host-cu_demo_login"
LOGIN_TTL = 8 * 3600
CHALLENGE_TTL = 600
MAX_SESSIONS = 128
MAX_ATTEMPTS = 10


class DemoAuthError(ValueError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


@dataclass
class LoginSession:
    csrf: str
    expires: float
    authenticated: bool = False


class DemoAuth:
    def __init__(self, username, password_hash):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
            raise ValueError("Demo username must be 1..64 ASCII letters, digits, underscore, dot or hyphen")
        if not isinstance(password_hash, str) or not re.fullmatch(
                r"scrypt:32768:8:1\$[A-Za-z0-9]{16}\$[a-f0-9]{128}", password_hash):
            raise ValueError("Demo password requires a Werkzeug scrypt:32768:8:1 hash with 16-character salt")
        self.username, self.password_hash = username, password_hash
        self.sessions = {}
        self.attempts = deque()
        self.lock = threading.RLock()

    def get(self, cookie):
        with self.lock:
            now = time.time()
            for token in [token for token, session in self.sessions.items() if session.expires <= now]:
                del self.sessions[token]
            return self.sessions.get(cookie)

    def issue(self, *, authenticated=False):
        with self.lock:
            self.get(None)
            if len(self.sessions) >= MAX_SESSIONS:
                raise DemoAuthError("登录会话已满，请稍后重试。", 429)
            token = secrets.token_urlsafe(32)
            session = LoginSession(secrets.token_urlsafe(32),
                                   time.time() + (LOGIN_TTL if authenticated else CHALLENGE_TTL),
                                   authenticated)
            self.sessions[token] = session
            return token, session

    def principal(self, cookie):
        session = self.get(cookie)
        return "demo:" + cookie if session and session.authenticated else None

    def login(self, cookie, csrf, username, password):
        with self.lock:
            session = self.get(cookie)
            if (not session or session.authenticated or not isinstance(csrf, str)
                    or not secrets.compare_digest(session.csrf.encode(), csrf.encode())):
                raise DemoAuthError("登录校验已失效，请刷新页面后重试。", 403)
            now = time.time()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= MAX_ATTEMPTS:
                raise DemoAuthError("登录尝试过于频繁，请一分钟后重试。", 429)
            self.attempts.append(now)
        password_ok = check_password_hash(self.password_hash, password[:256])
        if (len(password) > 256 or not password_ok
                or not secrets.compare_digest(username.encode(), self.username.encode())):
            raise DemoAuthError("用户名或密码错误。", 401)
        with self.lock:
            if self.get(cookie) is not session:
                raise DemoAuthError("登录校验已失效，请刷新页面后重试。", 403)
            del self.sessions[cookie]
            return self.issue(authenticated=True)

    def logout(self, cookie):
        with self.lock:
            self.sessions.pop(cookie, None)
