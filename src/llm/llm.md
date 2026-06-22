# LLM 模块文档

## 需求

LLM 适配层，为 Agent OS 提供统一的大语言模型调用能力：

1. **统一接口**：屏蔽不同 LLM 提供商（OpenAI/Anthropic/Zhipu/Azure 等）的差异
2. **多模型 fallback**：主模型调用失败时自动切换到备用模型
3. **流式输出**：支持非流式和流式两种调用模式
4. **多 key 聚合 + 限流**：按 API key 做并发控制、RPM 滑动窗口限流、配额追踪
5. **thinking 解析**：支持 reasoning_content（思考过程）的提取
6. **tool_calls 解析**：支持函数调用结果的解析（非流式和流式增量合并）

## 逻辑

### 调用架构

```
LLMCorePlugin
  → KeyPoolAdapter（生产路径，多 key 限流 + Router fallback）
    → litellm.Router（多模型 fallback + 负载均衡）
      → litellm.acompletion()
        → 各 Provider API

或直连路径（单元测试）：
LLMCorePlugin
  → LiteLLMAdapter
    → litellm.acompletion()（单 key，无限流）
```

### 响应结构

```
LLMResponse（dataclass）
  ├── text: str              — 文本内容
  ├── tool_calls: list       — 工具调用列表
  ├── thinking_text: str     — 思考过程（reasoning_content，可选）
  ├── usage: dict            — Token 用量统计
  ├── stream_repetition: bool
  └── thinking_truncated: bool
```

### Fallback 策略

```
Router model_list（从 llm.yaml 构建）：
  → 按优先级排列多个模型配置
  → 主模型失败 → 自动切换到下一个模型
  → 支持不同 Provider 的模型混合 fallback

Provider 映射（litellm 前缀）：
  openai       → openai/*
  anthropic    → anthropic/*
  zhipu_coding → zai/*
  zhipu        → zai/*
  azure        → azure/*
  minimax      → minimax/*
  deepseek     → deepseek/*
```

### 多 key 限流（KeyPoolAdapter）

```
KeyPoolAdapter：
  → 每个 provider 维护一个 KeyPool（多 key 聚合）
  → 每个 key 独立信号量（default_max_concurrent=2）
  → RPM 滑动窗口限流
  → 配额追踪（token_quota）
  → 429 自动冷却该 key，切换到下一个
  → 所有 key 失败 → 走 Router fallback（切模型）
```

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `adapter.py` | `LLMResponse`, `LLMAdapter`, `_BaseLiteLLMAdapter`, `LiteLLMAdapter`, `KeyPoolAdapter` | LLM 适配器中间层（统一响应 + 限流 + fallback） |
| `key_pool.py` | `KeyPool`, `KeySlot` | 多 key 聚合 + 滑动窗口限流 + 配额追踪 |
| `router_factory.py` | `build_model_list`, `get_or_create_router`, `get_or_create_adapter`, `build_adapter` | litellm.Router/Adapter 工厂（从 llm.yaml 构建） |
| `exceptions.py` | `LLMResourceError`, `KeyPoolExhaustedError` | LLM 领域异常 |

### 依赖

- `litellm` — 统一 LLM 调用库（支持 100+ 提供商）
- `config/models` — ModelConfigLoader（读取 llm.yaml 配置）
- Python 标准库：asyncio, logging, dataclasses

### 配置文件

LLM 配置位于 `config/models/llm.yaml`，包含：
- providers 节：API 密钥、基础 URL、key pool
- models 节：模型定义（provider、model_name、context_window 等）
- defaults 节：默认模型、tier 分级（large/medium/small）
- concurrency 节：默认并发数
