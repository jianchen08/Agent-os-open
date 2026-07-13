# MEMORY.md — 记忆模块文档

## 需求

将旧代码 `src/memory/` 的核心类搬迁到新架构，实现：
1. ORM Model → dataclass，移除 Pydantic/SQLAlchemy 硬依赖
2. 数据库连接从构造函数注入，不在类内部创建
3. 新增 JSON 文件存储作为 MVP 默认后端
4. pgvector 存储可选化（try/except 降级）
5. 公共方法注册为管道插件

## 逻辑

### 三层决策检索模型
1. 第一层：筛选条件（memory_type, knowledge_id/name, tags, session_id）
2. 第二层：注入方式（full, retrieval, summary）
3. 第三层：检索方法（vector, keyword, tagwave）

### 递进压缩
- L0(原文) → L1(十模块摘要) → L2(三元组) → L3(关键词)
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

### 根级文件

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 模块入口 | 公共类和接口 |
| `types.py` | 数据模型（dataclass） | MemoryType, InjectType, RetrievalMethod, ContextType, Episode, Knowledge, ToolInfo, ContextRequest, Context, SearchResult, RetrievalConfig, TagInfo, CooccurrenceEntry, TagBoostResult |
| `ports.py` | 存储和检索抽象接口 | IMemoryStore, IRetriever, IEpisodeStorage, ISemanticStorage, StorageError, EpisodeNotFoundError, KnowledgeNotFoundError, StorageConnectionError |
| `constants.py` | 常量定义 | TokenBudget, Retrieval, MemoryTypeConst, Compression, ContextManagement, Storage, Similarity, Priority, Lifecycle, ErrorMessages, VectorDB, ImportExport |
| `service.py` | 记忆服务门面（三层决策检索） | MemoryService |
| `episode_service.py` | 情景记忆存储服务 | EpisodeService |
| `knowledge_service.py` | 语义知识存储服务 | KnowledgeService |
| `tag_network.py` | Tag 网络检索（透镜-拓展-聚焦三阶段算法） | TagNetworkConfig, TagCooccurrenceMatrix, TagNetworkRetriever |
| `tag_service.py` | Tag CRUD + 向量化 + 共现关系 | TagService |
| `wave_retriever.py` | 波浪算法 RAG 检索器 | WaveRetriever |
| `chunk_service.py` | 压缩块服务（JSON+PG 混合持久化） | ChunkService |
| `context_compressor.py` | 上下文压缩器（L0→L1→L2 递进压缩） | ContextCompressor（旧版根级实现） |
| `memory_context_service.py` | 记忆上下文服务（协调压缩和组装） | MemoryContextService |

### 子目录

#### `storage/` — 存储后端实现

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 存储模块入口 | JsonMemoryStore, PgVectorStore（可选） |
| `json_store.py` | JSON 文件存储（MVP 默认） | JsonMemoryStore |
| `pgvector_retriever.py` | pgvector 向量检索 | PgVectorRetriever |

`JsonMemoryStore` 同时实现 `IMemoryStore`、`IEpisodeStorage`、`ISemanticStorage` 三个接口。

#### `compressor/` — 压缩器核心模块

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 压缩器模块入口，统一导出+懒加载 | CompressionConfig, PreservedZone, MemoryExtraction, ContextCompressor |
| `config.py` | 压缩配置管理 | CompressionConfig, ContextBudget, load_context_window_config |
| `models.py` | 压缩数据模型 | ChunkMetadata, ChunkStatus, ContentRef, CompressionResult, CompressionReport, PreservedZone, MemoryExtraction |
| `core.py` | 核心压缩逻辑（L0→L1→L2→L3 递进） | ContextCompressor, normalize_layer_name, LAYER_NAME_MAP |

#### `maintenance/` — 维护服务

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 维护模块入口 | MemoryMaintenanceService |
| `service.py` | 维护服务主逻辑 | MemoryMaintenanceService |
| `review_engine.py` | 记忆审查引擎 | ReviewEngine |
| `cleanup_engine.py` | 记忆清理引擎 | CleanupEngine |
