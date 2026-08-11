# concurrency 模块文档

## 概述

本模块文档描述系统实际的运行时并发控制机制。

说明：`infrastructure/concurrency.py` 中的 `ConcurrencyController` 已移除，本描述基于实际运行的并发控制实现，分布在 `llm/key_pool.py`、`isolation/hardware_profile.py`、`config/settings.py` 三个模块中。

## 并发控制层级

系统运行时并发控制分为三层，分别作用于不同粒度：

### 1. LLM Key 级并发

- 实现位置：`src/llm/key_pool.py`
- 核心类：`PrioritySemaphore` / `KeySlot` / `KeyPool`

#### 机制

每个 API key 对应一个 `KeySlot`，独立追踪四类状态：

| 状态 | 字段 | 说明 |
|------|------|------|
| 并发数 | `max_concurrent` + `PrioritySemaphore` | 信号量容量，默认 2 |
| RPM | `rpm_limit` + `_request_timestamps` | 60 秒滑动窗口，本地主动防御 |
| Token 配额 | `token_quota` + `_tokens_used` | 累加 usage 消耗 |
| 429 冷却 | `_cooling_until` | 被动兜底 + 校准本地计数 |

`PrioritySemaphore` 是优先级信号量，高优先级（数值小）的请求优先获取许可。优先级来源是 Agent 层级：

| Agent 层级 | 优先级数值 |
|-----------|-----------|
| L1 | 1 |
| L2 | 2 |
| L3 | 3 |
| 未知 | 99 |

优先级通过 `contextvars.ContextVar` 在协程间传递，由 `set_agent_priority(agent_level)` 设置、`get_agent_priority()` 读取。当信号量许可耗尽时，等待者按优先级排序入队（`_waiters` 列表），`release()` 唤醒队首（最高优先级）等待者。

`KeyPool` 从 provider 的多个 `KeySlot` 中选"最空闲"的 key，聚合吞吐量：

1. `select()` 排除冷却中 / RPM 满 / 配额耗尽的 key，在剩余 key 中按 `score()` 选最高分
2. `score()` = RPM 余量比 × 0.6 + Token 余量比 × 0.4（冷却中返回 -1.0）
3. `acquire_slot(timeout=60.0)` 选 key 并获取并发许可，阻塞直到有 key 可用或超时
4. 所有 key 不可用且等待超时时抛出 `KeyPoolExhaustedError`，避免 `drain_loop` 卡死

某个 key 限额到了自动切换到下一个最优 key，实现多 key 聚合吞吐。

### 2. 硬件任务级并发

- 实现位置：`src/isolation/hardware_profile.py`
- 配置项：`max_concurrent_tasks`

#### 机制

根据运行环境（容器内 / 裸机）的实际可用资源，按内存分级计算隔离容器的资源配额，包括最大并发任务数。

硬件检测优先级：cgroup limit > sysconf > 默认值。容器内读 cgroup（更准），裸机退化到 sysconf，Windows 或读不到时给保守默认值 8GB（按低配处理）。

按内存分三档（`_PROFILES`）：

| 分级 | 内存阈值 | max_environments | container_memory | container_cpus | max_concurrent_tasks |
|------|---------|-----------------|------------------|---------------|---------------------|
| low | < 12GB | 3 | 256m | 0.25 | 3 |
| mid | 12-24GB | 6 | 384m | 0.5 | 6 |
| high | > 24GB | 12 | 512m | 1.0 | 12 |

CPU 数会进一步约束并发：不能超过 (CPU 总数 - 预留给系统的核数)。预留核数：low=1，mid=2，high=4。最终值：

```
max_concurrent_tasks = min(分档默认值, cpu - reserved_cpu)
```

环境变量覆盖（优先级最高，最后应用）：

| 环境变量 | 类型 | 说明 |
|---------|------|------|
| `AO_MAX_CONCURRENT_TASKS` | int | 覆盖最大并发任务数 |
| `AO_MAX_ENVIRONMENTS` | int | 覆盖最大隔离容器数 |
| `AO_CONTAINER_MEMORY` | str | 覆盖单容器内存（如 "256m"） |
| `AO_CONTAINER_CPUS` | str | 覆盖单容器 CPU（如 "0.5"） |
| `AO_MEMORY_SWAP` | str | 覆盖 swap 限制 |
| `AO_PIDS_LIMIT` | int | 覆盖进程数上限 |

入口函数：`get_resource_profile()` 一步完成硬件检测 + 配额计算，由 `IsolationManager` 在初始化时调用一次。

### 3. 配置级并发

- 实现位置：`src/config/settings.py`
- 配置类：`Settings`（基于 `pydantic-settings`，支持 `.env` 文件与环境变量覆盖）

#### LLM 提供商并发配置

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| `llm_zhipu_max_concurrent` | `LLM_ZHIPU_MAX_CONCURRENT` | 2 | 智谱 AI 最大并发数 |
| `llm_openai_max_concurrent` | `LLM_OPENAI_MAX_CONCURRENT` | 10 | OpenAI 最大并发数 |
| `llm_anthropic_max_concurrent` | `LLM_ANTHROPIC_MAX_CONCURRENT` | 5 | Anthropic 最大并发数 |
| `llm_default_max_concurrent` | `LLM_DEFAULT_MAX_CONCURRENT` | 2 | 默认最大并发数（未配置的提供商使用） |
| `llm_max_concurrent` | `LLM_MAX_CONCURRENT` | 2 | LLM API 最大并发数（向后兼容，已弃用） |
| `llm_rate_limit_per_minute` | `LLM_RATE_LIMIT_PER_MINUTE` | 60 | LLM API 每分钟最大请求数（已弃用） |

#### 任务并发配置

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| `max_concurrent_tasks` | `MAX_CONCURRENT_TASKS` | 10 | 最大并发任务数 |
| `task_timeout` | `TASK_TIMEOUT` | 300 | 任务超时（秒，5 分钟） |
| `ac_max_retries` | `AC_MAX_RETRIES` | 5 | AC 最大重试次数 |
| `task_max_retries` | `TASK_MAX_RETRIES` | 6 | 任务最大重试次数 |

#### 机制

`Settings` 继承 `pydantic_settings.BaseSettings`，通过 `validation_alias` 绑定环境变量，`.env` 文件优先级低于环境变量。全局单例 `settings = Settings()`，通过 `get_settings()` 获取。`reset_settings()` 用于测试重置。

## 三层关系

```
配置级（settings.py）
  → 提供各提供商并发上限默认值（llm_zhipu_max_concurrent 等）
  → 提供任务级并发上限（max_concurrent_tasks）

硬件任务级（hardware_profile.py）
  → 根据实际硬件资源动态计算 max_concurrent_tasks
  → 环境变量 AO_MAX_CONCURRENT_TASKS 可覆盖

LLM Key 级（key_pool.py）
  → 每个 KeySlot 用 PrioritySemaphore 实现单 key 并发控制
  → KeyPool 聚合多 key，限额到了自动切换
  → Agent 层级（L1/L2/L3）决定排队优先级
```
