# builders

## 需求
### 职责
按 layer_order 顺序拼接各层内容，构建完整的 LLM 上下文。

### 对外接口
- 输入：LayeredContextStore 实例、用户消息、Agent 配置 → 输出：LangChain 消息列表

### 依赖
- 依赖组件：memory/loaders（层内容加载器）、memory/compressor（分层存储）
- 外部依赖：langchain_core（消息类型）

## 逻辑
### 流程设计
1. 读取配置的 layer_order
2. 按顺序构建各层内容：
   - 第1层（系统静态层）：system_prompt + tools_description + static_vars
   - 第2层（压缩层）：L3 关键词 + L2 三元组 + L1 八段摘要
   - 第3层（消息层）：recent_messages
   - 第4层（尾部动态层）：dynamic_vars
3. 将各层内容转换为 LangChain 消息
4. 返回完整的消息列表

### 数据流向
```
LayeredContextStore → 层内容加载 → 消息构建 → LangChain 消息列表
```

### 四层架构
| 层级 | 内容 | 消息类型 |
|------|------|----------|
| 第1层 | system_prompt、tools_description、static_vars | SystemMessage |
| 第2层 | L3/L2/L1 压缩内容 | SystemMessage |
| 第3层 | recent_messages | HumanMessage/AIMessage/ToolMessage |
| 第4层 | dynamic_vars | SystemMessage |

## 结构
### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，导出构建器
暴露接口：
- `ContextBuilder`：上下文构建器

#### context_builder.py
职责：上下文构建器实现
暴露接口：
- `ContextBuilder`：上下文构建器类
  - `build(store: Any, user_message: str | None, agent_config: Any | None) -> tuple[list[Any], list[dict]]`：构建上下文
  - `_get_layer_order(store: Any) -> list[str]`：获取层顺序配置
  - `_build_layer(store: Any, layer_name: str, messages: list, context_parts: list) -> None`：构建单层
  - `_build_system_prompt(content: Any, messages: list, context_parts: list) -> None`：构建系统提示层
  - `_build_tools_description(content: Any, messages: list, context_parts: list) -> None`：构建工具描述层
  - `_build_static_vars(content: Any, messages: list, context_parts: list) -> None`：构建静态变量层
  - `_build_l3_memory(content: Any, messages: list, context_parts: list) -> None`：构建 L3 层
  - `_build_l2_memory(content: Any, messages: list, context_parts: list) -> None`：构建 L2 层
  - `_build_l1_memory(content: Any, messages: list, context_parts: list) -> None`：构建 L1 层
  - `_build_recent_messages(content: Any, messages: list, context_parts: list) -> None`：构建最近消息层
  - `_build_dynamic_variables(content: Any, messages: list, context_parts: list) -> None`：构建动态变量层

### 测试策略
#### 组件测试
- 单元测试：各层构建逻辑、消息转换
- 集成测试：完整上下文构建流程

## 实现
→ 见代码文件
