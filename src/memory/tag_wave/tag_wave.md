# tag_wave

## 需求
### 职责
基于 VCPToolBox 的 TagMemo "浪潮"算法实现，提供高级语义检索能力。

### 对外接口
- 输入：查询文本/向量、用户 ID → 输出：增强的查询向量、检索结果

### 依赖
- 依赖组件：core/embedding（嵌入服务）
- 外部依赖：numpy（数值计算）、sqlalchemy（数据库操作）

## 逻辑
### 流程设计
1. EPA 投影分析：计算逻辑深度和熵
2. 残差金字塔分解：多级语义提取
3. Tag 网络扩展：共现矩阵联想
4. 向量检索召回：相似度计算
5. 结果去重：SVD + 残差投影
6. 动态 Beta 融合：增强查询向量

### 数据流向
```
查询向量 → EPA 分析 → 残差金字塔 → Tag 扩展 → 向量检索 → 去重 → 结果
```

### 核心算法
#### EPA（嵌入投影分析）
- K-Means 聚类生成语义中心
- 加权 PCA 构建正交基
- 投影分析计算逻辑深度和熵

#### 残差金字塔
- 投影到 Chunk 向量空间
- 提取 Top-K 相关 Chunks
- 计算残差向量
- 递归分解

#### 结果去重
- SVD 提取潜在主题
- 残差投影计算新信息量
- 贪心选择最大新信息量的结果

## 结构
### 文件清单（代码文件 - 具体接口）
#### __init__.py
职责：模块初始化，导出所有组件
暴露接口：
- `TagWaveConfig`：配置类
- `EPAProjectionResult`：EPA 投影结果
- `ResonanceResult`：共振结果
- `PyramidLevel`：金字塔层级
- `PyramidResult`：金字塔结果
- `SearchCandidate`：搜索候选
- `TagInfo`：标签信息
- `EPAModule`：EPA 模块
- `ResidualPyramid`：残差金字塔
- `ResultDeduplicator`：结果去重器
- `TagWaveRetriever`：浪潮检索器

#### types.py
职责：类型定义
暴露接口：
- `TagInfo`：标签信息
  - `id: int`：标签 ID
  - `name: str`：标签名称
  - `vector: np.ndarray`：标签向量
  - `frequency: int`：频率
- `EPAProjectionResult`：EPA 投影结果
  - `projections: np.ndarray`：投影值
  - `probabilities: np.ndarray`：概率分布
  - `entropy: float`：归一化熵
  - `logic_depth: float`：逻辑深度
  - `dominant_axes: list`：主导轴
- `ResonanceResult`：共振结果
  - `resonance: float`：共振值
  - `bridges: list`：桥梁连接
- `PyramidLevel`：金字塔层级
  - `level: int`：层级
  - `tags: list`：标签及贡献度
  - `projection_magnitude: float`：投影幅度
  - `residual_magnitude: float`：残差幅度
- `PyramidResult`：金字塔结果
  - `levels: list[PyramidLevel]`：层级列表
  - `total_explained_energy: float`：总解释能量
  - `final_residual: np.ndarray`：最终残差
- `SearchCandidate`：搜索候选
  - `id: str`：候选 ID
  - `content: str`：内容
  - `score: float`：得分
  - `vector: np.ndarray | None`：向量
  - `metadata: dict`：元数据
- `TagWaveConfig`：配置类
  - `max_basis_dim: int`：最大基维度
  - `cluster_count: int`：聚类数量
  - `dimension: int`：向量维度
  - `max_levels: int`：最大金字塔层级
  - `top_k: int`：Top-K 数量
  - `max_results: int`：最大结果数

#### epa_module.py
职责：EPA（嵌入投影分析）模块
暴露接口：
- `EPAModule`：EPA 模块类
  - `__init__(session, max_basis_dim: int, cluster_count: int, dimension: int)`：初始化
  - `initialize(force_refresh: bool) -> bool`：初始化正交基
  - `project(vector: np.ndarray) -> dict`：投影向量到语义空间
  - `detect_resonance(vector: np.ndarray) -> dict`：跨域共振检测

#### residual_pyramid.py
职责：残差金字塔模块
暴露接口：
- `ResidualPyramid`：残差金字塔类
  - `__init__(session, epa_module, max_levels: int, top_k: int, min_energy_ratio: float)`：初始化
  - `initialize(executor_type: str | None, executor_id: str | None, force_refresh: bool) -> bool`：初始化 Chunk 向量矩阵
  - `analyze(query_vector: np.ndarray, epa_result: dict, max_levels: int | None, top_k: int | None) -> dict`：执行残差金字塔分析

#### result_deduplicator.py
职责：结果去重器
暴露接口：
- `ResultDeduplicator`：结果去重器类
  - `__init__(config: TagWaveConfig | None)`：初始化
  - `deduplicate(candidates: list[SearchCandidate], max_results: int, topic_count: int, redundancy_threshold: float) -> list[SearchCandidate]`：去重
  - `compute_diversity_score(results: list[SearchCandidate]) -> float`：计算多样性分数
  - `rerank_by_diversity(results: list[SearchCandidate], query_vector: np.ndarray) -> list[SearchCandidate]`：多样性重排

#### tag_wave_retriever.py
职责：浪潮算法检索器（主入口）
暴露接口：
- `TagWaveRetriever`：浪潮检索器类
  - `__init__(session, embedding_service, config: TagWaveConfig | None, **kwargs)`：初始化
  - `initialize(executor_type: str | None, executor_id: str | None) -> bool`：初始化所有模块
  - `search(query: str, query_vector: np.ndarray | None, user_id: str | None, top_k: int, **kwargs) -> dict`：执行检索
  - `enhance_query(query: str, query_vector: np.ndarray | None) -> dict`：增强查询向量
  - `get_search_stats() -> dict`：获取检索统计

### 测试策略
#### 组件测试
- 单元测试：EPA 投影、金字塔分解、去重算法
- 集成测试：完整检索流程

## 实现
→ 见代码文件
