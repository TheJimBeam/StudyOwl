"""
Tests for the Co-Pilot router.

Goes through the router handlers directly with stubbed DB + teacher dep,
mirroring test_alerts.py's style. Pins:
  - _require_teacher returns 403 for students
  - GET /latest returns null on cold start
  - POST /regenerate is debounced within the configured window
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from config import settings
from models.copilot_report import STATUS_READY
from routers import copilot as copilot_router


def _teacher():
    return SimpleNamespace(id=uuid4(), role="teacher", name="Ms. Park")


def _student():
    return SimpleNamespace(id=uuid4(), role="student", name="Sam")


def _make_db_returning_latest(latest):
    """AsyncSession stub where _load_latest will see `latest`."""
    class _Result:
        def __init__(self, val):
            self._val = val
        def scalar_one_or_none(self):
            return self._val

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(latest))
    return db


def _fake_report(*, regenerated_at=None) -> MagicMock:
    """A CopilotReport-shaped MagicMock with .patterns iterable."""
    r = MagicMock()
    r.id = uuid4()
    r.iso_year = 2026
    r.iso_week = 20
    r.week_start_at = datetime(2026, 5, 11, tzinfo=timezone.utc)
    r.week_end_at = datetime(2026, 5, 18, tzinfo=timezone.utc)
    r.status = STATUS_READY
    r.student_count = 28
    r.session_count = 140
    r.resolved_count = 98
    r.narrative = "Class summary draft."
    r.narrative_status = "ok"
    r.generated_at = datetime(2026, 5, 18, 1, 0, tzinfo=timezone.utc)
    r.regenerated_at = regenerated_at
    r.patterns = []
    return r


# ── _require_teacher ─────────────────────────────────────────────────────────


def test_require_teacher_rejects_students():
    with pytest.raises(HTTPException) as exc:
        copilot_router._require_teacher(_student())
    assert exc.value.status_code == 403


def test_require_teacher_allows_teachers():
    copilot_router._require_teacher(_teacher())  # should not raise


# ── GET /latest ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_latest_returns_none_on_cold_start():
    db = _make_db_returning_latest(None)
    result = await copilot_router.get_latest_report(teacher=_teacher(), db=db)
    assert result is None


@pytest.mark.asyncio
async def test_latest_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "copilot_enabled", False)
    # When disabled, the handler short-circuits BEFORE the DB call.
    db = AsyncMock()
    result = await copilot_router.get_latest_report(teacher=_teacher(), db=db)
    assert result is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_latest_returns_report_when_present():
    report = _fake_report()
    db = _make_db_returning_latest(report)
    result = await copilot_router.get_latest_report(teacher=_teacher(), db=db)
    assert result is not None
    assert result.iso_week == 20
    assert result.status == STATUS_READY


# ── POST /regenerate — debounce ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerate_within_debounce_returns_existing_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "copilot_regenerate_min_interval_seconds", 300)
    recent = datetime.now(timezone.utc) - timedelta(seconds=30)
    report = _fake_report(regenerated_at=recent)
    db = _make_db_returning_latest(report)

    with patch("routers.copilot.copilot_scheduler.generate_report",
               new=AsyncMock()) as gen:
        result = await copilot_router.regenerate_report(teacher=_teacher(), db=db)

    assert result.debounced is True
    # Critical: generate_report MUST NOT have been called within debounce.
    gen.assert_not_called()


@pytest.mark.asyncio
async def test_regenerate_past_debounce_calls_generator(monkeypatch):
    monkeypatch.setattr(settings, "copilot_regenerate_min_interval_seconds", 60)
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    existing = _fake_report(regenerated_at=old)
    db = _make_db_returning_latest(existing)

    fresh = _fake_report(regenerated_at=datetime.now(timezone.utc))
    with patch("routers.copilot.copilot_scheduler.generate_report",
               new=AsyncMock(return_value=fresh)) as gen:
        result = await copilot_router.regenerate_report(teacher=_teacher(), db=db)

    assert result.debounced is False
    gen.assert_awaited_once()
    # force=True is the contract here — without it, the unique constraint
    # would refuse the second insert.
    assert gen.await_args.kwargs.get("force") is True


@pytest.mark.asyncio
async def test_regenerate_on_cold_start_calls_generator():
    db = _make_db_returning_latest(None)
    fresh = _fake_report()
    with patch("routers.copilot.copilot_scheduler.generate_report",
               new=AsyncMock(return_value=fresh)) as gen:
        result = await copilot_router.regenerate_report(teacher=_teacher(), db=db)
    assert result.debounced is False
    gen.assert_awaited_once()


@pytest.mark.asyncio
async def test_regenerate_returns_503_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "copilot_enabled", False)
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await copilot_router.regenerate_report(teacher=_teacher(), db=db)
    assert exc.value.status_code == 503
