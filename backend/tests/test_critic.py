"""
Tests for the Socratic critic.

`judge_hint` parses the LLM JSON, applies fail-open behavior, and never raises.
We mock the AzureOpenAI client at the module boundary.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from config import settings
from services import critic


def _mock_response(text: str) -> MagicMock:
    """Shape that mimics openai's ChatCompletion response object."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.mark.asyncio
async def test_judge_hint_parses_approve():
    payload = {"verdict": "approve", "severity": "low", "reasons": []}
    with patch.object(critic.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await critic.judge_hint(
            question="Solve x + 4 = 19", subject="math", hint_level=1,
            hint_text="What number plus 4 gives 19?",
        )
    assert out["verdict"] == "approve"
    assert out["reasons"] == []


@pytest.mark.asyncio
async def test_judge_hint_parses_reject_with_reasons():
    payload = {
        "verdict": "reject",
        "severity": "high",
        "reasons": ["reveals final answer (15)", "performs the calculation"],
    }
    with patch.object(critic.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await critic.judge_hint(
            question="Solve x + 4 = 19", subject="math", hint_level=1,
            hint_text="x is 15.",
        )
    assert out["verdict"] == "reject"
    assert out["severity"] == "high"
    assert len(out["reasons"]) == 2


@pytest.mark.asyncio
async def test_judge_hint_empty_hint_rejects():
    """Don't even call the LLM for an empty hint."""
    with patch.object(critic.client.chat.completions, "create") as mock_create:
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=1, hint_text="",
        )
    assert out["verdict"] == "reject"
    assert out["severity"] == "high"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_judge_hint_fails_open_on_llm_error():
    with patch.object(critic.client.chat.completions, "create",
                      side_effect=RuntimeError("Azure is down")), \
         patch.object(settings, "critic_fail_open", True):
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=1, hint_text="some hint",
        )
    assert out["verdict"] == "approve"
    assert out["_fail_open_reason"].startswith("error:")


@pytest.mark.asyncio
async def test_judge_hint_fails_closed_when_configured():
    with patch.object(critic.client.chat.completions, "create",
                      side_effect=RuntimeError("Azure is down")), \
         patch.object(settings, "critic_fail_open", False):
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=1, hint_text="some hint",
        )
    assert out["verdict"] == "reject"


@pytest.mark.asyncio
async def test_judge_hint_fails_open_on_malformed_json():
    with patch.object(critic.client.chat.completions, "create",
                      return_value=_mock_response("not json at all")), \
         patch.object(settings, "critic_fail_open", True):
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=1, hint_text="x",
        )
    assert out["verdict"] == "approve"
    assert out["_fail_open_reason"] == "error: JSONDecodeError"


@pytest.mark.asyncio
async def test_judge_hint_fails_open_on_unexpected_shape():
    """LLM returned valid JSON but missing/bad `verdict` field."""
    payload = {"some_other_field": "value"}
    with patch.object(critic.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))), \
         patch.object(settings, "critic_fail_open", True):
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=1, hint_text="x",
        )
    assert out["verdict"] == "approve"
    assert out["_fail_open_reason"] == "malformed_json"


@pytest.mark.asyncio
async def test_judge_hint_caps_reasons_to_three():
    payload = {
        "verdict": "reject",
        "severity": "medium",
        "reasons": ["r1", "r2", "r3", "r4", "r5"],
    }
    with patch.object(critic.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=2, hint_text="hint",
        )
    assert len(out["reasons"]) == 3


@pytest.mark.asyncio
async def test_judge_hint_clears_reasons_on_approve():
    """A verdict of 'approve' with stray reasons trusts the verdict + drops the reasons."""
    payload = {"verdict": "approve", "severity": "low", "reasons": ["leftover"]}
    with patch.object(critic.client.chat.completions, "create",
                      return_value=_mock_response(json.dumps(payload))):
        out = await critic.judge_hint(
            question="q", subject="math", hint_level=1, hint_text="hint",
        )
    assert out["verdict"] == "approve"
    assert out["reasons"] == []


def test_deployment_falls_back_to_main():
    with patch.object(settings, "azure_openai_critic_deployment", ""), \
         patch.object(settings, "azure_openai_deployment", "gpt-4"):
        assert critic._deployment() == "gpt-4"


def test_deployment_uses_override_when_set():
    with patch.object(settings, "azure_openai_critic_deployment", "haiku-fast"), \
         patch.object(settings, "azure_openai_deployment", "gpt-4"):
        assert critic._deployment() == "haiku-fast"
