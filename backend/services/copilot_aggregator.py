"""
Co-Pilot aggregator — pure SQL over the last completed ISO week.

Three signals, each org-wide (no roster/class scoping in v1):
- level3_stuck     : struggling concept memory rows whose student also hit
                     `fails_at_level >= 3` on a matching-subject session this week
- repeated_failure : struggling concept memory rows whose student has 2+
                     unresolved sessions on the same subject this week
- concept_struggle : struggling concept memory rows updated this week with
                     attempts_count >= 2 (no session join — catches gradual
                     struggle that hasn't tripped L3 yet)

Cohort denominator for ratios is distinct students with ≥1 session in that
subject this week. Floors (settings.copilot_min_cohort_size,
copilot_min_affected_ratio) keep the digest honest at low N — we never
fabricate patterns from sparse data.

No LLM dependency here, on purpose. Tests can seed a DB and assert pattern
shape without touching the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.concept_memory import ConceptMemory, STATUS_STRUGGLING
from models.session import Session
from models.student import Student


logger = logging.getLogger(__name__)


@dataclass
class PatternCandidate:
    signal_kind: str
    subject: str
    concept: str | None
    concept_label: str | None
    affected_count: int
    cohort_count: int
    affected_ratio: float
    evidence: dict = field(default_factory=dict)

    def dedupe_key(self) -> tuple[str, str | None]:
        return (self.subject, self.concept)


@dataclass
class WeeklyAggregate:
    week_start: datetime
    week_end: datetime
    student_count: int
    session_count: int
    resolved_count: int
    patterns: list[PatternCandidate]


# Signal priority for dedupe — when two detectors fire on the same
# (subject, concept), keep the higher-priority signal_kind.
_SIGNAL_PRIORITY = {
    "level3_stuck": 3,
    "repeated_failure": 2,
    "concept_struggle": 1,
}


async def _cohort_size_by_subject(
    db: AsyncSession, week_start: datetime, week_end: datetime,
) -> dict[str, int]:
    """Distinct students with ≥1 session in each subject this week."""
    stmt = (
        select(
            Session.subject,
            func.count(distinct(Session.student_id)).label("n"),
        )
        .where(Session.started_at >= week_start, Session.started_at < week_end)
        .group_by(Session.subject)
    )
    rows = (await db.execute(stmt)).all()
    return {row.subject: int(row.n) for row in rows}


async def detect_level3_stuck(
    db: AsyncSession,
    week_start: datetime,
    week_end: datetime,
    cohort: dict[str, int],
) -> list[PatternCandidate]:
    """
    Concept-level L3 stuck: students with a struggling concept_memory row
    AND a same-subject session this week with fails_at_level >= 3.
    """
    stmt = (
        select(
            ConceptMemory.subject,
            ConceptMemory.concept,
            ConceptMemory.label,
            func.count(distinct(ConceptMemory.student_id)).label("affected"),
        )
        .join(
            Session,
            (Session.student_id == ConceptMemory.student_id)
            & (Session.subject == ConceptMemory.subject),
        )
        .where(
            ConceptMemory.updated_at >= week_start,
            ConceptMemory.status == STATUS_STRUGGLING,
            Session.started_at >= week_start,
            Session.started_at < week_end,
            Session.fails_at_level >= 3,
        )
        .group_by(ConceptMemory.subject, ConceptMemory.concept, ConceptMemory.label)
    )
    return _shape_candidates(
        signal_kind="level3_stuck",
        rows=(await db.execute(stmt)).all(),
        cohort=cohort,
    )


async def detect_repeated_failure(
    db: AsyncSession,
    week_start: datetime,
    week_end: datetime,
    cohort: dict[str, int],
) -> list[PatternCandidate]:
    """
    Students with a struggling concept_memory row AND 2+ unresolved sessions
    in that subject this week. Catches kids who keep coming back to the same
    topic without finishing.
    """
    # Per-student-per-subject unresolved-session counts this week.
    unresolved_subq = (
        select(
            Session.student_id.label("sid"),
            Session.subject.label("subj"),
            func.count(Session.id).label("n_unresolved"),
        )
        .where(
            Session.started_at >= week_start,
            Session.started_at < week_end,
            Session.resolved.is_(False),
        )
        .group_by(Session.student_id, Session.subject)
        .having(func.count(Session.id) >= 2)
        .subquery()
    )

    stmt = (
        select(
            ConceptMemory.subject,
            ConceptMemory.concept,
            ConceptMemory.label,
            func.count(distinct(ConceptMemory.student_id)).label("affected"),
        )
        .join(
            unresolved_subq,
            (unresolved_subq.c.sid == ConceptMemory.student_id)
            & (unresolved_subq.c.subj == ConceptMemory.subject),
        )
        .where(
            ConceptMemory.updated_at >= week_start,
            ConceptMemory.status == STATUS_STRUGGLING,
        )
        .group_by(ConceptMemory.subject, ConceptMemory.concept, ConceptMemory.label)
    )
    return _shape_candidates(
        signal_kind="repeated_failure",
        rows=(await db.execute(stmt)).all(),
        cohort=cohort,
    )


async def detect_concept_struggle(
    db: AsyncSession,
    week_start: datetime,
    week_end: datetime,
    cohort: dict[str, int],
) -> list[PatternCandidate]:
    """
    Struggling concept_memory rows updated this week with attempts_count >= 2.
    No session join — catches concepts that show gradual struggle without
    a single bright-line L3 escalation yet.
    """
    stmt = (
        select(
            ConceptMemory.subject,
            ConceptMemory.concept,
            ConceptMemory.label,
            func.count(distinct(ConceptMemory.student_id)).label("affected"),
        )
        .where(
            ConceptMemory.updated_at >= week_start,
            ConceptMemory.status == STATUS_STRUGGLING,
            ConceptMemory.attempts_count >= 2,
        )
        .group_by(ConceptMemory.subject, ConceptMemory.concept, ConceptMemory.label)
    )
    return _shape_candidates(
        signal_kind="concept_struggle",
        rows=(await db.execute(stmt)).all(),
        cohort=cohort,
    )


def _shape_candidates(
    signal_kind: str,
    rows,
    cohort: dict[str, int],
) -> list[PatternCandidate]:
    min_cohort = settings.copilot_min_cohort_size
    min_ratio = settings.copilot_min_affected_ratio
    out: list[PatternCandidate] = []
    for row in rows:
        affected = int(row.affected)
        if affected < min_cohort:
            continue
        cohort_n = cohort.get(row.subject, 0)
        if cohort_n <= 0:
            continue
        ratio = affected / cohort_n
        if ratio < min_ratio:
            continue
        out.append(PatternCandidate(
            signal_kind=signal_kind,
            subject=row.subject,
            concept=row.concept,
            concept_label=row.label,
            affected_count=affected,
            cohort_count=cohort_n,
            affected_ratio=round(ratio, 4),
            evidence={"signal_kind": signal_kind},
        ))
    return out


def rank_and_top_n(candidates: list[PatternCandidate], n: int) -> list[PatternCandidate]:
    """
    Merge all candidates, dedupe by (subject, concept) keeping the
    higher-priority signal_kind, sort by affected_ratio DESC, clip to n.
    """
    best: dict[tuple[str, str | None], PatternCandidate] = {}
    for cand in candidates:
        key = cand.dedupe_key()
        prior = best.get(key)
        if prior is None:
            best[key] = cand
            continue
        # Higher signal priority wins; on a tie, higher ratio wins.
        if _SIGNAL_PRIORITY.get(cand.signal_kind, 0) > _SIGNAL_PRIORITY.get(prior.signal_kind, 0):
            best[key] = cand
        elif (
            _SIGNAL_PRIORITY.get(cand.signal_kind, 0) == _SIGNAL_PRIORITY.get(prior.signal_kind, 0)
            and cand.affected_ratio > prior.affected_ratio
        ):
            best[key] = cand

    ordered = sorted(best.values(), key=lambda c: c.affected_ratio, reverse=True)
    return ordered[:n]


async def aggregate_week(
    db: AsyncSession,
    week_start: datetime,
    week_end: datetime,
) -> WeeklyAggregate:
    """Run all detectors and shape the top-N candidate list."""
    # Audit context — shown in the report header.
    student_total = (
        await db.execute(select(func.count(Student.id)).where(Student.role == "student"))
    ).scalar_one()

    session_count = (
        await db.execute(
            select(func.count(Session.id)).where(
                Session.started_at >= week_start,
                Session.started_at < week_end,
            )
        )
    ).scalar_one()

    resolved_count = (
        await db.execute(
            select(func.count(Session.id)).where(
                Session.started_at >= week_start,
                Session.started_at < week_end,
                Session.resolved.is_(True),
            )
        )
    ).scalar_one()

    cohort = await _cohort_size_by_subject(db, week_start, week_end)

    candidates: list[PatternCandidate] = []
    candidates.extend(await detect_level3_stuck(db, week_start, week_end, cohort))
    candidates.extend(await detect_repeated_failure(db, week_start, week_end, cohort))
    candidates.extend(await detect_concept_struggle(db, week_start, week_end, cohort))

    patterns = rank_and_top_n(candidates, settings.copilot_max_patterns_per_report)

    return WeeklyAggregate(
        week_start=week_start,
        week_end=week_end,
        student_count=int(student_total),
        session_count=int(session_count),
        resolved_count=int(resolved_count),
        patterns=patterns,
    )


def build_headline(pattern: PatternCandidate) -> str:
    """
    Server-rendered headline so the UI never formats ratio prose.
    Uses ratio language ("14 of 28 students hit Level 3 on quadratic factoring"),
    NEVER directive language ("Re-teach quadratic factoring").
    """
    subject = pattern.subject.capitalize() if pattern.subject else "this subject"
    concept_phrase = (
        f" on {pattern.concept_label}"
        if pattern.concept_label
        else f" in {subject.lower()}"
    )

    if pattern.signal_kind == "level3_stuck":
        verb = "hit Level 3"
    elif pattern.signal_kind == "repeated_failure":
        verb = "had repeated unresolved sessions"
    else:
        verb = "showed struggle"

    return (
        f"{pattern.affected_count} of {pattern.cohort_count} students {verb}"
        f"{concept_phrase} this week"
    )
