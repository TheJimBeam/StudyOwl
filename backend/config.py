"""
Centralised configuration — all env vars loaded here via Pydantic Settings.
Never import os.environ directly elsewhere; use `from config import settings`.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    # Azure OpenAI
    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_deployment: str = "gpt-4"  # Your deployment name

    # Database
    database_url: str  # postgresql+asyncpg://...

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Auth
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 1 day

    # Alerts
    sendgrid_api_key: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    teacher_alert_email: str = "teacher@school.example.com"

    # OCR
    google_vision_api_key: str = ""  # Optional; Tesseract used if empty

    # Object storage (Cloudflare R2 / S3-compatible)
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = "studyowl-uploads"
    r2_endpoint: str = ""

    # CORS
    allowed_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:5174", 
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "https://urban-bassoon-g4grgwx55r5jh94w6-5175.app.github.dev",
        "https://urban-bassoon-g4grgwx55r5jh94w6-5173.app.github.dev",
    ]

    # Session escalation thresholds
    max_fails_before_review: int = 3
    max_fails_before_alert: int = 4
    inactivity_timeout_minutes: int = 10

    # Conversation memory — caps the number of prior hints + attempts fed back
    # into the hint prompt. Higher = better continuity, more tokens per call.
    conversation_history_limit: int = 5

    # Clarifications — cap per hint level. Resets when hint level advances.
    clarifications_per_level_limit: int = 3

    # Knowledge-graph memory
    memory_consolidation_enabled: bool = True
    memory_decay_half_life_days: int = 14
    memory_review_threshold: float = 0.4
    memory_max_concepts_per_session: int = 3
    memory_consolidation_timeout_seconds: int = 10

    # Socratic critic — adversarial second-pass judge on every generated hint.
    # When disabled, hints flow through unchanged (no extra LLM call, no DB row).
    critic_enabled: bool = True
    critic_max_retries: int = 1
    critic_timeout_seconds: int = 8
    # Fail open: if the critic LLM errors / times out / returns malformed JSON,
    # we deliver the original hint rather than blocking the student.
    critic_fail_open: bool = True
    # Empty string → fall back to azure_openai_deployment. Set this to point
    # at a cheaper / faster deployment (Haiku-class) once available.
    azure_openai_critic_deployment: str = ""

    # Inactivity scheduler
    inactivity_scheduler_enabled: bool = True
    inactivity_scan_interval_seconds: int = 60
    inactivity_max_session_age_hours: int = 24

    # Problem generator + verifier (closed-loop practice agent).
    # Killswitch disables /api/practice/generate; the read + attempt paths
    # remain available so existing rows stay usable.
    practice_generator_enabled: bool = True
    practice_max_generation_retries: int = 2
    practice_generator_timeout_seconds: int = 12
    practice_verifier_timeout_seconds: int = 8
    practice_history_limit: int = 20

    # Travily learning resource API
    travily_api_key: str = ""
    travily_api_url: str = ""


settings = Settings()
