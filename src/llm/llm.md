# LLM 模块文档

## 需求

LLM 适配层，为 Agent OS 提供统一的大语言模型调用能力：

1. **统一接口**：屏蔽不同 LLM 提供商（OpenAI/Anthropic/Zhipu/Azure 等）的差异
2. **多模型 fallback**：主模型调用失败时自动切换到备用模型
3. **流式输出**：支持非流式和流式两种调用模式
4. **自适应并发控制**：根据 API 限流信号动态调整并发数
5. **thinking 解析**：支持 reasoning_content（思考过程）的提取
6. **tool_calls 解析**：支持函数调用结果的解析（非流式和流式增量合并）

## 逻辑

### 调用架构

```
LLMCorePlugin
  → AdaptiveRouterAdapter（自适应并发 + Router 模式）
    → litellm.Router（多模型 fallback + 负载均衡）
      → litellm.completion() / litellm.acompletion()
        → 各 Provider API

或直连模式：
LLMCorePlugin
  → litellm.acompletion()（单一模型直连）
```

### 响应结构

```
LLMResponse
  ├── content: str           — 文本内容
  ├── reasoning_content: str — 思考过程（可选）
  ├── tool_calls: list       — 工具调用列表（可选）
  ├── usage: dict            — Token 用量统计
  ├── model: str             — 实际使用的模型名称
  └── finish_reason: str     — 结束原因
```

### Fallback 策略

```
Router model_list（从 llm.yaml 构建）：
  → 按优先级排列多个模型配置
  → 主模型失败 → 自动切换到下一个模型
  → 支持不同 Provider 的模型混合 fallback

Provider 映射：
  openai       → openai/*
  anthropic    → anthropic/*
  zhipu_coding → zai/*
  zhipu        → zai/*
  azure        → azure/*
  minimax      → minimax/*
```

### 自适应并发控制

```
AdaptiveRouterAdapter：
  → 并发范围：min_concurrency ~ max_concurrency（默认 1-3）
  → 限流信号（429/overloaded）→ 降低并发
  → 成功调用 → 逐步恢复并发
  → 信号量控制并发请求数
```

### 资源清理

```
cleanup_litellm_resources()
  → 取消 LoggingWorker 后台任务
  → 关闭 HTTP 异步客户端会话
  → 防止 asyncio 资源泄漏警告
```

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `adapter.py` | LLMResponse, AdaptiveRouterAdapter, FallbackAdapter, cleanup_litellm_resources, cleanup_litellm_resources_sync | LLM 适配器中间层（统一响应 + fallback + 自适应并发） |
| `router_factory.py` | build_model_list, get_or_create_router | litellm.Router 工厂（从 llm.yaml 构建共享 Router） |

### 依赖

- `litellm` — 统一 LLM 调用库（支持 100+ 提供商）
- `config/models` — ModelConfigLoader（读取 llm.yaml 配置）
- Python 标准库：asyncio, logging, dataclasses

### 配置文件

LLM 配置位于 `config/models/llm.yaml`，包含：
- providers 节：API 密钥、基础 URL
- models 节：模型定义（provider、model_name、context_window 等）
- defaults 节：默认模型、tier 分级（large/medium/small）
