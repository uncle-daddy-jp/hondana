"""_llm_transport.py — Low-level LLM HTTP calls shared by generator.py and ingest/llm_client.py.

Transport only: build the request, unwrap the response text. Rate-limit and other
provider-specific error mapping stays in the callers (so each can keep its own policy).
Lives at the api root (flat import) so root-level generator.py and the ingest subpackage
can both import it without a cross-package dependency.
"""

from __future__ import annotations

import os

import anthropic
import requests


def claude_complete(model: str, messages: list[dict], max_tokens: int, *, system: str | None = None) -> str:
    """Call the Anthropic Messages API; return the first content block's text.

    anthropic.RateLimitError propagates so the caller can map it to its own exception.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system is not None:
        kwargs["system"] = system
    msg = client.messages.create(**kwargs)
    return msg.content[0].text


def openai_compat_complete(
    base_url: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    *,
    timeout: int,
    extra_body: dict | None = None,
) -> str:
    """Call an OpenAI-compatible /v1/chat/completions endpoint; return the message content."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if extra_body:
        body.update(extra_body)
    resp = requests.post(url, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
