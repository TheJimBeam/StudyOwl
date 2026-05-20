"""
Database initialization and session management.

Async SQLAlchemy engine and session factory for PostgreSQL.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

from config import settings

# Base class for all models
Base = declarative_base()

# Async engine
engine = create_async_engine(
    settings.database_url,
    echo=False,  # Set to True for SQL debugging
    future=True,
)

# Async session factory
SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Initialize database tables and set up connection pool."""
    # Import models here to ensure they're registered with Base
    from models import Student, Session, Attempt, Alert, ConceptMemory  # noqa: F401

    async with engine.begin() as conn:
        # Create all tables from models (no-op for tables that already exist).
        await conn.run_sync(Base.metadata.create_all)

        # Idempotent in-place migrations for existing deployments where
        # create_all only adds new tables, not new columns on existing ones.
        if conn.dialect.name == "postgresql":
            # PR 8: kind column on attempts (default "answer").
            await conn.execute(text(
                "ALTER TABLE attempts "
                "ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'answer'"
            ))

            # last_activity_at column + index on sessions (from main).
            await conn.execute(text(
                "ALTER TABLE sessions "
                "ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ "
                "NOT NULL DEFAULT NOW()"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_sessions_last_activity_at "
                "ON sessions (last_activity_at)"
            ))

            # PR 7: backfill one Alert row per legacy `teacher_alerted=True` session,
            # so the dashboard doesn't lose visibility on existing open alerts.
            # The NOT EXISTS guard keeps this idempotent across restarts.
            await conn.execute(text(
                """
                INSERT INTO alerts (
                    id, session_id, student_id, severity, reason_kind, reason_text,
                    created_at, notification_status
                )
                SELECT
                    gen_random_uuid(),
                    s.id,
                    s.student_id,
                    'medium',
                    'legacy',
                    'Legacy alert backfilled from teacher_alerted flag',
                    s.started_at,
                    'sent'
                FROM sessions s
                WHERE s.teacher_alerted = TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM alerts a WHERE a.session_id = s.id
                  )
                """
            ))


async def get_db() -> AsyncSession:
    """Dependency: yields an async DB session for each request."""
    async with SessionLocal() as session:
        yield session


async def close_db() -> None:
    """Close the database engine."""
    await engine.dispose()
