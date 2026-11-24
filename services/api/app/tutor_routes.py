"""Personal HU/EN tutor. Authentication and CSRF use the normal API middleware."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, get_args

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, text
from teacher_common import tutor_store as store
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.study_models import FlashcardDeck
from teacher_common.tutor_models import TutorSession
from teacher_common.workspaces import accessible_workspace_ids

Language = Literal["en", "hu"]
Level = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
Mode = Literal["conversation", "roleplay", "listening", "writing"]
Phrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class Profile(BaseModel):
    language: Language = "en"
    level: Level = "B2"
    goal: str = Field(default="", max_length=500)
    auto_level: bool = False


class Start(BaseModel):
    mode: Mode = "conversation"
    scenario: str = Field(default="", max_length=500)
    source_path: str | None = Field(default=None, min_length=1, max_length=4096)
    deck_id: int | None = Field(default=None, gt=0)


class Answer(BaseModel):
    text: Message
    revision: int | None = Field(default=None, ge=1)


ErrorType = Literal["grammar", "spelling", "vocabulary", "comprehension", "other"]
# Local models drift from the prompt's four types; keep the correction and file it under the nearest one.
TYPE_ALIASES = {
    "usage": "vocabulary",
    "word_choice": "vocabulary",
    "lexical": "vocabulary",
    "word_order": "grammar",
    "syntax": "grammar",
    "tense": "grammar",
    "punctuation": "spelling",
    "orthography": "spelling",
    "listening": "comprehension",
    "understanding": "comprehension",
}
LIST_LIMIT = 6


class Correction(BaseModel):
    type: ErrorType
    explanation: Phrase
    example: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class Word(BaseModel):
    front: Phrase
    back: Phrase


class Card(Word):
    deck_id: int | None = Field(default=None, gt=0)


class Feedback(BaseModel):
    reply: Message
    corrections: list[Correction] = Field(default_factory=list, max_length=LIST_LIMIT)
    vocabulary: list[Word] = Field(default_factory=list, max_length=LIST_LIMIT)


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def parse_feedback(raw: str) -> dict:
    """Keep a usable reply even when the model bends the structure; only a missing reply is an error."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    corrections = []
    for item in data.get("corrections") if isinstance(data.get("corrections"), list) else []:
        if not isinstance(item, dict) or not _text(item.get("explanation"), 500):
            continue
        kind = _text(item.get("type"), 40).lower().replace(" ", "_").replace("-", "_")
        kind = TYPE_ALIASES.get(kind, kind)
        corrections.append(
            {
                "type": kind if kind in get_args(ErrorType) else "other",
                "explanation": _text(item["explanation"], 500),
                "example": _text(item.get("example"), 500),
            }
        )
    vocabulary = [
        {"front": _text(item.get("front"), 500), "back": _text(item.get("back"), 500)}
        for item in (data.get("vocabulary") if isinstance(data.get("vocabulary"), list) else [])
        if isinstance(item, dict) and _text(item.get("front"), 500) and _text(item.get("back"), 500)
    ]
    reply = _text(data.get("reply"), 4000)
    return Feedback(reply=reply, corrections=corrections[:LIST_LIMIT], vocabulary=vocabulary[:LIST_LIMIT]).model_dump()


def build_router(retrieve, llm) -> APIRouter:
    settings = get_settings()
    router = APIRouter(prefix="/tutor", tags=["tutor"])

    @contextmanager
    def scope(request: Request):
        principal = getattr(request.state, "session", None)
        if principal is None and settings.auth_enabled:
            raise HTTPException(401, "Bejelentkezés szükséges.")
        username = principal.username if principal else "local"
        with session_scope() as db:
            # Learner initialization, progress counters, SM-2 append positions and revision checks
            # require short serialized transactions. Retrieval and generation run outside them.
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            yield db, store.learner(db, username)

    def session_or_404(db, profile, sid):
        row = store.session_for(db, profile.workspace_id, sid)
        if row is None:
            raise HTTPException(404, "A gyakorlás nem található.")
        return row

    def deck_or_404(db, profile, deck_id):
        deck = store.personal_deck(db, profile.workspace_id, deck_id)
        if deck is None:
            raise HTTPException(404, "A kártyacsomag nem található.")
        return deck

    def generate(context, history, answer):
        instruction = (Path(settings.teacher_prompt_path).parent / "tutor_conversation.md").read_text(encoding="utf-8")
        prompt = instruction + "\n" + json.dumps({**context, "history": history, "learner": answer}, ensure_ascii=False)
        try:
            return parse_feedback(llm(prompt, True))
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(502, "A tutor válasza nem elérhető. Próbáld újra.") from exc

    @router.get("/profile")
    def get_profile(request: Request):
        with scope(request) as (_, profile):
            return store.profile_view(profile)

    @router.put("/profile")
    def put_profile(payload: Profile, request: Request):
        with scope(request) as (db, profile):
            store.apply_profile(db, profile, **payload.model_dump())
            return store.profile_view(profile)

    @router.get("/decks")
    def decks(request: Request):
        with scope(request) as (db, profile):
            rows = db.execute(
                select(FlashcardDeck.id, FlashcardDeck.title)
                .where(FlashcardDeck.workspace_id == profile.workspace_id)
                .order_by(FlashcardDeck.id)
            )
            return [{"id": id_, "title": title} for id_, title in rows]

    @router.post("/sessions", status_code=201)
    def start(payload: Start, request: Request):
        with scope(request) as (db, profile):
            context = {
                "mode": payload.mode,
                "scenario": payload.scenario,
                "language": profile.language,
                "level": profile.level,
                "goal": profile.goal,
            }
            material = {}
            if payload.deck_id is not None:
                material["words"] = store.vocabulary(db, deck_or_404(db, profile, payload.deck_id))
            if payload.source_path:
                principal = getattr(request.state, "session", None)
                allowed = accessible_workspace_ids(
                    db,
                    principal.username if principal else "local",
                    admin=bool(principal and principal.role == "admin"),
                )
        if payload.source_path:
            hits = retrieve(
                payload.scenario or context["goal"] or Path(payload.source_path).stem,
                settings.study_top_k,
                [payload.source_path],
                allowed,
            )
            passage = "\n".join(hit.get("text") or "" for hit in hits if hit.get("source_path") == payload.source_path)
            if not passage.strip():
                raise HTTPException(404, "A dokumentumhoz nincs elérhető gyakorlóanyag.")
            material["passage"] = passage[: settings.max_context_chars]
        context["material"] = json.dumps(material, ensure_ascii=False)
        feedback = generate(context, [], "")
        feedback["corrections"] = []
        with scope(request) as (db, profile):
            row = TutorSession(workspace_id=profile.workspace_id, **context)
            db.add(row)
            db.flush()
            store.record_answer(db, row, profile, "", feedback)
            return store.session_view(db, row)

    @router.get("/sessions/{sid}")
    def read_session(sid: int, request: Request):
        with scope(request) as (db, profile):
            return store.session_view(db, session_or_404(db, profile, sid))

    def stale():
        return HTTPException(409, "A gyakorlás időközben megváltozott. Töltsd be az aktuális beszélgetést.")

    @router.post("/sessions/{sid}/turns")
    def answer(sid: int, payload: Answer, request: Request):
        with scope(request) as (db, profile):
            row = session_or_404(db, profile, sid)
            revision = row.revision
            if payload.revision is not None and payload.revision != revision:
                raise stale()
            context = {key: getattr(row, key) for key in ("mode", "scenario", "language", "level", "goal", "material")}
            history = [
                {"learner": item["learner"][:800], "reply": item["reply"][:1200]} for item in store.turns(db, row, 5)
            ]
        feedback = generate(context, history, payload.text)
        with scope(request) as (db, profile):
            row = session_or_404(db, profile, sid)
            if row.revision != revision:
                raise stale()
            store.record_answer(db, row, profile, payload.text, feedback)
            return store.session_view(db, row)

    @router.post("/sessions/{sid}/cards", status_code=201)
    def card(sid: int, payload: Card, request: Request):
        with scope(request) as (db, profile):
            row = session_or_404(db, profile, sid)
            deck = deck_or_404(db, profile, payload.deck_id) if payload.deck_id is not None else None
            return {"deck_id": store.save_card(db, row, deck, payload.front, payload.back)}

    @router.get("/progress")
    def progress(request: Request):
        with scope(request) as (db, profile):
            return store.progress(db, profile)

    return router
