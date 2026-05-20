"""
Tests for problem_generator.

Mocks the Azure OpenAI client at the module boundary (same pattern as
test_critic.py). Asserts JSON parsing, defensive trimming, difficulty
defaulting, empty-on-error contract, and concept injection into the prompt.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from services import problem_generator


def _mock_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.mark.asyncio
async def test_generate_problem_parses_well_formed_json():
    payload = {
        "prompt": "A train travels 60 miles in 2 hours. What is its average speed?",
        "answer_key": "30",
        "explanation": "Speed = distance / time = 60 / 2.",
        "difficulty": "easy",
    }
    with patch.object(problem_generator.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_generator.generate_problem(
            subject="math", concept="rate-distance-time",
            concept_label="Rate, Distance, Time",
            difficulty="easy", grade_level="Grade 6",
        )
    assert out is not None
    assert out["prompt"].startswith("A train")
    assert out["answer_key"] == "30"
    assert out["difficulty"] == "easy"


@pytest.mark.asyncio
async def test_generate_problem_returns_none_on_malformed_json():
    with patch.object(problem_generator.client.chat.completions, "create",
                      return_value=_mock_response("not json at all")):
        out = await problem_generator.generate_problem(
            subject="math", concept=None, concept_label=None,
            difficulty="medium",
        )
    assert out is None


@pytest.mark.asyncio
async def test_generate_problem_returns_none_on_missing_required_fields():
    """No `answer_key` field → reject. We never persist half-baked rows."""
    payload = {"prompt": "What is 2+2?", "explanation": "Trivial.", "difficulty": "easy"}
    with patch.object(problem_generator.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_generator.generate_problem(
            subject="math", concept=None, concept_label=None, difficulty="easy",
        )
    assert out is None


@pytest.mark.asyncio
async def test_generate_problem_defaults_invalid_difficulty():
    """LLM emits an out-of-range difficulty → coerce to medium, not crash."""
    payload = {
        "prompt": "q", "answer_key": "5", "explanation": "x",
        "difficulty": "extreme",  # invalid
    }
    with patch.object(problem_generator.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await problem_generator.generate_problem(
            subject="math", concept=None, concept_label=None, difficulty="hard",
        )
    assert out is not None
    assert out["difficulty"] == "medium"


@pytest.mark.asyncio
async def test_generate_problem_returns_none_on_llm_error():
    """LLM raises → caught, logged, returns None. Never propagates."""
    with patch.object(problem_generator.client.chat.completions, "create",
                      side_effect=RuntimeError("Azure exploded")):
        out = await problem_generator.generate_problem(
            subject="math", concept=None, concept_label=None, difficulty="medium",
        )
    assert out is None


@pytest.mark.asyncio
async def test_generate_problem_returns_none_on_timeout():
    """asyncio.wait_for raising TimeoutError must yield None, not bubble up."""
    async def _slow():
        await asyncio.sleep(60)
    # The to_thread bridge means we need to patch the underlying call.
    with patch.object(problem_generator.client.chat.completions, "create",
                      side_effect=asyncio.TimeoutError):
        out = await problem_generator.generate_problem(
            subject="math", concept=None, concept_label=None, difficulty="easy",
        )
    assert out is None


def test_difficulty_or_default_passes_through_valid():
    for d in ("easy", "medium", "hard"):
        assert problem_generator._difficulty_or_default(d) == d


def test_difficulty_or_default_falls_back_for_invalid():
    assert problem_generator._difficulty_or_default(None) == "medium"
    assert problem_generator._difficulty_or_default("impossible") == "medium"
    assert problem_generator._difficulty_or_default("") == "medium"


def test_build_user_message_includes_concept_when_present():
    msg = problem_generator._build_user_message(
        subject="math", concept_label="Mean vs. Median",
        difficulty="hard", grade_level="Grade 8",
    )
    assert "Mean vs. Median" in msg
    assert "Grade 8" in msg
    assert "hard" in msg


def test_build_user_message_omits_concept_clause_on_cold_start():
    msg = problem_generator._build_user_message(
        subject="math", concept_label=None,
        difficulty="medium", grade_level="",
    )
    assert "Target sub-concept" not in msg
