"""
工具类型定义

暴露接口：
- create_success_result(data: Any, metadata: dict[str, Any] | None, duration_ms: int | None) -> 'ToolExecutionResult'：create_success_result功能
- create_failure_result(error: str, error_code: str | None, metadata: dict[str, Any] | None) -> 'ToolExecutionResult'：create_failure_result功能
- create_failure_result_with_code(error_code: ErrorCode, details: str, metadata: dict[str, Any] | None) -> 'ToolExecutionResult'：create_failure_result_with_code功能
- validate_name(cls, v: str) -> str：validate_name功能
- build_full_description(self) -> str：build_full_description功能
- to_llm_format(self) -> dict[str, Any]：to_llm_format功能
- get_tool_call_schema(self) -> dict[str, Any]：get_tool_call_schema功能
- to_llm_yaml_format(self) -> str：to_llm_yaml_format功能
- model_dump_yaml(self) -> dict[str, Any]：model_dump_yaml功能
- to_mcp_format(self) -> dict[str, Any]：to_mcp_format功能
- compute_checksum(self) -> str：compute_checksum功能
- to_runnable(self, handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]) -> 'ToolRunnable'：to_runnable功能
- ToolSource：ToolSource类
- ToolCategory：ToolCategory类
- ToolLevel：ToolLevel类
- ToolStatus：ToolStatus类
- ToolExample：ToolExample类
- InjectedArg：InjectedArg类
- InjectedParam：InjectedParam类
- Tool：Tool类
- ToolUsageStats：ToolUsageStats类
"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from core.errors import ErrorCode
from core.results import ToolExecutionResult

if TYPE_CHECKING:
    from core.runnable import ToolRunnable

ToolResult = ToolExecutionResult


class ToolSource(str, Enum):
    """工具来源"""

    CODE = "code"  # Python 代码
    BUILTIN = "builtin"  # 内置工具
    MCP = "mcp"  # MCP 协议
    HTTP = "http"  # HTTP API
    DATABASE = "database"  # 数据库配置


class ToolCategory(str, Enum):
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


class ToolLevel(str, Enum):
    """工具级别分类"""

    SYSTEM = "system"  # 系统级：内置、常用、每次都用
    USER = "user"  # 用户级：用户自定义、特定场景
    L1_ONLY = "l1_only"  # 只有L1能使用
    L1_L2_ONLY = "l1_l2_only"  # 只有L1和L2能使用
    ALL = "all"  # 所有层级都能使用


class ToolStatus(str, Enum):
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
    """
    注入参数标记类

    用于标记工具参数为运行时注入参数，这些参数：
    - 不出现在传给 LLM 的 tool_call_schema 中
    - 由系统在运行时自动注入（如 session_id, user_id, tool_record_id）

    使用方式（类似 LangChain InjectedToolArg）：
        from typing import Annotated
        from tools.types import InjectedArg

        class MyToolSchema(BaseModel):
            query: str  # LLM 决策参数
            user_id: Annotated[str, InjectedArg]  # 系统注入参数

    或者在 Tool 定义中：
        Tool(
            name="my_tool",
            input_schema={...},
            injected_params=["user_id", "session_id"],  # 声明注入参数
        )
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
    """
    工具定义

    工具描述应该自包含，包括使用边界说明，
    这些信息会在 to_llm_format() 时合并到 description 中注入 LLM。
    """

    # 基础标识
    name: str = Field(..., min_length=1, description="工具唯一标识")
    description: str = Field(..., description="工具功能描述（简短）")

    # 使用边界说明（新增，合并到 LLM description）
    when_to_use: list[str] = Field(
        default_factory=list, description="适用场景列表，说明什么情况下应该使用此工具"
    )
    when_not_to_use: list[str] = Field(
        default_factory=list,
        description="不适用场景列表，说明什么情况下不应该使用此工具",
    )
    examples: list[ToolExample] = Field(
        default_factory=list, description="使用示例列表"
    )
    caveats: list[str] = Field(
        default_factory=list, description="注意事项列表，使用时需要注意的问题"
    )

    # Schema 定义
    input_schema: dict[str, Any] = Field(..., description="输入参数 JSON Schema")
    output_schema: dict[str, Any] | None = Field(None, description="输出 Schema")

    # 注入参数声明（运行时注入，不传给 LLM）
    injected_params: list[str] = Field(
        default_factory=list,
        description="注入参数列表：这些参数由系统在运行时注入，不暴露给 LLM 决策。如 session_id, user_id, tool_record_id"
    )

    # 元数据
    source: ToolSource = Field(..., description="工具来源: code/mcp/http")
    category: ToolCategory | None = Field(None, description="工具功能分类")
    level: ToolLevel = Field(ToolLevel.USER, description="工具级别：system/user")
    version: str = Field("1.0.0", description="版本号")
    tags: list[str] = Field(default_factory=list, description="标签")
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展元数据")

    # 状态与权限
    status: ToolStatus = Field(ToolStatus.ACTIVE, description="工具状态")
    dangerous_operations: list[str] = Field(
        default_factory=list, description="危险操作列表，由安全插件统一决策审批"
    )

    # 数据库同步字段
    db_id: str | None = Field(None, description="数据库记录 ID")
    checksum: str | None = Field(None, description="定义校验和，用于检测变更")

    # 业务字段（从 ToolServiceTool 合并）
    enabled: bool = Field(True, description="工具是否启用")
    permissions: list[str] = Field(default_factory=list, description="工具权限列表")
    parameters: dict[str, Any] = Field(default_factory=dict, description="参数定义（兼容字段）")
    handler: Callable | None = Field(None, description="工具处理函数")
    author: str = Field("system", description="工具作者")
    created_at: datetime | None = Field(None, description="创建时间")

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

    def to_llm_format(self) -> dict[str, Any]:
        """转换为 LLM 可用的工具格式（OpenAI function calling 格式）"""
        # 构建排除注入参数后的 schema
        llm_schema = self._get_llm_schema()

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.build_full_description(),
                "parameters": llm_schema,
            },
        }

    def _get_llm_schema(self) -> dict[str, Any]:
        """获取传给 LLM 的 schema（排除注入参数）"""
        if not self.injected_params:
            return self.input_schema

        # 深拷贝 schema，避免修改原始数据
        import copy
        schema = copy.deepcopy(self.input_schema)

        # 从 properties 中移除注入参数
        if "properties" in schema:
            for param in self.injected_params:
                if param in schema["properties"]:
                    del schema["properties"][param]

        # 从 required 中移除注入参数
        if "required" in schema and isinstance(schema["required"], list):
            schema["required"] = [
                r for r in schema["required"] if r not in self.injected_params
            ]
            # 如果 required 为空，移除该字段
            if not schema["required"]:
                del schema["required"]

        return schema

    def get_tool_call_schema(self) -> dict[str, Any]:
        """获取工具调用 schema（排除注入参数）"""
        return self._get_llm_schema()

    def to_llm_yaml_format(self) -> str:
        """转换为 LLM 可用的 YAML 格式工具描述（节省 token）"""
        import yaml

        # 使用排除注入参数后的 schema
        llm_schema = self._get_llm_schema()

        # 构建简化的工具描述
        tool_desc = {
            "name": self.name,
            "desc": self.description,  # 使用简短字段名
            "params": self._simplify_schema(llm_schema),
        }

        return yaml.dump(tool_desc, default_flow_style=False, allow_unicode=True)

    def _simplify_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """简化 JSON Schema 为更紧凑的 YAML 格式"""
        if not isinstance(schema, dict):
            return schema

        simplified = {}

        # 处理 properties
        if "properties" in schema:
            simplified["props"] = {}
            for prop_name, prop_def in schema["properties"].items():
                prop_simple = {}

                # 简化类型定义
                if "type" in prop_def:
                    prop_simple["type"] = prop_def["type"]

                # 简化描述
                if "description" in prop_def:
                    prop_simple["desc"] = prop_def["description"]

                # 保留默认值
                if "default" in prop_def:
                    prop_simple["default"] = prop_def["default"]

                # 保留枚举值
                if "enum" in prop_def:
                    prop_simple["enum"] = prop_def["enum"]

                simplified["props"][prop_name] = prop_simple

        # 保留必需字段
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
        import hashlib
        import json

        # 只包含影响工具行为的核心字段
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
        handler: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]],
    ) -> "ToolRunnable":
        """转换为 ToolRunnable"""
        from core.runnable import ToolRunnable

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
) -> "ToolExecutionResult":
    """创建成功结果"""
    from core.results import ToolExecutionResult

    return ToolExecutionResult.create_completed(
        output=data,
        metadata=metadata or {},
        duration_ms=duration_ms,
    )


def create_failure_result(
    error: str,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> "ToolExecutionResult":
    """创建失败结果"""
    from core.results import ToolExecutionResult

    return ToolExecutionResult.create_failed(
        error=error,
        error_code=error_code,
        metadata=metadata or {},
    )


def create_failure_result_with_code(
    error_code: ErrorCode,
    details: str = "",
    metadata: dict[str, Any] | None = None,
) -> "ToolExecutionResult":
    """使用标准错误代码创建失败结果"""
    from core.errors import get_error_message
    from core.results import ToolExecutionResult

    error_message = get_error_message(error_code.value)
    if details:
        error_message = f"{error_message}: {details}"

    return ToolExecutionResult.create_failed(
        error=error_message,
        error_code=error_code.value,
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


# 向后兼容别名
ToolResult = ToolExecutionResult
