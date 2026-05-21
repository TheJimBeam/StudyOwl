"""
Tests for the teacher-comment endpoints on sessions:
  PUT    /api/session/{session_id}/comment
  DELETE /api/session/{session_id}/comment

Mirrors the AsyncMock-based unit-test style used in test_alerts.py — we exercise
the route handlers directly with a mocked DB rather than spinning up a real one.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from routers.sessions import (
    COMMENT_MAX_LENGTH,
    TeacherCommentRequest,
    delete_session_comment,
    upsert_session_comment,
)


def _make_session(*, with_comment_by=None, comment_body="prior comment"):
    """Build a mock Session with optional pre-existing comment."""
    session = MagicMock()
    session.id = uuid4()
    if with_comment_by is None:
        session.teacher_comment_body = None
        session.teacher_comment_by_id = None
        session.teacher_comment_at = None
        session.comment_teacher = None
    else:
        session.teacher_comment_body = comment_body
        session.teacher_comment_by_id = with_comment_by.id
        session.teacher_comment_at = datetime.now(timezone.utc)
        session.comment_teacher = with_comment_by
    return session


def _make_teacher(name="Mr. Smith"):
    t = MagicMock()
    t.id = uuid4()
    t.name = name
    t.role = "teacher"
    return t


def _make_student():
    s = MagicMock()
    s.id = uuid4()
    s.name = "Marie Curie"
    s.role = "student"
    return s


def _make_db_returning(sessions: list):
    """
    Build an AsyncMock that returns `sessions[i]` on the i-th `execute()` call.
    The route calls execute once on initial load and again on the post-commit
    reload, so each upsert path needs two return values.
    """
    db = AsyncMock()
    results = []
    for s in sessions:
        result = MagicMock()
        unique = MagicMock()
        scalars = MagicMock()
        scalars.first.return_value = s
        unique.scalars.return_value = scalars
        result.unique.return_value = unique
        results.append(result)
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    return db


# ── Schema-level guards ──────────────────────────────────────────────────────


def test_request_model_rejects_empty_body():
    with pytest.raises(ValueError):
        TeacherCommentRequest(body="")


def test_request_model_rejects_overlong_body():
    with pytest.raises(ValueError):
        TeacherCommentRequest(body="x" * (COMMENT_MAX_LENGTH + 1))


def test_request_model_accepts_max_length():
    req = TeacherCommentRequest(body="x" * COMMENT_MAX_LENGTH)
    assert len(req.body) == COMMENT_MAX_LENGTH


# ── PUT — upsert ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_creates_first_comment():
    """No existing comment → write succeeds, response carries the author."""
    teacher = _make_teacher()
    blank_session = _make_session()
    # After commit the route reloads with the author joined. The mutated copy
    # is what the reload should return.
    blank_session.teacher_comment_body = "Great work"
    blank_session.teacher_comment_by_id = teacher.id
    blank_session.teacher_comment_at = datetime.now(timezone.utc)
    blank_session.comment_teacher = teacher

    initial = _make_session()  # state before write
    db = _make_db_returning([initial, blank_session])

    resp = await upsert_session_comment(
        session_id=str(initial.id),
        payload=TeacherCommentRequest(body="Great work"),
        teacher=teacher,
        db=db,
    )

    assert resp.body == "Great work"
    assert resp.teacher_id == str(teacher.id)
    assert resp.teacher_name == teacher.name
    db.commit.assert_awaited_once()
    # Mutation applied on the loaded session.
    assert initial.teacher_comment_body == "Great work"
    assert initial.teacher_comment_by_id == teacher.id
    assert initial.teacher_comment_at is not None


@pytest.mark.asyncio
async def test_upsert_owner_can_edit_own_comment():
    """Same teacher PUTting again replaces body + bumps timestamp."""
    teacher = _make_teacher()
    existing = _make_session(with_comment_by=teacher, comment_body="old")
    old_at = existing.teacher_comment_at

    # Post-commit reload returns the same session, mutated.
    db = _make_db_returning([existing, existing])

    resp = await upsert_session_comment(
        session_id=str(existing.id),
        payload=TeacherCommentRequest(body="new body"),
        teacher=teacher,
        db=db,
    )

    assert resp.body == "new body"
    assert existing.teacher_comment_body == "new body"
    assert existing.teacher_comment_at > old_at
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_other_teacher_gets_409():
    """A different teacher trying to overwrite hits 409 Conflict."""
    owner = _make_teacher(name="Mr. Smith")
    intruder = _make_teacher(name="Ms. Jones")
    existing = _make_session(with_comment_by=owner, comment_body="hands off")
    db = _make_db_returning([existing])

    with pytest.raises(HTTPException) as exc:
        await upsert_session_comment(
            session_id=str(existing.id),
            payload=TeacherCommentRequest(body="my take"),
            teacher=intruder,
            db=db,
        )

    assert exc.value.status_code == 409
    assert "Mr. Smith" in exc.value.detail
    # Body unchanged, no commit.
    assert existing.teacher_comment_body == "hands off"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_student_role_blocked():
    """Students can't post comments — 403, no DB calls."""
    student = _make_student()
    db = _make_db_returning([])  # never reached

    with pytest.raises(HTTPException) as exc:
        await upsert_session_comment(
            session_id=str(uuid4()),
            payload=TeacherCommentRequest(body="hi"),
            teacher=student,
            db=db,
        )

    assert exc.value.status_code == 403
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_whitespace_only_body_rejected():
    """Body that is non-empty per Pydantic but blank after trim → 422."""
    teacher = _make_teacher()
    db = _make_db_returning([])  # never reached

    with pytest.raises(HTTPException) as exc:
        await upsert_session_comment(
            session_id=str(uuid4()),
            payload=TeacherCommentRequest(body="   \n\t   "),
            teacher=teacher,
            db=db,
        )

    assert exc.value.status_code == 422
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_invalid_session_id_returns_400():
    teacher = _make_teacher()
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await upsert_session_comment(
            session_id="not-a-uuid",
            payload=TeacherCommentRequest(body="hi"),
            teacher=teacher,
            db=db,
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_upsert_unknown_session_returns_404():
    teacher = _make_teacher()
    db = _make_db_returning([None])

    with pytest.raises(HTTPException) as exc:
        await upsert_session_comment(
            session_id=str(uuid4()),
            payload=TeacherCommentRequest(body="hi"),
            teacher=teacher,
            db=db,
        )

    assert exc.value.status_code == 404
    db.commit.assert_not_awaited()


# ── DELETE ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_owner_clears_comment():
    teacher = _make_teacher()
    session = _make_session(with_comment_by=teacher, comment_body="bye")
    db = _make_db_returning([session])

    result = await delete_session_comment(
        session_id=str(session.id),
        teacher=teacher,
        db=db,
    )

    assert result is None
    assert session.teacher_comment_body is None
    assert session.teacher_comment_by_id is None
    assert session.teacher_comment_at is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_non_owner_gets_403():
    owner = _make_teacher(name="Mr. Smith")
    intruder = _make_teacher(name="Ms. Jones")
    session = _make_session(with_comment_by=owner, comment_body="locked")
    db = _make_db_returning([session])

    with pytest.raises(HTTPException) as exc:
        await delete_session_comment(
            session_id=str(session.id),
            teacher=intruder,
            db=db,
        )

    assert exc.value.status_code == 403
    # Untouched.
    assert session.teacher_comment_body == "locked"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_no_existing_comment_returns_404():
    teacher = _make_teacher()
    blank = _make_session()
    db = _make_db_returning([blank])

    with pytest.raises(HTTPException) as exc:
        await delete_session_comment(
            session_id=str(blank.id),
            teacher=teacher,
            db=db,
        )

    assert exc.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_student_role_blocked():
    student = _make_student()
    db = AsyncMock()  # never reached

    with pytest.raises(HTTPException) as exc:
        await delete_session_comment(
            session_id=str(uuid4()),
            teacher=student,
            db=db,
        )

    assert exc.value.status_code == 403
