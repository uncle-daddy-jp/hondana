"""
generator.py — Answer generation from retrieved chunks.

Supports: Claude (Anthropic), OpenAI-compatible (local vLLM/Ollama).
"""

from __future__ import annotations

from _llm_transport import claude_complete, openai_compat_complete
from constants import GENERATOR_MAX_TOKENS

# ── Public API ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "あなたは知識ベースを参照して質問に答えるアシスタントです。\n"
    "各情報ソースには記録日（角括弧内に表示）が含まれます。\n"
    "複数の情報源に同じトピックがある場合は、記録日が新しいものを優先してください。\n"
    "価格・仕様・手法など更新されやすい情報は新しいものを正とし、"
    "変化がある場合はその変化に言及してください。"
)


def generator_answer(
    question: str,
    chunks: list[dict],
    model: str,
    *,
    provider: str = "claude",
    url: str | None = None,
    thinking_budget: int = 0,
) -> str:
    """
    Generate an answer from retrieved chunks.
    chunks: list of dicts from retriever_search (level, title, heading, text, source_url, tags)
    provider: "claude" or "openai_compat"
    url: base URL for openai_compat provider (e.g. "http://localhost:8000")
    thinking_budget: openai_compat(llama.cpp)で思考を効かせる際の reasoning トークン上限。0 で従来通り(指定なし)。
    """
    context = _generator_build_context(chunks)
    prompt = (
        f"以下の参考情報をもとに、質問に日本語で回答してください。\n"
        f"参考情報にない内容は回答に含めないでください。\n\n"
        f"参考情報:\n{context}\n\n"
        f"質問: {question}"
    )
    if provider == "openai_compat":
        if not url:
            raise ValueError("llm_answer_provider=openai_compat requires llm_answer_url to be set in config.yml")
        return _generator_call_openai_compat(prompt, model, url, thinking_budget)
    return _generator_call_claude(prompt, model)


def generator_build_sources(chunks: list[dict]) -> list[dict]:
    """
    Extract unique article sources from chunks for citation display.
    Returns list of {title, source_url, use_count, last_used_at, tags, _distance}.
    同一 article_id が複数チャンクにまたがる場合は最小距離（最類似）を採用する。
    """
    best: dict[str, dict] = {}  # article_id → source dict

    for c in chunks:
        aid = c.get("article_id", "")
        dist = c.get("_distance")
        if aid not in best or (dist is not None and dist < best[aid].get("_distance", float("inf"))):
            best[aid] = {
                "article_id": aid,
                "title": c.get("title", ""),
                "source_url": c.get("source_url", ""),
                "tags": c.get("tags", []),
                "use_count": c.get("use_count", 0),
                "last_used_at": c.get("last_used_at", ""),
                "_distance": dist,
            }

    return sorted(best.values(), key=lambda s: s.get("_distance") or float("inf"))


# ── Internal helpers ──────────────────────────────────────────────────────────


def _generator_build_context(chunks: list[dict]) -> str:
    parts: list[str] = []
    for i, c in enumerate(chunks, 1):
        heading = c.get("heading", "")
        title = c.get("title", "")
        text = c.get("text", "")
        recorded_at = c.get("recorded_at", "")[:10]
        date_str = f" [記録日: {recorded_at}]" if recorded_at else ""
        label = f"[{i}] {title}{date_str}" + (
            f" — {heading}" if heading not in ("__summary__", "__intro__", "__root__", "") else ""
        )
        parts.append(f"{label}\n{text}")
    return "\n\n---\n\n".join(parts)


def _generator_call_claude(prompt: str, model: str) -> str:
    return claude_complete(model, [{"role": "user", "content": prompt}], GENERATOR_MAX_TOKENS, system=_SYSTEM_PROMPT)


def _generator_call_openai_compat(prompt: str, model: str, base_url: str, thinking_budget: int = 0) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    max_tokens = GENERATOR_MAX_TOKENS
    extra_body: dict | None = None
    if thinking_budget and thinking_budget > 0:
        # 思考を効かせて回答品質を上げつつ、reasoning を budget で抑える。
        # 回答用に GENERATOR_MAX_TOKENS を別途確保するため max_tokens に budget を加算する
        # （これをしないと思考が枠を食って content が空になる）。
        # llama.cpp: --reasoning auto + per-request thinking_budget_tokens（vLLM系は無視）。
        extra_body = {"chat_template_kwargs": {"enable_thinking": True}, "thinking_budget_tokens": thinking_budget}
        max_tokens = GENERATOR_MAX_TOKENS + thinking_budget
    return openai_compat_complete(base_url, model, messages, max_tokens, timeout=180, extra_body=extra_body)
