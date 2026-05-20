"""
Problem generator — produces a fresh practice problem targeted at a concept.

Single JSON-mode Azure OpenAI call. Output is intentionally tight: one
self-contained question, a single canonical answer_key, and a short
explanation we reveal only after the student submits. The verifier
sub-agent (services/problem_verifier.py) is responsible for confirming the
answer_key actually solves the prompt — never trust the generator's
self-report.

Failure-safe: returns None on LLM error / malformed JSON so the orchestrator
can retry without an exception unwinding the request.
"""

import asyncio
import json
import logging

from openai import AzureOpenAI

from config import settings
from models.generated_problem import (
    DIFFICULTY_EASY,
    DIFFICULTY_HARD,
    DIFFICULTY_MEDIUM,
)


logger = logging.getLogger(__name__)


client = AzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)


_VALID_DIFFICULTIES = {DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_HARD}


GENERATOR_SYSTEM = """
You are StudyOwl's practice-problem author. Given a subject and (optionally)
a sub-concept the student is weak on, generate ONE fresh problem calibrated
to the requested difficulty. Output STRICT JSON only — no prose, no markdown
fences.

Output shape:
{{
  "prompt": "the question shown to the student",
  "answer_key": "the single canonical correct answer",
  "explanation": "1-3 sentences explaining the solution, shown ONLY after the student answers",
  "difficulty": "easy" | "medium" | "hard"
}}

Hard rules:
- The `prompt` must be a single, self-contained problem. No multi-part
  questions, no "show all your work", no "list three examples".
- The `answer_key` must be a single value or short expression.
  * For math: prefer a numeric or simple algebraic answer (e.g. "12", "3/4",
    "x = 5"). Avoid free-text explanations in the answer.
  * For science/history/english: prefer a short canonical phrase
    (e.g. "photosynthesis", "1776", "metaphor"). One acceptable form only.
- The `prompt` MUST NOT include the answer, hints toward the answer, or a
  worked example.
- Stay strictly within the subject area. Do not change topics.
- Difficulty:
  * easy   — directly tests recall / a single step. Numbers small, vocab basic.
  * medium — two steps, or one step with a small twist (units, fractions).
  * hard   — multi-step, edge-case numbers, or a less-common variation.
- Match the requested difficulty exactly. Do not "go easier to be safe".

Subject area: {subject}
Sub-concept to target: {concept_clause}
Difficulty requested: {difficulty}
Student grade level: {grade_level}
""".strip()


def _difficulty_or_default(value: str | None) -> str:
    if value in _VALID_DIFFICULTIES:
        return value
    return DIFFICULTY_MEDIUM


def _build_user_message(
    subject: str,
    concept_label: str | None,
    difficulty: str,
    grade_level: str,
) -> str:
    """User message is intentionally minimal — system carries the contract."""
    parts = [
        f"Subject: {subject}",
        f"Difficulty: {difficulty}",
        f"Grade level: {grade_level or 'unspecified'}",
    ]
    if concept_label:
        parts.append(f"Target sub-concept: {concept_label}")
    parts.append("Emit the JSON now.")
    return "\n".join(parts)


async def generate_problem(
    subject: str,
    concept: str | None,
    concept_label: str | None,
    difficulty: str,
    grade_level: str = "",
) -> dict | None:
    """
    Generate one practice problem. Returns dict shaped:
        {"prompt": str, "answer_key": str, "explanation": str,
         "difficulty": "easy" | "medium" | "hard"}
    or None on any failure (logged, never raises).
    """
    difficulty = _difficulty_or_default(difficulty)
    concept_clause = concept_label or (concept or "(none — generate against the subject)")
    system_msg = GENERATOR_SYSTEM.format(
        subject=subject,
        concept_clause=concept_clause,
        difficulty=difficulty,
        grade_level=grade_level or "unspecified",
    )
    user_msg = _build_user_message(subject, concept_label, difficulty, grade_level)

    def _call_openai():
        return client.chat.completions.create(
            model=settings.azure_openai_deployment,
            max_completion_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call_openai),
            timeout=settings.practice_generator_timeout_seconds,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except asyncio.TimeoutError:
        logger.warning(
            "Problem generator timed out after %ss",
            settings.practice_generator_timeout_seconds,
        )
        return None
    except Exception as exc:
        logger.exception("Problem generator call failed: %s", exc)
        return None

    if not isinstance(parsed, dict):
        return None

    prompt = (parsed.get("prompt") or "").strip()
    answer_key = (parsed.get("answer_key") or "").strip()
    explanation = (parsed.get("explanation") or "").strip()
    if not prompt or not answer_key:
        logger.warning("Generator returned incomplete problem: %r", parsed)
        return None

    return {
        "prompt": prompt[:1000],
        "answer_key": answer_key[:200],
        "explanation": explanation[:600],
        "difficulty": _difficulty_or_default(parsed.get("difficulty")) or difficulty,
    }
