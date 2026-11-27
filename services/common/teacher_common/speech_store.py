"""Smart Talk conversations, Elevator Speech takes and their recordings, per user. Every query names the user,
so one user's attempts and audio are never reachable by another (admins included: practice is private)."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher_common.speech_models import PitchTake, TalkSession

AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}
_AUDIO_NAME = re.compile(r"[0-9a-f]{32}\.[a-z0-9]{2,5}")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --- recordings ---------------------------------------------------------------------------------------------------


class AudioStore:
    """Recordings on disk, one folder per user (named by a hash, so a username never becomes a path)."""

    def __init__(self, root: Path | str, keep_per_user: int) -> None:
        self.root = Path(root)
        self.keep_per_user = keep_per_user

    def _folder(self, username: str) -> Path:
        return self.root / hashlib.sha256(username.encode()).hexdigest()[:24]

    def save(self, username: str, data: bytes, content_type: str | None) -> tuple[str, str]:
        base = (content_type or "").split(";")[0].strip().lower()
        suffix = AUDIO_TYPES.get(base, ".audio")
        folder = self._folder(username)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{suffix}"
        (folder / name).write_bytes(data)
        return name, base if base in AUDIO_TYPES else "application/octet-stream"

    def path(self, username: str, name: str | None) -> Path | None:
        if not name or not _AUDIO_NAME.fullmatch(name):
            return None
        path = self._folder(username) / name
        return path if path.is_file() else None

    def delete(self, username: str, name: str | None) -> None:
        path = self.path(username, name)
        if path is not None:
            path.unlink(missing_ok=True)


# --- Smart Talk ---------------------------------------------------------------------------------------------------


def create_talk(
    session: Session, username: str, *, scenario: str, style: str, language: str,
    focus: str | None = None, retry_of: int | None = None,
) -> TalkSession:
    row = TalkSession(
        username=username, scenario=scenario, style=style, language=language, focus=focus, retry_of=retry_of,
        status="active", turns=[],
    )
    session.add(row)
    session.flush()
    return row


def get_talk(session: Session, username: str, talk_id: int) -> TalkSession | None:
    return session.execute(
        select(TalkSession).where(TalkSession.id == talk_id, TalkSession.username == username)
    ).scalar_one_or_none()


def list_talks(session: Session, username: str, scenario: str | None = None, limit: int = 50) -> list[TalkSession]:
    query = select(TalkSession).where(TalkSession.username == username)
    if scenario:
        query = query.where(TalkSession.scenario == scenario)
    return list(session.execute(query.order_by(TalkSession.id.desc()).limit(limit)).scalars())


def append_turns(row: TalkSession, *turns: dict) -> None:
    # A new list, so SQLAlchemy sees the JSON column change.
    row.turns = [*(row.turns or []), *turns]


def previous_finished_talk(session: Session, row: TalkSession) -> TalkSession | None:
    """The user's last finished conversation in the same scenario before this one."""
    return session.execute(
        select(TalkSession)
        .where(
            TalkSession.username == row.username,
            TalkSession.scenario == row.scenario,
            TalkSession.language == row.language,
            TalkSession.style == row.style,
            TalkSession.status == "finished",
            TalkSession.id < row.id,
        )
        .order_by(TalkSession.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def finish_talk(row: TalkSession, metrics: dict, feedback: dict | None) -> None:
    row.metrics, row.feedback, row.status = metrics, feedback, "finished"
    row.finished_at = datetime.now(UTC)


def delete_talk(session: Session, username: str, talk_id: int) -> bool:
    row = get_talk(session, username, talk_id)
    if row is None:
        return False
    session.delete(row)
    return True


def serialize_talk(row: TalkSession, *, full: bool = True) -> dict:
    data = {
        "id": row.id,
        "scenario": row.scenario,
        "style": row.style,
        "language": row.language,
        "focus": row.focus,
        "retry_of": row.retry_of,
        "status": row.status,
        "user_turns": sum(1 for turn in row.turns or [] if turn.get("role") == "user"),
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
        "tip": (row.feedback or {}).get("tip"),
    }
    if full:
        data.update({"turns": row.turns or [], "metrics": row.metrics, "feedback": row.feedback})
    return data


# --- Elevator Speech ----------------------------------------------------------------------------------------------


def add_take(session: Session, username: str, **values) -> PitchTake:
    row = PitchTake(username=username, **values)
    session.add(row)
    session.flush()
    return row


def get_take(session: Session, username: str, take_id: int) -> PitchTake | None:
    return session.execute(
        select(PitchTake).where(PitchTake.id == take_id, PitchTake.username == username)
    ).scalar_one_or_none()


def list_takes(session: Session, username: str, scenario: str | None = None, limit: int = 100) -> list[PitchTake]:
    query = select(PitchTake).where(PitchTake.username == username)
    if scenario:
        query = query.where(PitchTake.scenario == scenario)
    return list(session.execute(query.order_by(PitchTake.id.desc()).limit(limit)).scalars())


def previous_take(session: Session, row: PitchTake) -> PitchTake | None:
    """The user's last earlier take of the same scenario and target time."""
    return session.execute(
        select(PitchTake)
        .where(
            PitchTake.username == row.username,
            PitchTake.scenario == row.scenario,
            PitchTake.language == row.language,
            PitchTake.target_seconds == row.target_seconds,
            PitchTake.id < row.id,
        )
        .order_by(PitchTake.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def prune_audio(session: Session, username: str, audio: AudioStore) -> list[str]:
    """Keep the user's newest ``keep_per_user`` recordings; older takes keep their transcript and scores."""
    rows = list(
        session.execute(
            select(PitchTake)
            .where(PitchTake.username == username, PitchTake.audio_file.is_not(None))
            .order_by(PitchTake.id.desc())
            .offset(audio.keep_per_user)
        ).scalars()
    )
    expired = [row.audio_file for row in rows]
    for row in rows:
        row.audio_file, row.audio_type = None, None
    return expired


def delete_take(session: Session, username: str, take_id: int, audio: AudioStore) -> bool:
    row = get_take(session, username, take_id)
    if row is None:
        return False
    audio.delete(username, row.audio_file)
    session.delete(row)
    return True


def serialize_take(row: PitchTake, *, full: bool = True) -> dict:
    data = {
        "id": row.id,
        "scenario": row.scenario,
        "target_seconds": row.target_seconds,
        "language": row.language,
        "input": row.input,
        "duration_seconds": row.duration_seconds,
        "has_audio": bool(row.audio_file),
        "scores": row.scores,
        "words_per_minute": (row.metrics or {}).get("words_per_minute"),
        "created_at": _iso(row.created_at),
    }
    if full:
        data.update({
            "key_messages": row.key_messages or [],
            "transcript": row.transcript,
            "metrics": row.metrics,
            "coverage": row.coverage or [],
            "feedback": row.feedback,
            "followups": row.followups,
        })
    return data
