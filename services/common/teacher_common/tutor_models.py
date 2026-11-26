"""Private learner state. Each session belongs to one personal learner profile."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from teacher_common.db import Base


class LearnerProfile(Base):
    __tablename__ = "tutor_profiles"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(2), default="en")
    level: Mapped[str] = mapped_column(String(2), default="B2")
    goal: Mapped[str] = mapped_column(Text, default="")
    auto_level: Mapped[bool] = mapped_column(default=False)
    window_turns: Mapped[int] = mapped_column(default=0)
    window_clean: Mapped[int] = mapped_column(default=0)


class TutorSession(Base):
    __tablename__ = "tutor_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("tutor_profiles.workspace_id"), index=True)
    language: Mapped[str] = mapped_column(String(2))
    level: Mapped[str] = mapped_column(String(2))
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20))
    scenario: Mapped[str] = mapped_column(Text)
    material: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(default=0)


class TutorTurn(Base):
    __tablename__ = "tutor_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("tutor_sessions.id"), index=True)
    learner: Mapped[str] = mapped_column(Text)
    feedback: Mapped[dict] = mapped_column(JSON)


class TutorLevelChange(Base):
    __tablename__ = "tutor_level_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("tutor_profiles.workspace_id"), index=True)
    language: Mapped[str] = mapped_column(String(2))
    previous: Mapped[str] = mapped_column(String(2))
    level: Mapped[str] = mapped_column(String(2))
    reason: Mapped[str] = mapped_column(String(10))
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
