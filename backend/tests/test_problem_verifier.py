"""
Tests for problem_verifier.

Math path runs real SymPy (no mocks needed). Rubric-LLM path mocks the
AzureOpenAI client. Fail-closed semantics asserted for timeout/error/
malformed-JSON.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from services import problem_verifier


def _mock_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ── Math (SymPy) path ────────────────────────────────────────────────────────


def test_verify_math_accepts_correct_answer_key():
    out = problem_verifier.verify_math_problem("Solve x + 4 = 19", "15")
    assert out["verified"] is True
    assert out["kind"] == "sympy"
    assert out["notes"] == []


def test_verify_math_rejects_wrong_answer_key():
    out = problem_verifier.verify_math_problem("Solve x + 4 = 19", "12")
    assert out["verified"] is False
    assert out["kind"] == "sympy"
    assert out["notes"]  # populated with a "does not match" note


def test_verify_math_accepts_numeric_equivalence():
    """0.5 vs 1/2 should be equivalent under sympy.simplify."""
    out = problem_verifier.verify_math_problem("Solve 2x = 1", "0.5")
    assert out["verified"] is True


def test_verify_math_falls_through_on_word_problem():
    """No parseable expression → kind 'none' so caller defers to rubric-LLM."""
    out = problem_verifier.verify_math_problem(
        "A train travels 60 miles in 2 hours. What is the average speed?",
        "30",
    )
    assert out["kind"] == "none"
    assert out["verified"] is False
    assert any("sympy could not parse" in n for n in out["notes"])


def test_verify_math_rejects_unparseable_answer_key():
    """Garbage that SymPy can't even build into an expression → reject, kind 'sympy'."""
    out = problem_verifier.verify_math_problem("Solve x + 4 = 19", "++**==")
    assert out["verified"] is False
    assert out["kind"] == "sympy"
    assert any("not a parseable expression" in n for n in out["notes"])


def test_verify_math_strips_answer_prefix():
    """'x = 15' should still match the canonical 15."""
    out = problem_verifier.verify_math_problem("Solve x + 4 = 19", "x = 15")
    assert out["verified"] is True


# ── Rubric-LLM path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_with_rubric_accepts_when_both_pass():
    payload = {"well_formed": True, "answer_correct": True, "notes": []}
    with patch.object(problem_verifier.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_verifier.verify_with_rubric(
            "What is the capital of France?", "Paris", "history",
        )
    assert out["verified"] is True
    assert out["kind"] == "rubric_llm"


@pytest.mark.asyncio
async def test_verify_with_rubric_rejects_when_either_fails():
    payload = {"well_formed": True, "answer_correct": False,
               "notes": ["off-by-one"]}
    with patch.object(problem_verifier.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_verifier.verify_with_rubric(
            "p", "bad-answer", "english",
        )
    assert out["verified"] is False
    assert "off-by-one" in out["notes"]


@pytest.mark.asyncio
async def test_verify_with_rubric_fails_closed_on_timeout():
    with patch.object(problem_verifier.client.chat.completions, "create",
                      side_effect=asyncio.TimeoutError):
        out = await problem_verifier.verify_with_rubric(
            "p", "a", "science",
        )
    assert out["verified"] is False
    assert any("timed out" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_verify_with_rubric_fails_closed_on_error():
    with patch.object(problem_verifier.client.chat.completions, "create",
                      side_effect=RuntimeError("kaboom")):
        out = await problem_verifier.verify_with_rubric(
            "p", "a", "english",
        )
    assert out["verified"] is False
    assert any("RuntimeError" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_verify_with_rubric_fails_closed_on_malformed_json():
    with patch.object(problem_verifier.client.chat.completions, "create",
                      return_value=_mock_response("not json")):
        out = await problem_verifier.verify_with_rubric("p", "a", "english")
    assert out["verified"] is False


# ── Dispatcher ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_problem_uses_sympy_for_parseable_math():
    """Math + parseable expression → sympy wins, rubric never runs."""
    with patch.object(problem_verifier, "verify_with_rubric") as mock_rubric:
        out = await problem_verifier.verify_problem("Solve x + 4 = 19", "15", "math")
    assert out["kind"] == "sympy"
    assert out["verified"] is True
    mock_rubric.assert_not_called()


@pytest.mark.asyncio
async def test_verify_problem_falls_through_to_rubric_on_word_math():
    """Math + word problem → sympy returns kind 'none' → rubric runs."""
    payload = {"well_formed": True, "answer_correct": True, "notes": []}
    with patch.object(problem_verifier.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_verifier.verify_problem(
            "A train travels 60 miles in 2 hours. What is its average speed?",
            "30",
            "math",
        )
    assert out["kind"] == "rubric_llm"
    assert out["verified"] is True
    # Carry-forward note about sympy fall-through.
    assert any("sympy fell through" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_verify_problem_goes_straight_to_rubric_for_non_math():
    payload = {"well_formed": True, "answer_correct": True, "notes": []}
    with patch.object(problem_verifier.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_verifier.verify_problem(
            "What is photosynthesis?", "the conversion of light into chemical energy",
            "science",
        )
    assert out["kind"] == "rubric_llm"


# ── Student-attempt checker ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_student_answer_math_equivalent():
    assert await problem_verifier.check_student_answer(
        prompt="Solve x + 4 = 19", answer_key="15",
        student_answer="x = 15", subject="math",
    ) is True


@pytest.mark.asyncio
async def test_check_student_answer_math_wrong():
    assert await problem_verifier.check_student_answer(
        prompt="Solve x + 4 = 19", answer_key="15",
        student_answer="12", subject="math",
    ) is False


@pytest.mark.asyncio
async def test_check_student_answer_empty_returns_false():
    assert await problem_verifier.check_student_answer(
        prompt="p", answer_key="a", student_answer="   ", subject="math",
    ) is False
