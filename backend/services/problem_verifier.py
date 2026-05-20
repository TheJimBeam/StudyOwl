"""
Problem verifier — adversarial sub-agent that judges a generated problem.

Two paths:
  * Math   — SymPy symbolically solves the prompt and compares against the
             LLM-provided answer_key. Falls through to rubric-LLM when the
             prompt isn't parseable (e.g. word problems with no clean
             expression form).
  * Other  — rubric-LLM judge (JSON mode). Asks two questions: is the
             problem well-formed, and does the answer_key actually solve it.

Fail-closed: on timeout / LLM error / malformed JSON we return
verified=False so a misbehaving verifier never silently approves a bad
problem (the practice agent retries, then persists `unverified` if the
retry budget is exhausted — see practice_agent.generate_verified_problem).

check_student_answer is a separate entry point used at attempt time. It
compares the student's answer against the canonical answer_key (NOT by
re-solving the prompt), which is cheaper and more reliable.
"""

import asyncio
import json
import logging

import sympy
from openai import AzureOpenAI
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

from config import settings
from models.generated_problem import (
    VERIFIER_KIND_NONE,
    VERIFIER_KIND_RUBRIC,
    VERIFIER_KIND_SYMPY,
)
from . import answer_verifier


logger = logging.getLogger(__name__)


client = AzureOpenAI(
    api_key=settings.azure_openai_api_key,
    api_version="2024-10-01-preview",
    azure_endpoint=settings.azure_openai_endpoint,
)


_SYMPY_TRANSFORMS = standard_transformations + (implicit_multiplication_application,)


RUBRIC_SYSTEM = """
You are StudyOwl's practice-problem verifier. Judge whether a generated
problem is safe to deliver to a student. Output STRICT JSON only.

Output shape:
{{
  "well_formed": true | false,
  "answer_correct": true | false,
  "notes": ["short phrase", ...]
}}

Approve (well_formed=true AND answer_correct=true) only if BOTH:
1. The problem is unambiguous, self-contained, on-topic for the subject,
   and solvable as written.
2. The provided answer_key is the canonical correct answer to the problem.
   (Trivial equivalences are fine: "1/2" and "0.5", "x=3" and "3". Anything
   that requires interpretation goes in `notes` and you reject.)

`notes` should be 1–3 short phrases. If both checks pass, notes MUST be an
empty list. Never emit prose outside the JSON object.

Subject area: {subject}
""".strip()


# ── Math path ─────────────────────────────────────────────────────────────────


def _parse_math_or_none(text: str):
    """Try to parse `text` as a SymPy expression. Returns the expression or None."""
    try:
        return parse_expr(text.strip(), transformations=_SYMPY_TRANSFORMS)
    except (sympy.SympifyError, ValueError, TypeError, SyntaxError):
        return None


def _answers_equivalent(a, b) -> bool:
    """SymPy-based equivalence: numeric within 1e-9, else symbolic simplify."""
    try:
        a_val = float(sympy.simplify(a))
        b_val = float(sympy.simplify(b))
        return abs(a_val - b_val) < 1e-9
    except (TypeError, ValueError):
        try:
            return sympy.simplify(a - b) == 0
        except (TypeError, ValueError):
            return False


def _strip_answer_prefix(text: str) -> str:
    """Same defensive cleanup as answer_verifier._verify_math."""
    cleaned = text.strip()
    for prefix in [
        "a =", "a=", "x =", "x=", "y =", "y=", "z =", "z=",
        "Answer:", "answer:", "The answer is",
    ]:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned


def verify_math_problem(prompt: str, answer_key: str) -> dict:
    """
    Symbolically solve the prompt and check that the LLM's answer_key matches.

    Returns:
        {"verified": bool, "kind": "sympy" | "none", "notes": [...]}
    On unparseable prompts we return kind="none" so the caller knows to fall
    back to the rubric-LLM verifier.
    """
    # Reuse answer_verifier's natural-language → expression extraction so we
    # don't duplicate the regex/cleanup table.
    solution_text = answer_verifier.solve_math_question(prompt)
    if solution_text is None:
        return {
            "verified": False,
            "kind": VERIFIER_KIND_NONE,
            "notes": ["sympy could not parse the prompt"],
        }

    answer_clean = _strip_answer_prefix(answer_key)
    expected = _parse_math_or_none(answer_clean)
    if expected is None:
        return {
            "verified": False,
            "kind": VERIFIER_KIND_SYMPY,
            "notes": ["answer_key is not a parseable expression"],
        }

    # `solve_math_question` returns comma-joined solutions when there are
    # multiple roots — accept any of them as a match.
    for candidate_text in [c.strip() for c in solution_text.split(",") if c.strip()]:
        candidate = _parse_math_or_none(candidate_text)
        if candidate is None:
            continue
        if _answers_equivalent(candidate, expected):
            return {
                "verified": True,
                "kind": VERIFIER_KIND_SYMPY,
                "notes": [],
            }
    return {
        "verified": False,
        "kind": VERIFIER_KIND_SYMPY,
        "notes": [f"answer_key does not match sympy solution ({solution_text})"],
    }


# ── Rubric-LLM path ───────────────────────────────────────────────────────────


async def verify_with_rubric(prompt: str, answer_key: str, subject: str) -> dict:
    """
    JSON-mode LLM judge. Fail-closed: any infrastructure failure returns
    verified=False so the practice agent retries instead of trusting a
    silent approval.
    """
    system_msg = RUBRIC_SYSTEM.format(subject=subject)
    user_msg = (
        f"Subject: {subject}\n"
        f"Problem:\n{prompt}\n\n"
        f"Provided answer_key:\n{answer_key}\n\n"
        "Judge it now. JSON only."
    )

    def _call_openai():
        return client.chat.completions.create(
            model=settings.azure_openai_deployment,
            max_completion_tokens=250,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call_openai),
            timeout=settings.practice_verifier_timeout_seconds,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except asyncio.TimeoutError:
        logger.warning(
            "Rubric verifier timed out after %ss",
            settings.practice_verifier_timeout_seconds,
        )
        return {
            "verified": False,
            "kind": VERIFIER_KIND_RUBRIC,
            "notes": ["verifier timed out"],
        }
    except Exception as exc:
        logger.exception("Rubric verifier call failed: %s", exc)
        return {
            "verified": False,
            "kind": VERIFIER_KIND_RUBRIC,
            "notes": [f"verifier error: {type(exc).__name__}"],
        }

    if not isinstance(parsed, dict):
        return {
            "verified": False,
            "kind": VERIFIER_KIND_RUBRIC,
            "notes": ["malformed verifier response"],
        }

    well_formed = bool(parsed.get("well_formed"))
    answer_correct = bool(parsed.get("answer_correct"))
    notes_raw = parsed.get("notes") or []
    if not isinstance(notes_raw, list):
        notes_raw = [str(notes_raw)]
    notes = [str(n).strip()[:200] for n in notes_raw if str(n).strip()][:3]

    return {
        "verified": well_formed and answer_correct,
        "kind": VERIFIER_KIND_RUBRIC,
        "notes": notes,
    }


# ── Dispatcher ────────────────────────────────────────────────────────────────


async def verify_problem(prompt: str, answer_key: str, subject: str) -> dict:
    """
    Pick the right verifier for the subject. Math gets SymPy first; if SymPy
    can't parse the prompt we fall through to the rubric-LLM. Everything else
    goes straight to rubric-LLM.
    """
    if subject == "math":
        math_result = verify_math_problem(prompt, answer_key)
        if math_result["kind"] == VERIFIER_KIND_SYMPY:
            # SymPy could reason about this prompt — trust its verdict
            # whether positive or negative.
            return math_result
        # SymPy couldn't parse → defer to the rubric-LLM judge.
        rubric_result = await verify_with_rubric(prompt, answer_key, subject)
        # Carry forward the sympy-couldn't-parse note for telemetry.
        rubric_result["notes"] = (
            ["sympy fell through"] + rubric_result.get("notes", [])
        )[:4]
        return rubric_result

    return await verify_with_rubric(prompt, answer_key, subject)


# ── Student-attempt check ─────────────────────────────────────────────────────


async def check_student_answer(
    prompt: str,
    answer_key: str,
    student_answer: str,
    subject: str,
) -> bool:
    """
    Compare a student's submission against the canonical answer_key.

    Math → SymPy equivalence on the two expressions. Non-math → Claude
    rubric (reusing answer_verifier._verify_with_claude). Falls through to
    Claude when SymPy can't parse either side.
    """
    if not student_answer or not student_answer.strip():
        return False

    if subject == "math":
        student_clean = _strip_answer_prefix(student_answer)
        key_clean = _strip_answer_prefix(answer_key)
        student_expr = _parse_math_or_none(student_clean)
        key_expr = _parse_math_or_none(key_clean)
        if student_expr is not None and key_expr is not None:
            return _answers_equivalent(student_expr, key_expr)
        # One side unparseable — fall through to the LLM grader.

    # Reuse the existing Claude-based grader. We frame it as "is the
    # student's answer equivalent to this canonical answer?" rather than
    # asking the model to re-solve the prompt.
    grader_question = (
        f"Question: {prompt}\n\n"
        f"Canonical correct answer: {answer_key}"
    )
    return await answer_verifier._verify_with_claude(  # noqa: SLF001
        grader_question, student_answer, subject
    )
