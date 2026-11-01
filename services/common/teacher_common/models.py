from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from teacher_common.db import Base


class DocumentRecord(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        String(128), ForeignKey("workspaces.id"), nullable=False, index=True, default="legacy", server_default="legacy"
    )
    source_path = Column(String(2048), unique=True, nullable=False, index=True)
    source_hash = Column(String(64), nullable=False)
    file_type = Column(String(32), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    modified_timestamp = Column(Integer, nullable=False)
    title = Column(String(512), nullable=True)
    author = Column(String(512), nullable=True)
    language = Column(String(32), nullable=True)
    status = Column(String(32), nullable=False, default="indexed")
    error_message = Column(Text, nullable=True)
    chunk_count = Column(Integer, nullable=False, default=0)
    last_indexed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted = Column(Boolean, nullable=False, default=False)

    chunks = relationship("ChunkRecord", back_populates="document", cascade="all, delete-orphan")


class ChunkRecord(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        String(128), ForeignKey("workspaces.id"), nullable=False, index=True, default="legacy", server_default="legacy"
    )
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    chunk_id = Column(String(128), unique=True, nullable=False, index=True)
    position = Column(Integer, nullable=False)
    locator = Column(String(255), nullable=True)
    preview = Column(Text, nullable=False)

    document = relationship("DocumentRecord", back_populates="chunks")


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id = Column(Integer, primary_key=True)
    # A run may cover several configured workspaces, so aggregate runs belong to the system workspace.
    workspace_id = Column(String(128), nullable=False, default="system", server_default="system", index=True)
    status = Column(String(32), nullable=False, default="running")
    message = Column(Text, nullable=True)
    scanned = Column(Integer, nullable=False, default=0)
    indexed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    deleted = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)



class OcrRecord(Base):
    """Marks a document whose scanned pages were OCR'd, so incremental runs do not redo it."""

    __tablename__ = "ocr_documents"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        String(128), ForeignKey("workspaces.id"), nullable=False, index=True, default="legacy", server_default="legacy"
    )
    source_path = Column(String(2048), unique=True, nullable=False, index=True)
    source_hash = Column(String(64), nullable=False)
    language = Column(String(64), nullable=False)
    dpi = Column(Integer, nullable=False)
    pages_total = Column(Integer, nullable=False, default=0)
    pages_ocr = Column(Integer, nullable=False, default=0)
    pages_pending = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False)
    message = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    # SHA-256 of the cookie token, so a copy of the database does not hand out live sessions.
    token_hash = Column(String(64), primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    csrf_token = Column(String(64), nullable=False)
    # Fingerprint of the user's password hash at login; a changed password invalidates old sessions.
    credential_tag = Column(String(64), nullable=False)
    created_at = Column(Integer, nullable=False)
    expires_at = Column(Integer, nullable=False, index=True)


class Workspace(Base):
    """A knowledge base owned by one configured account."""

    __tablename__ = "workspaces"

    id = Column(String(128), primary_key=True)
    name = Column(String(255), nullable=False)
    owner_username = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    shares = relationship("WorkspaceShare", back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceShare(Base):
    __tablename__ = "workspace_shares"
    __table_args__ = (UniqueConstraint("workspace_id", "username", name="uq_workspace_share_user"),)

    id = Column(Integer, primary_key=True)
    workspace_id = Column(String(128), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    permission = Column(String(16), nullable=False, default="read")

    workspace = relationship("Workspace", back_populates="shares")
