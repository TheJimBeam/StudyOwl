"""
Tests for the dual-agent hint pipeline.

Mocks hint_engine.get_hint and services.critic.judge_hint to exercise the
approve / reject / regen / persist branches.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config import settings
from services import hint_pipeline


def _make_db() -> MagicMock:
    """AsyncSession-shaped mock: add() is sync, commit() is async."""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_get_critiqued_hint_approves_first_draft():
    db = _make_db()
    sid, stid = uuid4(), uuid4()

    with patch.object(hint_pipeline.hint_engine, "get_hint",
                      new=AsyncMock(return_value="Good hint.")) as mock_hint, \
         patch.object(hint_pipeline.critic, "judge_hint",
                      new=AsyncMock(return_value={"verdict": "approve",
                                                  "severity": "low",
                                                  "reasons": []})) as mock_judge:
        out = await hint_pipeline.get_critiqued_hint(
            db, session_id=sid, student_id=stid,
            question="q", subject="math", level=1,
            previous_attempts=[], last_wrong_attempt=None,
        )

    assert out == "Good hint."
    assert mock_hint.call_count == 1  # no regen
    assert mock_judge.call_count == 1
    # CriticDecision row staged.
    db.add.assert_called_once()
    row = db.add.call_args.args[0]
    assert row.verdict == "approve"
    assert row.regenerated_hint is None
    assert row.original_hint == "Good hint."


@pytest.mark.asyncio
async def test_get_critiqued_hint_regenerates_on_reject_then_approves():
    db = _make_db()
    verdicts = [
        {"verdict": "reject", "severity": "high",
         "reasons": ["reveals final answer"]},
        {"verdict": "approve", "severity": "low", "reasons": []},
    ]
    drafts = ["x is 15.", "What number plus 4 gives 19?"]

    with patch.object(hint_pipeline.hint_engine, "get_hint",
                      new=AsyncMock(side_effect=drafts)) as mock_hint, \
         patch.object(hint_pipeline.critic, "judge_hint",
                      new=AsyncMock(side_effect=verdicts)) as mock_judge:
        out = await hint_pipeline.get_critiqued_hint(
            db, session_id=uuid4(), student_id=uuid4(),
            question="Solve x + 4 = 19", subject="math", level=1,
            previous_attempts=["20"], last_wrong_attempt="20",
        )

    assert out == "What number plus 4 gives 19?"
    assert mock_hint.call_count == 2
    # Critic ran on both drafts.
    assert mock_judge.call_count == 2
    # Second hint_engine call must include the critic feedback.
    second_kwargs = mock_hint.call_args_list[1].kwargs
    assert second_kwargs["critic_feedback"]["prior_draft"] == "x is 15."
    assert "reveals final answer" in second_kwargs["critic_feedback"]["reasons"]
    # CriticDecision row reflects the reject + regenerated text.
    row = db.add.call_args.args[0]
    assert row.verdict == "reject"
    assert row.original_hint == "x is 15."
    assert row.regenerated_hint == "What number plus 4 gives 19?"


@pytest.mark.asyncio
async def test_get_critiqued_hint_delivers_after_max_retries_even_if_still_rejected():
    """If retries are exhausted and the regen is still rejected, ship anyway —
    blocking the student forever is worse than a borderline hint."""
    db = _make_db()
    verdicts = [
        {"verdict": "reject", "severity": "high", "reasons": ["spoiler"]},
        {"verdict": "reject", "severity": "high", "reasons": ["still spoiler"]},
    ]
    drafts = ["bad-1", "bad-2"]

    with patch.object(hint_pipeline.hint_engine, "get_hint",
                      new=AsyncMock(side_effect=drafts)), \
         patch.object(hint_pipeline.critic, "judge_hint",
                      new=AsyncMock(side_effect=verdicts)), \
         patch.object(settings, "critic_max_retries", 1):
        out = await hint_pipeline.get_critiqued_hint(
            db, session_id=uuid4(), student_id=uuid4(),
            question="q", subject="math", level=1,
            previous_attempts=[], last_wrong_attempt=None,
        )

    assert out == "bad-2"  # delivered the final regen anyway
    row = db.add.call_args.args[0]
    assert row.verdict == "reject"
    assert row.original_hint == "bad-1"
    assert row.regenerated_hint == "bad-2"


@pytest.mark.asyncio
async def test_get_critiqued_hint_skips_critic_when_disabled():
    db = _make_db()
    with patch.object(hint_pipeline.hint_engine, "get_hint",
                      new=AsyncMock(return_value="raw hint")), \
         patch.object(hint_pipeline.critic, "judge_hint",
                      new=AsyncMock()) as mock_judge, \
         patch.object(settings, "critic_enabled", False):
        out = await hint_pipeline.get_critiqued_hint(
            db, session_id=uuid4(), student_id=uuid4(),
            question="q", subject="math", level=1, previous_attempts=[],
        )

    assert out == "raw hint"
    mock_judge.assert_not_called()
    db.add.assert_not_called()  # no CriticDecision persisted


@pytest.mark.asyncio
async def test_get_critiqued_hint_swallows_critic_wrapper_error_open():
    """Even if critic.judge_hint somehow raises (it shouldn't), the pipeline
    must not propagate — it's wrapped in _judge_safely."""
    db = _make_db()
    with patch.object(hint_pipeline.hint_engine, "get_hint",
                      new=AsyncMock(return_value="hint")), \
         patch.object(hint_pipeline.critic, "judge_hint",
                      new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(settings, "critic_fail_open", True):
        out = await hint_pipeline.get_critiqued_hint(
            db, session_id=uuid4(), student_id=uuid4(),
            question="q", subject="math", level=1, previous_attempts=[],
        )
    assert out == "hint"


@pytest.mark.asyncio
async def test_stream_critiqued_hint_yields_full_text():
    """The streaming variant must yield the concatenation of all chunks
    equal to the approved hint text."""
    db = _make_db()
    hint_text = "What number plus 4 gives 19? Take a moment to think."

    with patch.object(hint_pipeline.hint_engine, "get_hint",
                      new=AsyncMock(return_value=hint_text)), \
         patch.object(hint_pipeline.critic, "judge_hint",
                      new=AsyncMock(return_value={"verdict": "approve",
                                                  "severity": "low",
                                                  "reasons": []})), \
         patch.object(hint_pipeline, "_STREAM_CHUNK_DELAY_SECONDS", 0):
        chunks = [c async for c in hint_pipeline.stream_critiqued_hint(
            db, session_id=uuid4(), student_id=uuid4(),
            question="q", subject="math", level=1, previous_attempts=[],
        )]

    assert "".join(chunks) == hint_text
    assert len(chunks) > 1  # actually chunked, not one-shot
