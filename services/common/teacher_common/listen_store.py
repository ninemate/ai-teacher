"""Listening list, resume position and bookmarks per user. Every function takes a Session."""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from teacher_common.listen_models import ListenBookmark, ListenItem

MAX_BOOKMARKS_PER_DOCUMENT = 100
SPEED_RANGE = (0.5, 3.0)


class LimitError(ValueError):
    pass


def clamp_speed(speed: float) -> float:
    return min(max(speed, SPEED_RANGE[0]), SPEED_RANGE[1])


def serialize_item(row: ListenItem) -> dict:
    return {
        "document_id": row.document_id,
        "section": row.section,
        "segment": row.segment,
        "speed": row.speed,
        "voice": row.voice,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_bookmark(row: ListenBookmark) -> dict:
    return {
        "id": row.id,
        "section": row.section,
        "segment": row.segment,
        "label": row.label,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def get_item(session: Session, username: str, document_id: int) -> ListenItem | None:
    return session.execute(
        select(ListenItem).where(ListenItem.username == username, ListenItem.document_id == document_id)
    ).scalar_one_or_none()


def list_items(session: Session, username: str) -> list[ListenItem]:
    return list(
        session.execute(
            select(ListenItem).where(ListenItem.username == username).order_by(ListenItem.updated_at.desc())
        ).scalars()
    )


def add_item(session: Session, username: str, document_id: int) -> ListenItem:
    row = get_item(session, username, document_id)
    if row is None:
        row = ListenItem(username=username, document_id=document_id)
        session.add(row)
        session.flush()
    return row


def save_position(
    session: Session,
    username: str,
    document_id: int,
    *,
    section: int,
    segment: int,
    speed: float | None = None,
    voice: str | None = None,
) -> ListenItem:
    row = add_item(session, username, document_id)
    row.section, row.segment = max(section, 0), max(segment, 0)
    if speed is not None:
        row.speed = clamp_speed(speed)
    if voice is not None:
        row.voice = voice or None
    # onupdate only fires on a changed value; the last write must always win on another device.
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def remove_item(session: Session, username: str, document_id: int) -> bool:
    removed = session.execute(
        delete(ListenItem).where(ListenItem.username == username, ListenItem.document_id == document_id)
    ).rowcount
    session.execute(
        delete(ListenBookmark).where(ListenBookmark.username == username, ListenBookmark.document_id == document_id)
    )
    return bool(removed)


def list_bookmarks(session: Session, username: str, document_id: int) -> list[ListenBookmark]:
    return list(
        session.execute(
            select(ListenBookmark)
            .where(ListenBookmark.username == username, ListenBookmark.document_id == document_id)
            .order_by(ListenBookmark.section, ListenBookmark.segment, ListenBookmark.id)
        ).scalars()
    )


def add_bookmark(
    session: Session, username: str, document_id: int, *, section: int, segment: int, label: str
) -> ListenBookmark:
    if len(list_bookmarks(session, username, document_id)) >= MAX_BOOKMARKS_PER_DOCUMENT:
        raise LimitError(f"Legfeljebb {MAX_BOOKMARKS_PER_DOCUMENT} könyvjelző tárolható dokumentumonként.")
    row = ListenBookmark(
        username=username, document_id=document_id, section=max(section, 0), segment=max(segment, 0), label=label
    )
    session.add(row)
    session.flush()
    return row


def delete_bookmark(session: Session, username: str, document_id: int, bookmark_id: int) -> bool:
    return bool(
        session.execute(
            delete(ListenBookmark).where(
                ListenBookmark.id == bookmark_id,
                ListenBookmark.username == username,
                ListenBookmark.document_id == document_id,
            )
        ).rowcount
    )


def delete_for_document(session: Session, document_id: int) -> None:
    """Drop every user's list entry and bookmarks for a document that is gone."""
    session.execute(delete(ListenItem).where(ListenItem.document_id == document_id))
    session.execute(delete(ListenBookmark).where(ListenBookmark.document_id == document_id))
