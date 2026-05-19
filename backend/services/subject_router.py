"""
Subject router — classify questions into subject areas.

Uses Claude to categorize incoming questions.
"""

import re
from collections import OrderedDict

from openai import AsyncAzureOpenAI
from config import settings

async_client = AsyncAzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)

SUBJECT_SYSTEM = """
You classify homework questions into one of exactly 5 categories.
Respond with ONLY the category name, nothing else.

Categories:
- math: algebra, geometry, calculus, trigonometry, statistics, equations, numbers
- science: physics, chemistry, biology, earth science, astronomy
- english: literature, grammar, writing, reading comprehension, essays
- history: dates, events, historical figures, civilizations, timelines
- other: anything that doesn't fit above

IMPORTANT: Reply with ONLY the category word (e.g., 'math', 'science', etc). No explanation.
"""

_VALID_SUBJECTS = ("math", "science", "english", "history", "other")

# Cheap pre-filters. Anything obviously math (digits with an operator/equals or
# a "solve/simplify/..." stem) or obviously a date/era history question is
# resolved without an LLM call.
_MATH_OPERATOR_RE = re.compile(r"\d.*[=+\-*/^]|[=+\-*/^].*\d")
_MATH_STEM_RE = re.compile(
    r"^\s*(solve|simplify|calculate|evaluate|what is|compute|factor|expand)\b.*\d",
    re.IGNORECASE,
)
_HISTORY_PREFIX_RE = re.compile(
    r"^\s*(what year|in what year|who was|when did|in what century|which century)\b",
    re.IGNORECASE,
)

# In-process LRU for the LLM path. Resolved subject strings only — never
# coroutines. Bounded so repeated demo/test questions are free without leaking
# memory across long-running workers.
_SUBJECT_CACHE_MAX = 512
_subject_cache: "OrderedDict[str, str]" = OrderedDict()


def _normalize(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().lower()


def _regex_classify(normalized: str) -> str | None:
    """Return a subject when the question shape is unambiguous; else None."""
    if _MATH_OPERATOR_RE.search(normalized) or _MATH_STEM_RE.search(normalized):
        return "math"
    if _HISTORY_PREFIX_RE.search(normalized):
        return "history"
    return None


async def classify(question: str) -> str:
    """
    Classify a homework question into a subject area.

    Args:
        question: The homework question text.

    Returns:
        One of: 'math', 'science', 'english', 'history', 'other'.
    """
    normalized = _normalize(question)

    short = _regex_classify(normalized)
    if short is not None:
        return short

    cached = _subject_cache.get(normalized)
    if cached is not None:
        _subject_cache.move_to_end(normalized)
        return cached

    response = await async_client.chat.completions.create(
        model=settings.azure_openai_deployment,
        max_completion_tokens=10,
        messages=[
            {"role": "system", "content": SUBJECT_SYSTEM},
            {"role": "user", "content": question},
        ],
    )
    subject = response.choices[0].message.content.strip().lower()
    if subject not in _VALID_SUBJECTS:
        subject = "other"

    _subject_cache[normalized] = subject
    _subject_cache.move_to_end(normalized)
    if len(_subject_cache) > _SUBJECT_CACHE_MAX:
        _subject_cache.popitem(last=False)

    return subject
