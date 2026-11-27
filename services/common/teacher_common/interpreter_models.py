from sqlalchemy import Column, DateTime, String, Text, func

from teacher_common.db import Base


class InterpreterSession(Base):
    """A saved Tolmács conversation. Belongs to one user; every read and write filters by ``username``.

    ``turns_json`` holds the transcript (texts, languages, latency figures), never audio.
    """

    __tablename__ = "interpreter_sessions"

    id = Column(String(32), primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    mode = Column(String(20), nullable=False, default="fast")
    turns_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
