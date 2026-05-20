"""
Practice router — closed-loop practice agent endpoints.

POST /api/practice/generate          — orchestrate generate+verify, return row
GET  /api/practice/problems          — list the caller's history
POST /api/practice/{id}/attempt      — submit an answer, returns verdict
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db import get_db
from models.generated_problem import GeneratedProblem
from models.student import Student
from routers.auth import get_current_student
from services import practice_agent


router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    subject: str | None = Field(default=None, max_length=50)
    concept: str | None = Field(default=None, max_length=120)


class ProblemResponse(BaseModel):
    id: str
    subject: str
    concept: str | None
    concept_label: str | None
    difficulty: str
    prompt_text: str
    explanation: str | None
    verifier_kind: str
    verifier_status: str
    verifier_notes: list[str]
    generation_attempts: int
    attempted: bool
    correct: bool | None
    created_at: str
    # Answer key is intentionally NEVER exposed before the student submits.
    answer_key: str | None = None


class AttemptRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=1000)


class AttemptResponse(BaseModel):
    correct: bool
    answer_key: str
    explanation: str
    already_attempted: bool


class PracticeHistoryResponse(BaseModel):
    problems: list[ProblemResponse]


# ── Shaping helpers ───────────────────────────────────────────────────────────


def _shape_problem(
    row: GeneratedProblem,
    *,
    reveal_answer: bool,
) -> ProblemResponse:
    notes = [n for n in (row.verifier_notes or "").split("\n") if n]
    return ProblemResponse(
        id=str(row.id),
        subject=row.subject,
        concept=row.concept,
        concept_label=row.concept_label,
        difficulty=row.difficulty,
        prompt_text=row.prompt_text,
        explanation=row.explanation if reveal_answer else None,
        verifier_kind=row.verifier_kind,
        verifier_status=row.verifier_status,
        verifier_notes=notes,
        generation_attempts=row.generation_attempts,
        attempted=row.attempted,
        correct=row.correct,
        created_at=row.created_at.isoformat(),
        answer_key=row.answer_key if reveal_answer else None,
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/generate", response_model=ProblemResponse)
async def generate_practice_problem(
    req: GenerateRequest,
    current_student: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    """
    Orchestrate generate+verify and persist the resulting problem.

    Students only. Teachers can't generate against another student's memory
    here — that would change the memory closed-loop's attribution. They can
    still read history via GET /api/practice/problems?student_id=...
    """
    if current_student.role != "student":
        raise HTTPException(
            status_code=403,
            detail="Only students can generate practice problems",
        )
    if not settings.practice_generator_enabled:
        raise HTTPException(
            status_code=503,
            detail="Practice problem generator is currently disabled",
        )

    row = await practice_agent.generate_verified_problem(
        db=db,
        student=current_student,
        subject=req.subject,
        concept=req.concept,
    )
    # Pre-attempt: never leak the answer key or explanation.
    return _shape_problem(row, reveal_answer=False)


@router.get("/problems", response_model=PracticeHistoryResponse)
async def list_practice_problems(
    student_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_student: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    """
    List the caller's verified-or-unverified history. Teachers may pass
    `student_id` to view another student's history; students can only
    view their own.
    """
    if student_id is None:
        target_id = current_student.id
    else:
        try:
            target_uuid = UUID(student_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid student ID")
        if current_student.role == "student":
            if target_uuid != current_student.id:
                raise HTTPException(
                    status_code=403,
                    detail="Students can only view their own practice history",
                )
        elif current_student.role != "teacher":
            raise HTTPException(status_code=403, detail="Not authorized")
        target_id = target_uuid

    rows = await practice_agent.get_problem_history(
        db=db,
        student_id=target_id,
        limit=min(limit, settings.practice_history_limit),
        offset=offset,
    )
    # History always reveals the answer key (post-attempt or otherwise) —
    # students see their own past problems, teachers see for analytics.
    return PracticeHistoryResponse(
        problems=[_shape_problem(r, reveal_answer=True) for r in rows],
    )


@router.post("/{problem_id}/attempt", response_model=AttemptResponse)
async def submit_practice_attempt(
    problem_id: str,
    req: AttemptRequest,
    current_student: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    """
    Grade a single attempt. Idempotent: re-submitting on an already-attempted
    problem returns the stored verdict without re-grading.
    """
    try:
        problem_uuid = UUID(problem_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid problem ID")

    stmt = select(GeneratedProblem).where(GeneratedProblem.id == problem_uuid)
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Practice problem not found")

    if current_student.role == "student" and row.student_id != current_student.id:
        raise HTTPException(
            status_code=403,
            detail="Students can only submit attempts on their own problems",
        )
    if current_student.role not in ("student", "teacher"):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Unverified problems are still gradeable — the answer key was
    # generated, just not double-checked. Blocking would feel worse than
    # a possibly-wrong "wrong" verdict.
    result = await practice_agent.submit_attempt(db, row, req.answer)

    return AttemptResponse(
        correct=result["correct"],
        answer_key=result["answer_key"],
        explanation=result["explanation"],
        already_attempted=result.get("already_attempted", False),
    )


@router.get("/health")
async def practice_health() -> dict:
    """Lightweight introspection — useful from the frontend to disable the card."""
    return {
        "enabled": settings.practice_generator_enabled,
        "max_retries": settings.practice_max_generation_retries,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
