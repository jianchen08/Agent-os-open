"""工具 Schema 注入 Input 插件。

负责在管道循环的输入阶段将工具的 JSON Schema 描述
注入到 state 中，供 LLM Core 调用时作为 function calling 的 tools 参数。

默认不再生成 prompt.tool_descriptions（走 function calling）。
当配置 include_tools_description_in_prompt=true 时，
才额外生成人类可读的工具描述写入 state（供 prompt_build 拼入 SystemMessage）。

State 命名空间：
    - tool_schemas : 本插件写入的工具 Schema 列表（JSON 格式，始终写入）
    - prompt.tool_descriptions : 本插件写入的工具描述（文本格式，仅当开关开启时写入）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class ToolSchemaPlugin(IInputPlugin):
    """工具 Schema 注入 Input 插件。

    从 ToolRegistry 获取已注册工具的 Schema 信息，
    生成 OpenAI function calling 格式的 JSON 列表写入 state["tool_schemas"]。
    当配置 include_tools_description_in_prompt=true 时，
    额外生成人类可读的文本描述写入 state["prompt.tool_descriptions"]。

    优先级：50（构建级，与 prompt_build 同级）
    错误策略：FALLBACK（没工具描述也能对话）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具 Schema 注入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用工具 Schema 注入（默认 True）
                - tool_ids: 指定要注入的工具 ID 列表（空列表表示全部）
                - include_tools_description_in_prompt: 是否生成人类可读的工具描述
                  写入 state["prompt.tool_descriptions"]（默认 False）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._tool_ids = self._config.get("tool_ids", [])
        self._include_desc = self._config.get("include_tools_description_in_prompt", False)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_schema"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 50)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """注入工具 Schema 到 state。

        通过 ctx.get_service("tool_registry") 获取工具注册表，
        生成 OpenAI function calling 格式的 Schema 列表。
        当配置开关开启时，额外生成人类可读的工具描述。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含工具 Schema 状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0912
        """执行工具 Schema 注入逻辑。

        工具 ID 来源优先级：
        1. ctx.state["tool_ids"] — 运行时由 Agent 配置注入（优先）
        2. self._tool_ids — 插件初始化配置（降级）

        Returns:
            要写入 state 的工具字段字典
        """
        if not self._enabled:
            return {"tool_schemas": []}

        # 获取工具注册表
        try:
            tool_registry = ctx.get_service("tool_registry")
        except KeyError:
            logger.debug("[%s] No tool_registry service, skipping", self.name)
            return {"tool_schemas": []}

        # 确定工具过滤列表：优先从 state 读取（Agent 配置注入），降级用插件配置
        active_tool_ids: list[str] = ctx.state.get("tool_ids", []) or self._tool_ids

        # 合并动态加载的工具（由 resource_search 在运行时触发加载）
        dynamic_names = tool_registry.get_dynamic_tool_names()
        logger.debug(
            "[%s] registry_id=%s dynamic_names=%s",
            self.name,
            id(tool_registry),
            dynamic_names,
        )
        if dynamic_names:
            active_tool_ids = list(set(active_tool_ids) | dynamic_names)

        logger.debug("[%s] active_tool_ids=%s (count=%d)", self.name, active_tool_ids, len(active_tool_ids))

        if active_tool_ids:
            try:
                from tools.loader import get_dynamic_tool_loader  # noqa: PLC0415

                dyn_loader = get_dynamic_tool_loader()
                if dyn_loader is not None:
                    dyn_loader.ensure_loaded_sync(active_tool_ids)
            except Exception as exc:
                logger.debug("[%s] Dynamic sync preload failed: %s", self.name, exc)

        # 获取所有工具或指定工具
        if active_tool_ids:
            tools = []
            for tool_id in active_tool_ids:
                try:
                    tools.append(tool_registry.get(tool_id))
                except Exception:
                    logger.warning("[%s] Tool not found: %s", self.name, tool_id)
        else:
            tools = tool_registry.list_all()

        if not tools:
            return {"tool_schemas": []}

        # 构建 function calling 格式的 Schema（始终写入）
        agent_level = self._resolve_agent_level(ctx)
        schemas = []
        services = self._get_services(ctx)
        for tool in tools:
            enricher = tool_registry.get_schema_enricher(tool.name)
            if enricher:
                try:
                    enriched_tool = enricher(tool, services)
                    llm_format = enriched_tool.to_llm_format(agent_level=agent_level)
                except Exception as exc:
                    logger.debug(
                        "[%s] Schema enrichment failed for %s: %s",
                        self.name,
                        tool.name,
                        exc,
                    )
                    llm_format = tool.to_llm_format(agent_level=agent_level)
            else:
                llm_format = tool.to_llm_format(agent_level=agent_level)
            schemas.append(llm_format)

        result: dict[str, Any] = {"tool_schemas": schemas}

        # 仅当开关开启时，生成人类可读的工具描述
        if self._include_desc:
            descriptions = []
            for tool in tools:
                descriptions.append(f"- {tool.name}: {tool.description}")
            result["prompt.tool_descriptions"] = "## 可用工具\n" + "\n".join(descriptions)

        logger.debug(
            "[%s] Tool schemas injected | count=%d | desc=%s",
            self.name,
            len(schemas),
            self._include_desc,
        )

        return result

    def _get_services(self, ctx: PluginContext) -> dict[str, Any]:
        """从上下文获取可用服务字典，供 Schema 丰富器使用。"""
        services: dict[str, Any] = {}
        for key in (
            "tool_registry",
            "media_provider_registry",
            "memory_store",
            "retriever",
        ):
            try:
                svc = ctx.get_service(key)
                if svc is not None:
                    services[key] = svc
            except KeyError:
                continue
        return services

    @staticmethod
    def _resolve_agent_level(ctx: PluginContext) -> int | None:
        """从管道 state 解析当前 Agent 层级。

        Returns:
            Agent 层级数字（1/2/3），解析失败返回 None（不过滤）
        """
        from pipeline.types import StateKeys  # noqa: PLC0415

        raw_level = ctx.state.get(StateKeys.AGENT_LEVEL) or ctx.state.get("context.agent_level", "")
        if raw_level:
            level_str = str(raw_level).upper().lstrip("L")
            try:
                return int(level_str)
            except (ValueError, TypeError):
                pass
        return None
