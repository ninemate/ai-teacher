"""Workspace-scoped tutor sessions, progress and SM-2 deck integration."""

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher_common import study_store
from teacher_common.study_models import Flashcard, FlashcardDeck
from teacher_common.tutor_models import LearnerProfile, TutorLevelChange, TutorSession, TutorTurn
from teacher_common.workspaces import ensure_personal_workspace

LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")


def learner(db: Session, username: str) -> LearnerProfile:
    workspace = ensure_personal_workspace(db, username)
    row = db.get(LearnerProfile, workspace.id)
    if row is None:
        row = LearnerProfile(workspace_id=workspace.id)
        db.add(row)
        db.flush()
    return row


def profile_view(row: LearnerProfile) -> dict:
    return {"language": row.language, "level": row.level, "goal": row.goal, "auto_level": row.auto_level}


def set_level(db: Session, row: LearnerProfile, level: str, reason: str) -> None:
    if level != row.level:
        db.add(
            TutorLevelChange(
                workspace_id=row.workspace_id, language=row.language, previous=row.level, level=level, reason=reason
            )
        )
        row.level = level


def apply_profile(db: Session, row: LearnerProfile, language: str, level: str, goal: str, auto_level: bool) -> None:
    if (row.language, row.level, row.auto_level) != (language, level, auto_level):
        row.window_turns = row.window_clean = 0
    if language != row.language:
        # A language switch establishes a new baseline, not a promotion or demotion.
        row.language, row.level = language, level
    else:
        set_level(db, row, level, "manual")
    row.goal, row.auto_level = goal, auto_level


def session_for(db: Session, owner: str, session_id: int) -> TutorSession | None:
    return db.scalar(select(TutorSession).where(TutorSession.workspace_id == owner, TutorSession.id == session_id))


def turns(db: Session, row: TutorSession, limit: int) -> list[dict]:
    recent = db.scalars(
        select(TutorTurn).where(TutorTurn.session_id == row.id).order_by(TutorTurn.id.desc()).limit(limit)
    ).all()
    return [{"learner": turn.learner, **turn.feedback} for turn in reversed(recent)]


def session_view(db: Session, row: TutorSession) -> dict:
    return {
        "id": row.id,
        "language": row.language,
        "level": row.level,
        "mode": row.mode,
        "scenario": row.scenario,
        "turns": turns(db, row, 50),
        "revision": row.revision,
    }


def record_answer(db: Session, row: TutorSession, profile: LearnerProfile, text: str, feedback: dict) -> None:
    db.add(TutorTurn(session_id=row.id, learner=text, feedback=feedback))
    row.revision += 1
    if text:
        errors = [item["type"] for item in feedback["corrections"]]
        if profile.auto_level and (row.language, row.level) == (profile.language, profile.level):
            profile.window_turns += 1
            profile.window_clean += int(not errors)
            if profile.window_turns == 5:
                direction = int(profile.window_clean == 5) - int(profile.window_clean == 0)
                index = max(0, min(len(LEVELS) - 1, LEVELS.index(profile.level) + direction))
                set_level(db, profile, LEVELS[index], "auto")
                profile.window_turns = profile.window_clean = 0
    db.flush()


def progress(db: Session, profile: LearnerProfile) -> dict:
    changes = db.scalars(
        select(TutorLevelChange)
        .where(TutorLevelChange.workspace_id == profile.workspace_id)
        .order_by(TutorLevelChange.id.desc())
        .limit(100)
    )
    # Sessions, answers and errors describe the current target language only; level history keeps every language.
    own = (TutorSession.workspace_id == profile.workspace_id, TutorSession.language == profile.language)
    answers = db.scalars(
        select(TutorTurn.feedback)
        .join(TutorSession, TutorTurn.session_id == TutorSession.id)
        .where(*own, TutorTurn.learner != "")
    ).all()
    errors = Counter(item["type"] for feedback in answers for item in feedback.get("corrections", []))
    return {
        "language": profile.language,
        "sessions": db.scalar(select(func.count(TutorSession.id)).where(*own)),
        "turns": len(answers),
        "errors": dict(errors),
        "levels": [
            {
                "language": c.language,
                "previous": c.previous,
                "level": c.level,
                "reason": c.reason,
                "at": c.at.isoformat(),
            }
            for c in changes
        ],
    }


def personal_deck(db: Session, owner: str, deck_id: int) -> FlashcardDeck | None:
    return db.scalar(select(FlashcardDeck).where(FlashcardDeck.workspace_id == owner, FlashcardDeck.id == deck_id))


def vocabulary(db: Session, deck: FlashcardDeck) -> list[dict]:
    rows = db.execute(
        select(Flashcard.front, Flashcard.back)
        .where(Flashcard.deck_id == deck.id)
        .order_by(Flashcard.due_at, Flashcard.position)
        .limit(20)
    )
    return [{"front": front[:300], "back": back[:500]} for front, back in rows]


def save_card(db: Session, row: TutorSession, deck: FlashcardDeck | None, front: str, back: str) -> int:
    title = f"Nyelvi gyakorlás · {row.language.upper()}"
    if deck is None:
        deck = db.scalar(
            select(FlashcardDeck)
            .where(FlashcardDeck.workspace_id == row.workspace_id, FlashcardDeck.title == title)
            .order_by(FlashcardDeck.id)
        )
    if deck is None:
        created = study_store.create_deck(
            db,
            title=title,
            topic=None,
            level=row.level,
            scope_sources=[],
            model=None,
            cards=[{"front": front, "back": back, "sources": []}],
            workspace_id=row.workspace_id,
        )
        return created["id"]
    cards = select(Flashcard).where(Flashcard.deck_id == deck.id)
    if db.scalar(cards.where(Flashcard.front == front, Flashcard.back == back)) is None:
        last = db.scalar(select(func.max(Flashcard.position)).where(Flashcard.deck_id == deck.id))
        db.add(
            Flashcard(
                deck_id=deck.id,
                position=0 if last is None else last + 1,
                front=front,
                back=back,
                sources=[],
                due_at=study_store.utcnow(),
            )
        )
    return deck.id
