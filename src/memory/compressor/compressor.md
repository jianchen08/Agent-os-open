# compressor

## 需求
### 职责
支持分层递进压缩（L0→L1→L2→L3），管理上下文预算，实现存取分离架构。

### 对外接口
- 输入：对话消息、预算配置 → 输出：压缩后的摘要、压缩报告

### 依赖
- 依赖组件：llm/clients（LLM 客户端）、core/tokenizer（Token 计数）
- 外部依赖：sqlalchemy（数据库操作）、cachetools（缓存）

## 逻辑
### 流程设计
1. 检查总上下文是否超出触发阈值
2. 如果超出，执行递进压缩循环：
   - L0→L1：将消息压缩成八段摘要
   - L1→L2：将八段摘要压缩成三元组
   - L2→L3：将三元组压缩成关键词
   - L3 超出：丢弃最旧的内容（遗忘）
3. 更新元数据存储
4. 返回压缩报告

### 数据流向
```
L0 消息 → 八段压缩 → L1 摘要 → 三元组压缩 → L2 摘要 → 关键词提取 → L3 关键词
```

### 压缩模板
| 层级 | 模板 | 输出格式 |
|------|------|----------|
| L0→L1 | EIGHT_SECTION_PROMPT | 八段摘要 |
| L1→L2 | TRIPLET_PROMPT | 三元组（意图/过程/结果） |
| L2→L3 | KEYWORD_PROMPT | 关键词列表 |

### 配置设计
#### 预算比例
| 层级 | 比例 | 说明 |
|------|------|------|
| system_prompt | 配置文件 | 系统提示 |
| tools_description | 配置文件 | 工具描述 |
| dynamic_variables | 配置文件 | 动态变量 |
| L1 | 配置文件 | 八段摘要 |
| L2 | 配置文件 | 三元组摘要 |
| L3 | 配置文件 | 关键词索引 |
| recent | 配置文件 | 最近消息 |
| response_reserve | 配置文件 | 响应预留 |

## 结构
### 子组件清单（文件夹 - 抽象说明）
| 子组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| 无 | - | - | - |

### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，导出所有组件
暴露接口：
- `CompressionConfig`：压缩配置
- `ContextBudget`：上下文预算
- `ContextCompressor`：核心压缩器
- `StructuredCompressor`：结构化压缩器
- `LayeredContextStore`：分层上下文存储
- `MemoryChunkDB`：内存块数据库操作
- `ChunkMetadata`：块元数据
- `ChunkStatus`：块状态枚举
- `ContentRef`：内容引用
- `CompressionResult`：压缩结果
- `CompressionReport`：压缩报告
- `ChunkMetadataStore`：元数据存储
- `ContextWriter`：上下文写入器
- `ContextReader`：上下文读取器

#### config.py
职责：压缩配置管理
暴露接口：
- `load_context_window_config() -> dict`：加载上下文窗口配置
- `CompressionConfig`：压缩配置类
  - `context_window: int`：上下文窗口大小
  - `compress_trigger_ratio: float`：压缩触发比例
  - `get_budgets() -> dict[str, int]`：计算各层预算
  - `get_trigger_threshold() -> int`：获取触发阈值
  - `validate() -> bool`：验证配置
- `ContextBudget`：上下文预算状态类

#### core.py
职责：核心压缩逻辑
暴露接口：
- `normalize_layer_name(layer: str) -> str`：标准化层级名称
- `LAYER_NAME_MAP`：层级名称映射
- `ContextCompressor`：上下文压缩器类
  - `__init__(llm_client: LLMClient, config: CompressionConfig | None, model: str | None)`：初始化
  - `compress(messages: list[dict], preserve_structure: bool) -> str`：压缩对话历史
  - `compress_to_l1(messages: list[dict]) -> str`：L0→L1 压缩
  - `compress_to_l2(l1_summary: str) -> str`：L1→L2 压缩
  - `compress_to_l3(l2_summary: str) -> str`：L2→L3 压缩
  - `progressive_compress(l0: str, l1: str, l2: str, budgets: dict, **kwargs) -> tuple[str, str, str]`：递进压缩
  - `get_stats() -> dict`：获取统计信息
  - `clear_cache()`：清空缓存

#### models.py
职责：数据模型定义
暴露接口：
- `ChunkStatus(Enum)`：块状态枚举
- `ContentRef`：数据库内容引用
- `ChunkMetadata`：块元数据
- `CompressionResult`：压缩结果
- `CompressionReport`：压缩报告

#### db.py
职责：数据库操作
暴露接口：
- `MemoryChunkDB`：内存块数据库操作类
  - `save_chunk(session, user_id, session_id, layer, content, **kwargs) -> str`：保存分块
  - `load_chunks_by_session(session, session_id, executor_id) -> dict[str, list]`：加载会话分块
  - `load_ungraduated_l1_chunks(session, session_id, executor_id) -> list[dict]`：加载未毕业 L1
  - `load_ungraduated_l2_chunks(session, session_id, executor_id) -> list[dict]`：加载未毕业 L2
  - `mark_chunks_as_graduated(session, chunk_ids, episode_id) -> None`：标记已毕业
  - `delete_temporary_chunks(session, session_id, executor_id) -> None`：删除临时数据
  - `load_embeddings_for_retrieval(session, user_id, executor_id, limit) -> list[dict]`：加载检索向量

#### reader.py
职责：上下文读取器（读取端）
暴露接口：
- `ContextReader`：上下文读取器类
  - `__init__(session_id, config, metadata_store, db_session, context_repository, **kwargs)`：初始化
  - `read_compressed_layer(layer: str) -> list[str]`：读取压缩层内容
  - `read_message_layer() -> list[dict]`：读取消息层
  - `get_recent_messages(limit: int | None) -> list[dict]`：获取最近消息
  - `get_layer_chunks(layer: str) -> list[ChunkMetadata]`：获取层块元数据

#### writer.py
职责：上下文写入器（存储端）
暴露接口：
- `ContextWriter`：上下文写入器类
  - `__init__(session_id, user_id, config, llm_client, metadata_store, db_session, context_repository, **kwargs)`：初始化
  - `compress_if_needed() -> CompressionReport`：按需执行压缩

#### store.py
职责：分层上下文存储（协调器）
暴露接口：
- `LayeredContextStore`：分层上下文存储类
  - `__init__(llm_client, config, session, user_id, session_id, **kwargs)`：初始化
  - `set_db_session(session, user_id, session_id)`：设置数据库会话
  - `set_fixed_prompt(system_prompt: str)`：设置系统提示词
  - `set_tools_description(tools_description: str)`：设置工具描述
  - `add_message(message: dict, persist_to_db: bool) -> str`：添加消息
  - `clear_messages()`：清空消息
  - `check_and_compress()`：检查并压缩
  - `read_compressed_layer(layer: str) -> list[str]`：读取压缩层
  - `read_message_layer() -> list[dict]`：读取消息层
  - `get_stats() -> dict`：获取统计信息
  - `inject_static_var(name: str, inject_type: str, query: str, top_k: int) -> str`：注入静态变量
  - `inject_memory(name: str, inject_type: str, query: str, top_k: int) -> str`：注入记忆

#### metadata_store.py
职责：块元数据存储
暴露接口：
- `ChunkMetadataStore`：块元数据存储类

#### structured.py
职责：结构化压缩器
暴露接口：
- `StructuredCompressor`：结构化压缩器类

#### store_manager.py
职责：存储管理器
暴露接口：
- 存储管理相关功能

### 测试策略
#### 组件测试
- 单元测试：压缩逻辑、预算计算、模型转换
- 集成测试：完整压缩流程、数据库操作

## 实现
→ 见代码文件
