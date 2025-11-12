from __future__ import annotations

from typing import Iterable

from .schemas import ChatMessage


def _flatten_content(message: ChatMessage) -> Iterable[str]:
    if isinstance(message.content, str):
        yield message.content
    else:
        for chunk in message.content:
            if isinstance(chunk, str):
                yield chunk
            elif isinstance(chunk, dict):
                text = chunk.get("text")
                if text:
                    yield text


def estimate_input_tokens(messages: list[ChatMessage]) -> int:
    total_words = 0
    for message in messages:
        for piece in _flatten_content(message):
            total_words += len(piece.split())
    # Rough heuristic: assume 0.75 tokens per word, clamp to at least 1 token.
    estimated_tokens = max(1, int(total_words / 0.75))
    return estimated_tokens
