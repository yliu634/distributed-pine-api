#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field
from multiprocessing import Process, Queue
from typing import List, Optional

import httpx
import typer

app = typer.Typer(help="High-performance load generator for the distributed rate limiter")

PROMPTS = [
    "Explain the significance of distributed rate limiting in microservices.",
    "List three ways to optimize token usage when calling LLM APIs.",
    "Draft an email announcing a new AI assistant feature for our app.",
    "Summarize the latest sprint planning decisions in bullet points.",
    "Generate three creative marketing slogans for a coffee brand.",
]


@dataclass
class Stats:
    success: int = 0
    throttled: int = 0
    failed: int = 0
    total_latency: float = 0.0
    per_node_success: dict = field(default_factory=dict)
    per_node_throttled: dict = field(default_factory=dict)

    def record(self, node: str, status_code: int, latency: float) -> None:
        if status_code == 200:
            self.success += 1
            self.total_latency += latency
            self.per_node_success[node] = self.per_node_success.get(node, 0) + 1
        elif status_code == 429:
            self.throttled += 1
            self.per_node_throttled[node] = self.per_node_throttled.get(node, 0) + 1
        else:
            self.failed += 1

    def merge(self, other: "Stats") -> None:
        self.success += other.success
        self.throttled += other.throttled
        self.failed += other.failed
        self.total_latency += other.total_latency
        for node, count in other.per_node_success.items():
            self.per_node_success[node] = self.per_node_success.get(node, 0) + count
        for node, count in other.per_node_throttled.items():
            self.per_node_throttled[node] = self.per_node_throttled.get(node, 0) + count


def make_payload() -> dict:
    prompt = random.choice(PROMPTS)
    max_tokens = random.randint(32, 256)
    return {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": round(random.uniform(0.2, 1.0), 2),
    }


def build_payload_cache(size: int) -> List[bytes]:
    cache = []
    for _ in range(max(1, size)):
        payload = make_payload()
        cache.append(json.dumps(payload).encode("utf-8"))
    return cache


async def worker(
    client: httpx.AsyncClient,
    api_keys: List[str],
    nodes: List[str],
    payload_cache: List[bytes],
    end_time: float,
    stats: Stats,
    lock: asyncio.Lock,
) -> None:
    local = Stats()
    local_nodes = len(nodes)
    local_keys = len(api_keys)
    while time.time() < end_time:
        node = nodes[random.randint(0, local_nodes - 1)]
        api_key = api_keys[random.randint(0, local_keys - 1)]
        payload_bytes = payload_cache[random.randint(0, len(payload_cache) - 1)]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = node.rstrip("/") + "/v1/chat/completions"
        start = time.perf_counter()
        status_code = 0
        try:
            resp = await client.post(url, headers=headers, content=payload_bytes, timeout=10.0)
            status_code = resp.status_code
        except httpx.HTTPError:
            status_code = -1
        latency = time.perf_counter() - start
        local.record(node, status_code, latency)
    async with lock:
        stats.merge(local)


@app.command()
def run(
    nodes: List[str] = typer.Option(..., help="List of base URLs for rate limiter nodes"),
    api_keys: List[str] = typer.Option(..., help="API keys to rotate through during the test"),
    duration: int = typer.Option(20, help="Test duration in seconds"),
    concurrency: int = typer.Option(50, help="Number of concurrent workers"),
    payload_cache_size: int = typer.Option(512, help="Pre-generated payload variants to reduce CPU overhead"),
    max_connections: int = typer.Option(2000, help="HTTP connection pool size"),
    processes: int = typer.Option(1, help="Number of load-generator processes to spawn"),
) -> None:
    if not nodes:
        raise typer.BadParameter("Provide at least one node URL")
    if not api_keys:
        raise typer.BadParameter("Provide at least one API key")

    def run_single(queue: Queue | None = None) -> Stats:
        async def _run() -> Stats:
            payload_cache = build_payload_cache(payload_cache_size)
            limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
            async with httpx.AsyncClient(limits=limits) as client:
                stats = Stats()
                lock = asyncio.Lock()
                end_time = time.time() + duration
                tasks = [
                    asyncio.create_task(worker(client, api_keys, nodes, payload_cache, end_time, stats, lock))
                    for _ in range(concurrency)
                ]
                await asyncio.gather(*tasks)
                return stats

        result = asyncio.run(_run())
        if queue is not None:
            queue.put(result)
        return result

    stats = Stats()
    procs: List[Process] = []
    try:
        if processes <= 1:
            stats = run_single()
        else:
            queue: Queue = Queue()
            for _ in range(processes):
                p = Process(target=run_single, args=(queue,))
                p.start()
                procs.append(p)
            for _ in range(processes):
                proc_stats: Stats = queue.get()
                stats.merge(proc_stats)
    except KeyboardInterrupt:
        print("Interrupted, shutting down load generator...", file=sys.stderr)
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
            p.join()

    total = stats.success + stats.throttled + stats.failed
    avg_latency = stats.total_latency / stats.success if stats.success else 0.0
    print(json.dumps(
        {
            "total_requests": total,
            "success": stats.success,
            "throttled": stats.throttled,
            "failed": stats.failed,
            "success_avg_latency_ms": round(avg_latency * 1000, 2),
            "per_node_success": stats.per_node_success,
            "per_node_throttled": stats.per_node_throttled,
            "processes": processes,
        },
        indent=2,
    ))


if __name__ == "__main__":
    app()
