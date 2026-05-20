"""
StudyOwl — FastAPI application entry point.
Registers all routers and configures CORS, lifespan, and middleware.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from db import init_db
from routers import sessions, alerts, progress, auth, practice
from services import inactivity_scheduler


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB connection pool on startup, close on shutdown."""
    await init_db()

    scheduler_task: asyncio.Task | None = None
    if settings.inactivity_scheduler_enabled:
        scheduler_task = asyncio.create_task(inactivity_scheduler.run())
    else:
        logger.info("Inactivity scheduler disabled via config.")

    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="StudyOwl API",
    version="0.1.0",
    description="AI-powered homework assistant — Socratic hints, never spoilers.",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "https://urban-bassoon-g4grgwx55r5jh94w6-5173.app.github.dev",
        "https://urban-bassoon-g4grgwx55r5jh94w6-5174.app.github.dev",
        "https://urban-bassoon-g4grgwx55r5jh94w6-5175.app.github.dev",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["*"],
)

app.include_router(auth.router,     prefix="/api/auth",     tags=["auth"])
app.include_router(sessions.router, prefix="/api/session",  tags=["sessions"])
app.include_router(progress.router, prefix="/api/student",  tags=["progress"])
app.include_router(alerts.router,   prefix="/api/alert",    tags=["alerts"])
app.include_router(practice.router, prefix="/api/practice", tags=["practice"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "studyowl-api"}
