"""
Tests for the Co-Pilot narrator — LLM wrapper, fail-closed semantics.

Mocks the AzureOpenAI client at the module boundary. We pin the two contract
guarantees that the orchestrator relies on:
  - Never raises; status='failed' on any error path.
  - Empty pattern list → narrate_summary returns ('', 'skipped') with NO LLM call.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from services import copilot_narrator
from services.copilot_aggregator import PatternCandidate, WeeklyAggregate


def _aggregate(patterns: list[PatternCandidate]) -> WeeklyAggregate:
    return WeeklyAggregate(
        week_start=datetime(2026, 5, 11, tzinfo=timezone.utc),
        week_end=datetime(2026, 5, 18, tzinfo=timezone.utc),
        student_count=28,
        session_count=140,
        resolved_count=98,
        patterns=patterns,
    )


def _pattern(subject="math", concept="quadratic-factoring") -> PatternCandidate:
    return PatternCandidate(
        signal_kind="level3_stuck",
        subject=subject,
        concept=concept,
        concept_label="Quadratic Factoring",
        affected_count=14,
        cohort_count=28,
        affected_ratio=0.5,
    )


def _llm_response(payload: dict | str) -> MagicMock:
    """Shape the OpenAI client response the same way the service consumes it."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    msg = MagicMock()
    msg.content = body
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ── narrate_summary ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_narrate_summary_skipped_when_no_patterns():
    """No patterns → no LLM call, status='skipped'."""
    with patch.object(copilot_narrator.client.chat.completions, "create") as mock_create:
        text, status = await copilot_narrator.narrate_summary(_aggregate([]))
    assert text == ""
    assert status == "skipped"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_narrate_summary_happy_path_returns_ok():
    payload = {"narrative": "Last week, students struggled most with quadratic factoring."}
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        return_value=_llm_response(payload),
    ):
        text, status = await copilot_narrator.narrate_summary(_aggregate([_pattern()]))
    assert status == "ok"
    assert "quadratic factoring" in text


@pytest.mark.asyncio
async def test_narrate_summary_malformed_json_fails_closed():
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        return_value=_llm_response("not-json-at-all"),
    ):
        text, status = await copilot_narrator.narrate_summary(_aggregate([_pattern()]))
    assert text == ""
    assert status == "failed"


@pytest.mark.asyncio
async def test_narrate_summary_missing_field_fails_closed():
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        return_value=_llm_response({"something_else": "x"}),
    ):
        text, status = await copilot_narrator.narrate_summary(_aggregate([_pattern()]))
    assert (text, status) == ("", "failed")


@pytest.mark.asyncio
async def test_narrate_summary_llm_raises_returns_failed():
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        side_effect=RuntimeError("Azure down"),
    ):
        text, status = await copilot_narrator.narrate_summary(_aggregate([_pattern()]))
    assert (text, status) == ("", "failed")


@pytest.mark.asyncio
async def test_narrate_summary_timeout_returns_failed():
    async def _slow(*_a, **_kw):
        await asyncio.sleep(10)

    # Make the to_thread wrapper itself hang so wait_for trips.
    with patch.object(copilot_narrator.asyncio, "to_thread", side_effect=_slow), \
         patch.object(copilot_narrator.settings, "copilot_narrator_timeout_seconds", 0):
        text, status = await copilot_narrator.narrate_summary(_aggregate([_pattern()]))
    assert (text, status) == ("", "failed")


# ── draft_mini_lesson ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_mini_lesson_happy_path():
    md_body = "# Possible 12-min mini-lesson: Quadratic factoring re-anchor\n\nDraft body."
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        return_value=_llm_response({"mini_lesson_md": md_body}),
    ):
        md, status, error = await copilot_narrator.draft_mini_lesson(_pattern())
    assert status == "ok"
    assert md.startswith("# Possible 12-min mini-lesson")
    assert error is None


@pytest.mark.asyncio
async def test_draft_mini_lesson_malformed_json_fails_closed():
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        return_value=_llm_response("not json"),
    ):
        md, status, error = await copilot_narrator.draft_mini_lesson(_pattern())
    assert md is None
    assert status == "failed"
    assert error  # diagnostic string present


@pytest.mark.asyncio
async def test_draft_mini_lesson_empty_string_fails_closed():
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        return_value=_llm_response({"mini_lesson_md": "   "}),
    ):
        md, status, error = await copilot_narrator.draft_mini_lesson(_pattern())
    assert (md, status) == (None, "failed")


@pytest.mark.asyncio
async def test_draft_mini_lesson_llm_exception_fails_closed():
    with patch.object(
        copilot_narrator.client.chat.completions,
        "create",
        side_effect=ValueError("bad request"),
    ):
        md, status, error = await copilot_narrator.draft_mini_lesson(_pattern())
    assert (md, status) == (None, "failed")
    assert "ValueError" in (error or "")


# ── prompt guardrails sanity ─────────────────────────────────────────────────


def test_system_prompts_contain_draft_framing():
    """The 'never instruct' rule is the make-or-break for teacher trust."""
    assert "DRAFT" in copilot_narrator.NARRATOR_SYSTEM
    assert "never instruct" in copilot_narrator.NARRATOR_SYSTEM.lower()
    assert "DRAFT" in copilot_narrator.MINI_LESSON_SYSTEM
    assert "never instruct" in copilot_narrator.MINI_LESSON_SYSTEM.lower()
    assert "you might" in copilot_narrator.MINI_LESSON_SYSTEM.lower()
