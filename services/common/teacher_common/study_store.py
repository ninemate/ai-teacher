"""SQLite storage for stored quiz sets and flashcard decks. Every function takes a Session."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher_common.study import GRADES, ReviewState, initial_state, schedule
from teacher_common.study_models import (
    Flashcard,
    FlashcardDeck,
    QuizAttempt,
    QuizQuestion,
    QuizSet,
)


class NotFoundError(LookupError):
    pass


class InvalidInputError(ValueError):
    pass


def utcnow() -> datetime:
    """Naive UTC, matching how SQLite hands datetimes back."""
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _allowed_workspace_ids(workspace_ids: set[str] | None) -> set[str]:
    # Internal legacy callers stay confined to legacy data; HTTP routes always pass an explicit ACL scope.
    return {"legacy"} if workspace_ids is None else workspace_ids


# --- quizzes ------------------------------------------------------------------


def serialize_question(row: QuizQuestion, *, include_answers: bool = False) -> dict:
    """Answer key (``correct_index``, explanation, sources) is only included on request, i.e. after an attempt."""
    data = {"id": row.id, "position": row.position, "question": row.question, "options": row.options}
    if include_answers:
        data.update(correct_index=row.correct_index, explanation=row.explanation, sources=row.sources)
    return data


def serialize_attempt(row: QuizAttempt) -> dict:
    return {
        "id": row.id,
        "quiz_id": row.quiz_id,
        "score": row.score,
        "total": row.total,
        "answers": row.answers,
        "taken_at": _iso(row.taken_at),
    }


def _quiz_summary(session: Session, row: QuizSet) -> dict:
    attempts = session.execute(
        select(func.count(), func.max(QuizAttempt.score * 1.0 / QuizAttempt.total)).where(QuizAttempt.quiz_id == row.id)
    ).one()
    last = session.execute(
        select(QuizAttempt).where(QuizAttempt.quiz_id == row.id).order_by(QuizAttempt.id.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "id": row.id,
        "title": row.title,
        "topic": row.topic,
        "level": row.level,
        "scope_sources": row.scope_sources,
        "model": row.model,
        "created_at": _iso(row.created_at),
        "question_count": session.scalar(select(func.count()).where(QuizQuestion.quiz_id == row.id)),
        "attempt_count": attempts[0],
        "best_ratio": attempts[1],
        "last_score": last.score if last else None,
        "last_total": last.total if last else None,
    }


def create_quiz_set(
    session: Session,
    *,
    title: str,
    topic: str | None,
    level: str,
    scope_sources: list[str],
    model: str | None,
    questions: list[dict],
    include_answers: bool = False,
    workspace_id: str = "legacy",
) -> dict:
    quiz = QuizSet(workspace_id=workspace_id, title=title, topic=topic, level=level,
                   scope_sources=scope_sources, model=model)
    quiz.questions = [
        QuizQuestion(
            position=index,
            question=item["question"],
            options=item["options"],
            correct_index=item["correct_index"],
            explanation=item.get("explanation") or None,
            sources=item["sources"],
        )
        for index, item in enumerate(questions)
    ]
    session.add(quiz)
    session.flush()
    return get_quiz_set(session, quiz.id, include_answers=include_answers, workspace_ids={workspace_id})


def list_quiz_sets(session: Session, workspace_ids: set[str] | None = None) -> list[dict]:
    allowed = _allowed_workspace_ids(workspace_ids)
    if not allowed:
        return []
    rows = session.execute(
        select(QuizSet).where(QuizSet.workspace_id.in_(allowed)).order_by(QuizSet.id.desc())
    ).scalars()
    return [_quiz_summary(session, row) for row in rows]


def get_quiz_set(
    session: Session, quiz_id: int, *, include_answers: bool = False, workspace_ids: set[str] | None = None
) -> dict:
    allowed = _allowed_workspace_ids(workspace_ids)
    row = session.execute(
        select(QuizSet).where(QuizSet.id == quiz_id, QuizSet.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if row is None:
        raise NotFoundError(f"quiz {quiz_id} not found")
    return {
        **_quiz_summary(session, row),
        "questions": [serialize_question(q, include_answers=include_answers) for q in row.questions],
    }


def delete_quiz_set(session: Session, quiz_id: int, workspace_ids: set[str] | None = None) -> None:
    allowed = _allowed_workspace_ids(workspace_ids)
    row = session.execute(
        select(QuizSet).where(QuizSet.id == quiz_id, QuizSet.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if row is None:
        raise NotFoundError(f"quiz {quiz_id} not found")
    session.delete(row)


def record_attempt(
    session: Session, quiz_id: int, answers: dict[int, int | None], workspace_ids: set[str] | None = None
) -> dict:
    """Grade ``answers`` (question id -> chosen option index) and store the result.

    Unanswered questions count as wrong; ids that are not part of the quiz are rejected.
    """
    allowed = _allowed_workspace_ids(workspace_ids)
    quiz = session.execute(
        select(QuizSet).where(QuizSet.id == quiz_id, QuizSet.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if quiz is None:
        raise NotFoundError(f"quiz {quiz_id} not found")
    by_id = {q.id: q for q in quiz.questions}
    unknown = set(answers) - set(by_id)
    if unknown:
        raise InvalidInputError(f"unknown question ids: {sorted(unknown)}")
    results: dict[str, dict] = {}
    score = 0
    for question in quiz.questions:
        chosen = answers.get(question.id)
        if chosen is not None and not 0 <= chosen < len(question.options):
            raise InvalidInputError(f"option {chosen} out of range for question {question.id}")
        correct = chosen == question.correct_index
        score += correct
        results[str(question.id)] = {"chosen": chosen, "correct": correct}
    attempt = QuizAttempt(quiz_id=quiz_id, score=score, total=len(quiz.questions), answers=results)
    session.add(attempt)
    session.flush()
    session.refresh(attempt)
    # The answer key is released together with the result.
    return {**serialize_attempt(attempt), "questions": [serialize_question(q, include_answers=True) for q in quiz.questions]}


def list_attempts(session: Session, quiz_id: int, workspace_ids: set[str] | None = None) -> list[dict]:
    allowed = _allowed_workspace_ids(workspace_ids)
    quiz = session.execute(
        select(QuizSet).where(QuizSet.id == quiz_id, QuizSet.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if quiz is None:
        raise NotFoundError(f"quiz {quiz_id} not found")
    rows = session.execute(
        select(QuizAttempt).where(QuizAttempt.quiz_id == quiz_id).order_by(QuizAttempt.id.desc())
    ).scalars()
    return [serialize_attempt(row) for row in rows]


# --- flashcards ---------------------------------------------------------------


def serialize_card(row: Flashcard) -> dict:
    return {
        "id": row.id,
        "deck_id": row.deck_id,
        "position": row.position,
        "front": row.front,
        "back": row.back,
        "sources": row.sources,
        "ease": row.ease,
        "interval_days": row.interval_days,
        "repetitions": row.repetitions,
        "lapses": row.lapses,
        "due_at": _iso(row.due_at),
        "last_reviewed_at": _iso(row.last_reviewed_at),
    }


def _deck_summary(session: Session, row: FlashcardDeck, now: datetime) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "topic": row.topic,
        "level": row.level,
        "scope_sources": row.scope_sources,
        "model": row.model,
        "created_at": _iso(row.created_at),
        "card_count": session.scalar(select(func.count()).where(Flashcard.deck_id == row.id)),
        "due_count": session.scalar(
            select(func.count()).where(Flashcard.deck_id == row.id, Flashcard.due_at <= now)
        ),
    }


def create_deck(
    session: Session,
    *,
    title: str,
    topic: str | None,
    level: str,
    scope_sources: list[str],
    model: str | None,
    cards: list[dict],
    now: datetime | None = None,
    workspace_id: str = "legacy",
) -> dict:
    now = now or utcnow()
    start = initial_state(now)
    deck = FlashcardDeck(workspace_id=workspace_id, title=title, topic=topic, level=level,
                         scope_sources=scope_sources, model=model)
    deck.cards = [
        Flashcard(
            position=index,
            front=item["front"],
            back=item["back"],
            sources=item["sources"],
            ease=start.ease,
            interval_days=start.interval_days,
            repetitions=start.repetitions,
            lapses=start.lapses,
            due_at=start.due_at,
        )
        for index, item in enumerate(cards)
    ]
    session.add(deck)
    session.flush()
    return get_deck(session, deck.id, now=now, workspace_ids={workspace_id})


def list_decks(
    session: Session, now: datetime | None = None, workspace_ids: set[str] | None = None
) -> list[dict]:
    now = now or utcnow()
    allowed = _allowed_workspace_ids(workspace_ids)
    if not allowed:
        return []
    rows = session.execute(
        select(FlashcardDeck).where(FlashcardDeck.workspace_id.in_(allowed)).order_by(FlashcardDeck.id.desc())
    ).scalars()
    return [_deck_summary(session, row, now) for row in rows]


def get_deck(
    session: Session, deck_id: int, now: datetime | None = None, workspace_ids: set[str] | None = None
) -> dict:
    allowed = _allowed_workspace_ids(workspace_ids)
    row = session.execute(
        select(FlashcardDeck).where(FlashcardDeck.id == deck_id, FlashcardDeck.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if row is None:
        raise NotFoundError(f"deck {deck_id} not found")
    return {**_deck_summary(session, row, now or utcnow()), "cards": [serialize_card(c) for c in row.cards]}


def delete_deck(session: Session, deck_id: int, workspace_ids: set[str] | None = None) -> None:
    allowed = _allowed_workspace_ids(workspace_ids)
    row = session.execute(
        select(FlashcardDeck).where(FlashcardDeck.id == deck_id, FlashcardDeck.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if row is None:
        raise NotFoundError(f"deck {deck_id} not found")
    session.delete(row)


def due_cards(
    session: Session,
    deck_id: int,
    limit: int = 20,
    now: datetime | None = None,
    workspace_ids: set[str] | None = None,
) -> list[dict]:
    allowed = _allowed_workspace_ids(workspace_ids)
    deck = session.execute(
        select(FlashcardDeck).where(FlashcardDeck.id == deck_id, FlashcardDeck.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if deck is None:
        raise NotFoundError(f"deck {deck_id} not found")
    rows = session.execute(
        select(Flashcard)
        .where(Flashcard.deck_id == deck_id, Flashcard.due_at <= (now or utcnow()))
        .order_by(Flashcard.due_at, Flashcard.position)
        .limit(limit)
    ).scalars()
    return [serialize_card(row) for row in rows]


def review_card(
    session: Session, card_id: int, grade: str, now: datetime | None = None,
    workspace_ids: set[str] | None = None,
) -> dict:
    if grade not in GRADES:
        raise InvalidInputError(f"grade must be one of {sorted(GRADES)}")
    allowed = _allowed_workspace_ids(workspace_ids)
    card = session.execute(
        select(Flashcard)
        .join(FlashcardDeck, Flashcard.deck_id == FlashcardDeck.id)
        .where(Flashcard.id == card_id, FlashcardDeck.workspace_id.in_(allowed))
    ).scalar_one_or_none() if allowed else None
    if card is None:
        raise NotFoundError(f"card {card_id} not found")
    now = now or utcnow()
    new = schedule(
        ReviewState(card.ease, card.interval_days, card.repetitions, card.lapses, card.due_at),
        grade,
        now,
    )
    card.ease = new.ease
    card.interval_days = new.interval_days
    card.repetitions = new.repetitions
    card.lapses = new.lapses
    card.due_at = new.due_at
    card.last_reviewed_at = now
    session.flush()
    return serialize_card(card)


def delete_derived_items_for_source(session: Session, source_path: str, workspace_id: str) -> None:
    """Remove only this workspace's study material that depends on a deleted document."""
    for quiz in session.execute(select(QuizSet).where(QuizSet.workspace_id == workspace_id)).scalars().all():
        cited = any(
            ref.get("source_path") == source_path
            for question in quiz.questions
            for ref in (question.sources or [])
            if isinstance(ref, dict)
        )
        if source_path in (quiz.scope_sources or []) or cited:
            session.delete(quiz)
    for deck in session.execute(
        select(FlashcardDeck).where(FlashcardDeck.workspace_id == workspace_id)
    ).scalars().all():
        cited = any(
            ref.get("source_path") == source_path
            for card in deck.cards
            for ref in (card.sources or [])
            if isinstance(ref, dict)
        )
        if source_path in (deck.scope_sources or []) or cited:
            session.delete(deck)
