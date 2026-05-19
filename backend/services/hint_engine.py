"""
Hint engine — the core of StudyOwl.

Calls the Claude API to generate a Socratic hint at the appropriate level.
Never reveals the direct answer before Level 3 is exhausted.

Hint levels:
  1 — Socratic question only. No method hints.
  2 — Points to the relevant formula or concept. No solving.
  3 — Near-answer with all values filled in. Student must do the final step.
"""

import re

from openai import AsyncAzureOpenAI
from config import settings
from . import answer_verifier

async_client = AsyncAzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)

HINT_SYSTEM = """
You are StudyOwl, a Socratic homework assistant. You NEVER give the direct answer.
Current hint level: {level}/3.

Level 1 — Ask exactly one Socratic question. Do not explain the method or hint at the formula.
Level 2 — Name the relevant formula or concept and explain what it means. Do not substitute values or solve.
Level 3 — Write out the formula with all known values substituted. Stop before the final answer. The student must perform the last calculation step themselves.

Subject area: {subject}

Rules:
- Maximum 3 sentences per response.
- Always end with exactly one short, genuine encouraging sentence.
- If the student expresses frustration or distress, acknowledge it warmly before the hint.
"""


async def get_hint(
    question: str,
    subject: str,
    level: int,
    previous_attempts: list[str],
) -> str:
    """
    Generate a Socratic hint for the given question at the specified hint level.

    Args:
        question: The original homework question.
        subject: Classified subject area (math, science, english, history, other).
        level: Current hint level (1, 2, or 3).
        previous_attempts: List of the student's previous answer attempts.

    Returns:
        A hint string from AzureOpenAI, appropriate to the hint level.
    """
    attempts_text = (
        "\n".join(f"- {a}" for a in previous_attempts)
        if previous_attempts
        else "None yet."
    )

    response = await async_client.chat.completions.create(
        model=settings.azure_openai_deployment,
        max_completion_tokens=300,
        messages=[
            {"role": "system", "content": HINT_SYSTEM.format(level=level, subject=subject)},
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Student's previous attempts:\n{attempts_text}"
                ),
            },
        ],
    )
    return response.choices[0].message.content.strip()


async def get_direct_answer(question: str, subject: str) -> str:
    """
    Return the direct correct answer for a homework question after all hints are exhausted.

    For math, use symbolic solving when possible. For other subjects, fall back to the model.
    """
    if subject == "math":
        direct_answer = answer_verifier.solve_math_question(question)
        if direct_answer:
            return direct_answer

    response = await async_client.chat.completions.create(
        model=settings.azure_openai_deployment,
        max_completion_tokens=150,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are StudyOwl, a homework assistant. The student has already seen all three hints. "
                    "Provide the direct correct answer clearly and concisely. Do not add extra unrelated details."
                ),
            },
            {"role": "user", "content": f"Question: {question}"},
        ],
    )
    return response.choices[0].message.content.strip()


# Phrases that should trip a distress alert. Drawn from the original LLM-based
# detector's system prompt plus a few obvious synonyms / contractions students
# actually type. Matched case-insensitively against whitespace-normalized text.
_DISTRESS_PHRASES = (
    "i give up",
    "i quit",
    "i'm done",
    "im done",
    "i hate this",
    "i hate it",
    "i can't do this",
    "i cant do this",
    "i can't do it",
    "i cant do it",
    "i don't understand anything",
    "i dont understand anything",
    "i don't get it at all",
    "i dont get it at all",
    "this makes no sense",
    "this is impossible",
    "too hard",
    "i'm so stuck",
    "im so stuck",
)
_DISTRESS_RE = re.compile(
    "|".join(re.escape(p) for p in _DISTRESS_PHRASES),
    re.IGNORECASE,
)


async def detect_distress(message: str) -> bool:
    """
    Detect if a student message signals distress strong enough to alert a teacher.

    Uses a curated phrase list — the same examples the previous LLM-based detector
    was prompted with, plus common contractions. Local match, zero LLM cost, zero
    network latency. Async signature is kept so callers don't change.
    """
    if not message:
        return False
    # Collapse whitespace so "i  give    up" still matches.
    normalized = re.sub(r"\s+", " ", message).strip()
    return _DISTRESS_RE.search(normalized) is not None
