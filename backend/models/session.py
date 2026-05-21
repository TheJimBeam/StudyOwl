"""
Session model — represents a single homework session.
"""

from uuid import uuid4
from sqlalchemy import Column, String, DateTime, Boolean, Integer, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID

from db import Base
from datetime import datetime, timezone


class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    student_id = Column(UUID(as_uuid=True), ForeignKey("students.id"), nullable=False)
    question = Column(Text, nullable=False)
    subject = Column(String(50), nullable=False)  # math, science, english, history, other
    hint_level = Column(Integer, default=1)  # 1, 2, or 3
    fails_at_level = Column(Integer, default=0)
    resolved = Column(Boolean, default=False)
    teacher_alerted = Column(Boolean, default=False)
    photo_url = Column(String(500), nullable=True)  # Cloudflare R2 URL
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_activity_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Single teacher comment per session (one teacher writes/edits/deletes).
    # All three columns move together — either all null (no comment) or all set.
    teacher_comment_body = Column(Text, nullable=True)
    teacher_comment_by_id = Column(
        UUID(as_uuid=True), ForeignKey("students.id"), nullable=True
    )
    teacher_comment_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    student = relationship("Student", back_populates="sessions", foreign_keys=[student_id])
    attempts = relationship("Attempt", back_populates="session", cascade="all, delete-orphan")
    comment_teacher = relationship("Student", foreign_keys=[teacher_comment_by_id])

    def __repr__(self) -> str:
        return f"<Session {self.id} ({self.subject}) - {'resolved' if self.resolved else 'open'}>"
