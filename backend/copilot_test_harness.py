"""
Co-Pilot pattern test harness.

Seeds enough data to make at least one Teacher Co-Pilot weekly pattern surface
on the dashboard, then cleans up everything it inserted so the teacher view
returns to its pre-test state.

It writes a state file recording the exact UUIDs it inserted, so cleanup is
precise — never WHERE patterns that could match unrelated data.

Usage (run from inside the backend container, with the same env the app uses):

    python copilot_test_harness.py seed     # Insert test data
    python copilot_test_harness.py cleanup  # Remove everything 'seed' inserted
    python copilot_test_harness.py status   # Show whether a seed is active

The harness DOES NOT touch the COPILOT_MIN_COHORT_SIZE / COPILOT_MIN_AFFECTED_RATIO
env vars — those are loaded at startup. Lower them in your .env (or
docker-compose env block) before seeding, restart the backend, then put them
back after cleanup. Suggested test values:

    COPILOT_MIN_COHORT_SIZE=2
    COPILOT_MIN_AFFECTED_RATIO=0.20

(Defaults are 3 and 0.30 — see backend/config.py.)
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select

from db import SessionLocal
from models.alert import Alert
from models.concept_memory import ConceptMemory, STATUS_STRUGGLING
from models.session import Session
from models.student import Student


# State file lives next to the script so it survives container restarts on a
# mounted backend volume but doesn't pollute git (add to .gitignore if needed).
STATE_FILE = Path(__file__).parent / ".copilot_test_harness_state.json"

# Test students — emails use a marker domain so they're recognisable in the DB
# even if the state file is somehow lost. Names are also marker-prefixed.
TEST_STUDENTS = [
    {"email": "copilot-test-1@studyowl.local", "name": "[TEST] Alice"},
    {"email": "copilot-test-2@studyowl.local", "name": "[TEST] Bob"},
    {"email": "copilot-test-3@studyowl.local", "name": "[TEST] Cara"},
]

TEST_CONCEPT = "__copilot_test_mean_vs_median"
TEST_LABEL = "[TEST] Mean vs Median"
TEST_SUBJECT = "math"
TEST_QUESTION = "[TEST] What is the difference between mean and median?"

# We never use these accounts for login — the field is NOT NULL so we need
# *something*, but it doesn't need to be a real bcrypt hash.
DUMMY_HASH = "TEST_HARNESS_NO_LOGIN"


async def seed() -> int:
    if STATE_FILE.exists():
        print(
            f"Refusing to seed — state file already exists at {STATE_FILE}.\n"
            f"Run `python copilot_test_harness.py cleanup` first."
        )
        return 1

    now = datetime.now(timezone.utc)
    # The Co-Pilot digest analyzes the PREVIOUS ISO week, not the current one.
    # Target a midweek timestamp in that window so the seeded rows fall inside
    # the aggregator's [week_start, week_end) range.
    seed_at = _previous_week_start(now) + timedelta(days=3, hours=12)
    state: dict[str, list[str]] = {
        "students": [],
        "sessions": [],
        "concept_memories": [],
    }

    async with SessionLocal() as db:
        # Bail loudly if someone has a stale test student lingering from a
        # previous run where the state file got nuked manually.
        existing = await db.execute(
            select(Student).where(
                Student.email.in_([s["email"] for s in TEST_STUDENTS])
            )
        )
        if existing.scalars().first() is not None:
            print(
                "Refusing to seed — one or more test student emails already "
                "exist in the DB. Inspect with:\n"
                "  SELECT id, email FROM students WHERE email LIKE 'copilot-test-%@studyowl.local';\n"
                "and either delete those rows manually, or restore the state "
                "file under .copilot_test_harness_state.json and re-run cleanup."
            )
            return 1

        for spec in TEST_STUDENTS:
            student = Student(
                id=uuid4(),
                name=spec["name"],
                email=spec["email"],
                grade_level="[TEST]",
                role="student",
                hashed_password=DUMMY_HASH,
            )
            db.add(student)
            state["students"].append(str(student.id))

            # Two unresolved sessions per student → satisfies repeated_failure
            # (≥2 unresolved sessions in subject this week) AND level3_stuck
            # (fails_at_level >= 3) on the same rows.
            for _ in range(2):
                session = Session(
                    id=uuid4(),
                    student_id=student.id,
                    question=TEST_QUESTION,
                    subject=TEST_SUBJECT,
                    hint_level=3,
                    fails_at_level=3,
                    resolved=False,
                    started_at=seed_at,
                    last_activity_at=seed_at,
                )
                db.add(session)
                state["sessions"].append(str(session.id))

            # One struggling concept memory per student → satisfies
            # concept_struggle (struggling + attempts_count >= 2 + updated this
            # week). Also feeds the join for the other two signals.
            memory = ConceptMemory(
                id=uuid4(),
                student_id=student.id,
                subject=TEST_SUBJECT,
                concept=TEST_CONCEPT,
                label=TEST_LABEL,
                status=STATUS_STRUGGLING,
                confidence=0.15,
                attempts_count=2,
                correct_count=0,
                last_seen=seed_at,
                last_session_id=None,
                # The aggregator filters concept_struggle on
                # ConceptMemory.updated_at >= week_start, so we override the
                # default (now) to land inside last week's window.
                created_at=seed_at,
                updated_at=seed_at,
            )
            db.add(memory)
            state["concept_memories"].append(str(memory.id))

        await db.commit()

        # Print the cohort math so the operator can predict whether the floors
        # will pass without staring at config files. Window is *last* week —
        # that's what the digest analyzes.
        prev_start = _previous_week_start(now)
        prev_end = prev_start + timedelta(days=7)
        cohort_row = await db.execute(
            select(func.count(func.distinct(Session.student_id)))
            .where(Session.subject == TEST_SUBJECT)
            .where(Session.started_at >= prev_start)
            .where(Session.started_at < prev_end)
        )
        cohort_n = int(cohort_row.scalar_one())

    STATE_FILE.write_text(json.dumps(state, indent=2))

    affected = len(TEST_STUDENTS)
    ratio = affected / cohort_n if cohort_n else 0.0

    print(
        f"Seeded {len(state['students'])} students, "
        f"{len(state['sessions'])} sessions, "
        f"{len(state['concept_memories'])} concept memories."
    )
    print(f"State recorded at {STATE_FILE}")
    print()
    print(f"Cohort math (math subject, this week):")
    print(f"  affected = {affected}   cohort = {cohort_n}   ratio = {ratio:.2%}")
    print(f"  Pattern will surface if cohort_size <= {affected} AND "
          f"ratio >= configured COPILOT_MIN_AFFECTED_RATIO.")
    print()
    print("Next steps:")
    print("  1. Make sure COPILOT_MIN_COHORT_SIZE and COPILOT_MIN_AFFECTED_RATIO")
    print(f"     are low enough (e.g. 2 and {min(ratio - 0.01, 0.20):.2f}) and the backend was restarted.")
    print("  2. Log in as a teacher → Teacher Co-Pilot tab → 'Regenerate digest'.")
    print("  3. Verify the pattern card surfaces.")
    print("  4. When done: python copilot_test_harness.py cleanup")
    return 0


async def cleanup() -> int:
    if not STATE_FILE.exists():
        print(f"No state file at {STATE_FILE} — nothing to clean up.")
        return 0

    state = json.loads(STATE_FILE.read_text())
    student_ids = [UUID(s) for s in state.get("students", [])]
    session_ids = [UUID(s) for s in state.get("sessions", [])]
    memory_ids = [UUID(s) for s in state.get("concept_memories", [])]

    async with SessionLocal() as db:
        # Delete leaves first to respect the FK chain even though the cascades
        # would also handle it. Doing it explicitly keeps the script honest
        # about exactly which rows it removed.
        if memory_ids:
            await db.execute(
                delete(ConceptMemory).where(ConceptMemory.id.in_(memory_ids))
            )
        # Background services (inactivity scheduler, session_manager triggers)
        # can write Alert rows that reference our seeded sessions. The alerts
        # FK has no ON DELETE CASCADE, so we have to clear those by hand
        # before the session DELETE will succeed.
        alerts_removed = 0
        if session_ids:
            result = await db.execute(
                delete(Alert).where(Alert.session_id.in_(session_ids))
            )
            alerts_removed = result.rowcount or 0
            await db.execute(delete(Session).where(Session.id.in_(session_ids)))
        if student_ids:
            await db.execute(delete(Student).where(Student.id.in_(student_ids)))
        await db.commit()

    STATE_FILE.unlink()

    print(
        f"Removed {len(memory_ids)} concept memories, "
        f"{alerts_removed} alerts, "
        f"{len(session_ids)} sessions, "
        f"{len(student_ids)} students."
    )
    print(f"State file deleted.")
    print()
    print(
        "Reminder: restore COPILOT_MIN_COHORT_SIZE=3 and "
        "COPILOT_MIN_AFFECTED_RATIO=0.30 in your .env / docker-compose env, "
        "then restart the backend."
    )
    return 0


async def status() -> int:
    if not STATE_FILE.exists():
        print("No active seed (state file not found).")
        return 0
    state = json.loads(STATE_FILE.read_text())
    print(f"Active seed at {STATE_FILE}:")
    print(f"  - {len(state.get('students', []))} students")
    print(f"  - {len(state.get('sessions', []))} sessions")
    print(f"  - {len(state.get('concept_memories', []))} concept memories")
    print()
    print("Cleanup with: python copilot_test_harness.py cleanup")
    return 0


def _week_start(now: datetime) -> datetime:
    """Monday 00:00 UTC of the current ISO week."""
    day = now.date()
    monday = day.fromordinal(day.toordinal() - day.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def _week_end(now: datetime) -> datetime:
    """Next Monday 00:00 UTC (exclusive)."""
    return _week_start(now) + timedelta(days=7)


def _previous_week_start(now: datetime) -> datetime:
    """Monday 00:00 UTC of the previous ISO week — the digest's window."""
    return _week_start(now) - timedelta(days=7)


COMMANDS = {"seed": seed, "cleanup": cleanup, "status": status}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python copilot_test_harness.py {seed|cleanup|status}")
        print()
        print("  seed     Insert test students/sessions/concept memory so the")
        print("           Co-Pilot weekly digest surfaces a pattern.")
        print("  cleanup  Remove everything 'seed' inserted (uses recorded IDs,")
        print("           never WHERE patterns — safe against unrelated data).")
        print("  status   Show whether a seed is currently active.")
        sys.exit(2)
    sys.exit(asyncio.run(COMMANDS[sys.argv[1]]()))


if __name__ == "__main__":
    main()
