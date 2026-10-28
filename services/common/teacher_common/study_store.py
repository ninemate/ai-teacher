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


# --- quizzes ------------------------------------------------------------------


def serialize_question(row: QuizQuestion) -> dict:
    return {
        "id": row.id,
        "position": row.position,
        "question": row.question,
        "options": row.options,
        "correct_index": row.correct_index,
        "explanation": row.explanation,
        "sources": row.sources,
    }


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
) -> dict:
    quiz = QuizSet(title=title, topic=topic, level=level, scope_sources=scope_sources, model=model)
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
    return get_quiz_set(session, quiz.id)


def list_quiz_sets(session: Session) -> list[dict]:
    rows = session.execute(select(QuizSet).order_by(QuizSet.id.desc())).scalars()
    return [_quiz_summary(session, row) for row in rows]


def get_quiz_set(session: Session, quiz_id: int) -> dict:
    row = session.get(QuizSet, quiz_id)
    if row is None:
        raise NotFoundError(f"quiz {quiz_id} not found")
    return {**_quiz_summary(session, row), "questions": [serialize_question(q) for q in row.questions]}


def delete_quiz_set(session: Session, quiz_id: int) -> None:
    row = session.get(QuizSet, quiz_id)
    if row is None:
        raise NotFoundError(f"quiz {quiz_id} not found")
    session.delete(row)


def record_attempt(session: Session, quiz_id: int, answers: dict[int, int | None]) -> dict:
    """Grade ``answers`` (question id -> chosen option index) and store the result.

    Unanswered questions count as wrong; ids that are not part of the quiz are rejected.
    """
    quiz = session.get(QuizSet, quiz_id)
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
    return serialize_attempt(attempt)


def list_attempts(session: Session, quiz_id: int) -> list[dict]:
    if session.get(QuizSet, quiz_id) is None:
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
) -> dict:
    now = now or utcnow()
    start = initial_state(now)
    deck = FlashcardDeck(title=title, topic=topic, level=level, scope_sources=scope_sources, model=model)
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
    return get_deck(session, deck.id, now=now)


def list_decks(session: Session, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    rows = session.execute(select(FlashcardDeck).order_by(FlashcardDeck.id.desc())).scalars()
    return [_deck_summary(session, row, now) for row in rows]


def get_deck(session: Session, deck_id: int, now: datetime | None = None) -> dict:
    row = session.get(FlashcardDeck, deck_id)
    if row is None:
        raise NotFoundError(f"deck {deck_id} not found")
    return {**_deck_summary(session, row, now or utcnow()), "cards": [serialize_card(c) for c in row.cards]}


def delete_deck(session: Session, deck_id: int) -> None:
    row = session.get(FlashcardDeck, deck_id)
    if row is None:
        raise NotFoundError(f"deck {deck_id} not found")
    session.delete(row)


def due_cards(session: Session, deck_id: int, limit: int = 20, now: datetime | None = None) -> list[dict]:
    if session.get(FlashcardDeck, deck_id) is None:
        raise NotFoundError(f"deck {deck_id} not found")
    rows = session.execute(
        select(Flashcard)
        .where(Flashcard.deck_id == deck_id, Flashcard.due_at <= (now or utcnow()))
        .order_by(Flashcard.due_at, Flashcard.position)
        .limit(limit)
    ).scalars()
    return [serialize_card(row) for row in rows]


def review_card(session: Session, card_id: int, grade: str, now: datetime | None = None) -> dict:
    if grade not in GRADES:
        raise InvalidInputError(f"grade must be one of {sorted(GRADES)}")
    card = session.get(Flashcard, card_id)
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
