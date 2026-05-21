"""
Tests for the Co-Pilot scheduler.

Narrow on purpose — multi-worker advisory-lock behavior requires a real PG
fixture, and the orchestrator's DB writes are exercised end-to-end by the
router tests rather than re-stubbed here. What we pin:
  - current_iso_week returns last *completed* week (never the in-flight one)
  - Non-PG dialect short-circuits the tick
  - Loop survives a bad tick
  - generate_report still ships status=ready when per-pattern LLM fails
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services import copilot_scheduler
from services.copilot_aggregator import PatternCandidate, WeeklyAggregate


# ── current_iso_week ─────────────────────────────────────────────────────────


def test_current_iso_week_returns_previous_completed_week():
    """A Wednesday must produce last week's Monday → this Monday window."""
    wednesday = datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc)  # Wed
    iso_year, iso_week, week_start, week_end = copilot_scheduler.current_iso_week(now=wednesday)

    # Last week's Monday at 00:00 UTC.
    expected_start = datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)
    expected_end = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
    assert week_start == expected_start
    assert week_end == expected_end
    assert (iso_year, iso_week) == expected_start.isocalendar()[:2]


def test_current_iso_week_on_monday_returns_prior_week():
    """Monday 00:00:00 — we still report on *last* week, not the in-flight one."""
    monday_morning = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
    _, _, week_start, week_end = copilot_scheduler.current_iso_week(now=monday_morning)
    assert week_start == datetime(2026, 5, 11, tzinfo=timezone.utc)
    assert week_end == datetime(2026, 5, 18, tzinfo=timezone.utc)
    assert (week_end - week_start) == timedelta(days=7)


def test_current_iso_week_window_is_exactly_seven_days():
    sunday_evening = datetime(2026, 5, 17, 23, 59, tzinfo=timezone.utc)
    _, _, ws, we = copilot_scheduler.current_iso_week(now=sunday_evening)
    assert (we - ws) == timedelta(days=7)


# ── _tick ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_skipped_on_non_postgres():
    """SQLite (dev) must short-circuit without touching SessionLocal."""
    class _Dialect:
        name = "sqlite"

    with patch.object(copilot_scheduler, "engine") as mock_engine, \
         patch.object(copilot_scheduler, "SessionLocal") as mock_sl:
        mock_engine.dialect = _Dialect()
        result = await copilot_scheduler._tick()
    assert result == "skipped_dialect"
    mock_sl.assert_not_called()


# ── run loop ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_survives_tick_errors():
    call_count = 0

    async def flaky_tick():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated tick failure")
        return "skipped_dialect"

    async def fake_sleep(_):
        if call_count >= 2:
            raise asyncio.CancelledError()

    with patch.object(copilot_scheduler, "_tick", new=AsyncMock(side_effect=flaky_tick)), \
         patch("services.copilot_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
        with pytest.raises(asyncio.CancelledError):
            await copilot_scheduler.run()

    assert call_count >= 2, "Loop must retry after a failed tick"


# ── generate_report — per-pattern LLM failure ────────────────────────────────


def _aggregate_with_two_patterns() -> WeeklyAggregate:
    return WeeklyAggregate(
        week_start=datetime(2026, 5, 11, tzinfo=timezone.utc),
        week_end=datetime(2026, 5, 18, tzinfo=timezone.utc),
        student_count=28,
        session_count=140,
        resolved_count=98,
        patterns=[
            PatternCandidate(
                signal_kind="level3_stuck",
                subject="math",
                concept="quadratic-factoring",
                concept_label="Quadratic Factoring",
                affected_count=14, cohort_count=28, affected_ratio=0.5,
            ),
            PatternCandidate(
                signal_kind="concept_struggle",
                subject="english",
                concept="subject-verb-agreement",
                concept_label="Subject-Verb Agreement",
                affected_count=8, cohort_count=20, affected_ratio=0.4,
            ),
        ],
    )


class _FakeReport:
    """Captures the row passed to db.add so we can assert on its fields."""
    def __init__(self):
        self.id = uuid4()
        self.status = None
        self.narrative = ""
        self.narrative_status = "skipped"
        self.patterns = []


@pytest.mark.asyncio
async def test_generate_report_per_pattern_failure_still_ships_ready():
    """
    One pattern's mini-lesson errors; the other succeeds. The report flips to
    READY regardless, and only the failed pattern carries mini_lesson_status='failed'.
    """
    aggregate = _aggregate_with_two_patterns()

    added: list = []
    refreshed_report: list = []

    class _ExecuteResult:
        def __init__(self, value=None):
            self._value = value
        def scalar_one_or_none(self):
            return self._value
        def scalar_one(self):
            return self._value

    db = AsyncMock()
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # Two execute() calls inside generate_report:
    #  1. existing-row lookup (returns None for cold-path)
    #  2. final reload-with-patterns (returns our captured report)
    captured_report = None

    def _make_execute():
        call_count = {"n": 0}

        async def _execute(_stmt):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _ExecuteResult(None)
            # Final eager-load: return the captured report.
            return _ExecuteResult(refreshed_report[0] if refreshed_report else captured_report)

        return _execute

    db.execute = AsyncMock(side_effect=_make_execute())

    # Aggregate is supplied directly; narrator narrative path is OK.
    # First mini-lesson call OK, second raises — the orchestrator must
    # catch and mark only the second pattern as failed.
    call_idx = {"n": 0}

    async def _draft(_pat):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            return ("# Possible 12-min mini-lesson: ok", "ok", None)
        return (None, "failed", "timeout")

    with patch("services.copilot_scheduler.copilot_aggregator.aggregate_week",
               new=AsyncMock(return_value=aggregate)), \
         patch("services.copilot_scheduler.copilot_narrator.narrate_summary",
               new=AsyncMock(return_value=("Class summary draft.", "ok"))), \
         patch("services.copilot_scheduler.copilot_narrator.draft_mini_lesson",
               new=AsyncMock(side_effect=_draft)):
        # generate_report internally calls db.execute twice; on the second
        # call we need it to return the report we just built.
        # Capture the report instance after db.add for the eager-load shim.
        original_add = db.add.side_effect
        def _capture(obj):
            original_add(obj)
            if obj.__class__.__name__ == "CopilotReport":
                refreshed_report.append(obj)
        db.add.side_effect = _capture

        result = await copilot_scheduler.generate_report(db, force=False)

    assert result.status == "ready", "Report must still ship with per-pattern failure"
    # 1 report + 2 pattern rows
    pattern_rows = [o for o in added if o.__class__.__name__ == "CopilotPattern"]
    assert len(pattern_rows) == 2
    statuses = [p.mini_lesson_status for p in pattern_rows]
    assert statuses.count("ok") == 1
    assert statuses.count("failed") == 1


@pytest.mark.asyncio
async def test_generate_report_aggregation_failure_marks_report_failed():
    """If aggregation raises, persist a status=failed row so UI can show the error."""

    class _ExecuteResult:
        def scalar_one_or_none(self):
            return None

    db = AsyncMock()
    added: list = []
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecuteResult())

    with patch("services.copilot_scheduler.copilot_aggregator.aggregate_week",
               new=AsyncMock(side_effect=RuntimeError("DB blew up"))):
        result = await copilot_scheduler.generate_report(db, force=False)

    assert result.status == "failed"
    assert result.narrative_status == "failed"
    # No pattern rows were inserted on the aggregation-failed path.
    assert not [o for o in added if o.__class__.__name__ == "CopilotPattern"]
