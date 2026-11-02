from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint, func

from teacher_common.db import Base


class ListenItem(Base):
    """A document on one user's listening list, with where they stopped. Access is checked against the live
    document on every read, so a row outliving a lost share or a deleted document exposes nothing."""

    __tablename__ = "listen_items"
    __table_args__ = (UniqueConstraint("username", "document_id", name="uq_listen_item_user_document"),)

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    document_id = Column(Integer, nullable=False, index=True)
    section = Column(Integer, nullable=False, default=0)
    segment = Column(Integer, nullable=False, default=0)
    speed = Column(Float, nullable=False, default=1.0)
    voice = Column(String(100), nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ListenBookmark(Base):
    __tablename__ = "listen_bookmarks"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, index=True)
    document_id = Column(Integer, nullable=False, index=True)
    section = Column(Integer, nullable=False)
    segment = Column(Integer, nullable=False)
    label = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
