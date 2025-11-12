from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from threading import Lock
import time
import uuid
from typing import Any, Dict, List

import orjson
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import APIKeyLimits, APIKeyStore, load_settings
from .rate_limiter import RateLimiter

settings = load_settings()
key_store = APIKeyStore(settings.api_keys_file)
rate_limiter = RateLimiter(settings.redis_url, settings.window_seconds)

logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

_metrics_lock = Lock()
_metrics = {"success": 0, "throttled": 0, "failed": 0}
_metrics_task: asyncio.Task | None = None


class ORJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content)


def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, str):
                    total_chars += len(piece)
                elif isinstance(piece, dict):
                    text = piece.get("text")
                    if isinstance(text, str):
                        total_chars += len(text)
    return max(1, total_chars // 4 or 1)


def _build_mock_content(messages: List[Dict[str, Any]], completion_tokens: int) -> str:
    last_user = "Hello"
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            last_user = msg["content"]
            break
    return f"Mock response ({completion_tokens} tokens) to: {last_user[:120]}"


def _auth_headers(request: Request) -> tuple[str, APIKeyLimits] | JSONResponse:
    header = request.headers.get("authorization")
    if not header or " " not in header:
        return JSONResponse({"error": "Missing Authorization header"}, status_code=401)
    scheme, token = header.split(" ", 1)
    if scheme.lower() != "bearer":
        return JSONResponse({"error": "Authorization must be Bearer token"}, status_code=401)
    api_key = token.strip()
    limits = key_store.get(api_key)
    if not limits:
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    return api_key, limits


def _record_metric(kind: str) -> None:
    with _metrics_lock:
        _metrics[kind] += 1


async def _metrics_reporter() -> None:
    while True:
        await asyncio.sleep(1)
        with _metrics_lock:
            success = _metrics["success"]
            throttled = _metrics["throttled"]
            failed = _metrics["failed"]
            _metrics["success"] = _metrics["throttled"] = _metrics["failed"] = 0
        total = success + throttled + failed
        if total:
            logger.info(
                "node=%s throughput=%d/s success=%d throttled=%d failed=%d",
                settings.service_name,
                total,
                success,
                throttled,
                failed,
            )


async def health(_: Request) -> Response:
    payload = {
        "status": "ok",
        "service": settings.service_name,
        "window_seconds": settings.window_seconds,
        "api_keys": len(key_store.all_keys()),
    }
    return ORJSONResponse(payload)


async def chat_completions(request: Request) -> Response:
    auth = _auth_headers(request)
    if isinstance(auth, JSONResponse):
        return auth
    api_key, limits = auth

    body = await request.body()
    try:
        payload = orjson.loads(body or b"{}")
    except orjson.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"error": "messages must be a non-empty list"}, status_code=400)

    model = payload.get("model", "gpt-4o-mini")
    max_tokens = payload.get("max_tokens", 128)
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = 128

    prompt_tokens = _estimate_tokens(messages)
    completion_tokens = max(1, min(max_tokens, 512))

    try:
        result = await rate_limiter.check_and_consume(
            api_key=api_key,
            limits=limits,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )
    except Exception:
        _record_metric("failed")
        raise

    if not result.allowed:
        _record_metric("throttled")
        detail = {
            "error": {
                "message": "Rate limit exceeded",
                "type": "rate_limit",
                "code": result.limit_flag,
            }
        }
        headers = {"Retry-After": str(settings.window_seconds)}
        return ORJSONResponse(detail, status_code=429, headers=headers)

    _record_metric("success")
    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": _build_mock_content(messages, completion_tokens),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "rate_limit_state": {
            "rpm": {"used": result.rpm_usage, "limit": limits.rpm},
            "input_tpm": {"used": result.input_tokens_usage, "limit": limits.input_tpm},
            "output_tpm": {"used": result.output_tokens_usage, "limit": limits.output_tpm},
            "window_seconds": settings.window_seconds,
        },
        "node": settings.service_name,
    }
    return ORJSONResponse(response)


async def startup() -> None:
    global _metrics_task
    await rate_limiter.initialize()
    loop = asyncio.get_running_loop()
    _metrics_task = loop.create_task(_metrics_reporter())


async def shutdown() -> None:
    if _metrics_task:
        _metrics_task.cancel()
        with suppress(asyncio.CancelledError):
            await _metrics_task
    await rate_limiter.close()


routes = [
    Route("/healthz", health, methods=["GET"]),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
]

app = Starlette(routes=routes, on_startup=[startup], on_shutdown=[shutdown])
