"""Agent 自进化能力模块。

提供 Agent 自主扩展能力的完整闭环：
能力缺口分析 → 四层筛选 → 代码生成 → 契约校验 → 安全审查 → 热加载 → 日志记录

使用方式：
    from evolution import create_evolution_engine

    engine = create_evolution_engine(
        tool_registry=tool_registry,
        plugin_registry=plugin_registry,
    )

    result = engine.evolve("需要文件搜索能力")
    if result.success:
        print(f"进化成功: {result.loaded_plugin_name}")

暴露接口：
- create_evolution_engine: 工厂函数，创建进化引擎实例
- EvolutionEngine: 进化引擎类
- GapAnalyzer: 能力缺口分析器
- CodeGenerator: 代码生成器
- SecurityReviewer: 安全审查器
- HotLoader: 热加载器
- EvolutionLog: 进化日志管理器
- RollbackManager: 回滚管理器
"""

from __future__ import annotations

from evolution.code_generator import CodeGenerator
from evolution.engine import EvolutionEngine, create_evolution_engine
from evolution.evolution_log import EvolutionLog
from evolution.gap_analyzer import GapAnalyzer
from evolution.hot_loader import HotLoader
from evolution.rollback_manager import RollbackManager
from evolution.security_reviewer import SecurityReviewer
from evolution.types import (
    CapabilityGap,
    EvolutionRecord,
    EvolutionResult,
    EvolutionStatus,
    FilterLayer,
    FilterResult,
    GeneratedArtifact,
    GenerationType,
    SecurityReport,
)

__all__ = [
    # 工厂函数
    "create_evolution_engine",
    # 核心类
    "EvolutionEngine",
    "GapAnalyzer",
    "CodeGenerator",
    "SecurityReviewer",
    "HotLoader",
    "EvolutionLog",
    "RollbackManager",
    # 类型
    "EvolutionStatus",
    "FilterLayer",
    "GenerationType",
    "CapabilityGap",
    "FilterResult",
    "GeneratedArtifact",
    "SecurityReport",
    "EvolutionRecord",
    "EvolutionResult",
]
