"""
CopilotPattern model — one row per class-wide pattern surfaced in a report.

Each pattern has its own LLM-drafted mini-lesson. mini_lesson_status is
intentionally separate from CopilotReport.status so a single LLM failure
scopes to that pattern row — the rollup ships either way.

signal_kind values:
- level3_stuck     : students hit fails_at_level >= 3 on this concept this week
- repeated_failure : students with 2+ unresolved sessions on the same concept
- concept_struggle : student_concept_memory rows updated this week + struggling
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
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from db import Base


SIGNAL_LEVEL3_STUCK = "level3_stuck"
SIGNAL_REPEATED_FAILURE = "repeated_failure"
SIGNAL_CONCEPT_STRUGGLE = "concept_struggle"


class CopilotPattern(Base):
    __tablename__ = "copilot_patterns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    report_id = Column(
        UUID(as_uuid=True),
        ForeignKey("copilot_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    rank = Column(Integer, nullable=False)
    signal_kind = Column(String(32), nullable=False)

    subject = Column(String(50), nullable=False)
    concept = Column(String(120), nullable=True)
    concept_label = Column(String(200), nullable=True)

    affected_count = Column(Integer, nullable=False)
    cohort_count = Column(Integer, nullable=False)
    affected_ratio = Column(Float, nullable=False)

    # Frozen evidence snapshot at write time. Stores opaque counts, never
    # student names — so a deleted student doesn't change historical reports
    # and the UI never has to render PII from the digest.
    evidence_json = Column(JSONB, nullable=False, default=dict)

    mini_lesson_md = Column(Text, nullable=True)
    mini_lesson_status = Column(String(16), nullable=False, default="skipped")
    mini_lesson_error = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    report = relationship("CopilotReport", back_populates="patterns")

    __table_args__ = (
        Index("ix_copilot_pattern_report_rank", "report_id", "rank"),
    )

    def __repr__(self) -> str:
        return (
            f"<CopilotPattern rank={self.rank} {self.signal_kind} "
            f"{self.subject}/{self.concept} "
            f"{self.affected_count}/{self.cohort_count}>"
        )
