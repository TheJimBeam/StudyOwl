"""
Practice agent — closed-loop orchestrator for generated practice problems.

Read flow:
  1. Pick a target concept from student_concept_memory (weakest first).
     Cold-start: no rows → fall back to a subject-only generic problem.
  2. Calibrate difficulty from decayed_confidence
     (`< 0.4 → easy`, `0.4–0.7 → medium`, `≥ 0.7 → hard`).
  3. Loop up to `practice_max_generation_retries`: generate → verify.
     Persist every attempt for telemetry. Break on verified=True.
  4. If all retries fail, persist the last try with status `unverified`
     so the student can still see something — clearly flagged.

Attempt flow:
  - check_student_answer (SymPy / Claude grader)
  - write attempted/correct onto the row
  - bump the matching memory_agent row so the next hint and the next
    generated problem reflect this practice immediately.
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.generated_problem import (
    DIFFICULTY_EASY,
    DIFFICULTY_HARD,
    DIFFICULTY_MEDIUM,
    GeneratedProblem,
    VERIFIER_KIND_NONE,
    VERIFIER_STATUS_REJECTED,
    VERIFIER_STATUS_UNVERIFIED,
    VERIFIER_STATUS_VERIFIED,
)
from models.student import Student
from . import memory_agent, problem_generator, problem_verifier


logger = logging.getLogger(__name__)


_VALID_SUBJECTS = {"math", "science", "english", "history", "other"}


def calibrate_difficulty(decayed_confidence: float | None) -> str:
    """ZPD bucket. None → medium (cold start, no signal yet)."""
    if decayed_confidence is None:
        return DIFFICULTY_MEDIUM
    if decayed_confidence < 0.4:
        return DIFFICULTY_EASY
    if decayed_confidence < 0.7:
        return DIFFICULTY_MEDIUM
    return DIFFICULTY_HARD


def _coerce_subject(subject: str | None, fallback: str = "math") -> str:
    if subject and subject in _VALID_SUBJECTS:
        return subject
    return fallback


async def _pick_target_concept(
    db: AsyncSession,
    student_id: UUID,
    subject: str | None,
) -> dict | None:
    """
    Return the weakest concept the student should practice next, or None
    when there's no memory to target. Uses `get_student_memory` so we see
    the full graph (not just the review-threshold subset) — even a
    "partial" concept is a fine practice target if there are no
    struggling ones.
    """
    rows = await memory_agent.get_student_memory(db, student_id, subject=subject)
    if not rows:
        return None
    # get_student_memory already sorts weakest-first.
    return rows[0]


async def generate_verified_problem(
    db: AsyncSession,
    student: Student,
    subject: str | None = None,
    concept: str | None = None,
) -> GeneratedProblem:
    """
    Generate a verified practice problem for the student and persist it.

    Behaviour:
        - `concept` provided → target that concept directly. Difficulty
          defaults to medium unless the concept is in memory (then we
          calibrate against its decayed_confidence).
        - `concept` omitted → pick the weakest concept from memory; if
          memory is empty, generate against the subject alone.
        - `subject` omitted → defaults to the picked concept's subject,
          or "math" on cold start.

    The returned row is always persisted, even on retry exhaustion (status
    `unverified`). The router decides what to expose to the student.
    """
    student_id = student.id
    target = await _pick_target_concept(db, student_id, subject)

    if concept:
        # Honour the explicit concept. If memory has a row for it, lift
        # the label + difficulty from there; otherwise treat as a fresh
        # concept at medium difficulty.
        target_label: str | None = None
        target_decayed: float | None = None
        target_subject = subject
        if target and target.get("concept") == concept:
            target_label = target.get("label")
            target_decayed = target.get("decayed_confidence")
            target_subject = target.get("subject") or subject
        else:
            # Search the full memory for an exact slug match.
            full = await memory_agent.get_student_memory(db, student_id)
            for c in full:
                if c["concept"] == concept:
                    target_label = c["label"]
                    target_decayed = c["decayed_confidence"]
                    target_subject = c["subject"]
                    break
        concept_slug = concept
        concept_label = target_label
        decayed = target_decayed
        chosen_subject = _coerce_subject(target_subject or subject)
    elif target:
        concept_slug = target["concept"]
        concept_label = target["label"]
        decayed = target["decayed_confidence"]
        chosen_subject = _coerce_subject(subject or target["subject"])
    else:
        # Cold start — no memory rows.
        concept_slug = None
        concept_label = None
        decayed = None
        chosen_subject = _coerce_subject(subject)

    difficulty = calibrate_difficulty(decayed)
    grade_level = getattr(student, "grade_level", "") or ""

    last_generated: dict | None = None
    last_verification: dict | None = None
    max_retries = max(1, settings.practice_max_generation_retries)

    for attempt_idx in range(1, max_retries + 1):
        generated = await problem_generator.generate_problem(
            subject=chosen_subject,
            concept=concept_slug,
            concept_label=concept_label,
            difficulty=difficulty,
            grade_level=grade_level,
        )
        if generated is None:
            # Generator failure — try again (no row persisted for this iter).
            continue
        last_generated = generated

        verification = await problem_verifier.verify_problem(
            prompt=generated["prompt"],
            answer_key=generated["answer_key"],
            subject=chosen_subject,
        )
        last_verification = verification

        if verification.get("verified"):
            return await _persist(
                db=db,
                student_id=student_id,
                subject=chosen_subject,
                concept_slug=concept_slug,
                concept_label=concept_label,
                difficulty=generated.get("difficulty") or difficulty,
                generated=generated,
                verification=verification,
                status=VERIFIER_STATUS_VERIFIED,
                attempts=attempt_idx,
            )

        # Persist the rejected attempt for telemetry. Subsequent iterations
        # will overwrite via additional inserts — one row per attempt is
        # intentional (no dedup) so we can measure verifier hit rate.
        await _persist(
            db=db,
            student_id=student_id,
            subject=chosen_subject,
            concept_slug=concept_slug,
            concept_label=concept_label,
            difficulty=generated.get("difficulty") or difficulty,
            generated=generated,
            verification=verification,
            status=VERIFIER_STATUS_REJECTED,
            attempts=attempt_idx,
        )

    # Retry budget exhausted. If we have a last generated problem, return
    # it tagged `unverified` so the UI can warn but the student isn't blocked.
    if last_generated is not None:
        return await _persist(
            db=db,
            student_id=student_id,
            subject=chosen_subject,
            concept_slug=concept_slug,
            concept_label=concept_label,
            difficulty=last_generated.get("difficulty") or difficulty,
            generated=last_generated,
            verification=last_verification or {
                "kind": VERIFIER_KIND_NONE,
                "notes": ["no verification ran"],
            },
            status=VERIFIER_STATUS_UNVERIFIED,
            attempts=max_retries,
        )

    # Generator never produced anything parseable. Persist a sentinel row
    # so the caller has something to surface as an error.
    return await _persist(
        db=db,
        student_id=student_id,
        subject=chosen_subject,
        concept_slug=concept_slug,
        concept_label=concept_label,
        difficulty=difficulty,
        generated={
            "prompt": "(no problem generated)",
            "answer_key": "",
            "explanation": "",
        },
        verification={
            "kind": VERIFIER_KIND_NONE,
            "notes": ["generator failed every retry"],
        },
        status=VERIFIER_STATUS_UNVERIFIED,
        attempts=max_retries,
    )


async def _persist(
    db: AsyncSession,
    student_id: UUID,
    subject: str,
    concept_slug: str | None,
    concept_label: str | None,
    difficulty: str,
    generated: dict,
    verification: dict,
    status: str,
    attempts: int,
) -> GeneratedProblem:
    notes = verification.get("notes") or []
    row = GeneratedProblem(
        student_id=student_id,
        subject=subject,
        concept=concept_slug,
        concept_label=concept_label,
        difficulty=difficulty,
        prompt_text=generated.get("prompt", ""),
        answer_key=generated.get("answer_key", ""),
        explanation=generated.get("explanation", ""),
        verifier_kind=verification.get("kind", VERIFIER_KIND_NONE),
        verifier_status=status,
        verifier_notes="\n".join(notes),
        generation_attempts=attempts,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def submit_attempt(
    db: AsyncSession,
    problem: GeneratedProblem,
    answer: str,
) -> dict:
    """
    Grade a student's attempt against the row's answer_key, persist the
    result, and bump the matching memory_agent concept. Idempotent: once
    `attempted` is True, returns the stored verdict without re-grading.
    """
    if problem.attempted:
        return {
            "correct": bool(problem.correct),
            "answer_key": problem.answer_key,
            "explanation": problem.explanation,
            "already_attempted": True,
        }

    correct = await problem_verifier.check_student_answer(
        prompt=problem.prompt_text,
        answer_key=problem.answer_key,
        student_answer=answer,
        subject=problem.subject,
    )

    from datetime import datetime, timezone
    problem.attempted = True
    problem.correct = bool(correct)
    problem.student_answer = (answer or "")[:1000]
    problem.attempted_at = datetime.now(timezone.utc)
    await db.commit()

    # Close the loop: nudge the matching memory row. Skipped when the
    # problem wasn't targeted at a slug (cold-start fallback).
    if problem.concept:
        try:
            await memory_agent.bump_concept_after_practice(
                db=db,
                student_id=problem.student_id,
                concept=problem.concept,
                subject=problem.subject,
                correct=bool(correct),
                label=problem.concept_label,
            )
        except Exception as exc:
            # Never let a memory hiccup invalidate a graded attempt.
            logger.exception("Failed to bump memory after practice: %s", exc)

    return {
        "correct": bool(correct),
        "answer_key": problem.answer_key,
        "explanation": problem.explanation,
        "already_attempted": False,
    }


async def get_problem_history(
    db: AsyncSession,
    student_id: UUID,
    limit: int = 20,
    offset: int = 0,
) -> list[GeneratedProblem]:
    """Return the student's verified-or-unverified problems, newest first."""
    stmt = (
        select(GeneratedProblem)
        .where(GeneratedProblem.student_id == student_id)
        .where(GeneratedProblem.verifier_status != VERIFIER_STATUS_REJECTED)
        .order_by(GeneratedProblem.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)
