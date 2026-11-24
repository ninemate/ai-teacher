"""Security audit log.

Events hold only a fixed event name and non-secret identifiers (usernames, workspace/document ids,
an opaque target). Never pass passwords, tokens, cookies, titles, paths of private text or document
content here. Retention pruning is the only deletion path.
"""

import threading
import time

from sqlalchemy import delete
from teacher_common.config import get_settings
from teacher_common.models import AuditEvent

PRUNE_INTERVAL_SECONDS = 24 * 3600
_last_prune = 0.0
_prune_lock = threading.Lock()


def record(db, event: str, actor: str | None = None, target: str | None = None, ip: str | None = None) -> None:
    db.add(AuditEvent(timestamp=int(time.time()), event=event[:64], actor=actor[:64] if actor else None,
                      target=str(target)[:256] if target is not None else None, source_ip=ip))
    _maybe_prune(db)


def prune(db) -> None:
    days = get_settings().audit_retention_days
    if days:
        db.execute(delete(AuditEvent).where(AuditEvent.timestamp < int(time.time()) - days * 86400))


def _maybe_prune(db) -> None:
    """Prune at most once a day while the API runs, so a long-running service still honours retention."""
    global _last_prune
    now = time.monotonic()
    with _prune_lock:
        if _last_prune and now - _last_prune < PRUNE_INTERVAL_SECONDS:
            return
        _last_prune = now
    prune(db)


_windows: dict[str, float] = {}


def first_in_window(key: str, seconds: int = 60) -> bool:
    """True once per key per window: rate-limits audit writes that unauthenticated clients can trigger."""
    now = time.monotonic()
    with _prune_lock:
        if len(_windows) > 10_000:
            _windows.clear()
        last = _windows.get(key)
        if last is not None and now - last < seconds:
            return False
        _windows[key] = now
        return True


def client_ip(request) -> str | None:
    """The directly connected peer; forwarded headers are not trusted."""
    return request.client.host[:64] if request.client else None
