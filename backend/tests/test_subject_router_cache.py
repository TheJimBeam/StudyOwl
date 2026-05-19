import pytest

from services import subject_router


class _FakeMessage:
    def __init__(self, content): self.content = content


class _FakeChoice:
    def __init__(self, content): self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content): self.choices = [_FakeChoice(content)]


class _Recorder:
    """Stand-in for `async_client.chat.completions` that counts invocations."""
    def __init__(self, response_text="english"):
        self.calls = 0
        self.response_text = response_text

    async def create(self, **_kwargs):
        self.calls += 1
        return _FakeResponse(self.response_text)


@pytest.fixture(autouse=True)
def _reset_cache():
    subject_router._subject_cache.clear()
    yield
    subject_router._subject_cache.clear()


@pytest.mark.asyncio
async def test_math_regex_short_circuits_without_llm_call(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subject_router.async_client.chat, "completions", recorder)

    assert await subject_router.classify("Solve 2x + 3 = 7") == "math"
    assert await subject_router.classify("What is 12 * 5?") == "math"
    assert recorder.calls == 0


@pytest.mark.asyncio
async def test_history_regex_short_circuits_without_llm_call(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subject_router.async_client.chat, "completions", recorder)

    assert await subject_router.classify("What year did WWII end?") == "history"
    assert await subject_router.classify("Who was Marie Curie?") == "history"
    assert recorder.calls == 0


@pytest.mark.asyncio
async def test_llm_path_caches_repeated_questions(monkeypatch):
    recorder = _Recorder(response_text="english")
    monkeypatch.setattr(subject_router.async_client.chat, "completions", recorder)

    question = "Analyze the symbolism of the green light in The Great Gatsby"
    first = await subject_router.classify(question)
    second = await subject_router.classify(question)
    # Case-insensitive normalization should also hit the cache.
    third = await subject_router.classify(question.upper())

    assert first == "english"
    assert second == "english"
    assert third == "english"
    assert recorder.calls == 1


@pytest.mark.asyncio
async def test_llm_path_falls_back_to_other_on_garbage_response(monkeypatch):
    recorder = _Recorder(response_text="🦉")
    monkeypatch.setattr(subject_router.async_client.chat, "completions", recorder)

    assert await subject_router.classify(
        "What is the capital of an emotion?"
    ) == "other"
    assert recorder.calls == 1
