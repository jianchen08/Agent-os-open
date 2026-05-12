# 排行与推荐系统模块

提供工具/工作流排行、智能推荐、置信度评估和执行经验管理功能。

## 模块结构

```
src/ranking/
├── __init__.py        # 模块导出
├── service.py         # 排行服务
├── recommender.py     # 推荐器
├── confidence.py      # 置信度计算器
└── experience.py      # 执行经验服务
```

## 核心功能

### 1. 排行服务 (RankingService)

统计工具/工作流/Agent 的使用次数和成功率，支持多维度排行。

**主要方法**:

- `get_tool_ranking()` - 工具排行榜
- `get_workflow_ranking()` - 工作流排行榜
- `get_agent_ranking()` - Agent 排行榜
- `get_unified_ranking()` - 统一执行单元排行榜（新）
- `get_experience_stats()` - 执行单元详细统计（新）
- `get_scene_performance()` - 场景表现分析（新）
- `get_user_success_stats()` - 用户成功统计
- `get_trending_tools()` - 热门工具趋势

**使用示例**:

```python
from sqlalchemy.ext.asyncio import AsyncSession
from src.ranking import RankingService

async def get_rankings(session: AsyncSession):
    ranking_service = RankingService(session)

    # 获取统一执行单元排行榜（推荐使用）
    units = await ranking_service.get_unified_ranking(
        unit_type="tool",        # tool/agent/workflow，None 表示全部
        time_range=30,           # 最近30天
        limit=10,
        order_by="success_rate"  # success_count/success_rate/avg_score/last_used
    )

    for unit in units:
        print(f"{unit['rank']}. {unit['name']}: 成功率 {unit['success_rate']:.2%}")

    # 获取执行单元详细统计
    stats = await ranking_service.get_experience_stats(
        unit_id="xxx",
        time_range=30
    )
    print(f"成功率: {stats['success_rate']:.2%}")
    print(f"常见错误: {stats['error_distribution']}")
```

### 2. 执行经验服务 (ExperienceService) - 新增

管理执行经验的记录、查询和统计，支持场景关联分析。

**主要方法**:

- `record_experience()` - 记录执行经验
- `get_or_create_unit()` - 获取或创建执行单元
- `find_similar_experiences()` - 查找相似场景经验
- `get_best_params_for_scene()` - 获取最佳参数组合
- `get_common_errors()` - 获取常见错误
- `register_workflow_composition()` - 注册工作流组成
- `get_workflow_children()` - 获取工作流子单元

**使用示例**:

```python
from src.ranking import ExperienceService

async def record_tool_usage(session: AsyncSession):
    exp_service = ExperienceService(session)

    # 记录执行经验
    experience = await exp_service.record_experience(
        unit_type="tool",
        ref_id="tool_uuid",
        user_id="user_uuid",
        status="success",  # success/failed/partial/cancelled
        intent_text="用户想要搜索文件",
        input_params={"pattern": "*.py"},
        output_summary="找到 10 个文件",
        score=0.9,
        duration_ms=150,
    )

    # 查找相似场景的成功经验
    similar = await exp_service.find_similar_experiences(
        unit_id=experience.unit_id,
        intent_vector=[...],  # 意图向量
        limit=5
    )

    # 获取最佳参数
    best_params = await exp_service.get_best_params_for_scene(
        unit_id=experience.unit_id,
        intent_text="搜索文件"
    )
```

### 2. 推荐器 (Recommender)

基于语义相似度和历史成功率智能推荐工具/工作流/Agent。

**主要方法**:

- `recommend_tools()` - 推荐工具
- `recommend_workflows()` - 推荐工作流
- `recommend_agents()` - 推荐 Agent
- `collaborative_recommend()` - 协同过滤推荐

**使用示例**:

```python
from src.ranking import Recommender

async def get_recommendations(session: AsyncSession):
    recommender = Recommender(session)

    # 根据用户意图推荐工具
    tools = await recommender.recommend_tools(
        user_intent="我需要搜索文件",
        user_id=user_id,
        limit=5,
        min_success_rate=0.7
    )

    for tool in tools:
        print(f"{tool.name}: {tool.score:.2f}")
        print(f"  理由: {tool.reason}")
        print(f"  置信度: {tool.confidence:.2f}")

    # 推荐工作流
    workflows = await recommender.recommend_workflows(
        user_intent="数据处理流程",
        limit=5
    )

    # 协同过滤推荐（基于用户行为）
    collab_tools = await recommender.collaborative_recommend(
        user_id=user_id,
        item_type="tool",
        limit=5
    )
```

### 3. 置信度计算器 (ConfidenceCalculator)

评估工具/工作流执行成功概率，考虑历史表现和上下文匹配度。

**主要方法**:

- `calculate_tool_confidence()` - 计算工具执行置信度
- `calculate_workflow_confidence()` - 计算工作流执行置信度
- `calculate_agent_confidence()` - 计算 Agent 执行置信度

**使用示例**:

```python
from src.ranking import ConfidenceCalculator

async def evaluate_confidence(session: AsyncSession, tool_id: str):
    calculator = ConfidenceCalculator(session)

    # 计算工具执行置信度
    result = await calculator.calculate_tool_confidence(
        tool_id=tool_id,
        user_context={
            "user_id": str(user_id),
            "preferred_types": ["code", "api"]
        }
    )

    print(f"置信度分数: {result.confidence_score:.2f}")
    print(f"成功概率: {result.success_probability:.2%}")
    print("\n置信理由:")
    for reason in result.reasons:
        print(f"  + {reason}")

    print("\n风险因素:")
    for risk in result.risk_factors:
        print(f"  - {risk}")

    print("\n建议:")
    for suggestion in result.suggestions:
        print(f"  * {suggestion}")
```

## 数据模型

### RecommendationResult

推荐结果对象:

```python
{
    "id": "uuid",
    "type": "tool|workflow|agent",
    "name": "名称",
    "description": "描述",
    "score": 0.85,          # 推荐得分 (0-1)
    "confidence": 0.90,     # 置信度 (0-1)
    "reason": "推荐理由",
    "metadata": {}          # 额外元数据
}
```

### ConfidenceResult

置信度评估结果:

```python
{
    "id": "uuid",
    "type": "tool|workflow|agent",
    "confidence_score": 0.85,     # 置信度分数 (0-1)
    "success_probability": 0.78,  # 成功概率 (0-1)
    "reasons": ["理由1", "理由2"],
    "risk_factors": ["风险1"],
    "suggestions": ["建议1"]
}
```

## 排行榜结果格式

```python
{
    "rank": 1,
    "id": "uuid",
    "name": "工具名称",
    "description": "描述",
    "category": "分类",
    "success_count": 100,
    "success_rate": 0.95,
    "avg_score": 0.88,
    "last_used_at": "2024-01-01T00:00:00",
    "created_at": "2023-01-01T00:00:00"
}
```

## 推荐算法说明

### 1. 语义相似度推荐

- 使用关键词匹配和向量相似度
- 权重: 语义 70% + 热门度 30%

### 2. 协同过滤推荐

- 基于用户历史使用偏好
- 标签匹配度计算

### 3. 置信度评估因素

- **历史表现** (40%): 成功次数、平均分数
- **时效性** (20%): 最近使用时间
- **上下文匹配** (30%): 用户偏好、类型匹配
- **其他因素** (10%): 审批要求、复杂度等

## 集成示例

### API 集成

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.ranking import RankingService, Recommender

router = APIRouter(prefix="/ranking", tags=["ranking"])

@router.get("/tools")
async def get_tool_rankings(
    category: str = None,
    limit: int = 10,
    session: AsyncSession = Depends(get_session)
):
    service = RankingService(session)
    rankings = await service.get_tool_ranking(
        category=category,
        limit=limit
    )
    return {"rankings": rankings}

@router.post("/recommend/tools")
async def recommend_tools(
    user_intent: str,
    limit: int = 5,
    session: AsyncSession = Depends(get_session)
):
    recommender = Recommender(session)
    recommendations = await recommender.recommend_tools(
        user_intent=user_intent,
        limit=limit
    )
    return {
        "recommendations": [r.to_dict() for r in recommendations]
    }
```

## 性能优化建议

1. **缓存排行结果**: 排行数据可以缓存 5-10 分钟
2. **异步计算**: 推荐和置信度计算耗时较长，建议后台处理
3. **索引优化**: 确保数据库有合适的索引
   - `success_count` 降序索引
   - `last_used_at` 时间索引
   - `created_by` 用户索引

## 扩展方向

1. **机器学习模型**: 使用更先进的推荐算法
2. **实时更新**: 基于 WebSocket 推送排行变化
3. **A/B 测试**: 测试不同推荐策略的效果
4. **个性化**: 基于用户行为的深度个性化

## 依赖项

- SQLAlchemy 2.0+
- Pydantic 2.0+
- numpy (向量计算)
- sentence-transformers (可选，用于语义搜索)

## 测试

```bash
# 验证模块
python tests/verify_ranking.py

# 运行单元测试
pytest tests/test_ranking.py -v
```
