"""Listen to library documents: sections and segments in reading order, on-demand audio, resume and bookmarks.

Kept in its own module so main.py only needs one ``include_router`` call. The router has no auth of its own:
the session and CSRF middleware in ``main.py`` protects every route. Every route that names a document checks
it against the caller's workspaces first, audio included; the audio cache is only read after that check.
"""

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from teacher_common import listen_store, listening
from teacher_common.audio_cache import AudioCache
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.models import DocumentRecord
from teacher_common.workspaces import accessible_workspace_ids

# fetch_chunks(source_path) -> stored chunk payloads of the document, in any order
FetchChunks = Callable[[str], list[dict]]
# speak(text, voice) -> MP3 bytes; raises HTTPException when the voice service is unavailable
Speak = Callable[[str, str | None], bytes]

VOICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
SWEEP_INTERVAL_SECONDS = 600
BUILT_DOCUMENTS_KEPT = 4


class PositionRequest(BaseModel):
    section: int = Field(ge=0)
    segment: int = Field(ge=0)
    speed: float | None = Field(default=None, ge=0.5, le=3.0)
    voice: str | None = Field(default=None, max_length=100)


class AddRequest(BaseModel):
    document_id: int


class BookmarkRequest(BaseModel):
    section: int = Field(ge=0)
    segment: int = Field(ge=0)
    label: str | None = Field(default=None, max_length=200)


def sweep_orphans(cache: AudioCache) -> int:
    """Remove cached audio of documents that were deleted or re-indexed (the ingestion service cannot reach it)."""
    with session_scope() as session:
        rows = session.execute(
            select(DocumentRecord.id, DocumentRecord.source_hash).where(DocumentRecord.deleted.is_(False))
        ).all()
    return cache.sweep(AudioCache.document_key(row_id, source_hash) for row_id, source_hash in rows)


def start_periodic_sweep(cache: AudioCache, interval: float = SWEEP_INTERVAL_SECONDS) -> threading.Thread:
    """Sweep in the background, so audio of documents the indexer removed goes without waiting for a request."""

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                sweep_orphans(cache)
            except Exception:  # a failed sweep is retried at the next interval
                logging.getLogger("uvicorn").exception("Listening cache sweep failed")

    thread = threading.Thread(target=loop, name="listen-cache-sweep", daemon=True)
    thread.start()
    return thread


def build_router(fetch_chunks: FetchChunks, speak: Speak, cache: AudioCache) -> APIRouter:
    settings = get_settings()
    router = APIRouter(prefix="/listen", tags=["listen"])
    built: OrderedDict[tuple[int, str], list[listening.Section]] = OrderedDict()
    built_lock = threading.Lock()
    synthesis = threading.Semaphore(1)  # one segment at a time: the voice runs on the same machine as everything
    sweep_state = {"last": 0.0}

    def _identity(request: Request, session) -> tuple[str, set[str]]:
        principal = getattr(request.state, "session", None)
        if principal is None and settings.auth_enabled:
            raise HTTPException(status_code=401, detail="Bejelentkezés szükséges.")
        username = principal.username if principal else "local"
        role = principal.role if principal else "user"
        return username, accessible_workspace_ids(session, username, admin=role == "admin")

    def _document(session, workspace_ids: set[str], document_id: int) -> dict:
        row = session.execute(
            select(DocumentRecord).where(
                DocumentRecord.id == document_id,
                DocumentRecord.workspace_id.in_(workspace_ids),
                DocumentRecord.deleted.is_(False),
                DocumentRecord.status == "indexed",
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="A dokumentum nem található.")
        return {
            "id": row.id,
            "title": row.title,
            "author": row.author,
            "language": row.language,
            "file_type": row.file_type,
            "source_path": row.source_path,
            "source_hash": row.source_hash,
        }

    def _sections(document: dict) -> list[listening.Section]:
        key = (document["id"], document["source_hash"])
        with built_lock:
            if key in built:
                built.move_to_end(key)
                return built[key]
        chunks = fetch_chunks(document["source_path"])
        sections = listening.build_sections(
            chunks,
            max_overlap=settings.ingest_chunk_overlap,
            max_chars=settings.listen_segment_chars,
            ocr_min_confidence=settings.ocr_low_confidence,
        )
        if not sections:
            raise HTTPException(
                status_code=409,
                detail="A dokumentum szövege most nem olvasható vissza az indexből. Indíts új indexelést.",
            )
        with built_lock:
            built[key] = sections
            while len(built) > BUILT_DOCUMENTS_KEPT:
                built.popitem(last=False)
        return sections

    def _segment(sections: list[listening.Section], section: int, segment: int) -> listening.Segment:
        if section >= len(sections) or segment >= len(sections[section].segments):
            raise HTTPException(status_code=404, detail="Ez a szakasz vagy részlet nem létezik.")
        return sections[section].segments[segment]

    def _clamp(sections: list[listening.Section], section: int, segment: int) -> tuple[int, int]:
        section = min(section, len(sections) - 1)
        return section, min(segment, len(sections[section].segments) - 1)

    def _maybe_sweep() -> None:
        now = time.monotonic()
        if now - sweep_state["last"] >= SWEEP_INTERVAL_SECONDS:
            sweep_state["last"] = now
            sweep_orphans(cache)

    def _summary(document: dict) -> dict:
        return {key: document[key] for key in ("id", "title", "author", "language", "file_type")}

    # --- the user's list ---

    @router.get("/library")
    def library(request: Request):
        _maybe_sweep()
        with session_scope() as session:
            username, workspace_ids = _identity(request, session)
            items = listen_store.list_items(session, username)
            if not items or not workspace_ids:
                return []
            documents = {
                row.id: row
                for row in session.execute(
                    select(DocumentRecord).where(
                        DocumentRecord.id.in_([item.document_id for item in items]),
                        DocumentRecord.workspace_id.in_(workspace_ids),
                        DocumentRecord.deleted.is_(False),
                        DocumentRecord.status == "indexed",
                    )
                ).scalars()
            }
            return [
                {
                    "document": {
                        "id": documents[item.document_id].id,
                        "title": documents[item.document_id].title,
                        "author": documents[item.document_id].author,
                        "language": documents[item.document_id].language,
                        "file_type": documents[item.document_id].file_type,
                    },
                    "position": listen_store.serialize_item(item),
                }
                for item in items
                if item.document_id in documents
            ]

    @router.post("/library", status_code=201)
    def add_to_library(payload: AddRequest, request: Request):
        with session_scope() as session:
            username, workspace_ids = _identity(request, session)
            document = _document(session, workspace_ids, payload.document_id)
            item = listen_store.add_item(session, username, document["id"])
            return {"document": _summary(document), "position": listen_store.serialize_item(item)}

    @router.delete("/library/{document_id}", status_code=204)
    def remove_from_library(document_id: int, request: Request):
        with session_scope() as session:
            username, _ = _identity(request, session)
            if not listen_store.remove_item(session, username, document_id):
                raise HTTPException(status_code=404, detail="A dokumentum nincs a hallgatási listán.")
        return Response(status_code=204)

    # --- one document ---

    @router.get("/{document_id}")
    def overview(document_id: int, request: Request):
        with session_scope() as session:
            username, workspace_ids = _identity(request, session)
            document = _document(session, workspace_ids, document_id)
            item = listen_store.get_item(session, username, document_id)
            position = listen_store.serialize_item(item) if item else None
            bookmarks = [listen_store.serialize_bookmark(row) for row in listen_store.list_bookmarks(session, username, document_id)]
        sections = _sections(document)
        if position:
            position["section"], position["segment"] = _clamp(sections, position["section"], position["segment"])
        return {
            "document": _summary(document),
            "sections": [
                {
                    "index": index,
                    "title": section.title,
                    "locator": section.locator,
                    "page": section.page,
                    "segments": len(section.segments),
                    "chars": section.chars,
                    "ocr": any(segment.ocr for segment in section.segments),
                    "low_confidence": any(segment.low_confidence for segment in section.segments),
                }
                for index, section in enumerate(sections)
            ],
            "position": position,
            "bookmarks": bookmarks,
            "ocr_low_confidence_limit": settings.ocr_low_confidence,
        }

    @router.get("/{document_id}/sections/{section}")
    def section_text(document_id: int, section: int, request: Request):
        with session_scope() as session:
            _, workspace_ids = _identity(request, session)
            document = _document(session, workspace_ids, document_id)
        sections = _sections(document)
        _segment(sections, section, 0)
        current = sections[section]
        return {
            "index": section,
            "title": current.title,
            "locator": current.locator,
            "segments": [
                {
                    "index": index,
                    "text": segment.text,
                    "ocr": segment.ocr,
                    "ocr_confidence": segment.ocr_confidence,
                    "low_confidence": segment.low_confidence,
                }
                for index, segment in enumerate(current.segments)
            ],
        }

    @router.get("/{document_id}/audio/{section}/{segment}")
    def audio(document_id: int, section: int, segment: int, request: Request, voice: str | None = None):
        if voice is not None and not VOICE_NAME.fullmatch(voice):
            raise HTTPException(status_code=422, detail="Ismeretlen hang.")
        with session_scope() as session:
            _, workspace_ids = _identity(request, session)
            document = _document(session, workspace_ids, document_id)
        text = _segment(_sections(document), section, segment).text
        folder = AudioCache.document_key(document["id"], document["source_hash"])
        voice_key = voice or "default"
        data = cache.get(folder, voice_key, text)
        if data is None:
            try:
                with cache.key_lock(folder, voice_key, text):
                    data = cache.get(folder, voice_key, text)
                    if data is None:
                        with synthesis:
                            data = speak(text, voice)
                        cache.put(folder, voice_key, text, data)
            finally:
                cache.release_key_lock(folder, voice_key, text)
        return Response(data, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=0, no-store"})

    # --- resume and bookmarks (stored per user) ---

    @router.put("/{document_id}/position")
    def save_position(document_id: int, payload: PositionRequest, request: Request):
        if payload.voice is not None and payload.voice and not VOICE_NAME.fullmatch(payload.voice):
            raise HTTPException(status_code=422, detail="Ismeretlen hang.")
        with session_scope() as session:
            username, workspace_ids = _identity(request, session)
            document = _document(session, workspace_ids, document_id)
            _segment(_sections(document), payload.section, payload.segment)
            item = listen_store.save_position(
                session, username, document_id,
                section=payload.section, segment=payload.segment, speed=payload.speed, voice=payload.voice,
            )
            return listen_store.serialize_item(item)

    @router.post("/{document_id}/bookmarks", status_code=201)
    def create_bookmark(document_id: int, payload: BookmarkRequest, request: Request):
        with session_scope() as session:
            username, workspace_ids = _identity(request, session)
            document = _document(session, workspace_ids, document_id)
            sections = _sections(document)
            text = _segment(sections, payload.section, payload.segment).text
            label = (payload.label or "").strip() or f"{sections[payload.section].title}: {text[:50]}"
            try:
                row = listen_store.add_bookmark(
                    session, username, document_id, section=payload.section, segment=payload.segment, label=label
                )
            except listen_store.LimitError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return listen_store.serialize_bookmark(row)

    @router.delete("/{document_id}/bookmarks/{bookmark_id}", status_code=204)
    def remove_bookmark(document_id: int, bookmark_id: int, request: Request):
        with session_scope() as session:
            username, _ = _identity(request, session)
            if not listen_store.delete_bookmark(session, username, document_id, bookmark_id):
                raise HTTPException(status_code=404, detail="A könyvjelző nem található.")
        return Response(status_code=204)

    return router
