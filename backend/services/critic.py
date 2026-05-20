"""
Socratic critic — adversarial second-pass judge on every generated hint.

Single JSON-mode Azure OpenAI call that answers three questions:
  1. Does this hint spoil the answer?
  2. Is it at the right cognitive level for the stated hint tier?
  3. Does it address the student's most recent wrong attempt?

The pipeline (services/hint_pipeline.py) consumes this verdict and decides
whether to regenerate or deliver. The critic never touches the DB itself.

Failure mode: configurable via `settings.critic_fail_open`. Default is to
treat infrastructure failures as "approve" so a critic outage can't block
hint delivery.
"""

import asyncio
import json
import logging

from openai import AzureOpenAI

from config import settings


logger = logging.getLogger(__name__)


client = AzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)


CRITIC_SYSTEM = """
You are the Socratic Critic for a homework tutor. Your single job is to judge
whether a candidate hint is safe to deliver to a student.

Reject if ANY of the following are true:

1. SPOILER — the hint reveals or strongly telegraphs the final answer, or
   performs the calculation the student is supposed to perform themselves.
2. WRONG LEVEL — the hint doesn't match its tier:
   - Level 1: must be a single Socratic question only. No formulas, no method.
   - Level 2: names the relevant formula or concept and explains what it
     means, but does NOT substitute values or solve.
   - Level 3: writes out the formula with known values substituted but stops
     BEFORE the final calculation step.
3. IGNORES LAST MISTAKE — a `last_wrong_attempt` was provided and the hint
   makes no attempt to address what that specific attempt got wrong.
4. CONTRADICTS PRIOR HINT — directly contradicts something in `previous_hints`.

Approve otherwise. Be strict on SPOILER (highest severity); be lenient on
stylistic preferences. Encouragement, friendliness, and brevity are not your
concern.

Output STRICT JSON only:
{{
  "verdict": "approve" | "reject",
  "severity": "low" | "medium" | "high",
  "reasons": ["short phrase", ...]
}}

Severity guidance:
- high   → spoiler (final answer leaked, calculation performed)
- medium → wrong level (e.g. Level 1 hint names the formula)
- low    → ignores last mistake, contradicts prior hint, or borderline cases

`reasons` should have 1–3 short phrases. If verdict is "approve", reasons MUST
be an empty list. Do NOT emit prose outside the JSON object.
""".strip()


def _deployment() -> str:
    """Critic deployment falls back to the main one when unset."""
    return (
        settings.azure_openai_critic_deployment.strip()
        or settings.azure_openai_deployment
    )


def _approve_fallback(reason: str) -> dict:
    """Shape used when we fail open. `reason` is logged for telemetry."""
    return {
        "verdict": "approve",
        "severity": "low",
        "reasons": [],
        "_fail_open_reason": reason,
    }


def _build_user_message(
    question: str,
    subject: str,
    hint_level: int,
    hint_text: str,
    last_wrong_attempt: str | None,
    previous_hints: list[str] | None,
) -> str:
    parts = [
        f"Subject: {subject}",
        f"Hint level: {hint_level}/3",
        f"Question: {question}",
    ]
    if last_wrong_attempt:
        parts.append(f"Student's most recent wrong attempt: {last_wrong_attempt}")
    else:
        parts.append("Student's most recent wrong attempt: (none yet)")
    if previous_hints:
        rendered = "\n".join(f"- {h}" for h in previous_hints[-3:])
        parts.append(f"Previously delivered hints:\n{rendered}")
    parts.append(f"Candidate hint to judge:\n{hint_text}")
    return "\n\n".join(parts)


async def judge_hint(
    question: str,
    subject: str,
    hint_level: int,
    hint_text: str,
    last_wrong_attempt: str | None = None,
    previous_hints: list[str] | None = None,
) -> dict:
    """
    Run the critic on a candidate hint. Returns a dict with verdict, severity,
    and reasons. Never raises — `critic_fail_open` controls whether failures
    surface as "approve" (default) or "reject".

    Args:
        question: The homework question being tutored.
        subject: One of math / science / english / history / other.
        hint_level: 1, 2, or 3.
        hint_text: The hint draft to be judged.
        last_wrong_attempt: The student's most recent wrong answer text, if any.
        previous_hints: Hints already delivered in this session (cap to last 3
            internally).

    Returns:
        {"verdict": "approve" | "reject",
         "severity": "low" | "medium" | "high",
         "reasons": [...]}
        On fail-open path also includes `_fail_open_reason` (str) for telemetry.
    """
    if not hint_text or not hint_text.strip():
        # Empty hint never reaches the student in practice, but if it does,
        # flag it as a high-severity reject — nothing useful to deliver.
        return {
            "verdict": "reject",
            "severity": "high",
            "reasons": ["empty hint"],
        }

    user_msg = _build_user_message(
        question=question,
        subject=subject,
        hint_level=hint_level,
        hint_text=hint_text,
        last_wrong_attempt=last_wrong_attempt,
        previous_hints=previous_hints,
    )

    def _call_openai():
        return client.chat.completions.create(
            model=_deployment(),
            max_completion_tokens=200,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call_openai),
            timeout=settings.critic_timeout_seconds,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except asyncio.TimeoutError:
        logger.warning("Critic call timed out after %ss", settings.critic_timeout_seconds)
        if settings.critic_fail_open:
            return _approve_fallback("timeout")
        return {"verdict": "reject", "severity": "low",
                "reasons": ["critic timed out"]}
    except Exception as exc:
        logger.exception("Critic call failed: %s", exc)
        if settings.critic_fail_open:
            return _approve_fallback(f"error: {type(exc).__name__}")
        return {"verdict": "reject", "severity": "low",
                "reasons": [f"critic error: {type(exc).__name__}"]}

    verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    if verdict not in ("approve", "reject"):
        logger.warning("Critic returned unexpected verdict shape: %r", parsed)
        if settings.critic_fail_open:
            return _approve_fallback("malformed_json")
        return {"verdict": "reject", "severity": "low",
                "reasons": ["malformed critic response"]}

    severity = parsed.get("severity", "low")
    if severity not in ("low", "medium", "high"):
        severity = "low"

    reasons = parsed.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    # Normalise: drop empties, trim, cap to 3 short phrases.
    reasons = [str(r).strip()[:200] for r in reasons if str(r).strip()][:3]

    # An "approve" with non-empty reasons is contradictory — trust the verdict.
    if verdict == "approve":
        reasons = []

    return {"verdict": verdict, "severity": severity, "reasons": reasons}
