# MEMORY.md — 记忆模块文档

## 需求

将旧代码 `src/memory/` 的核心类搬迁到新架构，实现：
1. ORM Model → dataclass，移除 Pydantic/SQLAlchemy 硬依赖
2. 数据库连接从构造函数注入，不在类内部创建
3. 新增 JSON 文件存储作为 MVP 默认后端
4. pgvector 存储可选化（try/except 降级）
5. 公共方法注册为管道插件

## 逻辑

### 搬迁策略
- **不重写功能逻辑**，只搬过来适配
- 保持旧代码的核心算法不变（如 Tag 网络三阶段检索、递进压缩算法）
- SQLAlchemy 是可选依赖，业务逻辑不依赖 ORM query
- numpy/cachetools 降级为纯 Python 实现

### 三层决策检索模型
1. 第一层：筛选条件（memory_type, knowledge_id/name, tags, session_id）
2. 第二层：注入方式（full, retrieval, summary）
3. 第三层：检索方法（vector, keyword, tagwave）

### 递进压缩
- L0(原文) → L1(十模块摘要) → L2(三元组)
- 超过 token 阈值时触发
- LLM 调用通过注入的 callable 实现，不硬依赖 langchain

### 存储分层
- `IMemoryStore`：统一存储接口（save/load/delete/search）
- `IEpisodeStorage`：情景记忆专用接口
- `ISemanticStorage`：语义记忆专用接口
- `IRetriever`：统一检索接口
- 默认实现：`JsonMemoryStore`（同时实现三个接口）
- 可选实现：`PgVectorStore`（需 sqlalchemy + psycopg2）

## 结构

### 本文件夹文件

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 模块入口，统一导出 | 所有公共类和接口 |
| `types.py` | 数据模型（dataclass） | MemoryType, InjectType, RetrievalMethod, ContextType, Episode, Knowledge, ToolInfo, ContextRequest, Context, SearchResult, RetrievalConfig, TagInfo, CooccurrenceEntry, TagBoostResult |
| `ports.py` | 存储和检索抽象接口 | IMemoryStore, IRetriever, IEpisodeStorage, ISemanticStorage, StorageError, EpisodeNotFoundError, KnowledgeNotFoundError, StorageConnectionError |
| `constants.py` | 常量定义 | TokenBudget, Retrieval, MemoryTypeConst, Compression, ContextManagement, Storage, Similarity, Priority, Lifecycle, ErrorMessages, VectorDB, ImportExport |
| `service.py` | 记忆服务门面（三层决策） | MemoryService |
| `episode_service.py` | 情景记忆存储服务 | EpisodeService |
| `knowledge_service.py` | 语义知识存储服务 | KnowledgeService |
| `tag_network.py` | Tag 网络检索（透镜-拓展-聚焦） | TagNetworkConfig, TagCooccurrenceMatrix, TagNetworkRetriever |
| `context_compressor.py` | 上下文压缩器（L0→L1→L2） | CompressionConfig, ContextCompressor, normalize_layer_name |
| `memory_context_service.py` | 记忆上下文服务（协调压缩和组装） | MemoryContextService |
| `history_buffer.py` | 对话历史缓冲区 | MessageEntry, HistoryBuffer, ConversationHistory |
| `variable_priority.py` | 变量优先级枚举 | VariablePriority |
| `memory_metrics.py` | 记忆系统监控指标（检索延迟/命中率/存储容量） | MemoryMetrics |

### 子文件夹

#### `storage/` — 存储后端实现

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 存储模块入口 | JsonMemoryStore, PgVectorStore（可选） |
| `json_store.py` | JSON 文件存储（MVP 默认） | JsonMemoryStore |
| `pgvector_store.py` | pgvector 存储（可选依赖） | PgVectorStore |

`JsonMemoryStore` 同时实现 `IMemoryStore`、`IEpisodeStorage`、`ISemanticStorage` 三个接口。
`PgVectorStore` 同时实现 `IEpisodeStorage`、`ISemanticStorage` 两个接口。

#### `plugins/` — 管道插件

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 插件模块入口 | MemoryReadPlugin, MemoryWritePluginNew, KnowledgeInjectPluginNew, ContextCompressPlugin |
| `memory_read.py` | 记忆读取（IRetriever → IInputPlugin） | MemoryReadPlugin |
| `memory_write.py` | 记忆写入（IMemoryStore → IOutputPlugin） | MemoryWritePlugin |
| `knowledge_inject.py` | 知识注入（KnowledgeService → IInputPlugin） | KnowledgeInjectPlugin |
| `context_compress.py` | 上下文压缩（ContextCompressor → IOutputPlugin） | ContextCompressPlugin |

注意：`plugins/` 下的插件与 M6 的 `src/agent_os/plugins/` 下的 Mock 插件是不同的文件，不修改 M6 的实现。
