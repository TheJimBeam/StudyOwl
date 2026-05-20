"""
Session manager — orchestrates the hint state machine.

Controls hint level progression, attempt counting, and escalation logic.
All student interactions flow through process_attempt().
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.session import Session
from models.attempt import Attempt
from models.alert import (
    Alert,
    REASON_DISTRESS,
    REASON_REPEATED_FAILURE,
    SEVERITY_FOR_REASON,
)
from . import hint_engine
from . import answer_verifier
from . import alert_service
from . import memory_agent
from . import subject_router
from . import travily_service


async def start_session(
    db: AsyncSession,
    student_id: UUID,
    question: str,
    photo_url: str | None = None,
) -> tuple[Session, str]:
    """
    Create a new session, classify the subject, and return the first hint.

    Returns:
        Tuple of (Session ORM object, first hint string).
    """
    subject = await subject_router.classify(question)

    now = datetime.now(timezone.utc)
    session = Session(
        student_id=student_id,
        question=question,
        subject=subject,
        hint_level=1,
        fails_at_level=0,
        photo_url=photo_url,
        started_at=now,
        last_activity_at=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    prior_concepts = await memory_agent.get_review_concepts(
        db, student_id, subject=subject,
    )

    first_hint = await hint_engine.get_hint(
        question=question,
        subject=subject,
        level=1,
        previous_attempts=[],
        prior_concepts=prior_concepts,
    )

    return session, first_hint


async def process_attempt(
    db: AsyncSession,
    session: Session,
    attempt_text: str,
) -> dict:
    """
    Process a student's answer attempt against the current session state.

    State machine:
      - Correct → log success, close session.
      - Wrong   → increment fail counter, optionally advance hint level, fetch next hint.
      - 3 fails at same level → trigger teacher alert.
      - Distress detected → trigger teacher alert immediately.

    Returns:
        Dict with keys: status ('correct'|'wrong'), hint (if wrong),
        hint_level (if wrong), message.
    """
    # Bump activity timestamp — used by the inactivity scheduler to decide
    # whether a session is stuck. Updated whether the attempt is right or wrong.
    session.last_activity_at = datetime.now(timezone.utc)

    # Distress check (fires alert regardless of attempt correctness)
    if await hint_engine.detect_distress(attempt_text):
        await _trigger_alert(
            db, session,
            reason_kind=REASON_DISTRESS,
            reason_text=f"Student expressed distress: {attempt_text[:200]}",
        )

    # Fetch the conversation history for this session in chronological order.
    # Split by `kind` so clarifications (questions / clarify-responses) don't
    # leak into the "previous wrong attempts" list and confuse the hint AI.
    result = await db.execute(
        select(Attempt.attempt_text, Attempt.hint_shown, Attempt.kind)
        .where(Attempt.session_id == session.id)
        .order_by(Attempt.created_at)
    )
    rows = result.all()
    previous_attempts = [r[0] for r in rows if r[2] == "answer"]
    previous_hints = [r[1] for r in rows if r[2] == "answer" and r[1] is not None]
    previous_clarifications = [
        (r[0], r[1])
        for r in rows
        if r[2] == "clarification" and r[1] is not None
    ]

    # Verify the attempt
    is_correct = await answer_verifier.check(
        question=session.question,
        answer=attempt_text,
        subject=session.subject,
    )

    # Log the attempt
    attempt = Attempt(
        session_id=session.id,
        attempt_text=attempt_text,
        is_correct=is_correct,
        hint_level=session.hint_level,
    )
    db.add(attempt)

    if is_correct:
        session.resolved = True
        session.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        await memory_agent.consolidate_session(db, session)
        return {"status": "correct", "message": "Brilliant work! You got it!"}

    # Wrong answer — if the student has already seen all three hints, reveal the direct answer.
    total_wrong_attempts = len(previous_attempts) + 1
    if session.hint_level == 3 and total_wrong_attempts >= settings.max_fails_before_review:
        direct_answer = await hint_engine.get_direct_answer(session.question, session.subject)
        learning_resources = await travily_service.fetch_learning_resources(session.question, limit=5)
        session.resolved = True
        session.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        await memory_agent.consolidate_session(db, session)
        return {
            "status": "wrong",
            "hint": None,
            "hint_level": session.hint_level,
            "message": "You have reached the final hint, so here are some resources to review along with the correct answer.",
            "review_mode": True,
            "review_url": None,
            "learning_resources": learning_resources,
            "final_answer": direct_answer,
        }

    review_mode = total_wrong_attempts >= settings.max_fails_before_review
    review_url = None

    if review_mode:
        session.hint_level = 3
        hint = None
        learning_resources = await travily_service.fetch_learning_resources(session.question, limit=5)
        if learning_resources:
            hint = (
                "You've used up your 3 attempts and need to review a related concept before trying again. "
                "Check out the resources below to learn more, then try a new question."
            )
        else:
            hint = (
                "You've used up your 3 attempts. Review a related worked example or article about solving linear equations, "
                "then come back and try again."
            )
    else:
        if session.hint_level < 3:
            session.hint_level += 1
            session.fails_at_level = 0
        else:
            session.fails_at_level += 1
        prior_concepts = await memory_agent.get_review_concepts(
            db, session.student_id, subject=session.subject,
        )
        hint = await hint_engine.get_hint(
            question=session.question,
            subject=session.subject,
            level=session.hint_level,
            previous_attempts=previous_attempts + [attempt_text],
            previous_hints=previous_hints,
            previous_clarifications=previous_clarifications,
            prior_concepts=prior_concepts,
        )
        learning_resources = []

    if session.fails_at_level >= settings.max_fails_before_alert:
        await _trigger_alert(
            db, session,
            reason_kind=REASON_REPEATED_FAILURE,
            reason_text=(
                f"Stuck at hint level {session.hint_level} after "
                f"{session.fails_at_level} attempts"
            ),
        )

    attempt.hint_shown = hint
    await db.commit()

    return {
        "status": "wrong",
        "hint": hint,
        "hint_level": session.hint_level,
        "message": None,
        "review_mode": review_mode,
        "review_url": review_url,
        "learning_resources": learning_resources,
    }


async def start_session_stream(
    db: AsyncSession,
    student_id: UUID,
    question: str,
    photo_url: str | None = None,
) -> AsyncIterator[dict]:
    """
    Streaming variant of start_session. Yields NDJSON-shaped events:
      {"type": "session_created", "session_id": ..., "subject": ..., "hint_level": 1}
      {"type": "chunk", "text": "..."}
      ... (more chunks)
      {"type": "done"}
      OR
      {"type": "error", "message": "..."}

    Subject classification happens *before* streaming begins so the
    `session_created` event always lands first.
    """
    subject = await subject_router.classify(question)

    now = datetime.now(timezone.utc)
    session = Session(
        student_id=student_id,
        question=question,
        subject=subject,
        hint_level=1,
        fails_at_level=0,
        photo_url=photo_url,
        started_at=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    yield {
        "type": "session_created",
        "session_id": str(session.id),
        "subject": subject,
        "hint_level": session.hint_level,
    }

    prior_concepts = await memory_agent.get_review_concepts(
        db, student_id, subject=subject,
    )

    assembled: list[str] = []
    try:
        async for chunk in hint_engine.stream_hint(
            question=question,
            subject=subject,
            level=1,
            previous_attempts=[],
            prior_concepts=prior_concepts,
        ):
            assembled.append(chunk)
            yield {"type": "chunk", "text": chunk}
        yield {"type": "done"}
    except Exception as exc:
        # The session row already exists; we just couldn't stream the first hint.
        yield {"type": "error", "message": str(exc)}
    finally:
        # Save whatever assembled before the stream ended (success or failure).
        # We record this on a synthetic first Attempt-like row only if there
        # was actually content; otherwise leave the session bare.
        if assembled:
            attempt = Attempt(
                session_id=session.id,
                attempt_text="",  # no student input yet for the first hint
                is_correct=False,
                hint_shown="".join(assembled),
                hint_level=1,
                kind="answer",
            )
            db.add(attempt)
            await db.commit()


async def process_attempt_stream(
    db: AsyncSession,
    session: Session,
    attempt_text: str,
) -> AsyncIterator[dict]:
    """
    Streaming variant of process_attempt. Same state-machine semantics as the
    non-streaming version, but the hint text comes through as `chunk` events.

    Event shapes:
      {"type": "verdict", "status": "correct" | "wrong", "hint_level": int,
       "review_mode": bool, "final_answer": str | None,
       "learning_resources": [...]} — emitted FIRST so the UI can configure
       its layout before chunks arrive.
      {"type": "chunk", "text": "..."} — only when we'll stream a hint.
      {"type": "done"} — terminal success.
      {"type": "error", "message": "..."} — terminal failure.

    `verdict` is a single event so the client knows the high-level shape
    (correct vs wrong + review-mode + level) before the hint text starts
    arriving. For correct/review/final-answer paths there are no `chunk`
    events; the verdict has everything.
    """
    # Bump activity (cheap; mirrors process_attempt semantics).
    # NOTE: Session model doesn't yet have last_activity_at (PR 1 not landed
    # in this batch); a future PR re-introduces it.

    # Distress (alerts fire pre-stream so the teacher gets pinged even on
    # transient stream failure).
    if await hint_engine.detect_distress(attempt_text):
        await _trigger_alert(
            db, session,
            reason_kind=REASON_DISTRESS,
            reason_text=f"Student expressed distress: {attempt_text[:200]}",
        )

    # Snapshot history (split by kind, same as the non-streaming path).
    history_result = await db.execute(
        select(Attempt.attempt_text, Attempt.hint_shown, Attempt.kind)
        .where(Attempt.session_id == session.id)
        .order_by(Attempt.created_at)
    )
    rows = history_result.all()
    previous_attempts = [r[0] for r in rows if r[2] == "answer"]
    previous_hints = [r[1] for r in rows if r[2] == "answer" and r[1] is not None]
    previous_clarifications = [
        (r[0], r[1])
        for r in rows
        if r[2] == "clarification" and r[1] is not None
    ]

    is_correct = await answer_verifier.check(
        question=session.question,
        answer=attempt_text,
        subject=session.subject,
    )

    attempt = Attempt(
        session_id=session.id,
        attempt_text=attempt_text,
        is_correct=is_correct,
        hint_level=session.hint_level,
        kind="answer",
    )
    db.add(attempt)

    if is_correct:
        session.resolved = True
        session.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        await memory_agent.consolidate_session(db, session)
        yield {
            "type": "verdict",
            "status": "correct",
            "hint_level": session.hint_level,
            "message": "Brilliant work! You got it!",
            "review_mode": False,
            "final_answer": None,
            "learning_resources": [],
        }
        yield {"type": "done"}
        return

    total_wrong_attempts = len(previous_attempts) + 1

    # Path A: full review with direct answer.
    if session.hint_level == 3 and total_wrong_attempts >= settings.max_fails_before_review:
        direct_answer = await hint_engine.get_direct_answer(session.question, session.subject)
        learning_resources = await travily_service.fetch_learning_resources(session.question, limit=5)
        session.resolved = True
        session.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        await memory_agent.consolidate_session(db, session)
        yield {
            "type": "verdict",
            "status": "wrong",
            "hint_level": session.hint_level,
            "message": "You have reached the final hint, so here are some resources to review along with the correct answer.",
            "review_mode": True,
            "final_answer": direct_answer,
            "learning_resources": [
                lr if isinstance(lr, dict) else lr.__dict__ for lr in learning_resources
            ],
        }
        yield {"type": "done"}
        return

    # Path B: review-mode (resources only, no streaming hint).
    review_mode = total_wrong_attempts >= settings.max_fails_before_review
    if review_mode:
        session.hint_level = 3
        learning_resources = await travily_service.fetch_learning_resources(session.question, limit=5)
        hint_static = (
            "You've used up your 3 attempts and need to review a related concept before trying again."
            if learning_resources else
            "You've used up your 3 attempts. Review a related worked example, then come back and try again."
        )
        attempt.hint_shown = hint_static
        await db.commit()
        yield {
            "type": "verdict",
            "status": "wrong",
            "hint_level": session.hint_level,
            "message": None,
            "review_mode": True,
            "final_answer": None,
            "learning_resources": [
                lr if isinstance(lr, dict) else lr.__dict__ for lr in learning_resources
            ],
            "static_hint": hint_static,
        }
        # Check alert threshold (same as non-streaming path).
        if session.fails_at_level >= settings.max_fails_before_alert:
            await _trigger_alert(
                db, session,
                reason_kind=REASON_REPEATED_FAILURE,
                reason_text=(
                    f"Stuck at hint level {session.hint_level} after "
                    f"{session.fails_at_level} attempts"
                ),
            )
        yield {"type": "done"}
        return

    # Path C: stream the next hint.
    if session.hint_level < 3:
        session.hint_level += 1
        session.fails_at_level = 0
    else:
        session.fails_at_level += 1

    yield {
        "type": "verdict",
        "status": "wrong",
        "hint_level": session.hint_level,
        "message": None,
        "review_mode": False,
        "final_answer": None,
        "learning_resources": [],
    }

    prior_concepts = await memory_agent.get_review_concepts(
        db, session.student_id, subject=session.subject,
    )

    assembled: list[str] = []
    try:
        async for chunk in hint_engine.stream_hint(
            question=session.question,
            subject=session.subject,
            level=session.hint_level,
            previous_attempts=previous_attempts + [attempt_text],
            previous_hints=previous_hints,
            previous_clarifications=previous_clarifications,
            prior_concepts=prior_concepts,
        ):
            assembled.append(chunk)
            yield {"type": "chunk", "text": chunk}
        yield {"type": "done"}
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
    finally:
        # Save whatever we streamed — even if it errored mid-way — so the
        # conversation memory in PR 6 picks it up next time.
        attempt.hint_shown = "".join(assembled) if assembled else None
        await db.commit()

        # Alert check happens here so it fires whether we streamed all of the
        # hint or stopped early.
        if session.fails_at_level >= settings.max_fails_before_alert:
            await _trigger_alert(
                db, session,
                reason_kind=REASON_REPEATED_FAILURE,
                reason_text=(
                    f"Stuck at hint level {session.hint_level} after "
                    f"{session.fails_at_level} attempts"
                ),
            )


async def process_clarification(
    db: AsyncSession,
    session: Session,
    message: str,
) -> dict:
    """
    Handle a student's clarifying question about the current hint. Does NOT
    advance the hint level, NOT increment fails_at_level, NOT count as an
    attempt. Distress detection still runs — "I give up" via the clarify
    path should still alert a teacher.

    Returns {"clarification": str, "remaining": int} where `remaining` is the
    number of clarifications still allowed at the current hint level.
    """
    if not message or not message.strip():
        raise ValueError("Clarification message is empty")

    if session.resolved:
        raise ValueError("Session is already resolved")

    # Distress check first — fires alert regardless of input kind.
    if await hint_engine.detect_distress(message):
        await _trigger_alert(
            db, session,
            reason_kind=REASON_DISTRESS,
            reason_text=f"Student expressed distress in clarification: {message[:200]}",
        )

    # Count existing clarifications at the current hint level.
    existing_result = await db.execute(
        select(Attempt)
        .where(
            Attempt.session_id == session.id,
            Attempt.kind == "clarification",
            Attempt.hint_level == session.hint_level,
        )
    )
    existing_count = len(existing_result.scalars().all())
    if existing_count >= settings.clarifications_per_level_limit:
        raise PermissionError(
            f"Clarification cap reached for hint level {session.hint_level}"
        )

    # Fetch history (answer rows for hints+attempts, clarification rows for ctx).
    history_result = await db.execute(
        select(Attempt.attempt_text, Attempt.hint_shown, Attempt.kind)
        .where(Attempt.session_id == session.id)
        .order_by(Attempt.created_at)
    )
    rows = history_result.all()
    previous_hints = [r[1] for r in rows if r[2] == "answer" and r[1] is not None]
    previous_attempts = [r[0] for r in rows if r[2] == "answer"]
    previous_clarifications = [
        (r[0], r[1])
        for r in rows
        if r[2] == "clarification" and r[1] is not None
    ]

    response_text = await hint_engine.clarify(
        question=session.question,
        subject=session.subject,
        level=session.hint_level,
        student_message=message,
        previous_hints=previous_hints,
        previous_attempts=previous_attempts,
        previous_clarifications=previous_clarifications,
    )

    # Record the clarification round-trip in the attempts table.
    record = Attempt(
        session_id=session.id,
        attempt_text=message,
        is_correct=False,  # not meaningful for clarifications; flag via kind
        hint_shown=response_text,
        hint_level=session.hint_level,
        kind="clarification",
    )
    db.add(record)
    await db.commit()

    remaining = max(
        0, settings.clarifications_per_level_limit - (existing_count + 1)
    )
    return {"clarification": response_text, "remaining": remaining}


async def _trigger_alert(
    db: AsyncSession,
    session: Session,
    reason_kind: str,
    reason_text: str,
) -> None:
    """
    Insert an Alert row (dedup'd per (session, reason_kind)) and dispatch the
    notification. Multiple alerts of *different* kinds may exist for a single
    session — that's the point of the new table.
    """
    # Same-kind dedup: don't re-alert if an unresolved alert of this kind is
    # already on this session. Different kinds (distress + repeated_failure)
    # can both exist and that's intentional.
    existing = await db.execute(
        select(Alert).where(
            Alert.session_id == session.id,
            Alert.reason_kind == reason_kind,
            Alert.resolved_at.is_(None),
        )
    )
    if existing.scalars().first() is not None:
        return

    severity = SEVERITY_FOR_REASON.get(reason_kind, "medium")
    alert = Alert(
        session_id=session.id,
        student_id=session.student_id,
        severity=severity,
        reason_kind=reason_kind,
        reason_text=reason_text,
    )
    db.add(alert)

    # Keep the legacy boolean populated for one PR cycle so old clients /
    # queries that read it still see "this session had an alert." A follow-up
    # PR drops the column.
    session.teacher_alerted = True

    await db.commit()
    await db.refresh(alert)

    # Notify outside the row-creation txn so SendGrid latency doesn't hold
    # locks. `session.student` is eagerly loaded by the router's joinedload.
    await alert_service.notify_teacher(db, alert, session)
