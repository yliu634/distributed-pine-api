from __future__ import annotations

from typing import List, Union

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[str]]


class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="Model name")
    messages: List[ChatMessage]
    max_tokens: int = Field(default=128, ge=1, le=4096)
    temperature: float = 1.0


class UsageMetrics(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
