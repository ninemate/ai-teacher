"""Private meeting jobs and explicitly assigned action-item names."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, String
from sqlalchemy.orm import deferred

from teacher_common.db import Base


class MeetingJob(Base):
    __tablename__ = "meeting_jobs"

    id = Column(String(32), primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    workspace_id = Column(String(128), ForeignKey("workspaces.id"), nullable=False)
    filename = Column(String(255), nullable=False)
    created_at = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, index=True)
    phase = Column(String(16), nullable=False, default="queued")
    done = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=True)
    error = Column(String, nullable=True)
    transcript = deferred(Column(JSON, nullable=True))
    notes = deferred(Column(JSON, nullable=True))


class MeetingAssignment(Base):
    __tablename__ = "meeting_assignments"

    meeting_id = Column(String(32), ForeignKey("meeting_jobs.id", ondelete="CASCADE"), primary_key=True)
    action_id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=True)
