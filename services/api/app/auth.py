"""Built-in login for the teacher UI and API.

Users come from configuration (TEACHER_AUTH_USERS / TEACHER_AUTH_USERS_FILE) as argon2 hashes.
Sessions are random cookie tokens; only their SHA-256 is stored in the metadata database.

Run `python -m auth` (or `python auth.py`) to hash a password for the configuration.
"""

import getpass
import hashlib
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request, Response
from sqlalchemy import delete, select
from teacher_common.config import Settings
from teacher_common.db import session_scope
from teacher_common.models import AuthSession

SESSION_COOKIE = "teacher_session"
LOGIN_CSRF_COOKIE = "teacher_login_csrf"
CSRF_HEADER = "X-CSRF-Token"

password_hasher = PasswordHasher()
# Verified against when the username is unknown, so a miss costs the same time as a wrong password.
_DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class SessionInfo:
    username: str
    csrf_token: str
    role: str = "user"


def verify_credentials(users: dict[str, str], username: str, password: str) -> bool:
    stored = users.get(username)
    try:
        password_hasher.verify(stored or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return stored is not None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def credential_tag(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()


def create_session(username: str, password_hash: str, ttl_seconds: int, role: str = "user") -> tuple[str, SessionInfo]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = int(time.time())
    with session_scope() as db:
        db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        db.add(
            AuthSession(
                token_hash=_token_hash(token),
                username=username,
                csrf_token=csrf_token,
                credential_tag=credential_tag(password_hash),
                created_at=now,
                expires_at=now + ttl_seconds,
            )
        )
    return token, SessionInfo(username=username, csrf_token=csrf_token, role=role)


def load_session(token: str | None, users: dict[str, str], admins: set[str] | None = None) -> SessionInfo | None:
    if not token:
        return None
    with session_scope() as db:
        row = db.execute(select(AuthSession).where(AuthSession.token_hash == _token_hash(token))).scalars().first()
        if row is None:
            return None
        stored = users.get(row.username)
        if row.expires_at <= int(time.time()) or stored is None or row.credential_tag != credential_tag(stored):
            db.delete(row)
            return None
        return SessionInfo(username=row.username, csrf_token=row.csrf_token,
                           role="admin" if row.username in (admins or set()) else "user")


def delete_session(token: str | None) -> None:
    if not token:
        return
    with session_scope() as db:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))


def same_secret(supplied: str, expected: str) -> bool:
    """Constant-time comparison that also accepts non-ASCII input (str compare_digest raises on it)."""
    return bool(supplied) and secrets.compare_digest(supplied.encode(), expected.encode())


class LoginRateLimiter:
    """Counts login attempts per key in a sliding window. In memory: the API runs as a single process.

    `acquire` checks and counts an attempt in one step, so parallel guesses cannot all pass the check
    before any of them is counted; `release` takes the attempt back once the password proved right.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._failures.get(key, deque())
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if attempts:
            self._failures[key] = attempts
        else:
            self._failures.pop(key, None)
        return attempts

    def acquire(self, keys: list[str]) -> int:
        """Count an attempt and return 0, or return the seconds until a blocked key frees up."""
        now = time.monotonic()
        with self._lock:
            if len(self._failures) > 10_000:
                for key in list(self._failures):
                    self._prune(key, now)
            current = {key: self._prune(key, now) for key in keys}
            waits = [
                int(attempts[0] + self.window_seconds - now) + 1
                for attempts in current.values()
                if len(attempts) >= self.max_attempts
            ]
            if waits:
                return max(waits)
            for key, attempts in current.items():
                attempts.append(now)
                self._failures[key] = attempts
        return 0

    def release(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                attempts = self._failures.get(key)
                if attempts:
                    attempts.pop()
                if not attempts:
                    self._failures.pop(key, None)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._failures.clear()
            else:
                self._failures.pop(key, None)


def cookie_secure(request: Request, settings: Settings) -> bool:
    if settings.teacher_cookie_secure != "auto":
        return settings.teacher_cookie_secure == "true"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def set_cookie(response: Response, request: Request, settings: Settings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request, settings),
    )


def origin_allowed(request: Request) -> bool:
    """Reject cross-site requests that carry an Origin header pointing elsewhere."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return netloc == request.headers.get("host", "")


def csrf_valid(request: Request, session: SessionInfo) -> bool:
    return same_secret(request.headers.get(CSRF_HEADER, ""), session.csrf_token)


def safe_next(target: str | None) -> str:
    """Only allow local absolute paths as a post-login redirect."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def main() -> int:
    password = getpass.getpass("Jelszó: ")
    if len(password) < 8:
        print("A jelszó legyen legalább 8 karakter.", file=sys.stderr)
        return 1
    if getpass.getpass("Jelszó újra: ") != password:
        print("A két jelszó nem egyezik.", file=sys.stderr)
        return 1
    print(password_hasher.hash(password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
