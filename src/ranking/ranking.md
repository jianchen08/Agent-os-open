# ranking

## 需求
### 职责
提供工具、工作流、Agent 的智能推荐和排行服务，基于语义相似度、历史成功率和用户行为进行推荐。

### 对外接口
- 输入：用户意图/任务描述、用户ID、过滤条件
- 输出：推荐结果列表、排行榜、统计数据

### 依赖
- 依赖模块：db（数据库模型）、memory（向量检索）

## 逻辑
### 流程设计
1. 用户发起推荐请求（意图/任务描述）
2. 系统进行语义相似度匹配（可选）
3. 结合历史成功率和用户行为进行排序
4. 返回推荐结果列表

### 数据流向
```
用户请求 → Recommender/RankingService → 数据库查询 → 结果融合 → 返回推荐列表
```

### API设计
#### 模块API
| 方法 | 职责 |
|------|------|
| `recommend_tools()` | 推荐工具 |
| `recommend_workflows()` | 推荐工作流 |
| `recommend_agents()` | 推荐 Agent |
| `collaborative_recommend()` | 协同过滤推荐 |
| `get_tool_ranking()` | 获取工具排行榜 |
| `get_workflow_ranking()` | 获取工作流排行榜 |
| `get_agent_ranking()` | 获取 Agent 排行榜 |
| `get_trending_tools()` | 获取热门工具 |

### 配置设计
#### 模块配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| min_success_rate | 最低成功率过滤 | 0.5 |
| min_avg_score | 最低平均分过滤 | 0.6 |
| default_limit | 默认返回数量 | 5 |

### 错误处理
- 数据库查询失败：返回空列表
- 参数无效：使用默认值

## 结构
### 文件清单（代码文件 - 具体接口）
#### confidence.py
职责：置信度计算器
暴露接口：
- `ConfidenceCalculator`：置信度计算类
- `ConfidenceResult`：置信度结果类
- `calculate_tool_confidence(tool_id, user_context) -> ConfidenceResult`：计算工具置信度
- `calculate_workflow_confidence(workflow_id, user_context) -> ConfidenceResult`：计算工作流置信度
- `calculate_agent_confidence(agent_id, task_description, user_context) -> ConfidenceResult`：计算 Agent 置信度

#### experience.py
职责：执行经验记录服务
暴露接口：
- `ExperienceService`：经验服务类
- `record_experience(...)`：记录执行经验
- `find_similar_experiences(unit_id, intent_vector, limit) -> list`：查找相似经验
- `get_best_params_for_scene(unit_id, intent_text) -> dict`：获取最佳参数
- `get_common_errors(unit_id, limit) -> list`：获取常见错误

#### recommender.py
职责：智能推荐器
暴露接口：
- `Recommender`：推荐器类
- `RecommendationResult`：推荐结果类
- `recommend_tools(user_intent, user_id, limit) -> list`：推荐工具
- `recommend_workflows(user_intent, user_id, limit) -> list`：推荐工作流
- `recommend_agents(task_description, user_id, limit) -> list`：推荐 Agent
- `collaborative_recommend(user_id, item_type, limit) -> list`：协同过滤推荐

#### service.py
职责：排行服务
暴露接口：
- `RankingService`：排行服务类
- `get_tool_ranking(user_id, category, time_range, limit) -> list`：获取工具排行
- `get_workflow_ranking(user_id, workflow_type, time_range, limit) -> list`：获取工作流排行
- `get_agent_ranking(user_id, agent_type, time_range, limit) -> list`：获取 Agent 排行
- `get_user_success_stats(user_id, time_range) -> dict`：获取用户成功统计
- `get_trending_tools(user_id, days, limit) -> list`：获取热门工具
- `get_unified_ranking(unit_type, user_id, time_range, limit) -> list`：获取统一排行

### 测试策略
#### 模块测试
- 单元测试：各计算函数、排序算法
- 集成测试：推荐流程、排行查询
- Mock策略：数据库 Mock、向量检索 Mock

## 实现
→ 见代码文件
