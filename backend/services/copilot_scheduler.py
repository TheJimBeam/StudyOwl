"""
Co-Pilot scheduler — weekly background tick + the shared generate_report
entrypoint used by both the tick and the on-demand /regenerate endpoint.

Reports cover the *previous completed* ISO week, never the in-flight one —
otherwise Monday-morning regenerate would only see partial data.

Idempotency is layered:
  1. UNIQUE(iso_year, iso_week) on copilot_reports
  2. pg_try_advisory_xact_lock(_ADVISORY_LOCK_KEY) — distinct from
     inactivity_scheduler's lock so the two coexist on the same DB
  3. existence check inside _tick before any LLM call

Manual regenerate (force=True) deletes the prior row in the same transaction;
the cascade on copilot_patterns kills the children, so the new insert never
hits the UNIQUE constraint.

Per-pattern LLM failure is intentionally scoped to that pattern row — the
report still flips to status=ready with the failed pattern marked. The whole
report only fails if aggregation itself errors before commit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import settings
from db import SessionLocal, engine
from models.copilot_pattern import CopilotPattern
from models.copilot_report import (
    ARTIFACT_FAILED,
    CopilotReport,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
)
from models.student import Student

from . import copilot_aggregator, copilot_narrator


logger = logging.getLogger(__name__)


_ADVISORY_LOCK_KEY = 612_345_789  # distinct from inactivity_scheduler's 987_123_456


def current_iso_week(now: datetime | None = None) -> tuple[int, int, datetime, datetime]:
    """
    Return (iso_year, iso_week, week_start, week_end) for the *previous*
    completed ISO week. Week boundaries are Monday 00:00 UTC inclusive and
    next Monday 00:00 UTC exclusive.
    """
    now = now or datetime.now(timezone.utc)
    # Monday of the current ISO week, at 00:00 UTC.
    this_monday = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=now.weekday())
    )
    # Previous week's Monday → Sunday-end (== this Monday, exclusive).
    week_start = this_monday - timedelta(days=7)
    week_end = this_monday
    iso_year, iso_week, _ = week_start.isocalendar()
    return (iso_year, iso_week, week_start, week_end)


async def generate_report(
    db: AsyncSession,
    *,
    regenerated_by: Student | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> CopilotReport:
    """
    Single entrypoint used by both the weekly tick and the /regenerate route.

    - force=False : if an existing row exists for the target ISO week, return
                    it without mutating. Cheap (one indexed SELECT).
    - force=True  : delete the existing row + its patterns inside this
                    transaction, then generate fresh. Caller is responsible
                    for any debounce policy (the router enforces it).
    """
    iso_year, iso_week, week_start, week_end = current_iso_week(now=now)

    existing_stmt = (
        select(CopilotReport)
        .options(selectinload(CopilotReport.patterns))
        .where(
            CopilotReport.iso_year == iso_year,
            CopilotReport.iso_week == iso_week,
        )
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()

    if existing is not None and not force:
        return existing

    if existing is not None and force:
        await db.delete(existing)
        await db.flush()

    # Aggregation MUST happen before any LLM call: if it errors, we never
    # write a half-empty report row.
    try:
        aggregate = await copilot_aggregator.aggregate_week(db, week_start, week_end)
    except Exception as exc:
        logger.exception("Copilot aggregation failed for %s-W%s: %s", iso_year, iso_week, exc)
        # Persist a `failed` marker so the UI can show "this week's run errored"
        # instead of silently regressing to an older report.
        report = CopilotReport(
            iso_year=iso_year,
            iso_week=iso_week,
            week_start_at=week_start,
            week_end_at=week_end,
            status=STATUS_FAILED,
            student_count=0,
            session_count=0,
            resolved_count=0,
            narrative="",
            narrative_status=ARTIFACT_FAILED,
            regenerated_at=datetime.now(timezone.utc) if regenerated_by else None,
            regenerated_by=regenerated_by.id if regenerated_by else None,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        return report

    report = CopilotReport(
        iso_year=iso_year,
        iso_week=iso_week,
        week_start_at=week_start,
        week_end_at=week_end,
        status=STATUS_PENDING,
        student_count=aggregate.student_count,
        session_count=aggregate.session_count,
        resolved_count=aggregate.resolved_count,
        narrative="",
        narrative_status="skipped",
        regenerated_at=datetime.now(timezone.utc) if regenerated_by else None,
        regenerated_by=regenerated_by.id if regenerated_by else None,
    )
    db.add(report)
    await db.flush()  # so report.id is available for child rows

    # Narrative (class summary) — own try/except, never aborts the report.
    narrative_text, narrative_status = await copilot_narrator.narrate_summary(aggregate)
    report.narrative = narrative_text
    report.narrative_status = narrative_status

    # Per-pattern mini-lesson drafts. Each pattern is its own try — one
    # LLM stumble must not poison the rollup.
    for rank, candidate in enumerate(aggregate.patterns, start=1):
        pattern = CopilotPattern(
            report_id=report.id,
            rank=rank,
            signal_kind=candidate.signal_kind,
            subject=candidate.subject,
            concept=candidate.concept,
            concept_label=candidate.concept_label,
            affected_count=candidate.affected_count,
            cohort_count=candidate.cohort_count,
            affected_ratio=candidate.affected_ratio,
            evidence_json=candidate.evidence or {},
            mini_lesson_status="skipped",
        )
        db.add(pattern)

        try:
            md, status, error = await copilot_narrator.draft_mini_lesson(candidate)
        except Exception as exc:
            # draft_mini_lesson is supposed to be non-raising; this is a
            # belt-and-braces fallback so an unexpected raise doesn't tank
            # the rest of the report.
            logger.exception(
                "Copilot mini-lesson draft raised unexpectedly for %s/%s: %s",
                candidate.subject, candidate.concept, exc,
            )
            md, status, error = (None, "failed", f"unexpected: {type(exc).__name__}")

        pattern.mini_lesson_md = md
        pattern.mini_lesson_status = status
        pattern.mini_lesson_error = error

    report.status = STATUS_READY
    await db.commit()
    await db.refresh(report)

    # Re-load with patterns eager-loaded so the caller can serialize without
    # hitting MissingGreenlet on the relationship lazy load.
    refreshed = (
        await db.execute(
            select(CopilotReport)
            .options(selectinload(CopilotReport.patterns))
            .where(CopilotReport.id == report.id)
        )
    ).scalar_one()
    return refreshed


async def _tick() -> str:
    """
    One scheduler tick. Returns a short status string for logs:

      - "skipped_dialect" : non-PG dialect (SQLite dev), advisory locks N/A
      - "skipped_locked"  : another worker already holds the advisory lock
      - "skipped_exists"  : current ISO week already has a report
      - "created"         : a new report was generated this tick
      - "failed"          : aggregation errored (report row is `failed` status)
    """
    if engine.dialect.name != "postgresql":
        return "skipped_dialect"

    async with SessionLocal() as db:
        got_lock = await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=_ADVISORY_LOCK_KEY)
        )
        if not got_lock.scalar():
            await db.rollback()
            return "skipped_locked"

        iso_year, iso_week, week_start, week_end = current_iso_week()
        existing = (await db.execute(
            select(CopilotReport.id).where(
                CopilotReport.iso_year == iso_year,
                CopilotReport.iso_week == iso_week,
            )
        )).scalar_one_or_none()
        if existing is not None:
            await db.rollback()
            return "skipped_exists"

        report = await generate_report(db, force=False)
        return "failed" if report.status == STATUS_FAILED else "created"


async def run() -> None:
    """Long-running task: hourly heartbeat, only one tick per ISO week does work."""
    interval = settings.copilot_scheduler_interval_seconds
    logger.info(
        "Teacher Co-Pilot scheduler started (interval=%ss).",
        interval,
    )
    try:
        while True:
            try:
                outcome = await _tick()
                if outcome == "created":
                    logger.info("Co-Pilot weekly report created.")
                elif outcome == "failed":
                    logger.warning("Co-Pilot weekly report failed to aggregate.")
                else:
                    logger.debug("Co-Pilot tick: %s", outcome)
            except Exception as exc:
                # Never let a bad tick kill the scheduler loop.
                logger.exception("Co-Pilot tick error: %s", exc)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Teacher Co-Pilot scheduler stopped.")
        raise
