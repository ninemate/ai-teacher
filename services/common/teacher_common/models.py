from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from teacher_common.db import Base


class DocumentRecord(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
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
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    chunk_id = Column(String(128), unique=True, nullable=False, index=True)
    position = Column(Integer, nullable=False)
    locator = Column(String(255), nullable=True)
    preview = Column(Text, nullable=False)

    document = relationship("DocumentRecord", back_populates="chunks")


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id = Column(Integer, primary_key=True)
    status = Column(String(32), nullable=False, default="running")
    message = Column(Text, nullable=True)
    scanned = Column(Integer, nullable=False, default=0)
    indexed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    deleted = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

