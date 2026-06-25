# clients

## 需求
### 职责
提供多种 LLM 提供商的客户端实现，封装 API 调用、消息转换、错误处理等通用逻辑。

### 对外接口
- 输入：模型名称、API 密钥、配置参数 → 输出：统一的 LLMClient 实例

### 依赖
- 依赖组件：llm/base（LLMClient 基类、消息类型）
- 外部依赖：langchain_openai、langchain_anthropic、langchain_ollama、zhipuai

## 逻辑
### 流程设计
1. 根据提供商类型选择对应客户端
2. 初始化 LangChain 底层客户端
3. 实现 LLMClient 抽象接口
4. 处理消息格式转换
5. 统一错误处理和重试

### 数据流向
```
Message 列表 → 格式转换 → LangChain 客户端 → API 调用 → LLMResponse
```

### 错误处理
- 认证错误：AuthenticationError
- 速率限制：RateLimitError
- 无效请求：InvalidRequestError
- 模型不可用：ModelNotAvailableError

## 结构
### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，导出客户端类
暴露接口：
- `OpenAIClient`：OpenAI 客户端
- `AnthropicClient`：Anthropic 客户端
- `OllamaClient`：Ollama 客户端
- `ZhipuClient`：智谱客户端

#### openai.py
职责：OpenAI 及兼容 API 客户端
暴露接口：
- `OpenAIClient(LLMClient)`：OpenAI 客户端类
  - `__init__(model_name: str, api_key: str | None, api_base: str | None, default_params: dict | None, **kwargs)`：初始化
  - `_generate_internal(messages: list[Message], **kwargs) -> LLMResponse`：内部生成
  - `_stream_internal(messages: list[Message], **kwargs) -> AsyncIterator[str]`：流式生成
  - `_generate_with_tools_internal(messages: list[Message], tools: list[Tool], **kwargs) -> LLMResponse`：工具调用生成
  - `ainvoke(messages, **kwargs) -> AIMessage`：LangChain 兼容异步调用
  - `bind_tools(tools: list[Tool]) -> _BoundLLMClient`：绑定工具
  - `stream_with_tools(messages: list[Message], tools: list[Tool], **kwargs) -> AsyncIterator[str]`：带工具的流式生成
  - `as_langchain() -> BaseChatModel`：获取 LangChain 实例

#### anthropic.py
职责：Anthropic Claude 客户端
暴露接口：
- `AnthropicClient(LLMClient)`：Anthropic 客户端类
  - `__init__(model_name: str, api_key: str | None, api_base: str | None, default_params: dict | None, **kwargs)`：初始化
  - `_generate_internal(messages: list[Message], **kwargs) -> LLMResponse`：内部生成
  - `_stream_internal(messages: list[Message], **kwargs) -> AsyncIterator[str]`：流式生成
  - `_generate_with_tools_internal(messages: list[Message], tools: list[Tool], **kwargs) -> LLMResponse`：工具调用生成
  - `as_langchain() -> BaseChatModel`：获取 LangChain 实例

#### ollama.py
职责：Ollama 本地模型客户端
暴露接口：
- `OllamaClient(LLMClient)`：Ollama 客户端类
  - `__init__(model_name: str, api_key: str | None, api_base: str | None, default_params: dict | None, **kwargs)`：初始化
  - `_generate_internal(messages: list[Message], **kwargs) -> LLMResponse`：内部生成
  - `_stream_internal(messages: list[Message], **kwargs) -> AsyncIterator[str]`：流式生成
  - `_generate_with_tools_internal(messages: list[Message], tools: list[Tool], **kwargs) -> LLMResponse`：工具调用生成
  - `as_langchain() -> BaseChatModel`：获取 LangChain 实例

#### zhipu.py
职责：智谱 AI 客户端，支持思考模式
暴露接口：
- `ZhipuClient(LLMClient)`：智谱客户端类
  - `__init__(model_name: str, api_key: str | None, api_base: str | None, default_params: dict | None, **kwargs)`：初始化
  - `_generate_internal(messages: list[Message], **kwargs) -> LLMResponse`：内部生成
  - `_stream_internal(messages: list[Message], **kwargs) -> AsyncIterator[str]`：流式生成
  - `_generate_with_tools_internal(messages: list[Message], tools: list[Tool], **kwargs) -> LLMResponse`：工具调用生成
  - `ainvoke(messages, config: dict | None, **kwargs) -> AIMessage`：LangChain 兼容异步调用
  - `astream_with_thinking(messages, **kwargs) -> AsyncIterator[AIMessageChunk]`：带思考内容的流式生成
  - `bind_tools(tools: list[Tool]) -> _BoundZhipuClient`：绑定工具

#### reasoning.py
职责：思考模型客户端（DeepSeek R1、OpenAI o1/o3 等）
暴露接口：
- `ReasoningClient(LLMClient)`：思考模型客户端基类
  - `__init__(model_name: str, api_key: str, api_base: str, default_params: dict | None, reasoning_type: str)`：初始化
  - `_generate_internal(messages: list[Message], **kwargs) -> LLMResponse`：内部生成
  - `_stream_internal(messages: list[Message], **kwargs) -> AsyncIterator[str]`：流式生成
  - `_stream_with_reasoning_internal(messages: list[Message], **kwargs) -> AsyncIterator[tuple[str, str]]`：带思考内容的流式生成
  - `_generate_with_tools_internal(messages: list[Message], tools: list[Tool], **kwargs) -> LLMResponse`：工具调用生成
  - `as_langchain() -> ReasoningLangChainAdapter`：获取 LangChain 适配器
- `DeepSeekReasoningClient(ReasoningClient)`：DeepSeek R1 客户端
- `OpenAIReasoningClient(ReasoningClient)`：OpenAI o1/o3 客户端
- `AnthropicReasoningClient(ReasoningClient)`：Claude 思考模式客户端

#### mock.py
职责：测试用模拟客户端
暴露接口：
- `MockClient(LLMClient)`：模拟客户端类
  - `__init__(model_name: str, **kwargs)`：初始化
  - `_generate_internal(messages: list[Message], **kwargs) -> LLMResponse`：内部生成
  - `_stream_internal(messages: list[Message], **kwargs) -> AsyncIterator[str]`：流式生成
  - `_generate_with_tools_internal(messages: list[Message], tools: list[Tool], **kwargs) -> LLMResponse`：工具调用生成
  - `chat(message: str) -> str`：简单聊天接口

### 测试策略
#### 组件测试
- 单元测试：消息转换、响应解析、错误处理
- 集成测试：与真实 API 的集成（可选）

## 实现
→ 见代码文件
