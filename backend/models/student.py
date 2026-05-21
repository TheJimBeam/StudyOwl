"""
Student model.
"""

from uuid import uuid4
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID

from db import Base
from datetime import datetime, timezone


class Student(Base):
    __tablename__ = "students"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    grade_level = Column(String(50), nullable=False)  # e.g., "Grade 7", "University"
    role = Column(String(20), default="student")  # "student" | "teacher" | "admin"
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships. `foreign_keys` is explicit because Session has two FKs to
    # students.id (student_id and teacher_comment_by_id) — we only want this
    # collection to traverse the former.
    sessions = relationship(
        "Session",
        back_populates="student",
        foreign_keys="Session.student_id",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Student {self.email} ({self.role})>"
