# Distributed LLM API Rate Limiter

A Starlette + orjson mock OpenAI Chat Completions endpoint protected by a high-throughput, sliding-window rate limiter stored in Redis. Multiple HTTP nodes can run simultaneously (different ports/processes) while sharing a single Redis backend. A dedicated load generator simulates OpenAI-style traffic, randomly targets available nodes, and can push each node beyond 1K QPS to expose bottlenecks.

## Features
- OpenAI-compatible `POST /v1/chat/completions` endpoint returning mock responses.
- Per-API-key limits for requests-per-minute, input tokens per minute, and output tokens per minute.
- 1-second bucketed sliding window enforced via a single atomic Redis Lua script (no distributed locks or check-then-set races).
- Configurable API keys via `api_keys.yaml` + env overrides.
- High-performance load client (`scripts/load_client.py`) with payload caching and large connection pools for stress testing.

## Getting Started
1. **Install dependencies**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Run Redis** (Docker example)
   ```bash
   docker run --rm -p 6379:6379 redis:7-alpine
   ```
3. **Configure API keys** by editing `api_keys.yaml` (sample keys already included).
4. **Launch high-performance rate limiter nodes**
   ```bash
   # Terminal 1
   NODE_ID=node-a uvicorn app.main:app \
       --host 0.0.0.0 --port 8000 \
       --loop uvloop --http httptools --no-access-log

   # Terminal 2
   NODE_ID=node-b uvicorn app.main:app \
       --host 0.0.0.0 --port 8001 \
       --loop uvloop --http httptools --no-access-log
   ```
   Each node is stateless aside from Redis and comfortably exceeds 1K QPS due to O(1) rate-limit bookkeeping.
5. **Send manual request**
   ```bash
   curl http://localhost:8000/v1/chat/completions \
        -H 'Authorization: Bearer sk-test-tier-a' \
        -H 'Content-Type: application/json' \
        -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
   ```
6. **Run the high-performance load generator**
   ```bash
   python3 scripts/load_client.py \
       --nodes http://localhost:8000 --nodes http://localhost:8001 \
       --api-keys sk-test-tier-a --api-keys sk-test-tier-b \
       --duration 60 --concurrency 4 \
       --payload-cache-size 1024 \
       --max-connections 4000 \
       --processes 4
   ```
   Output includes aggregate throughput plus per-node success/throttle counts so you can pinpoint saturation. Increase `--processes` to spin up multiple load-generator processes (each with its own concurrency) when you need to saturate more nodes or CPUs.

## Configuration
- `REDIS_URL`: override Redis connection string (default `redis://localhost:6379/0`).
- `API_KEYS_FILE`: path to YAML file with key definitions.
- `WINDOW_SECONDS`: sliding window size (default 60s, bucketed at 1s resolution).
- `NODE_ID`: human-readable name reported via `/healthz` and response payloads.
- `BYPASS_LIMITER`: set to `1` to skip Redis accounting (useful for measuring pure framework throughput).

`api_keys.yaml` format:
```yaml
keys:
  sk-test-tier-a:
    request_per_minute: 120
    input_tokens_per_minute: 20000
    output_tokens_per_minute: 10000
```

## High-QPS tips
- `uvicorn app.main:app --loop uvloop --http httptools --no-access-log --workers 1` keeps a single process lean; raise `--workers` if you want a multi-process node.
- Keep Redis local (or on a Unix socket) so `redis-cli --latency` stays < 0.3 ms; otherwise the Lua script dominates latency.
- Lift API key limits during stress tests so 429s do not mask true saturation.
- Use `scripts/load_client.py --concurrency 800 --max-connections 8000 --processes 4` (or more) when you need to push beyond 1K QPS per node.

## Project Layout
```
app/
  config.py          # Settings + API key loader
  main.py            # Starlette application (orjson responses)
  rate_limiter.py    # Redis-backed sliding-window limiter
scripts/
  load_client.py     # High-performance async load tester
```

## Notes
- The service returns mock completions without calling upstream LLMs.
- The Redis script maintains per-second buckets and atomic totals to keep drift ≤1s while avoiding distributed locks or request serialization.
- Add more nodes simply by running additional `uvicorn` workers that point at the same Redis instance; limits remain globally consistent for each API key.
