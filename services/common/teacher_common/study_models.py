from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from teacher_common.db import Base


class QuizSet(Base):
    __tablename__ = "quiz_sets"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        String(128), ForeignKey("workspaces.id"), nullable=False, index=True, default="legacy", server_default="legacy"
    )
    title = Column(String(512), nullable=False)
    topic = Column(Text, nullable=True)
    level = Column(String(32), nullable=False, default="beginner")
    # Document paths the generation was restricted to; empty list = whole library.
    scope_sources = Column(JSON, nullable=False, default=list)
    model = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    questions = relationship(
        "QuizQuestion",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="QuizQuestion.position",
    )
    attempts = relationship(
        "QuizAttempt",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="QuizAttempt.taken_at",
    )


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id = Column(Integer, primary_key=True)
    quiz_id = Column(Integer, ForeignKey("quiz_sets.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    question = Column(Text, nullable=False)
    options = Column(JSON, nullable=False)
    correct_index = Column(Integer, nullable=False)
    explanation = Column(Text, nullable=True)
    # [{chunk_id, source_path, title, locator}]
    sources = Column(JSON, nullable=False, default=list)

    quiz = relationship("QuizSet", back_populates="questions")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id = Column(Integer, primary_key=True)
    quiz_id = Column(Integer, ForeignKey("quiz_sets.id"), nullable=False, index=True)
    score = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)
    # {question_id: {"chosen": int | None, "correct": bool}}
    answers = Column(JSON, nullable=False, default=dict)
    taken_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    quiz = relationship("QuizSet", back_populates="attempts")


class FlashcardDeck(Base):
    __tablename__ = "flashcard_decks"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        String(128), ForeignKey("workspaces.id"), nullable=False, index=True, default="legacy", server_default="legacy"
    )
    title = Column(String(512), nullable=False)
    topic = Column(Text, nullable=True)
    level = Column(String(32), nullable=False, default="beginner")
    scope_sources = Column(JSON, nullable=False, default=list)
    model = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    cards = relationship(
        "Flashcard",
        back_populates="deck",
        cascade="all, delete-orphan",
        order_by="Flashcard.position",
    )


class Flashcard(Base):
    __tablename__ = "flashcards"

    id = Column(Integer, primary_key=True)
    deck_id = Column(Integer, ForeignKey("flashcard_decks.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    front = Column(Text, nullable=False)
    back = Column(Text, nullable=False)
    sources = Column(JSON, nullable=False, default=list)

    # SM-2 review state. due_at is naive UTC.
    ease = Column(Float, nullable=False, default=2.5)
    interval_days = Column(Integer, nullable=False, default=0)
    repetitions = Column(Integer, nullable=False, default=0)
    lapses = Column(Integer, nullable=False, default=0)
    due_at = Column(DateTime, nullable=False, index=True)
    last_reviewed_at = Column(DateTime, nullable=True)

    deck = relationship("FlashcardDeck", back_populates="cards")
