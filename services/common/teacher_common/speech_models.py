from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text, func

from teacher_common.db import Base


class TalkSession(Base):
    """One Smart Talk conversation of one user: the turns, and once finished its metrics and feedback."""

    __tablename__ = "talk_sessions"

    revision = Column(Integer, nullable=False)
    __mapper_args__ = {"version_id_col": revision}

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    scenario = Column(String(64), nullable=False, index=True)
    style = Column(String(32), nullable=False)
    language = Column(String(8), nullable=False, default="hu")
    focus = Column(String(32), nullable=True)  # the skill a targeted retry practises
    retry_of = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="active")  # active | finished
    turns = Column(JSON, nullable=False, default=list)  # [{role, text, input, metrics?}]
    metrics = Column(JSON, nullable=True)
    feedback = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class PitchTake(Base):
    """One Elevator Speech take of one user. The audio file, when kept, lives under the speech audio folder in
    a per-user directory; ``audio_file`` is its name there, never a path from the client."""

    __tablename__ = "pitch_takes"

    # Answers to follow-up questions rewrite ``followups``; two at once must not drop one silently (409 instead).
    revision = Column(Integer, nullable=False)
    __mapper_args__ = {"version_id_col": revision}

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    scenario = Column(String(64), nullable=False, index=True)
    target_seconds = Column(Integer, nullable=False)
    language = Column(String(8), nullable=False, default="hu")
    input = Column(String(8), nullable=False, default="text")  # text | voice
    key_messages = Column(JSON, nullable=False, default=list)
    transcript = Column(Text, nullable=False)
    duration_seconds = Column(Float, nullable=True)
    audio_file = Column(String(128), nullable=True)
    audio_type = Column(String(64), nullable=True)
    metrics = Column(JSON, nullable=False)
    coverage = Column(JSON, nullable=False, default=list)
    scores = Column(JSON, nullable=False)
    feedback = Column(JSON, nullable=True)
    followups = Column(JSON, nullable=True)  # {interest, questions: [{question, answer?, metrics?, feedback?}]}
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
