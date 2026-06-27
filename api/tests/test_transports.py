"""Unit tests for the shared LLM/embedding transports and the generator provider dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import _llm_transport
import generator
from ingest import embed_client
from ingest.llm_client import _ClaudeClient, _OpenAICompatClient, llm_client_build

# ── _llm_transport.openai_compat_complete ─────────────────────────────────────


def test_openai_compat_complete_builds_request_and_unwraps():
    with patch.object(_llm_transport.requests, "post") as mock_post:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": "hello"}}]}
        mock_post.return_value = resp

        out = _llm_transport.openai_compat_complete(
            "http://host:8000/",
            "m",
            [{"role": "user", "content": "hi"}],
            100,
            timeout=120,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    assert out == "hello"
    _, kwargs = mock_post.call_args
    assert mock_post.call_args[0][0] == "http://host:8000/v1/chat/completions"  # trailing slash collapsed
    body = kwargs["json"]
    assert body["model"] == "m"
    assert body["max_tokens"] == 100
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert kwargs["timeout"] == 120


# ── _llm_transport.claude_complete ────────────────────────────────────────────


def test_claude_complete_builds_call_and_unwraps():
    fake_client = MagicMock()
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(text="claude-out")]
    fake_client.messages.create.return_value = fake_msg

    with patch.object(_llm_transport.anthropic, "Anthropic", return_value=fake_client):
        out = _llm_transport.claude_complete("model-x", [{"role": "user", "content": "q"}], 50, system="sys")

    assert out == "claude-out"
    _, kwargs = fake_client.messages.create.call_args
    assert kwargs["model"] == "model-x"
    assert kwargs["max_tokens"] == 50
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "q"}]


# ── generator provider dispatch ───────────────────────────────────────────────


def test_generator_answer_openai_compat_requires_url():
    with pytest.raises(ValueError):
        generator.generator_answer("q", [], "m", provider="openai_compat", url=None)


def test_generator_answer_claude_path_passes_system():
    with patch.object(generator, "claude_complete", return_value="cans") as m:
        out = generator.generator_answer("q", [], "haiku", provider="claude")
    assert out == "cans"
    args, kwargs = m.call_args
    assert args[0] == "haiku"
    assert kwargs.get("system")  # the RAG system prompt is forwarded


def test_generator_openai_compat_thinking_budget_extends_max_tokens():
    with patch.object(generator, "openai_compat_complete", return_value="ans") as m:
        out = generator.generator_answer(
            "q", [], "m", provider="openai_compat", url="http://h:8000", thinking_budget=512
        )
    assert out == "ans"
    args, kwargs = m.call_args
    assert args[0] == "http://h:8000"
    assert args[3] == generator.GENERATOR_MAX_TOKENS + 512  # budget added to max_tokens
    assert kwargs["timeout"] == 180
    assert kwargs["extra_body"]["thinking_budget_tokens"] == 512
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


# ── embed_client.embed_texts ──────────────────────────────────────────────────


def test_embed_texts_truncates_and_l2_normalizes():
    with patch.object(embed_client.requests, "post") as mock_post:
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"data": [{"index": 0, "embedding": [3.0, 4.0]}]}
        mock_post.return_value = resp

        arr = embed_client.embed_texts("http://h:8001", "bge", ["x" * 10000], normalize=True, max_chars=8000)

    assert abs(float(arr[0][0]) - 0.6) < 1e-5  # [3,4] -> [0.6, 0.8]
    assert abs(float(arr[0][1]) - 0.8) < 1e-5
    sent = mock_post.call_args[1]["json"]["input"][0]
    assert len(sent) == 8000  # truncated to max_chars


def test_embed_texts_blank_becomes_space_without_stripping_nonblank():
    with patch.object(embed_client.requests, "post") as mock_post:
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0]}]}
        mock_post.return_value = resp

        embed_client.embed_texts("http://h:8001", "bge", ["   ", "  keep me  "], normalize=False, strip=False)

    inputs = mock_post.call_args[1]["json"]["input"]
    assert inputs[0] == " "  # blank -> single space
    assert inputs[1] == "  keep me  "  # non-blank preserved verbatim (no strip)


# ── llm_client_build summary<-answer inheritance ──────────────────────────────


def test_summary_client_inherits_answer_when_unset():
    cfg = {"llm_answer_provider": "openai_compat", "llm_answer_model": "local-x", "llm_answer_url": "http://h:8000"}
    client = llm_client_build(cfg)
    assert isinstance(client, _OpenAICompatClient)
    assert client._base_url == "http://h:8000"
    assert client._model == "local-x"


def test_summary_client_explicit_overrides_answer():
    cfg = {
        "llm_answer_provider": "openai_compat",
        "llm_answer_model": "local-x",
        "llm_answer_url": "http://h:8000",
        "llm_summary_provider": "openai_compat",
        "llm_summary_model": "sum-y",
        "llm_summary_url": "http://s:9000",
    }
    client = llm_client_build(cfg)
    assert isinstance(client, _OpenAICompatClient)
    assert client._base_url == "http://s:9000"
    assert client._model == "sum-y"


def test_summary_client_defaults_to_claude_when_nothing_set():
    assert isinstance(llm_client_build({}), _ClaudeClient)
