"""统一 Runnable 抽象。

暴露接口：
- create_tool_runnable(name, description, handler, input_schema) -> ToolRunnable
- YamlRunnable / ToolRunnable：可执行单元基类与工具实现
- RunnableMetadata / RunnableResult / RunnableType / RunnableStatus：元数据与结果类型

注：AgentRunnable / CompositeRunnable 及其工厂函数（compose_sequence /
compose_parallel / create_agent_runnable）已移除——经全仓库扫描确认无任何
调用方，属遗留死代码。Agent 执行链路改由 pipeline/engine + AgentConfig
直接驱动，无需 Runnable 体系包装。
"""

from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Any

import yaml
from pydantic import BaseModel, Field

# ============================================
# 类型定义
# ============================================


class RunnableType(str, Enum):
    """Runnable 类型"""

    TOOL = "tool"
    AGENT = "agent"
    COMPOSITE = "composite"


class RunnableStatus(str, Enum):
    """Runnable 状态"""

    ACTIVE = "active"
    DISABLED = "disabled"
    DEPRECATED = "deprecated"


class RunnableMetadata(BaseModel):
    """Runnable 元数据"""

    name: str = Field(..., description="名称")
    description: str = Field("", description="描述")
    runnable_type: RunnableType = Field(..., description="类型")
    version: str = Field("1.0.0", description="版本")
    tags: list[str] = Field(default_factory=list, description="标签")
    status: RunnableStatus = Field(RunnableStatus.ACTIVE, description="状态")
    # 输入输出 Schema
    input_schema: dict[str, Any] | None = Field(None, description="输入 Schema")
    output_schema: dict[str, Any] | None = Field(None, description="输出 Schema")
    # 扩展元数据
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展元数据")


class RunnableResult(BaseModel):
    """Runnable 执行结果"""

    success: bool = Field(..., description="是否成功")
    data: Any | None = Field(None, description="输出数据")
    error: str | None = Field(None, description="错误信息")
    error_code: str | None = Field(None, description="错误代码")
    duration_ms: int | None = Field(None, description="执行时间（毫秒）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")


# ============================================
# 基类定义
# ============================================


class YamlRunnable:
    """支持 YAML 输入的 Runnable 基类。

    特性：
    - 自动检测输入格式（YAML 字符串或字典）
    - YAML 字符串自动解析为字典
    - 提供 invoke/ainvoke 同步异步执行入口
    """

    def invoke(
        self,
        input: str | dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> Any:
        """同步执行"""
        parsed_input = self._parse_input(input)
        return self._execute(parsed_input)

    async def ainvoke(
        self,
        input: str | dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """异步执行"""
        parsed_input = self._parse_input(input)
        return await self._aexecute(parsed_input)

    def _parse_input(self, input: str | dict[str, Any]) -> dict[str, Any]:
        """解析输入"""
        if isinstance(input, dict):
            return input

        if isinstance(input, str):
            try:
                parsed = yaml.safe_load(input)
                if parsed is None:
                    return {}
                if not isinstance(parsed, dict):
                    raise ValueError(f"YAML 解析结果必须是字典，得到: {type(parsed)}")
                return parsed
            except yaml.YAMLError as e:
                raise ValueError(f"YAML 解析失败: {e}") from e

        raise ValueError(f"不支持的输入类型: {type(input)}")

    def _execute(self, input: dict[str, Any]) -> Any:
        """同步执行实现（子类可覆盖）"""
        import asyncio  # noqa: PLC0415

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 在异步上下文中，创建新任务
            import concurrent.futures  # noqa: PLC0415

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, self._aexecute(input))
                return future.result()
        else:
            return asyncio.run(self._aexecute(input))

    async def _aexecute(self, input: dict[str, Any]) -> Any:
        """异步执行实现（子类应覆盖）"""
        # 默认调用同步方法
        return self._execute(input)

    def get_metadata(self) -> RunnableMetadata:
        """获取元数据（子类应覆盖）"""
        return RunnableMetadata(
            name="unknown",
            description="",
            runnable_type=RunnableType.TOOL,
        )


# ============================================
# 工具 Runnable
# ============================================


# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


class ToolRunnable(YamlRunnable):
    """工具 Runnable。

    将工具处理函数包装为 Runnable，支持：
    - YAML/字典输入
    - MCP 格式输出
    - LLM 格式输出
    - 管道组合
    """

    def __init__(
        self,
        name: str,
        description: str,
        handler: ToolHandler,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """初始化工具 Runnable"""
        self.name = name
        self.description = description
        self._handler = handler
        # 使用 _tool_input_schema 避免与 Runnable 基类的 input_schema 属性冲突
        self._tool_input_schema = input_schema or {"type": "object", "properties": {}}
        self._tool_output_schema = output_schema
        self._metadata = metadata or {}

    @property
    def tool_input_schema(self) -> dict[str, Any]:
        """获取工具输入 Schema"""
        return self._tool_input_schema

    @property
    def tool_output_schema(self) -> dict[str, Any] | None:
        """获取工具输出 Schema"""
        return self._tool_output_schema

    async def _aexecute(self, input: dict[str, Any]) -> Any:
        """异步执行工具"""
        return await self._handler(input)

    def get_metadata(self) -> RunnableMetadata:
        """获取元数据"""
        return RunnableMetadata(
            name=self.name,
            description=self.description,
            runnable_type=RunnableType.TOOL,
            input_schema=self._tool_input_schema,
            output_schema=self._tool_output_schema,
            extra=self._metadata,
        )

    def to_mcp_format(self) -> dict[str, Any]:
        """转换为 MCP 工具格式"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self._tool_input_schema,
        }

    def to_llm_format(self) -> dict[str, Any]:
        """转换为 LLM 工具格式（OpenAI function calling）"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._tool_input_schema,
            },
        }

    def to_llm_yaml_format(self) -> str:
        """转换为 LLM 可用的 YAML 格式工具描述（节省 token）"""
        # 构建简化的工具描述
        tool_desc = {
            "name": self.name,
            "desc": self.description,
            "params": self._simplify_schema(self._tool_input_schema),
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


# ============================================
# 工厂函数
# ============================================


def create_tool_runnable(
    name: str,
    description: str,
    handler: ToolHandler,
    input_schema: dict[str, Any] | None = None,
) -> ToolRunnable:
    """创建工具 Runnable"""
    return ToolRunnable(
        name=name,
        description=description,
        handler=handler,
        input_schema=input_schema,
    )
