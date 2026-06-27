"""hondana_types.py — Protocol definitions for dependency-injected interfaces.

Using Protocol (structural subtyping) so concrete classes (SentenceTransformer,
_ClaudeClient, _GroqClient) don't need to explicitly inherit from these.
"""

from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    """Minimal interface expected from any LLM client (summarise / expand_queries)."""

    def summarise(self, text: str, summary_chars: int, tag_count: int) -> tuple[str, list[str]]: ...
    def expand_queries(self, question: str) -> list[str]: ...


class EmbedModel(Protocol):
    """Minimal interface expected from any embedding model (SentenceTransformer-compatible)."""

    def encode(self, sentences, *, normalize_embeddings: bool = False): ...
    def get_sentence_embedding_dimension(self) -> int: ...
