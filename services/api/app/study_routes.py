"""Stored quizzes and flashcard decks: generation, storage and review endpoints.

Kept in its own module so main.py only needs one ``include_router`` call. The router has no
auth of its own; the app includes it with its auth dependency (see ``main.py``).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from teacher_common import study, study_store
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.models import DocumentRecord

# retrieve(query, top_k, source_paths) -> hits as returned by main.retrieve_sources
Retrieve = Callable[[str, int, list[str] | None], list[dict]]
# llm(prompt, json_mode) -> raw model text
LLM = Callable[[str, bool], str]

Level = Literal["beginner", "intermediate", "advanced"]


class GenerateRequest(BaseModel):
    topic: str | None = Field(default=None, min_length=3)
    source_paths: list[str] = Field(default_factory=list, max_length=50)
    level: Level = "beginner"
    count: int | None = Field(default=None, ge=1, le=30)
    title: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _needs_scope(self) -> "GenerateRequest":
        if not self.topic and not self.source_paths:
            raise ValueError("adj meg témát és/vagy legalább egy dokumentumot")
        return self


class AttemptRequest(BaseModel):
    # question id -> chosen option index (null/missing = unanswered)
    answers: dict[int, int | None]


class ReviewRequest(BaseModel):
    grade: Literal["again", "hard", "good", "easy"]


def build_router(retrieve: Retrieve, llm: LLM) -> APIRouter:
    settings = get_settings()
    router = APIRouter(prefix="/study", tags=["study"])

    def _scope(payload: GenerateRequest) -> tuple[str, str]:
        """(topic text for the prompt/search, default title)."""
        if payload.topic:
            return payload.topic, payload.topic
        with session_scope() as session:
            rows = session.execute(
                select(DocumentRecord.title, DocumentRecord.source_path).where(
                    DocumentRecord.source_path.in_(payload.source_paths)
                )
            ).all()
        if not rows:
            raise HTTPException(status_code=404, detail="Egyik megadott dokumentum sem található az indexben.")
        names = [title or Path(path).name for title, path in rows]
        text = ", ".join(names)
        return text, text[:200]

    def _generate(kind: str, payload: GenerateRequest, default_count: int) -> tuple[list[dict], str, str, int]:
        topic, default_title = _scope(payload)
        count = payload.count or default_count
        hits = retrieve(topic, settings.study_top_k, payload.source_paths or None)
        if not hits:
            raise HTTPException(
                status_code=422,
                detail="A könyvtárban nem találtam elég releváns információt ehhez a kéréshez.",
            )
        template = study.load_prompt(Path(settings.teacher_prompt_path).parent, kind)
        try:
            items = study.generate_items(
                kind,
                llm=lambda prompt: llm(prompt, True),
                template=template,
                hits=hits,
                level=payload.level,
                count=count,
                topic=topic,
                max_context_chars=settings.max_context_chars,
                max_attempts=settings.study_max_attempts,
            )
        except study.GenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"A nyelvi modell nem érhető el: {exc}") from exc
        return items, topic, payload.title or default_title, count

    def _not_found(exc: Exception) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    # --- quizzes ---

    @router.post("/quizzes", status_code=201)
    def create_quiz(payload: GenerateRequest):
        items, topic, title, _ = _generate("quiz", payload, default_count=5)
        with session_scope() as session:
            return study_store.create_quiz_set(
                session,
                title=title,
                topic=payload.topic,
                level=payload.level,
                scope_sources=payload.source_paths,
                model=settings.ollama_model,
                questions=items,
            )

    @router.get("/quizzes")
    def list_quizzes():
        with session_scope() as session:
            return study_store.list_quiz_sets(session)

    @router.get("/quizzes/{quiz_id}")
    def get_quiz(quiz_id: int):
        with session_scope() as session:
            try:
                return study_store.get_quiz_set(session, quiz_id)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc

    @router.delete("/quizzes/{quiz_id}", status_code=204)
    def delete_quiz(quiz_id: int):
        with session_scope() as session:
            try:
                study_store.delete_quiz_set(session, quiz_id)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc
        return Response(status_code=204)

    @router.post("/quizzes/{quiz_id}/attempts", status_code=201)
    def submit_attempt(quiz_id: int, payload: AttemptRequest):
        with session_scope() as session:
            try:
                return study_store.record_attempt(session, quiz_id, payload.answers)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc
            except study_store.InvalidInputError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/quizzes/{quiz_id}/attempts")
    def quiz_attempts(quiz_id: int):
        with session_scope() as session:
            try:
                return study_store.list_attempts(session, quiz_id)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc

    # --- flashcards ---

    @router.post("/decks", status_code=201)
    def create_deck(payload: GenerateRequest):
        items, topic, title, _ = _generate("flashcards", payload, default_count=10)
        with session_scope() as session:
            return study_store.create_deck(
                session,
                title=title,
                topic=payload.topic,
                level=payload.level,
                scope_sources=payload.source_paths,
                model=settings.ollama_model,
                cards=items,
            )

    @router.get("/decks")
    def list_decks():
        with session_scope() as session:
            return study_store.list_decks(session)

    @router.get("/decks/{deck_id}")
    def get_deck(deck_id: int):
        with session_scope() as session:
            try:
                return study_store.get_deck(session, deck_id)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc

    @router.delete("/decks/{deck_id}", status_code=204)
    def delete_deck(deck_id: int):
        with session_scope() as session:
            try:
                study_store.delete_deck(session, deck_id)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc
        return Response(status_code=204)

    @router.get("/decks/{deck_id}/due")
    def deck_due(deck_id: int, limit: int = 20):
        with session_scope() as session:
            try:
                return study_store.due_cards(session, deck_id, limit=max(1, min(limit, 100)))
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc

    @router.post("/cards/{card_id}/review")
    def review_card(card_id: int, payload: ReviewRequest):
        with session_scope() as session:
            try:
                return study_store.review_card(session, card_id, payload.grade)
            except study_store.NotFoundError as exc:
                raise _not_found(exc) from exc

    return router
