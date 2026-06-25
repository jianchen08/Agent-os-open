# adapters

## 需求
### 职责
将思考模型客户端适配为 LangChain BaseChatModel，实现统一的消息格式转换和流式输出支持。

### 对外接口
- 输入：思考模型客户端实例 → 输出：LangChain 兼容的 BaseChatModel

### 依赖
- 依赖组件：llm/clients（思考模型客户端）
- 外部依赖：langchain_core（BaseChatModel、消息类型）

## 逻辑
### 流程设计
1. 接收思考模型客户端实例
2. 实现 LangChain BaseChatModel 接口
3. 转换消息格式（LangChain → 内部 Message）
4. 调用底层客户端生成响应
5. 转换响应格式（内部 LLMResponse → LangChain AIMessage）

### 数据流向
```
LangChain 消息 → 消息格式转换 → 思考模型客户端 → LLMResponse → AIMessage
```

### 错误处理
- 生成失败时返回错误消息 AIMessage
- 工具调用参数解析失败时使用空字典兜底

## 结构
### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，动态加载 LLMClientAdapter
暴露接口：
- `LLMClientAdapter`：LLM 客户端适配器基类

#### reasoning_adapter.py
职责：思考模型 LangChain 适配器
暴露接口：
- `ReasoningLangChainAdapter(BaseChatModel)`：思考模型适配器类
  - `__init__(reasoning_client, **kwargs)`：初始化适配器
  - `_generate(messages: list[BaseMessage], stop: list[str] | None, **kwargs) -> ChatResult`：同步生成
  - `_agenerate(messages: list[BaseMessage], stop: list[str] | None, **kwargs) -> ChatResult`：异步生成
  - `_stream(messages: list[BaseMessage], stop: list[str] | None, **kwargs) -> AsyncIterator`：流式生成
  - `astream_with_thinking(messages: list[BaseMessage], **kwargs) -> AsyncIterator`：带思考内容的流式生成
  - `ainvoke(messages: list[BaseMessage], **kwargs) -> AIMessage`：异步调用
  - `bind_tools(tools: list[Any], **kwargs) -> ReasoningLangChainAdapter`：绑定工具

### 测试策略
#### 组件测试
- 单元测试：消息格式转换、响应解析
- 集成测试：与思考模型客户端的集成

## 实现
→ 见代码文件
