"""
Co-Pilot narrator — LLM wrapper for the two drafted artifacts:
  1. The class-summary narrative (one paragraph, neutral, ratio-language).
  2. The per-pattern 12-minute mini-lesson draft (markdown).

Both calls are fail-closed: any error, timeout, or malformed JSON yields a
sentinel "failed" status with no raw exception bubbling up. The orchestrator
records the failure on the pattern row but still ships the rollup — a bad
LLM tick must never block teacher visibility into the underlying signal.

Hard guardrails in the system prompts: every output is framed as a DRAFT,
edits expected, never directive. The card UI doubles down on this with a
persistent "DRAFT" badge — the model is one belt, the UI is the suspenders.
"""

from __future__ import annotations

import asyncio
import json
import logging

from openai import AzureOpenAI

from config import settings

from .copilot_aggregator import PatternCandidate, WeeklyAggregate, build_headline


logger = logging.getLogger(__name__)


client = AzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)


def _deployment() -> str:
    return settings.azure_openai_copilot_deployment or settings.azure_openai_deployment


NARRATOR_SYSTEM = """
You write a single-paragraph weekly class summary for a teacher dashboard.
This output is a DRAFT — the teacher will read it, may edit, may discard it.
You never instruct ("Re-teach X", "You should ..."). You describe what
happened in ratio language and surface uncertainty when relevant.

Output STRICT JSON, no markdown fences:
{
  "narrative": "<one paragraph, 2-4 sentences, neutral observer tone>"
}

Rules:
- Mention the headline numbers (students affected / cohort size, subject,
  concept) for the top patterns. Stay factual.
- End with a soft pointer to the mini-lesson drafts ("Drafts for possible
  re-anchor mini-lessons are attached below — review before using.").
- Never make claims you can't ground in the patterns list.
- If patterns is empty, say so plainly and suggest the report needs more
  class activity before drawing conclusions.
""".strip()


MINI_LESSON_SYSTEM = """
You draft a 12-minute mini-lesson plan for a teacher to consider running with
their class. This output is a DRAFT — the teacher will read it, edit it, or
ignore it. You never instruct ("Do X", "You must ..."). You propose
("You might consider...", "A possible angle is..."). The audience for this
draft is the TEACHER, not the student.

Output STRICT JSON, no markdown fences:
{
  "mini_lesson_md": "<markdown body — see structure below>"
}

Required markdown structure for `mini_lesson_md`:

# Possible 12-min mini-lesson: <short concept-anchored title>

**Why this surfaced:** <one sentence grounded in the headline numbers>

**Possible objective (you might adapt):** <one sentence>

**Suggested flow (≈12 minutes):**
- **0–2 min — Hook:** <a question or observation>
- **2–6 min — Worked example:** <a step-by-step example, no spoilers
  of the kids' own homework>
- **6–10 min — Check for understanding:** <a small task students can try>
- **10–12 min — Re-anchor:** <how to tie it back to what they've been
  struggling with>

**Reminder:** This is an auto-generated draft. Edit freely or skip entirely.

Rules:
- Concept-faithful: if the concept is "quadratic-factoring", the worked
  example must factor a quadratic.
- No directives. Use "you might", "consider", "a possible angle".
- Never reference individual students.
""".strip()


async def narrate_summary(aggregate: WeeklyAggregate) -> tuple[str, str]:
    """
    Return (narrative_text, status).

    status ∈ {"ok", "skipped", "failed"}:
      - "ok"      : narrative produced and non-empty
      - "skipped" : no patterns found AND no LLM call attempted
      - "failed"  : LLM call raised, timed out, or returned malformed JSON

    Never raises.
    """
    if not aggregate.patterns:
        # Nothing to narrate — caller can still ship the rollup with the
        # "not enough class activity" empty-state UI.
        return ("", "skipped")

    patterns_block = "\n".join(
        f"- {build_headline(p)} ({p.signal_kind})" for p in aggregate.patterns
    )
    user_msg = (
        f"Week: {aggregate.week_start.date().isoformat()} → "
        f"{aggregate.week_end.date().isoformat()}\n"
        f"Roster size (students): {aggregate.student_count}\n"
        f"Sessions this week: {aggregate.session_count} "
        f"({aggregate.resolved_count} resolved)\n\n"
        f"Top patterns:\n{patterns_block}"
    )

    def _call_openai():
        return client.chat.completions.create(
            model=_deployment(),
            max_completion_tokens=350,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": NARRATOR_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call_openai),
            timeout=settings.copilot_narrator_timeout_seconds,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except asyncio.TimeoutError:
        logger.warning(
            "Copilot narrator timed out after %ss",
            settings.copilot_narrator_timeout_seconds,
        )
        return ("", "failed")
    except Exception as exc:  # never propagate
        logger.exception("Copilot narrator failed: %s", exc)
        return ("", "failed")

    text = parsed.get("narrative") if isinstance(parsed, dict) else None
    if not isinstance(text, str) or not text.strip():
        return ("", "failed")
    return (text.strip(), "ok")


async def draft_mini_lesson(
    pattern: PatternCandidate,
) -> tuple[str | None, str, str | None]:
    """
    Return (markdown, status, error).

    status ∈ {"ok", "failed"}; error is None on success and a short
    diagnostic string on failure. Never raises.
    """
    user_msg = (
        f"Headline: {build_headline(pattern)}\n"
        f"Subject: {pattern.subject}\n"
        f"Concept slug: {pattern.concept or '(unattributed)'}\n"
        f"Concept label: {pattern.concept_label or '(unattributed)'}\n"
        f"Signal kind: {pattern.signal_kind}\n"
        f"Affected: {pattern.affected_count} of {pattern.cohort_count} "
        f"({pattern.affected_ratio:.0%})"
    )

    def _call_openai():
        return client.chat.completions.create(
            model=_deployment(),
            max_completion_tokens=700,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": MINI_LESSON_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call_openai),
            timeout=settings.copilot_mini_lesson_timeout_seconds,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except asyncio.TimeoutError:
        return (None, "failed", "timeout")
    except Exception as exc:
        logger.exception("Copilot mini-lesson draft failed: %s", exc)
        return (None, "failed", f"{type(exc).__name__}: {exc}"[:200])

    md = parsed.get("mini_lesson_md") if isinstance(parsed, dict) else None
    if not isinstance(md, str) or not md.strip():
        return (None, "failed", "malformed_json")
    return (md.strip(), "ok", None)
