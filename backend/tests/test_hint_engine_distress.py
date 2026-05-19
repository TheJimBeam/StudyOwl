import pytest

from services.hint_engine import detect_distress


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "I give up",
        "i give up.",
        "I  give    up",  # collapsed whitespace
        "I QUIT",
        "i'm done with this",
        "im done",
        "I hate this",
        "I can't do this",
        "I cant do it",
        "I don't understand anything",
        "this makes no sense",
        "this is impossible",
        "too hard",
        "I'm so stuck",
    ],
)
async def test_detect_distress_triggers_on_known_phrases(message):
    assert await detect_distress(message) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "",
        "x = 5",
        "Is the answer 12?",
        "Can you give me a hint?",
        "I think it's the quadratic formula",
        "this is interesting",
    ],
)
async def test_detect_distress_ignores_neutral_messages(message):
    assert await detect_distress(message) is False
