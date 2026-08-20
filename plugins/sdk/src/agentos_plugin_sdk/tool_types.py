"""工具定义类型——插件共享层。

定义工具的富数据模型（``Tool``）、来源/分类/级别枚举、注入参数标记，
以及成功/失败结果的工厂函数。供所有 MCP sidecar 工具共用。

来源：原 0.1 ``tools/types.py``。该模块被 task / triggers_ext / search /
task_submit 四个工具共同依赖（``Tool`` / ``ToolCategory`` / ``ToolLevel`` /
``ToolSource`` / ``create_success_result`` / ``create_failure_result``），
属于跨插件共享类型，故上提到 SDK 公共依赖层，不再依赖 0.1 兼容 shim。

适配说明：
- ``ToolExecutionResult`` 改从本包 ``results`` 导入。
- ``create_failure_result_with_code`` 依赖 0.1 ``core.errors.ErrorCode``，
  四个 sidecar 工具均未使用，故未上提（如需可后续补齐 errors 层）。
- ``Tool.to_runnable`` 依赖 0.1 ``core.runnable.ToolRunnable``，sidecar 工具
  未调用，保留方法体但其中的懒导入仅在显式调用时触发。

公共 API:
    ToolSource / ToolCategory / ToolLevel / ToolStatus: 枚举
    ToolExample / InjectedArg / InjectedParam: 辅助数据结构
    Tool: 工具定义（pydantic 模型）
    ToolUsageStats: 工具使用统计
    create_success_result / create_failure_result: 结果工厂函数
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agentos_plugin_sdk.results import ToolExecutionResult

__all__ = [
    "InjectedArg",
    "InjectedParam",
    "Tool",
    "ToolCategory",
    "ToolExample",
    "ToolLevel",
    "ToolResult",
    "ToolSource",
    "ToolStatus",
    "ToolUsageStats",
    "create_failure_result",
    "create_success_result",
]

logger = logging.getLogger(__name__)

ToolResult = ToolExecutionResult


class ToolSource(StrEnum):
    """工具来源"""

    CODE = "code"  # Python 代码
    BUILTIN = "builtin"  # 内置工具
    MCP = "mcp"  # MCP 协议
    HTTP = "http"  # HTTP API
    DATABASE = "database"  # 数据库配置


class ToolCategory(StrEnum):
    """工具功能分类"""

    FILE = "file"  # 文件操作
    FILE_SYSTEM = "file_system"  # 文件系统操作（目录、复制、移动等）
    SEARCH = "search"  # 搜索
    WEB = "web"  # Web 操作
    MEMORY = "memory"  # 记忆检索
    TASK = "task"  # 任务管理
    SYSTEM = "system"  # 系统工具
    EXECUTION = "execution"  # 执行
    ANALYSIS = "analysis"  # 分析
    EVALUATION = "evaluation"  # 评估
    AGENT = "agent"  # Agent调用
    MONITORING = "monitoring"  # 监控


class ToolLevel(StrEnum):
    """工具级别分类"""

    SYSTEM = "system"  # 系统级：内置、常用、每次都用
    USER = "user"  # 用户级：用户自定义、特定场景
    L1_ONLY = "l1_only"  # 只有L1能使用
    L1_L2_ONLY = "l1_l2_only"  # 只有L1和L2能使用
    ALL = "all"  # 所有层级都能使用


class ToolStatus(StrEnum):
    """工具状态"""

    ACTIVE = "active"  # 活跃
    DISABLED = "disabled"  # 禁用
    DEPRECATED = "deprecated"  # 已弃用


class ToolExample(BaseModel):
    """工具使用示例"""

    input: dict[str, Any] = Field(..., description="示例输入参数")
    output: Any | None = Field(None, description="预期输出")
    description: str | None = Field(None, description="示例说明")


class InjectedArg:
    """注入参数标记类。

    用于标记工具参数为运行时注入参数，这些参数：
    - 不出现在传给 LLM 的 tool_call_schema 中
    - 由系统在运行时自动注入（如 session_id, user_id, tool_record_id）
    """

    pass


# 预定义的常用注入参数
class InjectedParam:
    """预定义的注入参数常量"""

    SESSION_ID = "session_id"
    USER_ID = "user_id"
    TOOL_RECORD_ID = "tool_record_id"
    THREAD_ID = "thread_id"
    EXECUTION_ID = "execution_id"
    AGENT_ID = "agent_id"
    TASK_ID = "task_id"

    # 所有预定义注入参数列表
    ALL = [
        SESSION_ID,
        USER_ID,
        TOOL_RECORD_ID,
        THREAD_ID,
        EXECUTION_ID,
        AGENT_ID,
        TASK_ID,
    ]


class Tool(BaseModel):
    """工具定义。

    工具描述应该自包含，包括使用边界说明，
    这些信息会在 to_llm_format() 时合并到 description 中注入 LLM。
    """

    # 基础标识
    name: str = Field(..., min_length=1, description="工具唯一标识")
    description: str = Field(..., description="工具功能描述（简短）")

    # 使用边界说明（合并到 LLM description）
    when_to_use: list[str] = Field(default_factory=list, description="适用场景列表，说明什么情况下应该使用此工具")
    when_not_to_use: list[str] = Field(
        default_factory=list,
        description="不适用场景列表，说明什么情况下不应该使用此工具",
    )
    examples: list[ToolExample] = Field(default_factory=list, description="使用示例列表")
    caveats: list[str] = Field(default_factory=list, description="注意事项列表，使用时需要注意的问题")

    # Schema 定义
    input_schema: dict[str, Any] = Field(..., description="输入参数 JSON Schema")
    output_schema: dict[str, Any] | None = Field(default=None, description="输出 Schema")

    # 注入参数声明（运行时注入，不传给 LLM）
    injected_params: list[str] = Field(
        default_factory=list,
        description="注入参数列表：这些参数由系统在运行时注入，不暴露给 LLM 决策。"
        "如 session_id, user_id, tool_record_id",
    )

    # 参数层级限制：声明哪些参数/枚举值只在特定 Agent 层级可见
    param_level_restrictions: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "参数层级限制：控制参数在不同 Agent 层级的可见性和枚举值。"
            "支持两种限制: "
            "(1) max_visible_level: 整个参数仅对 <=该层级的 Agent 可见（如 max_visible_level=1 则仅 L1 可见）；"
            "(2) enum_restrictions: { 'enum_value': max_agent_level }，max_agent_level=0 所有层级可见"
        ),
    )

    # Schema 动态增强器
    schema_enricher: Callable[..., dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Schema 动态增强器：签名 (llm_format: dict, ctx: Any) -> dict，"
            "在每轮迭代时由 ToolSchemaPlugin 调用，可修改传给 LLM 的 schema（如注入可用 Provider 列表）。"
        ),
    )

    # 元数据
    source: ToolSource = Field(..., description="工具来源: code/mcp/http")
    category: ToolCategory | None = Field(default=None, description="工具功能分类")
    level: ToolLevel = Field(default=ToolLevel.USER, description="工具级别：system/user")
    version: str = Field(default="1.0.0", description="版本号")
    tags: list[str] = Field(default_factory=list, description="标签")
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展元数据")

    # 状态与权限
    status: ToolStatus = Field(default=ToolStatus.ACTIVE, description="工具状态")
    dangerous_operations: list[str] = Field(default_factory=list, description="危险操作列表，由安全插件统一决策审批")

    # 数据库同步字段
    db_id: str | None = Field(default=None, description="数据库记录 ID")
    checksum: str | None = Field(default=None, description="定义校验和，用于检测变更")

    # 业务字段（从 ToolServiceTool 合并）
    enabled: bool = Field(default=True, description="工具是否启用")
    permissions: list[str] = Field(default_factory=list, description="工具权限列表")
    parameters: dict[str, Any] = Field(default_factory=dict, description="参数定义（兼容字段）")
    handler: Callable[..., Any] | None = Field(default=None, description="工具处理函数")
    author: str = Field(default="system", description="工具作者")
    created_at: datetime | None = Field(default=None, description="创建时间")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """验证工具名称"""
        if not v or not v.strip():
            raise ValueError("工具名称不能为空")
        return v.strip()

    def build_full_description(self) -> str:
        """构建完整的工具描述（包含使用边界说明）"""
        parts = [self.description]

        if self.when_to_use:
            parts.append("\n【适用场景】")
            for item in self.when_to_use:
                parts.append(f"- {item}")

        if self.when_not_to_use:
            parts.append("\n【不适用场景】")
            for item in self.when_not_to_use:
                parts.append(f"- {item}")

        if self.caveats:
            parts.append("\n【注意事项】")
            for item in self.caveats:
                parts.append(f"- {item}")

        if self.examples:
            parts.append("\n【使用示例】")
            for i, example in enumerate(self.examples[:2], 1):  # 最多2个示例
                if example.description:
                    parts.append(f"示例{i}: {example.description}")
                parts.append(f"  输入: {example.input}")
                if example.output is not None:
                    parts.append(f"  输出: {example.output}")

        return "\n".join(parts)

    def to_llm_format(self, agent_level: int | None = None) -> dict[str, Any]:
        """转换为 LLM 可用的工具格式（OpenAI function calling 格式）

        Args:
            agent_level: 当前 Agent 层级，用于过滤参数枚举值
        """
        llm_schema = self._get_llm_schema(agent_level=agent_level)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.build_full_description(),
                "parameters": llm_schema,
            },
        }

    def _get_llm_schema(self, agent_level: int | None = None) -> dict[str, Any]:
        """获取传给 LLM 的 schema（排除注入参数 + 按层级过滤枚举值）。"""
        import copy  # noqa: PLC0415

        if not self.injected_params and not self.param_level_restrictions:
            return self.input_schema

        schema = copy.deepcopy(self.input_schema)

        if "properties" in schema:
            for param in self.injected_params:
                if param in schema["properties"]:
                    del schema["properties"][param]

        if "required" in schema and isinstance(schema["required"], list):
            schema["required"] = [r for r in schema["required"] if r not in self.injected_params]
            if not schema["required"]:
                del schema["required"]

        if agent_level is not None and self.param_level_restrictions:
            self._apply_level_restrictions(schema, agent_level)

        return schema

    def _apply_level_restrictions(self, schema: dict[str, Any], agent_level: int) -> None:  # noqa: PLR0912
        """根据 Agent 层级过滤 schema：隐藏参数 + 过滤枚举值。"""
        properties = schema.get("properties", {})

        # 第一遍：收集需要删除的参数（max_visible_level）
        to_remove: list[str] = []
        for param_name, restriction in self.param_level_restrictions.items():
            if param_name not in properties:
                continue
            max_visible = restriction.get("max_visible_level")
            if max_visible is not None and agent_level > max_visible:
                to_remove.append(param_name)

        for param_name in to_remove:
            del properties[param_name]
            logger.debug(
                "[ToolSchema] hidden param '%s' from L%d (max_visible=%d)",
                param_name,
                agent_level,
                self.param_level_restrictions[param_name]["max_visible_level"],
            )

        # 从 required 中移除已隐藏的参数
        if "required" in schema and isinstance(schema["required"], list):
            schema["required"] = [r for r in schema["required"] if r not in set(to_remove)]
            if not schema["required"]:
                del schema["required"]

        # 第二遍：过滤枚举值
        for param_name, restriction in self.param_level_restrictions.items():
            if param_name not in properties:
                continue
            enum_restrictions = restriction.get("enum_restrictions", {})
            if not enum_restrictions:
                continue
            prop_def = properties[param_name]
            if "enum" not in prop_def:
                continue
            allowed_values = []
            for value in prop_def["enum"]:
                max_level = enum_restrictions.get(value, 0)
                if max_level == 0 or agent_level <= max_level:
                    allowed_values.append(value)
            if allowed_values:
                prop_def["enum"] = allowed_values
                if len(allowed_values) == 1:
                    prop_def.pop("default", None)

    def get_tool_call_schema(self) -> dict[str, Any]:
        """获取工具调用 schema（排除注入参数）"""
        return self._get_llm_schema()

    def to_llm_yaml_format(self) -> str:
        """转换为 LLM 可用的 YAML 格式工具描述（节省 token）"""
        import yaml  # type: ignore[import-untyped]  # noqa: PLC0415（第三方 stubs 缺失，懒导入）

        llm_schema = self._get_llm_schema()

        tool_desc = {
            "name": self.name,
            "desc": self.description,
            "params": self._simplify_schema(llm_schema),
        }

        dumped = yaml.dump(tool_desc, default_flow_style=False, allow_unicode=True)
        return str(dumped)

    def _simplify_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """简化 JSON Schema 为更紧凑的 YAML 格式"""
        if not isinstance(schema, dict):
            return schema

        simplified: dict[str, Any] = {}

        if "properties" in schema:
            simplified["props"] = {}
            for prop_name, prop_def in schema["properties"].items():
                prop_simple = {}

                if "type" in prop_def:
                    prop_simple["type"] = prop_def["type"]

                if "description" in prop_def:
                    prop_simple["desc"] = prop_def["description"]

                if "default" in prop_def:
                    prop_simple["default"] = prop_def["default"]

                if "enum" in prop_def:
                    prop_simple["enum"] = prop_def["enum"]

                simplified["props"][prop_name] = prop_simple

        if "required" in schema:
            simplified["required"] = schema["required"]

        return simplified

    def model_dump_yaml(self) -> dict[str, Any]:
        """转换为 YAML 友好的字典格式"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "source": self.source,
            "category": self.category if self.category else None,
        }

    def to_mcp_format(self) -> dict[str, Any]:
        """转换为 MCP 工具格式"""
        return {
            "name": self.name,
            "description": self.build_full_description(),
            "inputSchema": self.input_schema,
        }

    def compute_checksum(self) -> str:
        """计算工具定义的校验和"""
        import hashlib  # noqa: PLC0415
        import json  # noqa: PLC0415

        data = {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "caveats": self.caveats,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "version": self.version,
        }
        content = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_runnable(
        self,
        handler: Callable[[dict[str, Any]], Coroutine[Any, Any, ToolExecutionResult]],
    ) -> Any:
        """转换为 ToolRunnable。

        依赖 0.1 ``core.runnable.ToolRunnable``（0.2 暂无对应物）。
        sidecar 工具不调用此方法；仅在显式调用时触发懒导入。
        """
        from core.runnable import ToolRunnable  # type: ignore[import-not-found]  # noqa: PLC0415（0.1 依赖，懒导入）

        return ToolRunnable(
            name=self.name,
            description=self.description,
            handler=handler,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            metadata={
                "source": self.source,
                "category": self.category if self.category else None,
                "level": self.level,
                "version": self.version,
                "tags": self.tags,
                **self.metadata,
            },
        )


def create_success_result(
    data: Any = None,
    metadata: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> ToolExecutionResult:
    """创建成功结果"""
    return ToolExecutionResult.create_completed(
        output=data,
        metadata=metadata or {},
        duration_ms=duration_ms,
    )


def create_failure_result(
    error: str,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    """创建失败结果"""
    return ToolExecutionResult.create_failed(
        error=error,
        error_code=error_code,
        metadata=metadata or {},
    )


@dataclass
class ToolUsageStats:
    """工具使用统计"""

    tool_name: str
    total_calls: int = 0
    success_calls: int = 0
    failed_calls: int = 0
    total_duration: float = 0.0
    avg_duration: float = 0.0
    last_used: datetime | None = None
    error_rate: float = 0.0
