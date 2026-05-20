"""
Tests for the knowledge-graph memory agent.

Decay helpers are pure and tested directly. Consolidation tests mock out the
LLM concept extractor and the AsyncSession (mirroring the AsyncMock pattern
used in test_alerts.py and test_clarifications.py — this repo doesn't carry
a real-DB fixture).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config import settings
from services import memory_agent


# ── Pure helpers ──────────────────────────────────────────────────────────────


def test_decayed_confidence_halves_after_one_half_life():
    half_life = settings.memory_decay_half_life_days
    last_seen = datetime.now(timezone.utc) - timedelta(days=half_life)
    decayed = memory_agent.decayed_confidence(0.8, last_seen)
    assert decayed == pytest.approx(0.4, abs=1e-3)


def test_decayed_confidence_unchanged_at_zero_age():
    now = datetime.now(timezone.utc)
    assert memory_agent.decayed_confidence(0.8, now, now=now) == pytest.approx(0.8)


def test_decayed_confidence_handles_future_last_seen():
    """Clock skew shouldn't make confidence climb above stored value."""
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=2)
    assert memory_agent.decayed_confidence(0.8, future, now=now) == pytest.approx(0.8)


def test_derive_status_thresholds():
    assert memory_agent.derive_status(0.9) == "mastered"
    assert memory_agent.derive_status(0.75) == "mastered"  # boundary inclusive
    assert memory_agent.derive_status(0.5) == "partial"
    assert memory_agent.derive_status(0.4) == "partial"   # boundary inclusive
    assert memory_agent.derive_status(0.39) == "struggling"
    assert memory_agent.derive_status(0.0) == "struggling"


# ── Consolidation ─────────────────────────────────────────────────────────────


def _make_session(*, resolved: bool = True):
    s = MagicMock()
    s.id = uuid4()
    s.student_id = uuid4()
    s.subject = "math"
    s.question = "Find the mean of 4, 8, 12"
    s.resolved = resolved
    s.resolved_at = datetime.now(timezone.utc)
    return s


def _make_db_no_existing_row() -> AsyncMock:
    """AsyncSession returning empty attempts list + no existing concept row."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()

    # First execute() = attempt fetch → empty rows
    # Second execute() (and beyond) = concept lookup → no row
    attempts_result = MagicMock()
    attempts_result.all = MagicMock(return_value=[])

    concept_result = MagicMock()
    concept_result.scalar_one_or_none = MagicMock(return_value=None)

    db.execute = AsyncMock(side_effect=[attempts_result, concept_result, concept_result])
    return db


@pytest.mark.asyncio
async def test_consolidate_session_inserts_new_concept():
    db = _make_db_no_existing_row()
    session = _make_session()

    extracted = [
        {"concept": "mean-vs-median", "label": "Mean vs. Median",
         "outcome": "struggling", "confidence": 0.3},
    ]
    with patch.object(memory_agent.concept_extractor, "extract_concepts",
                      new=AsyncMock(return_value=extracted)):
        touched = await memory_agent.consolidate_session(db, session)

    assert touched == 1
    db.add.assert_called_once()
    added = db.add.call_args.args[0]
    assert added.concept == "mean-vs-median"
    assert added.subject == "math"
    assert added.confidence == pytest.approx(0.3)
    assert added.status == "struggling"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_consolidate_session_blends_with_existing_row():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    session = _make_session()

    # Existing row with confidence 0.6 seen *today* — no decay yet.
    existing_row = MagicMock()
    existing_row.confidence = 0.6
    existing_row.last_seen = datetime.now(timezone.utc)
    existing_row.attempts_count = 2
    existing_row.correct_count = 1
    existing_row.label = "Mean vs. Median"

    attempts_result = MagicMock()
    attempts_result.all = MagicMock(return_value=[])
    concept_result = MagicMock()
    concept_result.scalar_one_or_none = MagicMock(return_value=existing_row)
    db.execute = AsyncMock(side_effect=[attempts_result, concept_result])

    extracted = [{"concept": "mean-vs-median", "label": "Mean vs. Median",
                  "outcome": "mastered", "confidence": 1.0}]
    with patch.object(memory_agent.concept_extractor, "extract_concepts",
                      new=AsyncMock(return_value=extracted)):
        touched = await memory_agent.consolidate_session(db, session)

    assert touched == 1
    # blended = 0.6 * 1.0 + 0.4 * 0.6 = 0.84
    assert existing_row.confidence == pytest.approx(0.84, abs=1e-3)
    assert existing_row.status == "mastered"
    db.add.assert_not_called()  # row was updated in place, not inserted


@pytest.mark.asyncio
async def test_consolidate_session_returns_zero_when_extractor_empty():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    attempts_result = MagicMock()
    attempts_result.all = MagicMock(return_value=[])
    db.execute = AsyncMock(return_value=attempts_result)

    session = _make_session()
    with patch.object(memory_agent.concept_extractor, "extract_concepts",
                      new=AsyncMock(return_value=[])):
        touched = await memory_agent.consolidate_session(db, session)

    assert touched == 0
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_consolidate_session_swallows_extractor_error():
    """An extractor exception must not propagate — the resolve path must survive."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    attempts_result = MagicMock()
    attempts_result.all = MagicMock(return_value=[])
    db.execute = AsyncMock(return_value=attempts_result)

    session = _make_session()
    with patch.object(memory_agent.concept_extractor, "extract_concepts",
                      new=AsyncMock(side_effect=RuntimeError("LLM down"))):
        touched = await memory_agent.consolidate_session(db, session)

    assert touched == 0


@pytest.mark.asyncio
async def test_consolidate_session_disabled_via_config():
    db = AsyncMock()
    session = _make_session()
    with patch.object(settings, "memory_consolidation_enabled", False):
        touched = await memory_agent.consolidate_session(db, session)
    assert touched == 0
    db.execute.assert_not_called()


# ── Reads ─────────────────────────────────────────────────────────────────────


def _fake_row(concept: str, confidence: float, days_ago: int, subject: str = "math"):
    row = MagicMock()
    row.concept = concept
    row.label = concept.replace("-", " ").title()
    row.subject = subject
    row.confidence = confidence
    row.last_seen = datetime.now(timezone.utc) - timedelta(days=days_ago)
    row.attempts_count = 1
    row.correct_count = 0
    return row


@pytest.mark.asyncio
async def test_get_student_memory_orders_weakest_first():
    db = AsyncMock()
    rows = [
        _fake_row("mastered-topic", 0.9, days_ago=1),
        _fake_row("forgotten-topic", 0.8, days_ago=14),   # decays to ~0.4
        _fake_row("struggling-topic", 0.3, days_ago=1),
    ]
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    db.execute = AsyncMock(return_value=result)

    out = await memory_agent.get_student_memory(db, uuid4())
    assert [c["concept"] for c in out] == [
        "struggling-topic", "forgotten-topic", "mastered-topic",
    ]


@pytest.mark.asyncio
async def test_get_review_concepts_filters_by_threshold():
    db = AsyncMock()
    rows = [
        _fake_row("strong", 0.9, days_ago=0),
        _fake_row("weak", 0.3, days_ago=0),  # below threshold (0.4)
    ]
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    db.execute = AsyncMock(return_value=result)

    weak = await memory_agent.get_review_concepts(db, uuid4())
    assert len(weak) == 1
    assert weak[0]["concept"] == "weak"
