"""工具 Schema 注入 Input 插件。

在管道 prepare 链把 LLM 工具面写入 state：读 state["tool_ids"]（context_build
按 agent yaml 注入，显式空表 = 声明零工具），经内核 tool-surface capability
过滤能力注册表，把 OpenAI function calling 格式的 schema 列表与工具输出契约
写回 state，供 llm_core（tools 参数）与 tool_core（输出契约校验）消费。

agent 配置解析（读 config/agents/** 取 tool_ids）归 context_build；本插件不做
任何 yaml 读取，也不做全量兜底——state 无 tool_ids = 配置断链，工具面置空。

State 命名空间：
    - tool_schemas : 过滤后的工具 Schema 列表（始终写入；配置断链 = 空列表）
    - tool_output_contracts : tool_name → {schema, render} 输出契约表（始终写入）
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

# tool-surface capability 调用通道（server.py on_load 注入，与 llm_core 的
# llm 调用通道同构）。签名：(method, params, timeout=None) → result dict。
_capability_caller: Callable[..., Awaitable[dict[str, Any]]] | None = None


def set_capability_caller(
    caller: Callable[..., Awaitable[dict[str, Any]]] | None,
) -> None:
    """注入/清除 tool-surface capability 调用通道。

    server.py on_load 注入（``plugin.get_capability("tool-surface").call``
    组装 ``<capability>.<method>`` 全名，method 传短名）；on_unload 清除。
    未注入时工具面置空（fail-closed，不阻断管道）。
    """
    global _capability_caller
    _capability_caller = caller


class ToolSchemaPlugin(IInputPlugin):
    """工具 Schema 注入 Input 插件。

    经内核 tool-surface capability 按 state["tool_ids"] 过滤注册表工具，
    结果写入 state["tool_schemas"] 与 state["tool_output_contracts"]。

    优先级：50（构建级，与 prompt_build 同级）；排在 context_build（写
    tool_ids）之后。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具 Schema 注入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用工具 Schema 注入（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_schema"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 50)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """拉取过滤后的工具面并写入 state。"""
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行工具 Schema 拉取。

        白名单来源：ctx.state["tool_ids"]（context_build 按 agent yaml 注入；
        显式空表 = agent 声明零工具）。缺失 = 配置断链 → 空工具面 + 报警，
        禁止兜底全量（K10：agent 配置断链时权限边界不得静默放宽）。

        Returns:
            要写入 state 的工具字段字典
        """
        if not self._enabled:
            return {"tool_schemas": [], "tool_output_contracts": {}}

        wanted = ctx.state.get("tool_ids")
        if not isinstance(wanted, list):
            logger.warning(
                "[%s] state 无 tool_ids（context_build 应按 agent yaml 注入），"
                "工具面置空（K10 配置断链，不兜底全量）",
                self.name,
            )
            return {"tool_schemas": [], "tool_output_contracts": {}}

        caller = _capability_caller
        if caller is None:
            logger.error(
                "[%s] capability caller 未注入：tool-surface 调用通道未接线"
                "（server.py on_load 应调用 set_capability_caller），工具面置空",
                self.name,
            )
            return {"tool_schemas": [], "tool_output_contracts": {}}

        try:
            result = await caller("schemas", {"tool_ids": wanted})
        except Exception as exc:
            logger.error(
                "[%s] tool-surface.schemas 调用失败，工具面置空 | %s", self.name, exc
            )
            return {"tool_schemas": [], "tool_output_contracts": {}}

        schemas = result.get("schemas") or []
        contracts = result.get("contracts") or {}

        # 工具面漂移检测：agent tool_ids 引用了注册表不存在的工具 = 配置错误/
        # 注册异常（被 G2 净化、插件未启用、名字写错），报警暴露而非静默缩面。
        # 只检 agent 声明的 wanted——框架强制工具（spill_retrieve）非 agent
        # 配置管辖，不计入漂移。
        available = {
            (s.get("function") or {}).get("name")
            for s in schemas
            if isinstance(s, dict)
        }
        missing = sorted(t for t in set(wanted) if t not in available)
        if missing:
            logger.warning(
                "[%s] 工具面漂移：agent tool_ids 引用的工具不在注册表"
                "（被 G2 净化/插件未启用/名字有误，需排查）| missing=%s",
                self.name, missing,
            )

        logger.info(
            "[%s] 工具面注入 | tool_ids=%d | schemas=%d",
            self.name, len(wanted), len(schemas),
        )
        return {"tool_schemas": schemas, "tool_output_contracts": contracts}
