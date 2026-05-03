"""进化模块类型定义。

定义 Agent 自进化能力所需的所有数据类型，包括枚举、数据类等。

暴露接口：
- FilterLayer: 四层筛选层级枚举
- EvolutionStatus: 进化状态枚举
- GenerationType: 代码生成类型枚举
- CapabilityGap: 能力缺口数据类
- FilterResult: 四层筛选结果数据类
- GeneratedArtifact: 生成的代码产物数据类
- SecurityReport: 安全审查报告数据类
- EvolutionRecord: 进化日志记录数据类
- EvolutionResult: 进化结果数据类
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FilterLayer(Enum):
    """四层筛选层级。

    从低到高，优先使用低层方案：
    - TOOL: 已有工具可直接满足
    - CONFIG: 通过配置变更满足
    - PLUGIN: 通过安装/生成插件满足
    - CORE: 需要核心代码修改（最高成本）
    """

    TOOL = "tool"
    CONFIG = "config"
    PLUGIN = "plugin"
    CORE = "core"


class EvolutionStatus(Enum):
    """进化流程状态。"""

    IDLE = "idle"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    LOADING = "loading"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"


class GenerationType(Enum):
    """代码生成类型。"""

    BUILTIN_TOOL = "builtin_tool"
    MCP_SERVER = "mcp_server"


@dataclass
class CapabilityGap:
    """能力缺口描述。

    Attributes:
        missing_capability: 缺失的能力描述
        required_by: 谁需要这个能力
        priority: 优先级 1-10（1 最高）
        suggested_layer: 建议的筛选层
        context: 附加上下文信息
    """

    missing_capability: str
    required_by: str
    priority: int = 5
    suggested_layer: FilterLayer = FilterLayer.PLUGIN
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterResult:
    """四层筛选结果。

    Attributes:
        gap: 原始能力缺口
        tool_layer_result: 工具层检查结果描述
        config_layer_result: 配置层检查结果描述
        plugin_layer_result: 插件层检查结果描述
        core_layer_result: 核心层检查结果描述
        recommended_action: 推荐的动作描述
        recommended_layer: 推荐的筛选层
    """

    gap: CapabilityGap
    tool_layer_result: str | None = None
    config_layer_result: str | None = None
    plugin_layer_result: str | None = None
    core_layer_result: str | None = None
    recommended_action: str = ""
    recommended_layer: FilterLayer = FilterLayer.PLUGIN


@dataclass
class GeneratedArtifact:
    """生成的代码产物。

    Attributes:
        generation_type: 生成类型（BuiltinTool / MCP Server）
        code: 生成的代码字符串
        file_path: 目标文件路径
        contract_valid: 契约校验是否通过
        contract_errors: 契约校验错误列表
    """

    generation_type: GenerationType
    code: str
    file_path: str
    contract_valid: bool = False
    contract_errors: list[str] = field(default_factory=list)


@dataclass
class SecurityReport:
    """安全审查报告。

    Attributes:
        passed: 审查是否通过
        static_analysis_issues: 静态分析发现的问题列表
        sandbox_result: 沙箱执行结果
        permission_issues: 权限问题列表
        resource_violations: 资源限制违规列表
        overall_risk: 综合风险等级（low/medium/high/critical）
    """

    passed: bool = False
    static_analysis_issues: list[dict[str, Any]] = field(default_factory=list)
    sandbox_result: dict[str, Any] | None = None
    permission_issues: list[str] = field(default_factory=list)
    resource_violations: list[str] = field(default_factory=list)
    overall_risk: str = "unknown"


@dataclass
class EvolutionRecord:
    """进化日志记录。

    Attributes:
        record_id: 记录唯一标识
        timestamp: 记录时间戳
        capability_gap: 能力缺口描述
        filter_result: 四层筛选结果
        generated_artifact: 生成的代码产物
        security_report: 安全审查报告
        status: 当前进化状态
        error_message: 错误信息
        rollback_point: 回滚点 ID
    """

    record_id: str
    timestamp: str
    capability_gap: str
    filter_result: FilterResult | None = None
    generated_artifact: GeneratedArtifact | None = None
    security_report: SecurityReport | None = None
    status: EvolutionStatus = EvolutionStatus.IDLE
    error_message: str = ""
    rollback_point: str | None = None


@dataclass
class EvolutionResult:
    """进化操作最终结果。

    Attributes:
        success: 进化是否成功
        record: 进化记录
        loaded_plugin_name: 加载的插件名称
        message: 结果描述信息
    """

    success: bool = False
    record: EvolutionRecord | None = None
    loaded_plugin_name: str = ""
    message: str = ""
