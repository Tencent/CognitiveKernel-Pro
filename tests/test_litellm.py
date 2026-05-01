"""Unit tests for the LiteLLM `call_target` integration in `ck_pro.agents.model`.

Run with:
    pytest tests/test_litellm.py -v
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow `import ck_pro.agents.model` when tests are run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ck_pro.agents.model import LLM, LiteLLMHelper  # noqa: E402


def _fake_litellm_response(content: str, **extra: object) -> MagicMock:
    """Build an OpenAI-shaped mock that mimics litellm.completion's return."""
    response = MagicMock()
    response.model_dump.return_value = {
        "choices": [
            {
                "message": {"content": content, **extra},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return response


def test_get_call_target_type_recognizes_litellm() -> None:
    llm = LLM(call_target="litellm:anthropic/claude-sonnet-4-6")
    assert llm.get_call_target_type() == "litellm"


def test_get_call_target_type_unknown_still_raises() -> None:
    with pytest.raises(RuntimeError, match="UNK call_target"):
        LLM(call_target="bogus-prefix:model-name")


def test_litellm_helper_extracts_content() -> None:
    response = _fake_litellm_response("hello from litellm")
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = MagicMock(return_value=response)

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = LiteLLMHelper.call_chat(
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-sonnet-4-6",
        )

    assert result == "hello from litellm"
    fake_litellm.completion.assert_called_once()
    call_kwargs = fake_litellm.completion.call_args.kwargs
    assert call_kwargs["model"] == "anthropic/claude-sonnet-4-6"
    # drop_params=True is on by default to mirror existing OpenaiHelper tolerance
    assert call_kwargs["drop_params"] is True


def test_litellm_helper_falls_back_to_reasoning_content() -> None:
    """Reasoning models (o1, GPT-5 thinking, Claude extended-thinking) sometimes
    return empty `content` and put the answer in `reasoning_content`."""
    response = MagicMock()
    response.model_dump.return_value = {
        "choices": [
            {
                "message": {"content": "", "reasoning_content": "the actual reply"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = MagicMock(return_value=response)

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = LiteLLMHelper.call_chat(
            messages=[{"role": "user", "content": "x"}],
            model="openai/o1",
        )

    assert result == "the actual reply"


def test_litellm_helper_raises_on_empty_response() -> None:
    response = MagicMock()
    response.model_dump.return_value = {
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        "usage": {},
    }
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = MagicMock(return_value=response)

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        with pytest.raises(RuntimeError, match="empty response"):
            LiteLLMHelper.call_chat(
                messages=[{"role": "user", "content": "x"}],
                model="openai/gpt-4o",
            )


def test_litellm_helper_forwards_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LITELLM_API_KEY` / `LITELLM_API_BASE` from env should reach litellm.completion."""
    monkeypatch.setenv("LITELLM_API_KEY", "proxy-key-xyz")
    monkeypatch.setenv("LITELLM_API_BASE", "http://localhost:4000")

    response = _fake_litellm_response("ok")
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = MagicMock(return_value=response)

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        LiteLLMHelper.call_chat(
            messages=[{"role": "user", "content": "ping"}],
            model="anthropic/claude-sonnet-4-6",
        )

    call_kwargs = fake_litellm.completion.call_args.kwargs
    assert call_kwargs["api_key"] == "proxy-key-xyz"
    assert call_kwargs["api_base"] == "http://localhost:4000"


@pytest.fixture
def stubbed_llm_init(monkeypatch: pytest.MonkeyPatch):
    """Bypass MessageTruncator's HF tokenizer download + the rich-markup
    `Finish init` rprint that fails on bracket-heavy model names."""
    import ck_pro.agents.model as model_mod

    monkeypatch.setattr(model_mod, "MessageTruncator", lambda *a, **kw: object())
    monkeypatch.setattr(model_mod, "rprint", lambda *a, **kw: None)


def test_llm_call_target_type_routes_to_litellm_branch(stubbed_llm_init) -> None:
    """The dispatcher inside `_call_with_messages` should pick the litellm branch."""
    llm = LLM(
        call_target="litellm:anthropic/claude-sonnet-4-6",
        print_call_in=None,
        print_call_out=None,
        max_retry_times=0,
    )

    with patch(
        "ck_pro.agents.model.LiteLLMHelper.call_chat",
        return_value="dispatched",
    ) as helper:
        result = llm._call_with_messages([{"role": "user", "content": "hi"}])

    assert result == "dispatched"
    helper.assert_called_once()
    kwargs = helper.call_args.kwargs
    assert kwargs["model"] == "anthropic/claude-sonnet-4-6"


def test_llm_litellm_dispatch_passes_call_kwargs(stubbed_llm_init) -> None:
    """temperature/top_p/max_tokens defaults (set on LLM) should reach the helper."""
    llm = LLM(
        call_target="litellm:openai/gpt-4o",
        print_call_in=None,
        print_call_out=None,
        call_kwargs={"temperature": 0.7, "max_tokens": 256},
        max_retry_times=0,
    )

    with patch(
        "ck_pro.agents.model.LiteLLMHelper.call_chat",
        return_value="ok",
    ) as helper:
        llm._call_with_messages([{"role": "user", "content": "hi"}])

    kwargs = helper.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 256
    assert kwargs["model"] == "openai/gpt-4o"
