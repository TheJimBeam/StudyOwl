"""
Hint engine — the core of StudyOwl.

Calls the Claude API to generate a Socratic hint at the appropriate level.
Never reveals the direct answer before Level 3 is exhausted.

Hint levels:
  1 — Socratic question only. No method hints.
  2 — Points to the relevant formula or concept. No solving.
  3 — Near-answer with all values filled in. Student must do the final step.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from openai import AzureOpenAI
from config import settings
from . import answer_verifier

client = AzureOpenAI(
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
- If a "Knowledge graph" section is provided in the user message, lead with a probing question that surfaces one of those weaknesses — but only if it's relevant to the current question. Otherwise ignore it.
"""


def _render_prior_concepts(prior_concepts: list[dict] | None) -> str | None:
    """
    Render the knowledge-graph context block for the hint prompt. Returns
    None when there are no weak concepts to surface so the caller can skip
    the section entirely.
    """
    if not prior_concepts:
        return None
    now = datetime.now(timezone.utc)
    lines = []
    for c in prior_concepts:
        last_seen_raw = c.get("last_seen")
        days_ago_str = ""
        if last_seen_raw:
            try:
                last_seen = datetime.fromisoformat(last_seen_raw)
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                days = max(0, int((now - last_seen).total_seconds() // 86400))
                days_ago_str = f"last seen {days}d ago, "
            except (TypeError, ValueError):
                pass
        decayed = c.get("decayed_confidence", c.get("confidence", 0.0))
        status = c.get("status", "struggling")
        label = c.get("label") or c.get("concept", "")
        lines.append(
            f"- {label} ({days_ago_str}confidence {decayed:.2f}) — {status}"
        )
    rendered = "\n".join(lines)
    return (
        "Knowledge graph — this student has shown weakness recently:\n"
        f"{rendered}\n\n"
        "If your hint can probe one of these weaknesses, prioritise that — "
        "but never sacrifice fit to the current question."
    )


def _build_hint_user_message(
    question: str,
    previous_hints: list[str],
    previous_attempts: list[str],
    previous_clarifications: list[tuple[str, str]] | None = None,
    prior_concepts: list[dict] | None = None,
) -> str:
    """
    Build the user-message body for a hint request, including the conversation
    history. Capped at `settings.conversation_history_limit` entries per kind
    so prompts can't balloon on long sessions.

    `previous_clarifications` is a list of (student_question, ai_clarification)
    pairs — passed only by the clarify path (PR 8).
    `prior_concepts` is the student's weakest knowledge-graph concepts, fed in
    so the LLM can probe known weaknesses (see memory_agent.get_review_concepts).
    """
    limit = settings.conversation_history_limit
    parts = [f"Question: {question}"]

    # Cap each list to the last N entries (most recent are most relevant).
    hints_tail = previous_hints[-limit:] if previous_hints else []
    attempts_tail = previous_attempts[-limit:] if previous_attempts else []
    clar_tail = (
        (previous_clarifications or [])[-limit:]
    )

    if hints_tail:
        rendered = "\n".join(f"{i}. {h}" for i, h in enumerate(hints_tail, 1))
        parts.append(
            "Hints you have ALREADY given to the student. "
            "Do NOT repeat or paraphrase any of these — build on them:\n"
            f"{rendered}"
        )

    if clar_tail:
        rendered = "\n".join(
            f"{i}. Student asked: {q}\n   You clarified: {a}"
            for i, (q, a) in enumerate(clar_tail, 1)
        )
        parts.append(
            "Clarifying exchanges that have already happened. "
            "Do not contradict yourself:\n"
            f"{rendered}"
        )

    if attempts_tail:
        rendered = "\n".join(f"- {a}" for a in attempts_tail)
        parts.append(f"Student's previous wrong attempts:\n{rendered}")
    else:
        parts.append("Student's previous wrong attempts: None yet.")

    kg_section = _render_prior_concepts(prior_concepts)
    if kg_section:
        parts.append(kg_section)

    return "\n\n".join(parts)


async def get_hint(
    question: str,
    subject: str,
    level: int,
    previous_attempts: list[str],
    previous_hints: list[str] | None = None,
    previous_clarifications: list[tuple[str, str]] | None = None,
    prior_concepts: list[dict] | None = None,
) -> str:
    """
    Generate a Socratic hint for the given question at the specified hint level.

    Args:
        question: The original homework question.
        subject: Classified subject area (math, science, english, history, other).
        level: Current hint level (1, 2, or 3).
        previous_attempts: List of the student's previous wrong attempts.
        previous_hints: Hints already shown for this session. The AI is told
            explicitly not to repeat or paraphrase them. Default empty list.
        previous_clarifications: (student_question, ai_clarification) pairs from
            the clarify path. Threaded into the prompt so the AI is consistent
            across hints and clarifications.
        prior_concepts: Weak concepts from the knowledge-graph memory, used to
            probe known weaknesses across sessions.

    Returns:
        A hint string from AzureOpenAI, appropriate to the hint level.
    """
    user_message = _build_hint_user_message(
        question=question,
        previous_hints=previous_hints or [],
        previous_attempts=previous_attempts,
        previous_clarifications=previous_clarifications,
        prior_concepts=prior_concepts,
    )

    # Wrap sync OpenAI call in thread pool to avoid blocking event loop
    def _call_openai():
        return client.chat.completions.create(
            model=settings.azure_openai_deployment,
            max_completion_tokens=300,
            messages=[
                {"role": "system", "content": HINT_SYSTEM.format(level=level, subject=subject)},
                {"role": "user", "content": user_message},
            ],
        )

    response = await asyncio.to_thread(_call_openai)
    return response.choices[0].message.content.strip()


CLARIFY_SYSTEM = """
You are StudyOwl, a Socratic homework assistant. The student has a follow-up
question about your most recent hint. Clarify the concept they are asking
about WITHOUT giving away the answer to the homework question and WITHOUT
revealing the next hint level. Keep it short and conceptual.

Subject area: {subject}
Current hint level: {level}/3 — do not advance past this level.

Rules:
- Maximum 3 sentences.
- Do NOT solve or partially solve the homework question.
- Do NOT name the formula or method if we're still at Level 1.
- End with one short encouraging sentence.
"""


async def clarify(
    question: str,
    subject: str,
    level: int,
    student_message: str,
    previous_hints: list[str],
    previous_attempts: list[str],
    previous_clarifications: list[tuple[str, str]],
    prior_concepts: list[dict] | None = None,
) -> str:
    """
    Answer a student's clarifying question about the current hint without
    advancing them or revealing the homework answer.
    """
    history = _build_hint_user_message(
        question=question,
        previous_hints=previous_hints,
        previous_attempts=previous_attempts,
        previous_clarifications=previous_clarifications,
        prior_concepts=prior_concepts,
    )
    user_message = (
        f"{history}\n\n"
        f"The student is now asking a clarifying question (NOT submitting an answer):\n"
        f"{student_message}\n\n"
        f"Clarify the concept they're confused about. Stay at Level {level}; "
        f"do not reveal the answer."
    )

    def _call_openai():
        return client.chat.completions.create(
            model=settings.azure_openai_deployment,
            max_completion_tokens=250,
            messages=[
                {"role": "system", "content": CLARIFY_SYSTEM.format(level=level, subject=subject)},
                {"role": "user", "content": user_message},
            ],
        )

    response = await asyncio.to_thread(_call_openai)
    return response.choices[0].message.content.strip()


async def stream_hint(
    question: str,
    subject: str,
    level: int,
    previous_attempts: list[str],
    previous_hints: list[str] | None = None,
    previous_clarifications: list[tuple[str, str]] | None = None,
    prior_concepts: list[dict] | None = None,
) -> AsyncIterator[str]:
    """
    Stream a hint as text chunks (token-ish granularity from Azure OpenAI).

    Why a thread-pool bridge: the `openai` package's sync `Stream` object is
    iterated synchronously. Iterating it on the event loop blocks. We pull
    from it in a dedicated thread and forward chunks via an asyncio.Queue.

    Cancellation: the caller (FastAPI's StreamingResponse generator) closes
    this generator on client disconnect, which raises GeneratorExit. We catch
    it, signal the producer thread via the queue sentinel, and let the upstream
    Stream finalize on its own (the openai client closes its HTTP connection
    when the iterator goes out of scope).
    """
    user_message = _build_hint_user_message(
        question=question,
        previous_hints=previous_hints or [],
        previous_attempts=previous_attempts,
        previous_clarifications=previous_clarifications,
        prior_concepts=prior_concepts,
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=64)
    loop = asyncio.get_running_loop()
    cancel = asyncio.Event()

    def _producer():
        try:
            stream = client.chat.completions.create(
                model=settings.azure_openai_deployment,
                max_completion_tokens=300,
                stream=True,
                messages=[
                    {"role": "system", "content": HINT_SYSTEM.format(level=level, subject=subject)},
                    {"role": "user", "content": user_message},
                ],
            )
            for chunk in stream:
                if cancel.is_set():
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    asyncio.run_coroutine_threadsafe(queue.put(text), loop).result()
        finally:
            # Sentinel: tells the consumer we're done (either naturally or on cancel).
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    producer_task = asyncio.create_task(asyncio.to_thread(_producer))

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    except GeneratorExit:
        # Client disconnected — tell the producer to stop and bail out.
        cancel.set()
        raise
    finally:
        # Ensure the thread wraps up so we don't leak it.
        try:
            await asyncio.wait_for(producer_task, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass


async def get_direct_answer(question: str, subject: str) -> str:
    """
    Return the direct correct answer for a homework question after all hints are exhausted.

    For math, use symbolic solving when possible. For other subjects, fall back to the model.
    """
    if subject == "math":
        direct_answer = answer_verifier.solve_math_question(question)
        if direct_answer:
            return direct_answer

    # Wrap sync OpenAI call in thread pool to avoid blocking event loop
    def _call_openai():
        return client.chat.completions.create(
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

    response = await asyncio.to_thread(_call_openai)
    return response.choices[0].message.content.strip()


async def detect_distress(message: str) -> bool:
    """
    Use Claude to detect if a student message signals genuine distress.
    Returns True if the student should trigger a teacher alert immediately.
    """
    # Wrap sync OpenAI call in thread pool to avoid blocking event loop
    def _call_openai():
        return client.chat.completions.create(
            model=settings.azure_openai_deployment,
            max_completion_tokens=10,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You detect student distress in homework messages. "
                        "Reply with only 'yes' or 'no'. "
                        "Examples of distress: 'I give up', 'I don't understand anything', "
                        "'this makes no sense', 'I hate this', 'I can't do this'."
                    ),
                },
                {"role": "user", "content": message},
            ],
        )

    response = await asyncio.to_thread(_call_openai)
    return response.choices[0].message.content.strip().lower() == "yes"
