# adapters

## 需求
### 职责
提供记忆模块的存储适配器实现，将领域模型转换为数据库模型，支持多种存储后端。

### 对外接口
- 输入：Episode/Knowledge 领域对象 → 输出：存储结果（ID）

### 依赖
- 依赖组件：memory/ports（存储接口定义）、memory/types（领域类型）
- 外部依赖：sqlalchemy（异步数据库操作）

## 逻辑
### 流程设计
1. 接收领域模型对象
2. 转换为数据库模型
3. 执行数据库操作（保存/查询/更新/删除）
4. 将数据库模型转换回领域模型

### 数据流向
```
领域模型 → 数据库模型转换 → SQLAlchemy 操作 → 数据库模型 → 领域模型转换
```

### 错误处理
- 存储错误：StorageError
- 连接错误：StorageConnectionError
- 记录不存在：EpisodeNotFoundError、KnowledgeNotFoundError

## 结构
### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，导出存储适配器
暴露接口：
- `DBEpisodeStorage`：情景记忆数据库存储
- `DBSemanticStorage`：语义记忆数据库存储

#### db_storage.py
职责：数据库存储适配器实现
暴露接口：
- `DBEpisodeStorage(IEpisodeStorage)`：情景记忆数据库存储
  - `__init__(session_factory)`：初始化
  - `save(episode: Episode) -> str`：保存情景记忆
  - `get(episode_id: UUID) -> Episode | None`：获取情景记忆
  - `find_by_user(user_id: UUID, limit: int, offset: int) -> list[Episode]`：按用户查找
  - `search(query: str, user_id: UUID, limit: int, filters: dict | None) -> list[SearchResult]`：搜索
  - `update(episode_id: UUID, **kwargs) -> bool`：更新
  - `delete(episode_id: UUID) -> bool`：删除
  - `count_by_user(user_id: UUID) -> int`：统计数量

- `DBSemanticStorage(ISemanticStorage)`：语义记忆数据库存储
  - `__init__(session_factory)`：初始化
  - `save(knowledge: Knowledge) -> str`：保存知识
  - `get(knowledge_id: UUID) -> Knowledge | None`：获取知识
  - `find_by_user(user_id: UUID, limit: int) -> list[Knowledge]`：按用户查找
  - `search(query: str, user_id: UUID, limit: int, domain: str | None) -> list[SearchResult]`：搜索
  - `update_embedding(knowledge_id: UUID, embedding: list[float]) -> bool`：更新向量
  - `delete(knowledge_id: UUID) -> bool`：删除

#### file_storage.py
职责：文件系统存储适配器（预留实现）
暴露接口：
- `FileEpisodeStorage(IEpisodeStorage)`：情景记忆文件存储（预留）
- `FileSemanticStorage(ISemanticStorage)`：语义记忆文件存储（预留）

### 测试策略
#### 组件测试
- 单元测试：模型转换、查询构建
- 集成测试：与数据库的集成

## 实现
→ 见代码文件
