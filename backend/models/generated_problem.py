"""
GeneratedProblem model — one row per LLM-generated practice problem.

The practice agent (services/practice_agent.py) targets a student's weak
concept (from student_concept_memory), calibrates difficulty against decayed
confidence, asks the LLM for a fresh problem + answer key, and runs a
verifier sub-agent (SymPy for math, rubric-LLM otherwise) before persisting
a row here. Rows whose verifier_status is "rejected" are kept for telemetry
but never returned to students.

attempted/correct stay NULL until the student submits an answer via the
practice attempt endpoint — that's the closed-loop signal we feed back into
the knowledge graph (see memory_agent.bump_concept_after_practice).
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
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


DIFFICULTY_EASY = "easy"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_HARD = "hard"

VERIFIER_KIND_SYMPY = "sympy"
VERIFIER_KIND_RUBRIC = "rubric_llm"
VERIFIER_KIND_NONE = "none"

VERIFIER_STATUS_VERIFIED = "verified"
VERIFIER_STATUS_REJECTED = "rejected"
VERIFIER_STATUS_UNVERIFIED = "unverified"


class GeneratedProblem(Base):
    __tablename__ = "generated_problems"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    student_id = Column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    subject = Column(String(50), nullable=False)
    # Optional — present when the problem targeted a specific weak concept.
    # NULL means the practice agent generated against the subject alone
    # (cold start, no memory rows yet).
    concept = Column(String(120), nullable=True)
    concept_label = Column(String(200), nullable=True)

    difficulty = Column(String(16), nullable=False, default=DIFFICULTY_MEDIUM)

    prompt_text = Column(Text, nullable=False)
    answer_key = Column(Text, nullable=False)
    explanation = Column(Text, nullable=False, default="")

    verifier_kind = Column(String(16), nullable=False, default=VERIFIER_KIND_NONE)
    verifier_status = Column(String(16), nullable=False, default=VERIFIER_STATUS_UNVERIFIED)
    # Newline-joined short notes from the verifier — mirrors the
    # critic_decisions.reasons shape (no JSON column for v1).
    verifier_notes = Column(Text, nullable=False, default="")

    # How many (generate, verify) loops the practice agent ran before this
    # row reached its current status. 1 = first try. Telemetry for tuning.
    generation_attempts = Column(Integer, nullable=False, default=1)

    attempted = Column(Boolean, nullable=False, default=False)
    correct = Column(Boolean, nullable=True)
    student_answer = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    attempted_at = Column(DateTime(timezone=True), nullable=True)

    student = relationship("Student")

    __table_args__ = (
        Index("ix_generated_problems_student_created", "student_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<GeneratedProblem {self.id} subject={self.subject} "
            f"concept={self.concept} difficulty={self.difficulty} "
            f"verifier={self.verifier_kind}:{self.verifier_status}>"
        )
