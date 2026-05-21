"""
CopilotReport model — one row per ISO week per Teacher Co-Pilot run.

Composite UNIQUE on (iso_year, iso_week) is the primary idempotency guard:
the weekly scheduler tick and the manual /regenerate path both target this
constraint. Manual regenerate uses force=True to delete the prior row inside
the same transaction; the cascade on copilot_patterns kills the children.

Statuses:
- pending : aggregation complete, narrative/patterns still being written
- ready   : report committed (per-pattern LLM failures don't flip this)
- failed  : aggregation itself errored — never reached commit
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from db import Base


STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

ARTIFACT_OK = "ok"
ARTIFACT_SKIPPED = "skipped"
ARTIFACT_FAILED = "failed"


class CopilotReport(Base):
    __tablename__ = "copilot_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    iso_year = Column(Integer, nullable=False)
    iso_week = Column(Integer, nullable=False)

    week_start_at = Column(DateTime(timezone=True), nullable=False)
    week_end_at = Column(DateTime(timezone=True), nullable=False)

    status = Column(String(16), nullable=False, default=STATUS_PENDING)

    student_count = Column(Integer, nullable=False, default=0)
    session_count = Column(Integer, nullable=False, default=0)
    resolved_count = Column(Integer, nullable=False, default=0)

    narrative = Column(Text, nullable=False, default="")
    narrative_status = Column(String(16), nullable=False, default=ARTIFACT_SKIPPED)

    generated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    regenerated_at = Column(DateTime(timezone=True), nullable=True)
    regenerated_by = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
    )

    patterns = relationship(
        "CopilotPattern",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="CopilotPattern.rank",
    )
    regenerator = relationship("Student", foreign_keys=[regenerated_by])

    __table_args__ = (
        UniqueConstraint("iso_year", "iso_week", name="uq_copilot_report_week"),
        Index("ix_copilot_report_week_desc", "iso_year", "iso_week"),
    )

    def __repr__(self) -> str:
        return (
            f"<CopilotReport {self.iso_year}-W{self.iso_week:02d} "
            f"status={self.status} patterns={len(self.patterns or [])}>"
        )
