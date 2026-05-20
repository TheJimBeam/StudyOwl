"""
ConceptMemory model — one row per (student, concept) pair.

Stores the consolidated knowledge-graph state for a student. Each completed
session contributes 1–3 concept rows via memory_agent.consolidate_session().
Confidence is stored as the raw value at last_seen; the decayed value used
for hint conditioning and the teacher dashboard is computed at read time
(see memory_agent.decayed_confidence).

The architect note explicitly calls for a flat table here, not a graph.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from db import Base


# Status buckets — derived from decayed_confidence at read time but also
# persisted on each consolidation so the column is queryable as-is.
STATUS_MASTERED = "mastered"
STATUS_PARTIAL = "partial"
STATUS_STRUGGLING = "struggling"


class ConceptMemory(Base):
    __tablename__ = "student_concept_memory"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    student_id = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    subject = Column(String(50), nullable=False)
    concept = Column(String(120), nullable=False)
    label = Column(String(200), nullable=False)

    status = Column(String(16), nullable=False)
    confidence = Column(Float, nullable=False)

    attempts_count = Column(Integer, nullable=False, default=0)
    correct_count = Column(Integer, nullable=False, default=0)

    last_seen = Column(DateTime(timezone=True), nullable=False)
    last_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    student = relationship("Student")

    __table_args__ = (
        UniqueConstraint("student_id", "concept", name="uq_student_concept"),
        Index("ix_concept_memory_student_subject_seen", "student_id", "subject", "last_seen"),
    )

    def __repr__(self) -> str:
        return (
            f"<ConceptMemory student={self.student_id} concept={self.concept} "
            f"status={self.status} confidence={self.confidence:.2f}>"
        )
