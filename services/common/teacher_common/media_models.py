"""Photos read in the Média panel, with their recognised text and translation. Private to one user."""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import deferred

from teacher_common.db import Base


class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True)
    # Always the owner's personal workspace (user:<name>): photos are never shared.
    workspace_id = Column(String(128), ForeignKey("workspaces.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    # Upright, downscaled JPEG without metadata; deferred so lists never load it.
    image = deferred(Column(LargeBinary, nullable=False))
    engine = Column(String(32), nullable=False, default="tesseract")
    text = Column(Text, nullable=False)
    lines = Column(Text, nullable=False, default="[]")  # JSON: [[{"text", "confidence"}, ...], ...]
    mean_confidence = Column(Float, nullable=True)
    direction = Column(String(8), nullable=False)
    translation = Column(Text, nullable=True)
    explanation = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
