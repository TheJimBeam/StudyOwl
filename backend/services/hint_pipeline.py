"""
Hint pipeline — dual-agent (generator + critic) wrapper around hint_engine.

This module is the only place the rest of the codebase should ask for a hint.
It orchestrates:
  1. Generate a hint via hint_engine.
  2. Judge it via services.critic.
  3. If rejected, regenerate ONCE with critic feedback baked into the prompt.
  4. Persist a CriticDecision row (approve OR reject) for the teacher view.
  5. Return / stream the approved hint.

Streaming model: buffer-then-stream. We generate the full hint internally
(non-streaming), critique it, optionally regenerate, then replay the approved
text to the client as `chunk` events. Trade-off: loses time-to-first-token.
Documented in PR description; switching to optimistic-forward-with-retract is
a follow-up.

The pipeline is a no-op when `settings.critic_enabled` is false: it just
delegates to hint_engine and persists nothing.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.critic_decision import CriticDecision

from . import critic
from . import hint_engine


logger = logging.getLogger(__name__)


# Synthetic-stream pacing so the client still sees gradual hint rendering even
# though we generated the full text before yielding. ~24 tokens/sec feels
# natural and matches the cadence the real Azure stream produces.
_STREAM_CHUNK_CHARS = 24
_STREAM_CHUNK_DELAY_SECONDS = 0.04


def _persist_decision(
    db: AsyncSession,
    *,
    session_id: UUID,
    student_id: UUID,
    hint_level: int,
    verdict: dict,
    original_hint: str,
    regenerated_hint: str | None,
) -> None:
    """
    Stage a CriticDecision row on the active session. The caller decides when
    to commit — typically alongside the rest of the attempt's writes so a
    failure here doesn't get out of sync.
    """
    if not settings.critic_enabled:
        return
    db.add(
        CriticDecision(
            session_id=session_id,
            student_id=student_id,
            hint_level=hint_level,
            verdict=verdict.get("verdict", "approve"),
            severity=verdict.get("severity", "low"),
            reasons="\n".join(verdict.get("reasons") or []),
            original_hint=original_hint,
            regenerated_hint=regenerated_hint,
        )
    )


async def _judge_safely(
    *,
    question: str,
    subject: str,
    hint_level: int,
    hint_text: str,
    last_wrong_attempt: str | None,
    previous_hints: list[str] | None,
) -> dict:
    """
    Critic call wrapped so timeout + LLM errors never propagate. `critic.judge_hint`
    already fails-open by default; the outer try is belt-and-braces.
    """
    try:
        return await critic.judge_hint(
            question=question,
            subject=subject,
            hint_level=hint_level,
            hint_text=hint_text,
            last_wrong_attempt=last_wrong_attempt,
            previous_hints=previous_hints,
        )
    except Exception as exc:  # never break hint delivery
        logger.exception("Critic wrapper raised unexpectedly: %s", exc)
        if settings.critic_fail_open:
            return {
                "verdict": "approve", "severity": "low", "reasons": [],
                "_fail_open_reason": f"wrapper_error: {type(exc).__name__}",
            }
        return {"verdict": "reject", "severity": "low",
                "reasons": ["critic wrapper error"]}


async def get_critiqued_hint(
    db: AsyncSession,
    *,
    session_id: UUID,
    student_id: UUID,
    question: str,
    subject: str,
    level: int,
    previous_attempts: list[str],
    previous_hints: list[str] | None = None,
    previous_clarifications: list[tuple[str, str]] | None = None,
    prior_concepts: list[dict] | None = None,
    last_wrong_attempt: str | None = None,
) -> str:
    """
    Generate → judge → (regenerate once on reject) → persist CriticDecision →
    return the hint to deliver.

    `db` is the active AsyncSession. This function only `db.add(...)` — the
    caller commits.
    """
    original = await hint_engine.get_hint(
        question=question,
        subject=subject,
        level=level,
        previous_attempts=previous_attempts,
        previous_hints=previous_hints,
        previous_clarifications=previous_clarifications,
        prior_concepts=prior_concepts,
    )

    if not settings.critic_enabled:
        return original

    verdict = await _judge_safely(
        question=question,
        subject=subject,
        hint_level=level,
        hint_text=original,
        last_wrong_attempt=last_wrong_attempt,
        previous_hints=previous_hints,
    )

    if verdict.get("verdict") == "approve":
        _persist_decision(
            db,
            session_id=session_id,
            student_id=student_id,
            hint_level=level,
            verdict=verdict,
            original_hint=original,
            regenerated_hint=None,
        )
        return original

    # Reject — regenerate up to `critic_max_retries` times with feedback.
    # In practice max_retries is 1; we deliver the last draft regardless after
    # that, since blocking the student forever is worse than a borderline hint.
    delivered = original
    for attempt_idx in range(max(1, settings.critic_max_retries)):
        feedback = {"prior_draft": delivered, "reasons": verdict.get("reasons", [])}
        regen = await hint_engine.get_hint(
            question=question,
            subject=subject,
            level=level,
            previous_attempts=previous_attempts,
            previous_hints=previous_hints,
            previous_clarifications=previous_clarifications,
            prior_concepts=prior_concepts,
            critic_feedback=feedback,
        )
        delivered = regen
        # Re-judge the regen — if it's still bad, log it but ship anyway after
        # exhausting retries.
        verdict = await _judge_safely(
            question=question,
            subject=subject,
            hint_level=level,
            hint_text=regen,
            last_wrong_attempt=last_wrong_attempt,
            previous_hints=previous_hints,
        )
        if verdict.get("verdict") == "approve":
            break

    _persist_decision(
        db,
        session_id=session_id,
        student_id=student_id,
        hint_level=level,
        verdict={"verdict": "reject", "severity": verdict.get("severity", "low"),
                 "reasons": verdict.get("reasons", [])},
        original_hint=original,
        regenerated_hint=delivered,
    )
    return delivered


async def _chunkify(text: str) -> AsyncIterator[str]:
    """Yield `text` in small chunks with a small inter-chunk delay so the
    client sees gradual rendering. Mirrors the cadence of real token streams."""
    for i in range(0, len(text), _STREAM_CHUNK_CHARS):
        yield text[i:i + _STREAM_CHUNK_CHARS]
        if _STREAM_CHUNK_DELAY_SECONDS > 0:
            await asyncio.sleep(_STREAM_CHUNK_DELAY_SECONDS)


async def stream_critiqued_hint(
    db: AsyncSession,
    *,
    session_id: UUID,
    student_id: UUID,
    question: str,
    subject: str,
    level: int,
    previous_attempts: list[str],
    previous_hints: list[str] | None = None,
    previous_clarifications: list[tuple[str, str]] | None = None,
    prior_concepts: list[dict] | None = None,
    last_wrong_attempt: str | None = None,
) -> AsyncIterator[str]:
    """
    Streaming variant. Generates the full hint internally, runs the critic,
    then yields the approved text in small chunks so the client sees gradual
    rendering. The buffer-then-stream design is documented in this module's
    docstring.
    """
    final = await get_critiqued_hint(
        db,
        session_id=session_id,
        student_id=student_id,
        question=question,
        subject=subject,
        level=level,
        previous_attempts=previous_attempts,
        previous_hints=previous_hints,
        previous_clarifications=previous_clarifications,
        prior_concepts=prior_concepts,
        last_wrong_attempt=last_wrong_attempt,
    )
    async for chunk in _chunkify(final):
        yield chunk
