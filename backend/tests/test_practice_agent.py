"""
Tests for the practice agent orchestrator.

Mocks problem_generator + problem_verifier + memory_agent at module
boundaries — orchestration is the only thing under test. AsyncMock pattern
mirrors test_memory_agent.py (this repo doesn't carry a real-DB fixture).
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config import settings
from models.generated_problem import (
    VERIFIER_STATUS_REJECTED,
    VERIFIER_STATUS_UNVERIFIED,
    VERIFIER_STATUS_VERIFIED,
)
from services import practice_agent


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_calibrate_difficulty_buckets():
    assert practice_agent.calibrate_difficulty(0.1) == "easy"
    assert practice_agent.calibrate_difficulty(0.39) == "easy"
    assert practice_agent.calibrate_difficulty(0.4) == "medium"
    assert practice_agent.calibrate_difficulty(0.69) == "medium"
    assert practice_agent.calibrate_difficulty(0.7) == "hard"
    assert practice_agent.calibrate_difficulty(0.99) == "hard"


def test_calibrate_difficulty_none_is_medium():
    """Cold start (no memory signal) defaults to medium difficulty."""
    assert practice_agent.calibrate_difficulty(None) == "medium"


def test_coerce_subject_falls_back_when_invalid():
    assert practice_agent._coerce_subject(None) == "math"
    assert practice_agent._coerce_subject("astrology") == "math"
    assert practice_agent._coerce_subject("english") == "english"


# ── Orchestrator: shared fixtures ────────────────────────────────────────────


def _make_student(grade="Grade 6"):
    s = MagicMock()
    s.id = uuid4()
    s.grade_level = grade
    return s


def _make_db():
    """AsyncSession with add/commit/refresh stubbed for _persist."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _generated(prompt="Solve x + 4 = 19", answer="15"):
    return {
        "prompt": prompt,
        "answer_key": answer,
        "explanation": "Subtract 4 from both sides.",
        "difficulty": "easy",
    }


# ── generate_verified_problem ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_first_try_pass_persists_verified():
    db = _make_db()
    student = _make_student()
    with patch.object(practice_agent.memory_agent, "get_student_memory",
                      new=AsyncMock(return_value=[])), \
         patch.object(practice_agent.problem_generator, "generate_problem",
                      new=AsyncMock(return_value=_generated())), \
         patch.object(practice_agent.problem_verifier, "verify_problem",
                      new=AsyncMock(return_value={
                          "verified": True, "kind": "sympy", "notes": [],
                      })):
        row = await practice_agent.generate_verified_problem(
            db=db, student=student, subject="math",
        )
    assert row.verifier_status == VERIFIER_STATUS_VERIFIED
    assert row.generation_attempts == 1
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_generate_retries_after_first_rejection():
    """First verify rejects; second passes. Both rows persisted."""
    db = _make_db()
    student = _make_student()
    verifier = AsyncMock(side_effect=[
        {"verified": False, "kind": "sympy", "notes": ["mismatch"]},
        {"verified": True, "kind": "sympy", "notes": []},
    ])
    with patch.object(settings, "practice_max_generation_retries", 2), \
         patch.object(practice_agent.memory_agent, "get_student_memory",
                      new=AsyncMock(return_value=[])), \
         patch.object(practice_agent.problem_generator, "generate_problem",
                      new=AsyncMock(side_effect=[_generated(), _generated()])), \
         patch.object(practice_agent.problem_verifier, "verify_problem", new=verifier):
        row = await practice_agent.generate_verified_problem(
            db=db, student=student, subject="math",
        )
    assert row.verifier_status == VERIFIER_STATUS_VERIFIED
    assert row.generation_attempts == 2
    # One rejected row + one verified row = 2 inserts.
    assert db.add.call_count == 2


@pytest.mark.asyncio
async def test_generate_persists_unverified_when_retries_exhausted():
    db = _make_db()
    student = _make_student()
    verifier = AsyncMock(return_value={
        "verified": False, "kind": "sympy", "notes": ["bad"],
    })
    with patch.object(settings, "practice_max_generation_retries", 2), \
         patch.object(practice_agent.memory_agent, "get_student_memory",
                      new=AsyncMock(return_value=[])), \
         patch.object(practice_agent.problem_generator, "generate_problem",
                      new=AsyncMock(return_value=_generated())), \
         patch.object(practice_agent.problem_verifier, "verify_problem", new=verifier):
        row = await practice_agent.generate_verified_problem(
            db=db, student=student, subject="math",
        )
    # Two rejected attempts + one unverified final = 3 inserts.
    assert row.verifier_status == VERIFIER_STATUS_UNVERIFIED
    assert db.add.call_count == 3


@pytest.mark.asyncio
async def test_generate_uses_weakest_concept_from_memory():
    db = _make_db()
    student = _make_student()
    weakest = {
        "concept": "mean-vs-median",
        "label": "Mean vs. Median",
        "subject": "math",
        "status": "struggling",
        "confidence": 0.2,
        "decayed_confidence": 0.2,
        "last_seen": "2026-04-01T00:00:00+00:00",
        "attempts": 1,
        "correct": 0,
    }
    captured = {}

    async def _gen(**kwargs):
        captured.update(kwargs)
        return _generated()

    with patch.object(practice_agent.memory_agent, "get_student_memory",
                      new=AsyncMock(return_value=[weakest])), \
         patch.object(practice_agent.problem_generator, "generate_problem",
                      new=AsyncMock(side_effect=_gen)), \
         patch.object(practice_agent.problem_verifier, "verify_problem",
                      new=AsyncMock(return_value={
                          "verified": True, "kind": "sympy", "notes": [],
                      })):
        row = await practice_agent.generate_verified_problem(
            db=db, student=student,  # no subject / concept
        )
    assert captured["concept"] == "mean-vs-median"
    assert captured["concept_label"] == "Mean vs. Median"
    # decayed_confidence=0.2 < 0.4 → easy
    assert captured["difficulty"] == "easy"
    assert row.concept == "mean-vs-median"


@pytest.mark.asyncio
async def test_generate_cold_start_with_no_memory_falls_back_to_subject_only():
    db = _make_db()
    student = _make_student()
    captured = {}

    async def _gen(**kwargs):
        captured.update(kwargs)
        return _generated()

    with patch.object(practice_agent.memory_agent, "get_student_memory",
                      new=AsyncMock(return_value=[])), \
         patch.object(practice_agent.problem_generator, "generate_problem",
                      new=AsyncMock(side_effect=_gen)), \
         patch.object(practice_agent.problem_verifier, "verify_problem",
                      new=AsyncMock(return_value={
                          "verified": True, "kind": "sympy", "notes": [],
                      })):
        await practice_agent.generate_verified_problem(
            db=db, student=student, subject="math",
        )
    assert captured["concept"] is None
    assert captured["concept_label"] is None
    # No signal → medium difficulty by default.
    assert captured["difficulty"] == "medium"


@pytest.mark.asyncio
async def test_generate_handles_generator_failure_gracefully():
    """Generator returns None on every retry → persists a sentinel unverified row."""
    db = _make_db()
    student = _make_student()
    with patch.object(settings, "practice_max_generation_retries", 2), \
         patch.object(practice_agent.memory_agent, "get_student_memory",
                      new=AsyncMock(return_value=[])), \
         patch.object(practice_agent.problem_generator, "generate_problem",
                      new=AsyncMock(return_value=None)), \
         patch.object(practice_agent.problem_verifier, "verify_problem") as mock_verify:
        row = await practice_agent.generate_verified_problem(
            db=db, student=student, subject="math",
        )
    assert row.verifier_status == VERIFIER_STATUS_UNVERIFIED
    mock_verify.assert_not_called()


# ── submit_attempt ───────────────────────────────────────────────────────────


def _problem_row(concept="mean-vs-median", attempted=False):
    row = MagicMock()
    row.id = uuid4()
    row.student_id = uuid4()
    row.subject = "math"
    row.concept = concept
    row.concept_label = "Mean vs. Median" if concept else None
    row.prompt_text = "Solve x + 4 = 19"
    row.answer_key = "15"
    row.explanation = "Subtract 4."
    row.attempted = attempted
    row.correct = None
    return row


@pytest.mark.asyncio
async def test_submit_attempt_correct_flips_row_and_bumps_memory():
    db = _make_db()
    problem = _problem_row()
    bump = AsyncMock()
    with patch.object(practice_agent.problem_verifier, "check_student_answer",
                      new=AsyncMock(return_value=True)), \
         patch.object(practice_agent.memory_agent, "bump_concept_after_practice",
                      new=bump):
        out = await practice_agent.submit_attempt(db, problem, "x = 15")
    assert out["correct"] is True
    assert problem.attempted is True
    assert problem.correct is True
    bump.assert_awaited_once()
    # Memory bump was called with the right slug + correct=True.
    bump_kwargs = bump.call_args.kwargs
    assert bump_kwargs["concept"] == "mean-vs-median"
    assert bump_kwargs["correct"] is True


@pytest.mark.asyncio
async def test_submit_attempt_skips_memory_bump_when_no_concept():
    """Cold-start problems (concept=None) don't have a slug to attribute to."""
    db = _make_db()
    problem = _problem_row(concept=None)
    bump = AsyncMock()
    with patch.object(practice_agent.problem_verifier, "check_student_answer",
                      new=AsyncMock(return_value=True)), \
         patch.object(practice_agent.memory_agent, "bump_concept_after_practice",
                      new=bump):
        out = await practice_agent.submit_attempt(db, problem, "15")
    assert out["correct"] is True
    bump.assert_not_called()


@pytest.mark.asyncio
async def test_submit_attempt_idempotent_after_first_submission():
    """Re-submitting on an already-attempted row returns stored verdict."""
    db = _make_db()
    problem = _problem_row(attempted=True)
    problem.correct = False
    grade = AsyncMock(return_value=True)
    with patch.object(practice_agent.problem_verifier, "check_student_answer",
                      new=grade):
        out = await practice_agent.submit_attempt(db, problem, "anything")
    assert out["already_attempted"] is True
    assert out["correct"] is False  # stored verdict, not re-graded
    grade.assert_not_called()


@pytest.mark.asyncio
async def test_submit_attempt_swallows_memory_bump_errors():
    """A memory hiccup must not invalidate the graded attempt."""
    db = _make_db()
    problem = _problem_row()
    with patch.object(practice_agent.problem_verifier, "check_student_answer",
                      new=AsyncMock(return_value=True)), \
         patch.object(practice_agent.memory_agent, "bump_concept_after_practice",
                      new=AsyncMock(side_effect=RuntimeError("memory dead"))):
        out = await practice_agent.submit_attempt(db, problem, "15")
    # The attempt was graded and committed despite the memory failure.
    assert out["correct"] is True
    assert problem.attempted is True
