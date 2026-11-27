"""Média: read text from a photo locally, translate it HU↔EN, explain it, and keep chosen phrases as flashcards.

Photos and results belong to one user (their personal workspace) and are never shared or listed for anyone
else, admins included. Recognition is Tesseract by default; a local vision model is used only when
MEDIA_VISION_MODEL is set. Every route is a plain ``def``, so OCR and model calls run in the threadpool.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from teacher_common import media, study_store
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.docintel import TRANSLATE_DIRECTIONS, render
from teacher_common.media_models import MediaItem
from teacher_common.workspaces import ensure_personal_workspace

logger = logging.getLogger(__name__)

LLM = Callable[[str, bool], str]
# Takes a JPEG, returns the plain text it reads.
Vision = Callable[[bytes], str]
OCR = Callable[[object], media.OcrResult]
LANGUAGE_NAMES = {"hu": ("magyar", "angol"), "en": ("angol", "magyar")}


class TranslateRequest(BaseModel):
    direction: str = Field(pattern="^(hu-en|en-hu)$")


class GlossaryRequest(BaseModel):
    phrases: list[str] = Field(min_length=1, max_length=30)


class CardPair(BaseModel):
    front: str = Field(min_length=1, max_length=300)
    back: str = Field(min_length=1, max_length=300)


class DeckRequest(BaseModel):
    pairs: list[CardPair] = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)


def _summary(row: MediaItem, low_confidence: float) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "engine": row.engine,
        "text": row.text,
        "lines": json.loads(row.lines or "[]"),
        "mean_confidence": row.mean_confidence,
        "direction": row.direction,
        "translation": row.translation,
        "explanation": row.explanation,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "low_confidence": low_confidence,  # words scored below this are marked as uncertain
    }


def _chunks(text: str, limit: int) -> list[str]:
    """Paragraph groups of at most ``limit`` characters (a longer paragraph goes alone)."""
    out: list[str] = []
    for paragraph in text.split("\n\n"):
        if out and len(out[-1]) + len(paragraph) + 2 <= limit:
            out[-1] += "\n\n" + paragraph
        else:
            out.append(paragraph)
    return [c for c in out if c.strip()]


def build_router(*, llm: LLM, ocr: OCR | None = None, vision: Vision | None = None) -> APIRouter:
    settings = get_settings()
    router = APIRouter(prefix="/media", tags=["media"])
    prompt_dir = Path(settings.teacher_prompt_path).parent

    def summary(row: MediaItem) -> dict:
        return _summary(row, settings.ocr_low_confidence)

    def prompt(name: str) -> str:
        return (prompt_dir / name).read_text(encoding="utf-8")

    def ask(text: str, json_mode: bool = False) -> str:
        try:
            return llm(text, json_mode).strip()
        except Exception as exc:  # noqa: BLE001 - any model failure is one answer for the user
            logger.warning("media model call failed: %s", exc)
            raise HTTPException(502, "A helyi nyelvi modell most nem érhető el. Próbáld újra később.") from exc

    def workspace_id(request: Request, session) -> str:
        principal = getattr(request.state, "session", None)
        if principal is None and settings.auth_enabled:
            raise HTTPException(401, "Bejelentkezés szükséges.")
        return ensure_personal_workspace(session, principal.username if principal else "local").id

    def owned(session, request: Request, item_id: int) -> MediaItem:
        row = session.execute(
            select(MediaItem).where(MediaItem.id == item_id, MediaItem.workspace_id == workspace_id(request, session))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "A kép nem található.")
        return row

    def recognise(image, preview: bytes) -> media.OcrResult:
        if vision is not None:
            try:
                text = vision(preview)
                if text.strip():
                    return media.result_from_text(text, "vision")
            except Exception as exc:  # noqa: BLE001 - fall back to Tesseract
                logger.warning("vision model failed, using Tesseract: %s", exc)
        prepared, info = media.preprocess(image)
        logger.info("media preprocess: %s", info)
        if ocr is not None:
            return ocr(prepared)
        try:
            return media.tesseract_ocr(prepared, settings.ocr_language, settings.media_ocr_timeout_seconds)
        except RuntimeError as exc:  # pytesseract raises RuntimeError on its timeout
            raise HTTPException(504, "A szövegfelismerés túl sokáig tartott. Próbálj kisebb vagy élesebb képet.") from exc
        except Exception as exc:  # noqa: BLE001 - engine or language pack missing
            logger.error("tesseract failed: %s", exc)
            raise HTTPException(503, "A helyi szövegfelismerő (Tesseract) nem érhető el.") from exc

    @router.post("/items", status_code=201)
    def create_item(request: Request, image: UploadFile = File(...), direction: str = Form("auto")):
        if direction not in {"auto", "hu-en", "en-hu"}:
            raise HTTPException(422, "Ismeretlen fordítási irány.")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > media.MAX_BYTES + 64 * 1024:
            raise HTTPException(413, "A kép legfeljebb 10 MB lehet.")
        data = image.file.read(media.MAX_BYTES + 1)
        try:
            picture = media.load_image(data)
        except media.ImageRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        preview = media.preview_jpeg(picture)
        with session_scope() as session:  # check the quota before the slow part
            ws = workspace_id(request, session)
            count = session.execute(select(func.count()).where(MediaItem.workspace_id == ws)).scalar_one()
            if count >= settings.media_max_items_per_user:
                raise HTTPException(409, "Elérted a tárolható képek számát. Törölj néhány régit.")
        result = recognise(picture, preview)
        text = result.text
        if not text:
            raise HTTPException(422, "Nem sikerült szöveget felismerni a képen. Próbáld élesebb, egyenesebb fotóval.")
        if direction == "auto":
            direction = media.guess_direction(text)
        title = next((line for line in text.splitlines() if line.strip()), "Fénykép")[:120]
        with session_scope() as session:
            row = MediaItem(
                workspace_id=workspace_id(request, session),
                title=title,
                image=preview,
                engine=result.engine,
                text=text,
                lines=json.dumps(result.to_json(), ensure_ascii=False),
                mean_confidence=result.mean_confidence,
                direction=direction,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return summary(row)

    @router.get("/items")
    def list_items(request: Request):
        with session_scope() as session:
            rows = session.execute(
                select(MediaItem).where(MediaItem.workspace_id == workspace_id(request, session)).order_by(MediaItem.id.desc())
            ).scalars()
            return [summary(row) for row in rows]

    @router.get("/items/{item_id}")
    def get_item(item_id: int, request: Request):
        with session_scope() as session:
            return summary(owned(session, request, item_id))

    @router.get("/items/{item_id}/image")
    def get_image(item_id: int, request: Request):
        with session_scope() as session:
            data = owned(session, request, item_id).image
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @router.delete("/items/{item_id}", status_code=204)
    def delete_item(item_id: int, request: Request):
        with session_scope() as session:
            session.delete(owned(session, request, item_id))
        return Response(status_code=204)

    @router.post("/items/{item_id}/translate")
    def translate(item_id: int, payload: TranslateRequest, request: Request):
        with session_scope() as session:
            text = owned(session, request, item_id).text
        template = prompt(TRANSLATE_DIRECTIONS[payload.direction])
        limit = max(1000, settings.max_context_chars // 2)
        translated = "\n\n".join(ask(render(template, text=chunk)) for chunk in _chunks(text, limit))
        with session_scope() as session:
            row = owned(session, request, item_id)
            row.translation, row.direction, row.explanation = translated, payload.direction, None
            return summary(row)

    @router.post("/items/{item_id}/explain")
    def explain(item_id: int, request: Request):
        with session_scope() as session:
            row = owned(session, request, item_id)
            text, translation, direction = row.text, row.translation or "(még nincs)", row.direction
        source, target = LANGUAGE_NAMES[direction[:2]]
        explanation = ask(render(prompt("media_explain_hu.md"), source=source, target=target,
                                 translation=translation, text=text[: settings.max_context_chars]))
        with session_scope() as session:
            owned(session, request, item_id).explanation = explanation
        return {"explanation": explanation}

    @router.post("/items/{item_id}/glossary")
    def glossary(item_id: int, payload: GlossaryRequest, request: Request):
        """Suggested translations for chosen phrases. Nothing is saved: the user edits them, then saves a deck."""
        phrases = [p.strip()[:120] for p in payload.phrases if p.strip()]
        if not phrases:
            raise HTTPException(422, "Válassz ki legalább egy kifejezést.")
        with session_scope() as session:
            row = owned(session, request, item_id)
            text, direction = row.text, row.direction
        source, target = LANGUAGE_NAMES[direction[:2]]
        reply = ask(render(prompt("media_glossary_hu.md"), source=source, target=target,
                           phrases="\n".join(f"- {p}" for p in phrases), text=text[: settings.max_context_chars]), True)
        try:
            suggested = {str(p.get("front", "")).strip(): str(p.get("back", "")).strip()
                         for p in json.loads(reply).get("pairs", []) if isinstance(p, dict)}
        except (ValueError, AttributeError):
            suggested = {}
        # One pair per chosen phrase, in order; a phrase the model skipped gets an empty side to fill in.
        return {"pairs": [{"front": p, "back": suggested.get(p, "")[:300]} for p in phrases]}

    @router.post("/items/{item_id}/deck", status_code=201)
    def save_deck(item_id: int, payload: DeckRequest, request: Request):
        """Only on the user's explicit choice: the chosen pairs become a flashcard deck in Tanulás."""
        cards = [{"front": p.front.strip(), "back": p.back.strip(), "sources": []}
                 for p in payload.pairs if p.front.strip() and p.back.strip()]
        if not cards:
            raise HTTPException(422, "Minden kártyának legyen eleje és hátoldala.")
        with session_scope() as session:
            row = owned(session, request, item_id)
            title = (payload.title or "").strip() or f"Fotó: {row.title}"
            deck = study_store.create_deck(
                session, title=title[:200], topic="Fényképről felismert szöveg", level="beginner",
                scope_sources=[], model=None, cards=cards, workspace_id=row.workspace_id,
            )
            return {"deck_id": deck["id"], "card_count": len(cards), "title": deck.get("title", title)}

    return router
