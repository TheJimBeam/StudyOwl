"""
Memory agent — per-student long-term knowledge-graph memory.

On session resolve, consolidate_session() extracts concepts via the LLM and
upserts them into the student_concept_memory table. Reads (get_student_memory,
get_review_concepts) compute decayed confidence at query time so we don't
need a background decay writer.

The architect note warns against jumping to a graph too early — keep this flat.
"""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.attempt import Attempt
from models.concept_memory import (
    ConceptMemory,
    STATUS_MASTERED,
    STATUS_PARTIAL,
    STATUS_STRUGGLING,
)
from models.session import Session

from . import concept_extractor


logger = logging.getLogger(__name__)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def decayed_confidence(
    confidence: float,
    last_seen: datetime,
    now: datetime | None = None,
) -> float:
    """
    Exponential half-life decay. After settings.memory_decay_half_life_days
    elapse, the returned value is half of `confidence`.
    """
    half_life = settings.memory_decay_half_life_days
    if half_life <= 0:
        return confidence
    now = now or datetime.now(timezone.utc)
    delta_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
    return confidence * (0.5 ** (delta_days / half_life))


def derive_status(decayed: float) -> str:
    """Bucket a (decayed) confidence value into a status label."""
    if decayed >= 0.75:
        return STATUS_MASTERED
    if decayed >= 0.4:
        return STATUS_PARTIAL
    return STATUS_STRUGGLING


def _shape_row(row: ConceptMemory, now: datetime) -> dict:
    decayed = decayed_confidence(row.confidence, row.last_seen, now=now)
    return {
        "concept": row.concept,
        "label": row.label,
        "subject": row.subject,
        "status": derive_status(decayed),
        "confidence": round(row.confidence, 4),
        "decayed_confidence": round(decayed, 4),
        "last_seen": row.last_seen.isoformat(),
        "attempts": row.attempts_count,
        "correct": row.correct_count,
    }


# ── Consolidation ─────────────────────────────────────────────────────────────


async def consolidate_session(db: AsyncSession, session: Session) -> int:
    """
    Extract concepts from a resolved session and upsert them into
    student_concept_memory. Returns the number of rows touched.

    Never raises — failures are logged and 0 is returned so the resolve path
    is never broken by extraction issues.
    """
    if not settings.memory_consolidation_enabled:
        return 0

    try:
        return await asyncio.wait_for(
            _consolidate_inner(db, session),
            timeout=settings.memory_consolidation_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Concept consolidation timed out for session %s after %ss",
            session.id, settings.memory_consolidation_timeout_seconds,
        )
        return 0
    except Exception as exc:  # never break the resolve path
        logger.exception("Concept consolidation failed for session %s: %s", session.id, exc)
        return 0


async def _consolidate_inner(db: AsyncSession, session: Session) -> int:
    # Pull answer attempts for this session in chronological order.
    result = await db.execute(
        select(Attempt.attempt_text, Attempt.is_correct, Attempt.hint_level)
        .where(Attempt.session_id == session.id, Attempt.kind == "answer")
        .order_by(Attempt.created_at)
    )
    attempts = [(r[0], bool(r[1]), int(r[2])) for r in result.all()]

    concepts = await concept_extractor.extract_concepts(
        question=session.question,
        subject=session.subject,
        attempts=attempts,
        resolved=bool(session.resolved),
    )
    if not concepts:
        return 0

    now = datetime.now(timezone.utc)
    session_attempts = len(attempts)
    session_correct = sum(1 for _, ok, _ in attempts if ok)

    touched = 0
    for item in concepts:
        slug = item["concept"]
        # Look up existing row for this (student, concept) pair.
        existing = await db.execute(
            select(ConceptMemory).where(
                ConceptMemory.student_id == session.student_id,
                ConceptMemory.concept == slug,
            )
        )
        row = existing.scalar_one_or_none()

        new_conf = item["confidence"]
        if row is None:
            blended = new_conf
        else:
            # Blend: weight the new signal more than the (decayed) prior
            # so confidence can climb back up with practice.
            prior_decayed = decayed_confidence(row.confidence, row.last_seen, now=now)
            blended = 0.6 * new_conf + 0.4 * prior_decayed

        blended = max(0.0, min(1.0, blended))
        status = derive_status(blended)

        if row is None:
            row = ConceptMemory(
                student_id=session.student_id,
                subject=session.subject,
                concept=slug,
                label=item["label"],
                status=status,
                confidence=blended,
                attempts_count=session_attempts,
                correct_count=session_correct,
                last_seen=session.resolved_at or now,
                last_session_id=session.id,
            )
            db.add(row)
        else:
            row.subject = session.subject
            row.label = item["label"] or row.label
            row.confidence = blended
            row.status = status
            row.attempts_count = row.attempts_count + session_attempts
            row.correct_count = row.correct_count + session_correct
            row.last_seen = session.resolved_at or now
            row.last_session_id = session.id
        touched += 1

    await db.commit()
    return touched


# ── Reads ─────────────────────────────────────────────────────────────────────


async def get_student_memory(
    db: AsyncSession,
    student_id: UUID,
    subject: str | None = None,
) -> list[dict]:
    """
    Return all concept memory rows for a student, with decayed_confidence
    and derived status computed at read time. Weakest concepts first.
    """
    stmt = select(ConceptMemory).where(ConceptMemory.student_id == student_id)
    if subject:
        stmt = stmt.where(ConceptMemory.subject == subject)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    now = datetime.now(timezone.utc)
    shaped = [_shape_row(r, now) for r in rows]
    shaped.sort(key=lambda r: r["decayed_confidence"])
    return shaped


async def get_review_concepts(
    db: AsyncSession,
    student_id: UUID,
    subject: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Return concepts whose decayed_confidence has dropped below the review
    threshold — the set the hint engine should consider probing. Capped at
    `limit` so prompts stay tight.
    """
    all_concepts = await get_student_memory(db, student_id, subject=subject)
    threshold = settings.memory_review_threshold
    weak = [c for c in all_concepts if c["decayed_confidence"] < threshold]
    return weak[:limit]
