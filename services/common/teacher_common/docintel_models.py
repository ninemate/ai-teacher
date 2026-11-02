from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func

from teacher_common.db import Base


class DocIntelJob(Base):
    """A background Document Intelligence run (summary, extraction, comparison, tasks or translation)."""

    __tablename__ = "docintel_jobs"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(String(128), ForeignKey("workspaces.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    title = Column(String(512), nullable=False)
    # queued, running, succeeded, failed, cancelled, interrupted (the process ended mid-run; resumable)
    status = Column(String(16), nullable=False, default="queued", index=True)
    # {"sources": [{source_path, source_hash, file_name, title}], "options": {...}}
    params = Column(JSON, nullable=False, default=dict)
    progress_done = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=False, default=0)
    phase = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    # A source document was deleted or changed after the result was made.
    stale = Column(Boolean, nullable=False, default=False)
    stale_reason = Column(String(255), nullable=True)
    model = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DocIntelPart(Base):
    """Stored output of one finished step, so an interrupted or failed job resumes instead of starting over."""

    __tablename__ = "docintel_parts"
    __table_args__ = (UniqueConstraint("job_id", "key", name="uq_docintel_part_key"),)

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("docintel_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(128), nullable=False)
    payload = Column(JSON, nullable=False)


class DocIntelResult(Base):
    """The finished result of a job, kept apart from the job row so listing jobs stays cheap.

    Never indexed: nothing here reaches the vector store or the document list.
    """

    __tablename__ = "docintel_results"

    job_id = Column(Integer, ForeignKey("docintel_jobs.id", ondelete="CASCADE"), primary_key=True)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
