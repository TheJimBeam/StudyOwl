"""
Conversation memory plumbing for the hint engine.

These tests target `_build_hint_user_message` directly because it's pure and
doesn't touch the Azure client. The end-to-end "the AI doesn't repeat itself"
property has to be verified manually — that's a model-behavior check, not a
deterministic test.
"""

from services.hint_engine import _build_hint_user_message
from config import settings


def test_user_message_includes_prior_hints_with_warning():
    msg = _build_hint_user_message(
        question="Solve x + 4 = 19",
        previous_hints=["What number plus 4 equals 19?", "Think about subtraction."],
        previous_attempts=["20", "16"],
    )
    assert "Solve x + 4 = 19" in msg
    # Both prior hints surface, in order, with a do-not-repeat directive.
    assert "What number plus 4 equals 19?" in msg
    assert "Think about subtraction." in msg
    assert "Do NOT repeat" in msg
    # Previous attempts also rendered.
    assert "- 20" in msg
    assert "- 16" in msg


def test_user_message_omits_hint_section_on_first_call():
    msg = _build_hint_user_message(
        question="What is 3 + 2?",
        previous_hints=[],
        previous_attempts=[],
    )
    # No "Do NOT repeat" preamble when there's nothing to avoid.
    assert "Do NOT repeat" not in msg
    # First-call attempts section says "None yet."
    assert "None yet." in msg


def test_user_message_caps_history_at_configured_limit():
    limit = settings.conversation_history_limit
    many_hints = [f"hint-{i}" for i in range(limit + 5)]
    many_attempts = [f"attempt-{i}" for i in range(limit + 5)]

    msg = _build_hint_user_message(
        question="q",
        previous_hints=many_hints,
        previous_attempts=many_attempts,
    )

    # The earliest entries should be dropped (we keep the last N).
    assert "hint-0" not in msg
    assert f"hint-{limit + 4}" in msg  # most recent retained
    assert "attempt-0" not in msg
    assert f"attempt-{limit + 4}" in msg


def test_user_message_includes_clarifications_when_present():
    msg = _build_hint_user_message(
        question="Solve x + 4 = 19",
        previous_hints=["Try subtraction."],
        previous_attempts=["20"],
        previous_clarifications=[
            ("what does subtract mean?", "Subtracting takes a number away from another."),
        ],
    )
    assert "what does subtract mean?" in msg
    assert "Subtracting takes a number away" in msg
    assert "Clarifying exchanges" in msg


def test_user_message_includes_knowledge_graph_when_prior_concepts_present():
    msg = _build_hint_user_message(
        question="Find the median of 1, 5, 9",
        previous_hints=[],
        previous_attempts=[],
        prior_concepts=[
            {
                "concept": "mean-vs-median",
                "label": "Mean vs. Median",
                "decayed_confidence": 0.32,
                "status": "struggling",
                "last_seen": "2025-04-01T00:00:00+00:00",
            }
        ],
    )
    assert "Knowledge graph" in msg
    assert "Mean vs. Median" in msg
    assert "struggling" in msg
    assert "0.32" in msg


def test_user_message_omits_knowledge_graph_section_when_no_prior_concepts():
    msg = _build_hint_user_message(
        question="q",
        previous_hints=[],
        previous_attempts=[],
        prior_concepts=[],
    )
    assert "Knowledge graph" not in msg
