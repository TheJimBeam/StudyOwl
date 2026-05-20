"""
CriticDecision model — one row per judged hint.

Persisted by the hint pipeline whenever the Socratic critic returns a verdict
(approve OR reject). Powers the teacher dashboard "Recent critic rejects"
panel and gives us a corpus for measuring spoiler-catch rate over time.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from db import Base


VERDICT_APPROVE = "approve"
VERDICT_REJECT = "reject"

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"


class CriticDecision(Base):
    __tablename__ = "critic_decisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised — every read filters by student_id, joining to sessions on
    # the hot path would be wasteful.
    student_id = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )

    hint_level = Column(Integer, nullable=False)
    verdict = Column(String(16), nullable=False)  # approve | reject
    severity = Column(String(16), nullable=False)  # low | medium | high
    # Stored as a newline-joined string. Reasons are short — no need for JSON
    # column complexity at v1 scale.
    reasons = Column(Text, nullable=False, default="")

    original_hint = Column(Text, nullable=False)
    # Only populated on reject (after the regen). Stays NULL on approve.
    regenerated_hint = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    session = relationship("Session")
    student = relationship("Student")

    __table_args__ = (
        # Teacher dashboard reads "latest critic decisions for this student".
        Index("ix_critic_decisions_student_created", "student_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<CriticDecision {self.id} verdict={self.verdict} "
            f"severity={self.severity} session={self.session_id}>"
        )
