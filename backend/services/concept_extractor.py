"""
Concept extractor — distil a completed session into 1–3 sub-concepts.

Used by memory_agent.consolidate_session() to turn raw attempt history into
knowledge-graph rows. The LLM returns a kebab-case slug, human label, outcome
bucket, and confidence per concept. We post-normalise slugs defensively
because the LLM occasionally drifts (camelCase, spaces, punctuation).
"""

import asyncio
import json
import logging
import re

from openai import AzureOpenAI

from config import settings


logger = logging.getLogger(__name__)


client = AzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)


EXTRACTOR_SYSTEM = """
You analyse a completed homework session and identify the sub-concepts inside a
subject area that the question exercised. Output STRICT JSON only.

Output shape:
{{
  "concepts": [
    {{
      "concept": "kebab-case-slug",
      "label": "Human Readable Name",
      "outcome": "mastered" | "partial" | "struggling",
      "confidence": 0.0 to 1.0
    }}
  ]
}}

Rules:
- 1 to {max_concepts} concepts. Pick the most informative ones — granular enough
  to be useful (e.g. "linear-equations" not "math"), but not so narrow they
  never repeat across students (avoid "speed-of-cyclist-bob").
- Slugs: lowercase, hyphenated, no punctuation. Examples: "mean-vs-median",
  "newton-second-law", "pythagorean-theorem", "subject-verb-agreement".
- Outcome reflects the attempt trail:
  * mastered  → correct on first or second try, no review-mode. confidence ≥ 0.75.
  * partial   → correct after 2+ wrong attempts OR resolved=true after seeing
                level-3 hint. confidence 0.4 – 0.74.
  * struggling → reached review mode without solving, or session unresolved.
                 confidence < 0.4.
- Subject area is fixed: {subject}. Do NOT emit concepts from other subjects.
- Output ONLY the JSON object. No prose, no markdown fences.
"""


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _normalise_slug(raw: str) -> str:
    """Defensive: collapse anything that isn't a-z/0-9 into single dashes."""
    slug = _SLUG_PATTERN.sub("-", (raw or "").lower()).strip("-")
    return slug[:60]


def _format_attempt_trail(attempts: list[tuple[str, bool, int]]) -> str:
    if not attempts:
        return "(no attempts recorded)"
    lines = []
    for i, (text, is_correct, hint_level) in enumerate(attempts, 1):
        verdict = "CORRECT" if is_correct else "wrong"
        lines.append(f"{i}. [level {hint_level}, {verdict}] {text}")
    return "\n".join(lines)


async def extract_concepts(
    question: str,
    subject: str,
    attempts: list[tuple[str, bool, int]],
    resolved: bool,
) -> list[dict]:
    """
    Extract concepts from a completed session.

    Args:
        question: The homework question.
        subject: One of math/science/english/history/other.
        attempts: List of (attempt_text, is_correct, hint_level) tuples in
            chronological order. Only 'answer' kind attempts — clarifications
            are filtered upstream.
        resolved: Whether the session resolved (correct OR exhausted review).

    Returns:
        List of dicts shaped {"concept": str, "label": str, "outcome": str,
        "confidence": float}. Empty list on any failure — never raises.
    """
    max_concepts = settings.memory_max_concepts_per_session
    system_msg = EXTRACTOR_SYSTEM.format(
        subject=subject,
        max_concepts=max_concepts,
    )
    user_msg = (
        f"Question:\n{question}\n\n"
        f"Resolved: {resolved}\n\n"
        f"Attempt trail (chronological):\n{_format_attempt_trail(attempts)}"
    )

    def _call_openai():
        return client.chat.completions.create(
            model=settings.azure_openai_deployment,
            max_completion_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        response = await asyncio.to_thread(_call_openai)
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except Exception as exc:  # broad on purpose — never break consolidation
        logger.exception("Concept extraction failed: %s", exc)
        return []

    items = parsed.get("concepts") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []

    cleaned: list[dict] = []
    for item in items[:max_concepts]:
        if not isinstance(item, dict):
            continue
        slug = _normalise_slug(item.get("concept", ""))
        if not slug:
            continue
        label = (item.get("label") or slug.replace("-", " ").title()).strip()[:200]
        outcome = item.get("outcome", "partial")
        if outcome not in ("mastered", "partial", "struggling"):
            outcome = "partial"
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        cleaned.append({
            "concept": slug,
            "label": label,
            "outcome": outcome,
            "confidence": confidence,
        })
    return cleaned
