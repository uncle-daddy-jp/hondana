"""
llm_client.py — Unified LLM client for summary/tag generation and query expansion.

Supports: Claude (Anthropic), Groq (free tier), OpenAI-compatible (local vLLM/Ollama).
Answer generation uses generator.py (same provider support).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import anthropic
from groq import Groq, RateLimitError as GroqRateLimitError

from _llm_transport import claude_complete, openai_compat_complete
from constants import LLM_MIN_CALL_INTERVAL, LLM_SUMMARY_MAX_TOKENS


class LLMRateLimitError(Exception):
    """LLMプロバイダーがレート制限（429）を返した場合の例外。"""


# ── Factory ───────────────────────────────────────────────────────────────────


def llm_client_build(cfg: dict) -> _BaseLLMClient:
    """Return the summary/query-expansion LLM client based on config.

    llm_summary_* falls back to llm_answer_* when unset, so a local setup only needs to
    configure llm_answer_provider/model/url and summary+query-expansion reuse the same endpoint.
    """
    provider = (cfg.get("llm_summary_provider") or cfg.get("llm_answer_provider") or "claude").lower()
    model = cfg.get("llm_summary_model") or cfg.get("llm_answer_model") or "claude-haiku-4-5-20251001"

    if provider == "groq":
        return _GroqClient(model)
    if provider == "openai_compat":
        url = cfg.get("llm_summary_url") or cfg.get("llm_answer_url") or "http://localhost:8000"
        return _OpenAICompatClient(model, url)
    return _ClaudeClient(model)


# ── Shared interface ──────────────────────────────────────────────────────────


class _BaseLLMClient:
    def summarise(self, text: str, summary_chars: int, tag_count: int) -> tuple[str, list[str]]:
        """Generate summary and tags from document text in a single LLM call."""
        prompt = (
            f"以下の文章を読み、JSON形式で回答してください。\n"
            f'- "summary": {summary_chars}字以内の日本語要約\n'
            f'- "tags": 内容を表すキーワードを{tag_count}個以内のJSON配列（日本語または英語）\n\n'
            f"文章:\n{text[:4000]}\n\n"
            f"JSONのみを出力してください。説明文は不要です。"
        )
        raw = self._complete(prompt)
        return _parse_summary_response(raw, summary_chars)

    def expand_queries(self, question: str) -> list[str]:
        """Expand a user question into multiple search queries."""
        prompt = (
            f"以下の質問に対して、ベクトル検索で広くヒットさせるための検索クエリを生成してください。\n"
            f"- 日本語クエリ: 3種類（言い換え・同義語を含む）\n"
            f"- 英語クエリ: 2種類\n"
            f"JSON配列で出力してください（文字列のリストのみ）。\n\n"
            f"質問: {question}"
        )
        raw = self._complete(prompt)
        queries = _parse_json_list(raw)
        return queries if queries else [question]

    def _complete(self, prompt: str) -> str:
        raise NotImplementedError


# ── Claude client ─────────────────────────────────────────────────────────────


class _ClaudeClient(_BaseLLMClient):
    def __init__(self, model: str):
        self._model = model

    def _complete(self, prompt: str) -> str:
        try:
            return claude_complete(self._model, [{"role": "user", "content": prompt}], LLM_SUMMARY_MAX_TOKENS)
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError(str(exc)) from exc


# ── Groq client ───────────────────────────────────────────────────────────────


class _GroqClient(_BaseLLMClient):
    _lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self, model: str):
        self._client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
        self._model = model

    def _complete(self, prompt: str) -> str:
        with _GroqClient._lock:
            elapsed = time.monotonic() - _GroqClient._last_request_at
            if elapsed < LLM_MIN_CALL_INTERVAL:
                time.sleep(LLM_MIN_CALL_INTERVAL - elapsed)
            _GroqClient._last_request_at = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=LLM_SUMMARY_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content
        except GroqRateLimitError as exc:
            raise LLMRateLimitError(str(exc)) from exc


# ── OpenAI-compatible client ──────────────────────────────────────────────────


class _OpenAICompatClient(_BaseLLMClient):
    def __init__(self, model: str, base_url: str):
        self._model = model
        self._base_url = base_url

    def _complete(self, prompt: str) -> str:
        return openai_compat_complete(
            self._base_url,
            self._model,
            [{"role": "user", "content": prompt}],
            LLM_SUMMARY_MAX_TOKENS,
            timeout=120,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )


# ── Response parsers ──────────────────────────────────────────────────────────


def _strip_code_fence(raw: str) -> str:
    """Remove Markdown code fences (```json ... ``` or ``` ... ```) from LLM output."""
    return re.sub(r"```(?:json)?|```", "", raw).strip()


def _parse_summary_response(raw: str, max_chars: int) -> tuple[str, list[str]]:
    """Extract summary and tags from LLM JSON response."""
    try:
        data = json.loads(_strip_code_fence(raw))
        summary = str(data.get("summary", ""))[:max_chars]
        tags = [str(t) for t in data.get("tags", [])]
        return summary, tags
    except (json.JSONDecodeError, AttributeError):
        return raw.strip()[:max_chars], []


def _parse_json_list(raw: str) -> list[str]:
    """Extract a JSON string list from LLM output."""
    try:
        result = json.loads(_strip_code_fence(raw))
        if isinstance(result, list):
            return [str(x) for x in result]
    except (json.JSONDecodeError, ValueError):
        pass
    return []
