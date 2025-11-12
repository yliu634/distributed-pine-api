# 分布式 LLM Rate Limiter 技术概述

## 项目目标
- 构建一个 OpenAI 格式兼容的 `POST /v1/chat/completions` 接口，按 API key 同时限制请求数、输入 TPM、输出 TPM。
- 滑动窗口误差控制在 1 秒以内；多节点部署时仍保持严格一致，禁止出现先查后改或全局锁带来的串行化。
- 任一指标触发限流即返回 429，否则返回结构与 OpenAI 相同的 Mock 响应。
- 提供一个能对多节点施加高压、发现吞吐瓶颈的高性能压测客户端。

## 架构概览
### HTTP 层
- 使用 Starlette + orjson 以降低单请求开销，Bearer 鉴权、JSON 解析与响应模板全部手写，避免 Pydantic 验证成本。
- 通过环境变量（`NODE_ID`、`REDIS_URL`、`WINDOW_SECONDS`、`BYPASS_LIMITER` 等）控制节点行为；设置 `BYPASS_LIMITER=1` 可跳过 Redis，用于测量框架极限。

### 滑动窗口限流
- 每个 API key 和指标维护一组 **1 秒粒度的桶**，在 Redis 中由 ZSET（时间序）、HASH（桶值）和 STRING（总量）三部分组成。
- 单个 Lua 脚本一次性执行“剔除过期桶 → 读取当前总量 → 判断 RPM / 输入 / 输出三项限额 → 插入新桶并设置 TTL”。脚本在 Redis 主线程原子运行，不需要任何分布式锁。
- TTL 略大于窗口时长，既保证滑窗误差 ≤ 1 秒，又让数据量随时间自动收敛。

### 压测客户端
- `scripts/load_client.py` 通过 httpx + asyncio 发送请求，并缓存 payload 降低 CPU 消耗；同时记录每个节点的成功 / 429 数据。
- 新增 `--processes` 参数，可按需 fork 多个子进程（每个进程独立事件循环），充分利用多核。Ctrl+C 时会优雅终止全部子进程并汇总统计。

## 性能瓶颈与诊断
- 随着压测客户端的线程/协程（`--concurrency`、`--processes`）不断增加，整体吞吐会先快速增长，随后进入“边际收益递减”区间。如果 throughput 开始趋于平缓、而延迟持续上升，说明系统已被某个瓶颈限制。
- 典型瓶颈一是应用框架（Starlette + orjson）自身的解析/序列化/业务逻辑；二是 Redis RTT 与 Lua 执行时间。可以通过设置 `BYPASS_LIMITER=1` 暂时跳过 Redis，若吞吐明显提升，则 Redis 是瓶颈，否则就是框架本身。
- 也可以结合 `redis-cli --latency`、`SLOWLOG`, 以及 `py-spy` / `perf` 采样来定位热点：Redis 延迟大说明需要本地化或分片；CPU 核被 Starlette 占满则意味着需要更轻量的 ASGI 或多节点扩容。

## 运维建议
- Redis 尽量使用本地或 Unix Socket，确保 `redis-cli --latency` 小于 0.3 ms，否则 Lua 执行延迟会成为主瓶颈。
- 以 `uvicorn app.main:app --loop uvloop --http httptools --no-access-log` 启动节点，并把 `ulimit -n` 提升到几万以支撑高并发连接。
- 压测时适当提升 API key 的 RPM / TPM，避免 429 过早触发导致吞吐无法评估。
- 提升压测强度时，提高 `--concurrency`、`--max-connections`、`--processes`，并查看节点输出的每秒吞吐日志（success / throttled / failed），实时定位瓶颈。

## 后续方向
- 在高负载下 profile Starlette/Redis 调用链，进一步定位热点（JSON 解析、token 估算、Redis RTT 等），评估是否需要更底层的 ASGI 框架或其他语言实现。
- 当 API key 规模扩大时，可考虑 Redis Cluster / 分片方案实现多核存储。
- 接入 Prometheus / OpenTelemetry，完善生产级监控与告警。
