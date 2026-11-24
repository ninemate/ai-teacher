"""Tolmács routes: push-to-talk HU<->EN interpreting, saved sessions per user, export and latency summary.

The router has no auth of its own: the session and CSRF middleware in ``main.py`` protects every route. Saved
sessions belong to the account that made them; every query filters by username and another account's id is a 404.
"""

import base64
import csv
import io
import json
import uuid
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from teacher_common import interpreter
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.interpreter_models import InterpreterSession

MAX_TURNS = 500
MAX_SESSIONS_PER_USER = 200
EXPORT_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
}
LANG_NAMES = {"hu": "Magyar", "en": "English"}


class TurnLatency(BaseModel):
    stt_ms: float | None = None
    translate_ms: float | None = None
    tts_ms: float | None = None
    total_ms: float | None = None


class SavedTurn(BaseModel):
    source_lang: Literal["hu", "en"]
    target_lang: Literal["hu", "en"]
    source_text: str = Field(max_length=6000)
    translation: str = Field(max_length=6000)
    latency: TurnLatency | None = None


class SessionBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    mode: str = Field(default="fast", max_length=20)
    turns: list[SavedTurn] = Field(max_length=MAX_TURNS)


def render_export(row: dict, fmt: str) -> str:
    turns = row["turns"]
    if fmt == "json":
        return json.dumps(row, ensure_ascii=False, indent=2)
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["#", "from", "source", "to", "translation", "stt_ms", "translate_ms", "tts_ms", "total_ms"])
        for i, t in enumerate(turns, 1):
            lat = t.get("latency") or {}
            writer.writerow(
                [i, t["source_lang"], _csv_text(t["source_text"]), t["target_lang"], _csv_text(t["translation"])]
                + [lat.get(k) for k in interpreter.STAGES]
            )
        return out.getvalue()
    if fmt == "md":
        lines = [f"# {row['title']}", "", f"Mód: {row['mode']} · {row['created_at']}", "", "| Magyar | English |", "| --- | --- |"]
        for t in turns:
            hu, en = (
                (t["source_text"], t["translation"]) if t["source_lang"] == "hu" else (t["translation"], t["source_text"])
            )
            lines.append(f"| {hu.replace('|', '\\|')} | {en.replace('|', '\\|')} |")
        return "\n".join(lines) + "\n"
    lines = [row["title"], ""]
    for t in turns:
        lines += [f"[{LANG_NAMES[t['source_lang']]}] {t['source_text']}", f"[{LANG_NAMES[t['target_lang']]}] {t['translation']}", ""]
    return "\n".join(lines)


def _csv_text(value: str) -> str:
    """Keep spreadsheet applications from evaluating a transcript cell: a leading apostrophe makes it literal text."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") else value


def build_router(engine: interpreter.Interpreter, log: interpreter.LatencyLog, admins: set[str]) -> APIRouter:
    settings = get_settings()
    router = APIRouter(prefix="/interpreter", tags=["interpreter"])

    def _user(request: Request) -> tuple[str, bool]:
        principal = getattr(request.state, "session", None)
        if principal is None and settings.auth_enabled:
            raise HTTPException(status_code=401, detail="Bejelentkezés szükséges.")
        username = principal.username if principal else "local"
        return username, (principal.role == "admin" if principal else False) or username in admins

    def _row(db, username: str, session_id: str) -> InterpreterSession:
        row = db.execute(
            select(InterpreterSession).where(
                InterpreterSession.id == session_id, InterpreterSession.username == username
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="A munkamenet nem található.")
        return row

    def _dump(row: InterpreterSession) -> dict:
        return {
            "id": row.id,
            "title": row.title,
            "mode": row.mode,
            "turns": json.loads(row.turns_json),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @router.get("/config")
    def config(request: Request):
        _user(request)
        return {
            "modes": {name: {"label": m.get("label", name)} for name, m in engine.modes.items()},
            "languages": list(interpreter.LANGUAGES),
            "max_glossary_chars": interpreter.MAX_GLOSSARY_CHARS,
            "voices": {lang: engine.voice_for(lang) for lang in interpreter.LANGUAGES},
        }

    @router.post("/turn")
    def turn(
        request: Request,
        audio: UploadFile | None = File(default=None),
        text: str | None = Form(default=None, max_length=4000),
        side: str = Form(default="auto"),
        mode: str = Form(default="fast"),
        glossary: str = Form(default="", max_length=20000),
        speak: bool = Form(default=True),
    ):
        username, _ = _user(request)
        payload = None
        if audio is not None:
            limit = int(settings.voice_max_upload_mb * 1024 * 1024)
            content = audio.file.read(limit + 1)
            if len(content) > limit:
                raise HTTPException(status_code=413, detail="A hangfelvétel túl nagy.")
            if not content:
                raise HTTPException(status_code=422, detail="Üres hangfelvétel.")
            suffix = "." + (audio.filename or "").rpartition(".")[2][:7] if "." in (audio.filename or "") else ""
            payload = (content, f"audio{suffix}", audio.content_type or "application/octet-stream")
        try:
            result = engine.run(mode, side, audio=payload, text=text, glossary=glossary, speak=speak)
        except interpreter.InterpreterError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        log.record(username, result)
        return {
            "mode": result.mode,
            "source_lang": result.source_lang,
            "target_lang": result.target_lang,
            "source_text": result.source_text,
            "translation": result.translation,
            "latency": result.latency.as_dict(),
            "audio": base64.b64encode(result.audio).decode() if result.audio else None,
            "audio_type": "audio/mpeg",
            "tts_error": result.tts_error,
        }

    @router.get("/latency")
    def latency(request: Request, scope: Literal["mine", "all"] = "mine"):
        username, is_admin = _user(request)
        if scope == "all" and not is_admin:
            raise HTTPException(status_code=403, detail="Ehhez adminisztrátori jogosultság kell.")
        return interpreter.summarise(log.read(None if scope == "all" else username))

    @router.get("/sessions")
    def list_sessions(request: Request):
        username, _ = _user(request)
        with session_scope() as db:
            rows = db.execute(
                select(InterpreterSession)
                .where(InterpreterSession.username == username)
                .order_by(InterpreterSession.updated_at.desc())
            ).scalars()
            return [
                {"id": r.id, "title": r.title, "mode": r.mode, "turns": len(json.loads(r.turns_json)),
                 "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                for r in rows
            ]

    @router.post("/sessions", status_code=201)
    def create_session(body: SessionBody, request: Request):
        username, _ = _user(request)
        with session_scope() as db:
            count = len(db.execute(select(InterpreterSession.id).where(InterpreterSession.username == username)).all())
            if count >= MAX_SESSIONS_PER_USER:
                raise HTTPException(status_code=409, detail="Elértad a mentett munkamenetek számának korlátját.")
            row = InterpreterSession(
                id=uuid.uuid4().hex,
                username=username,
                title=(body.title or "").strip() or "Tolmács munkamenet",
                mode=body.mode,
                turns_json=json.dumps([t.model_dump() for t in body.turns], ensure_ascii=False),
            )
            db.add(row)
            db.flush()
            db.refresh(row)
            return _dump(row)

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str, request: Request):
        username, _ = _user(request)
        with session_scope() as db:
            return _dump(_row(db, username, session_id))

    @router.put("/sessions/{session_id}")
    def update_session(session_id: str, body: SessionBody, request: Request):
        username, _ = _user(request)
        with session_scope() as db:
            row = _row(db, username, session_id)
            if body.title and body.title.strip():
                row.title = body.title.strip()
            row.mode = body.mode
            row.turns_json = json.dumps([t.model_dump() for t in body.turns], ensure_ascii=False)
            db.flush()
            db.refresh(row)
            return _dump(row)

    @router.delete("/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str, request: Request):
        username, _ = _user(request)
        with session_scope() as db:
            db.delete(_row(db, username, session_id))
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/export")
    def export_session(session_id: str, request: Request, format: Literal["md", "txt", "csv", "json"] = "md"):
        username, _ = _user(request)
        with session_scope() as db:
            data = _dump(_row(db, username, session_id))
        filename = quote(f"tolmacs-{session_id[:8]}.{format}")
        return Response(
            render_export(data, format),
            media_type=EXPORT_TYPES[format],
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    return router
