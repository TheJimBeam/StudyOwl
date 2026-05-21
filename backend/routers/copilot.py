"""
Teacher Co-Pilot router — class-wide weekly digest endpoints.

All routes are teacher-gated via inline `_require_teacher(teacher)`,
mirroring the alerts router. There is no per-student scoping here — the
digest is class-wide.

Debounce on /regenerate guards against teacher button-mashing; the real
idempotency guarantee lives in copilot_scheduler.generate_report (DB unique
constraint + advisory lock).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import settings
from db import get_db
from models.copilot_pattern import CopilotPattern
from models.copilot_report import CopilotReport
from models.student import Student
from routers.auth import get_current_student
from services import copilot_scheduler
from services.copilot_aggregator import PatternCandidate, build_headline


router = APIRouter()


# ── Pydantic shapes ──────────────────────────────────────────────────────────


class PatternOut(BaseModel):
    id: str
    rank: int
    signal_kind: str
    subject: str
    concept: str | None
    concept_label: str | None
    affected_count: int
    cohort_count: int
    affected_ratio: float
    headline: str
    mini_lesson_md: str | None
    mini_lesson_status: str


class ReportOut(BaseModel):
    id: str
    iso_year: int
    iso_week: int
    week_start_at: str
    week_end_at: str
    status: str
    student_count: int
    session_count: int
    resolved_count: int
    narrative: str
    narrative_status: str
    generated_at: str
    regenerated_at: str | None
    patterns: list[PatternOut]


class ReportsListResponse(BaseModel):
    reports: list[ReportOut]


class RegenerateResponse(BaseModel):
    report: ReportOut
    debounced: bool


# ── Helpers ──────────────────────────────────────────────────────────────────


def _require_teacher(teacher: Student) -> None:
    if teacher.role != "teacher":
        raise HTTPException(
            status_code=403, detail="Only teachers can access the Co-Pilot digest"
        )


def _pattern_out(pattern: CopilotPattern) -> PatternOut:
    # Reuse the same headline builder the narrator used — guarantees the UI
    # never disagrees with the LLM about the underlying numbers.
    candidate = PatternCandidate(
        signal_kind=pattern.signal_kind,
        subject=pattern.subject,
        concept=pattern.concept,
        concept_label=pattern.concept_label,
        affected_count=pattern.affected_count,
        cohort_count=pattern.cohort_count,
        affected_ratio=pattern.affected_ratio,
    )
    return PatternOut(
        id=str(pattern.id),
        rank=pattern.rank,
        signal_kind=pattern.signal_kind,
        subject=pattern.subject,
        concept=pattern.concept,
        concept_label=pattern.concept_label,
        affected_count=pattern.affected_count,
        cohort_count=pattern.cohort_count,
        affected_ratio=pattern.affected_ratio,
        headline=build_headline(candidate),
        mini_lesson_md=pattern.mini_lesson_md,
        mini_lesson_status=pattern.mini_lesson_status,
    )


def _report_out(report: CopilotReport) -> ReportOut:
    patterns = sorted(report.patterns or [], key=lambda p: p.rank)
    return ReportOut(
        id=str(report.id),
        iso_year=report.iso_year,
        iso_week=report.iso_week,
        week_start_at=report.week_start_at.isoformat(),
        week_end_at=report.week_end_at.isoformat(),
        status=report.status,
        student_count=report.student_count,
        session_count=report.session_count,
        resolved_count=report.resolved_count,
        narrative=report.narrative,
        narrative_status=report.narrative_status,
        generated_at=report.generated_at.isoformat(),
        regenerated_at=report.regenerated_at.isoformat() if report.regenerated_at else None,
        patterns=[_pattern_out(p) for p in patterns],
    )


async def _load_latest(db: AsyncSession) -> CopilotReport | None:
    stmt = (
        select(CopilotReport)
        .options(selectinload(CopilotReport.patterns))
        .order_by(CopilotReport.iso_year.desc(), CopilotReport.iso_week.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/latest", response_model=ReportOut | None)
async def get_latest_report(
    teacher: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    """
    Latest weekly digest. Returns null when no report has been generated yet
    (cold-start state — the UI shows the empty-state copy).
    """
    _require_teacher(teacher)
    if not settings.copilot_enabled:
        return None
    report = await _load_latest(db)
    return _report_out(report) if report is not None else None


@router.get("/reports", response_model=ReportsListResponse)
async def list_reports(
    limit: int = Query(8, ge=1, le=52),
    teacher: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    """Recent reports, newest first. Default of 8 covers ~2 months of history."""
    _require_teacher(teacher)
    stmt = (
        select(CopilotReport)
        .options(selectinload(CopilotReport.patterns))
        .order_by(CopilotReport.iso_year.desc(), CopilotReport.iso_week.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return ReportsListResponse(reports=[_report_out(r) for r in rows])


@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate_report(
    teacher: Student = Depends(get_current_student),
    db: AsyncSession = Depends(get_db),
):
    """
    Force-regenerate this ISO week's digest. Debounced by
    `copilot_regenerate_min_interval_seconds` — within the window we return
    the existing report unchanged (`debounced=true`).
    """
    _require_teacher(teacher)
    if not settings.copilot_enabled:
        raise HTTPException(status_code=503, detail="Co-Pilot is disabled")

    latest = await _load_latest(db)
    if latest is not None and latest.regenerated_at is not None:
        elapsed = (datetime.now(timezone.utc) - latest.regenerated_at).total_seconds()
        if elapsed < settings.copilot_regenerate_min_interval_seconds:
            return RegenerateResponse(
                report=_report_out(latest),
                debounced=True,
            )

    report = await copilot_scheduler.generate_report(
        db, regenerated_by=teacher, force=True,
    )
    return RegenerateResponse(report=_report_out(report), debounced=False)
