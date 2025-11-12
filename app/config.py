from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import yaml
from pydantic import BaseModel, Field


class APIKeyLimits(BaseModel):
    api_key: str
    rpm: int = Field(..., description="Requests per minute")
    input_tpm: int = Field(..., description="Input tokens per minute")
    output_tpm: int = Field(..., description="Output tokens per minute")


class Settings(BaseModel):
    redis_url: str = Field(default="redis://localhost:6379/0")
    api_keys_file: str = Field(default="api_keys.yaml")
    window_seconds: int = Field(default=60)
    service_name: str = Field(default=os.getenv("NODE_ID", "rate-limiter"))


class APIKeyStore:
    """Loads and caches API key configs from YAML."""

    def __init__(self, file_path: str) -> None:
        self._file_path = Path(file_path)
        self._keys: Dict[str, APIKeyLimits] = {}
        self.reload()

    def reload(self) -> None:
        if not self._file_path.exists():
            raise FileNotFoundError(f"API key config not found: {self._file_path}")
        with self._file_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        keys_section = raw.get("keys", {})
        parsed: Dict[str, APIKeyLimits] = {}
        for key, cfg in keys_section.items():
            parsed[key] = APIKeyLimits(
                api_key=key,
                rpm=int(cfg["request_per_minute"]),
                input_tpm=int(cfg["input_tokens_per_minute"]),
                output_tpm=int(cfg["output_tokens_per_minute"]),
            )
        self._keys = parsed

    def get(self, api_key: str) -> APIKeyLimits | None:
        return self._keys.get(api_key)

    def all_keys(self) -> Dict[str, APIKeyLimits]:
        return dict(self._keys)


def load_settings() -> Settings:
    return Settings(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        api_keys_file=os.getenv("API_KEYS_FILE", "api_keys.yaml"),
        window_seconds=int(os.getenv("WINDOW_SECONDS", 60)),
    )
