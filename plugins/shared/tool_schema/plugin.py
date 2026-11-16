"""工具 Schema 注入 Input 插件。

在管道 prepare 链把 LLM 工具面写入 state：读 state["tool_ids"]（context_build
按 agent yaml 注入，显式空表 = 声明零工具），经内核 tool-surface capability
过滤能力注册表，把 OpenAI function calling 格式的 schema 列表与工具输出契约
写回 state，供 llm_core（tools 参数）与 tool_core（输出契约校验）消费。

隔离/worktree 会话（依赖隔离边界免审批，execution_context 归一口径见
_is_boundary_reliant）的默认工具面剥离宿主 GUI 自动化工具（HOST_GUI_TOOL_IDS，
BUG-65）：触达隔离边界之外的工具不得出现在依赖该边界免审批的会话里；
非隔离会话审批闸生效，不剥离。state["tool_ids"] 保留 agent 原声明（审计面）。

agent 配置解析（读 config/agents/** 取 tool_ids）归 context_build；本插件不做
任何 yaml 读取，也不做全量兜底——state 无 tool_ids = 配置断链，工具面置空。

State 命名空间：
    - tool_schemas : 过滤后的工具 Schema 列表（配置断链 tool_ids 缺失 = 空列表；
      tool-surface 通道断链 = 抛错，绝不静默空面出站）
    - tool_output_contracts : tool_name → {schema, render} 输出契约表（始终写入）
    - tool_surface.unavailable : 声明但本轮不在面的工具名排序表（成功路径始终
      写入；卡4① 数组缩水显式告知数据源，prompt_build 据此注入模型可读通告。
      run 内 tool_ids 静态，该差集只在可用性变化时变化 = 上一轮数组 diff 本轮
      消失的工具，且缩水发生在首轮 LLM 之前也能捕获；工具恢复即空表、通告自愈
      消失）
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

# 接线等待轮询间隔（秒）：等待期让出事件循环，on_load 通知处理器在同一
# loop 上落地注入。
_WIRE_POLL_SECONDS = 0.1

# 宿主 GUI 自动化工具 id 集（用户空间 external MCP computer_use 上游原名，
# agent yaml tool_ids 显式列名的同一集合）：这些工具直接触达宿主桌面 =
# 隔离边界之外。隔离/worktree 会话按「隔离容器即安全边界」裁定默认免审批
# （2026-09-15），GUI 工具留在面内等于 Type/Shortcut 进终端 ≈ Shell 等价物
# 结构性绕开 bash 审批闸（R220：Shell/PowerShell 不进 LLM 面）——边界依赖
# 上下文的默认工具面一律剥离（BUG-65）。非隔离会话审批闸生效，不剥离。
HOST_GUI_TOOL_IDS: frozenset[str] = frozenset({
    "App", "Snapshot", "Click", "Type", "Shortcut", "Wait",
    "Move", "Screenshot", "Scroll",
})

# 外部/用户空间提供方工具 id 集：仓内 manifest 声明面看不见，运行时才注册——
# 用户空间插件根（`<USER_ROOT>/plugins/`，装机布局：external_mcp/godot_mcp 供
# godot_run、external_mcp/omnisearch 供 universal_search）或内核 external MCP
# 通道（mcpServers 配置 → mcp:external 空声明目录）。agent yaml 显式列名合法
# （ADR 2026-09-14 单一存在：用户根插件经热发现注册；仓内独立部署/临时用户根
# ——e2e 矩阵等——不含这些 id，见 tests/plugins/test_tool_ids_contract.py 白名单）。
# 缺席注册表 = 提供方未安装/未连接（非 agent 配置错误）→ 漂移告警按「外部提供
# 方缺席」降级 INFO；真漂移（注册表应有而无）保持 WARNING。
EXTERNAL_PROVIDED_TOOL_IDS: frozenset[str] = frozenset({
    "godot_run",         # external_mcp/godot_mcp（mode_godot 系与 general_agent 消费）
    "universal_search",  # external_mcp/omnisearch（general/research 系消费）
}) | HOST_GUI_TOOL_IDS    # computer_use 外部 MCP 宿主桌面九件（上游原名，同属外部提供方）


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
                - wire_wait_seconds: caller 未就绪时的接线等待上限秒数
                    （默认 10；cohost 冷启动竞态下 on_load 晚于首次 execute
                    数百毫秒落地，生产 2026-09-15 16:52/17:02 实证）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._wire_wait_seconds = float(self._config.get("wire_wait_seconds", 10.0))
        # capability 调用通道挂在**单例实例**上：合宿下同进程多个 pipeline 插件
        # 共享裸名 `plugin` 模块（先加载者占住 sys.modules），模块级全局会被
        # 别家插件的注入写走（llm_core 也叫 plugin.py 且有同名 setter）——
        # server.py 经自己的 get_instance() 缓存注入/读取，实例身份唯一。
        self._capability_caller: Callable[..., Awaitable[dict[str, Any]]] | None = None

    def set_capability_caller(
        self, caller: Callable[..., Awaitable[dict[str, Any]]] | None
    ) -> None:
        """注入/清除 tool-surface capability 调用通道（server.py on_load 调用）。"""
        self._capability_caller = caller

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

    @staticmethod
    def _is_boundary_reliant(state: dict[str, Any]) -> bool:
        """判定当前上下文是否依赖隔离边界免审批（隔离/worktree 会话）。

        归一口径与 isolation_guard 一致：execution_context.isolation.level
        缺省 = 默认隔离（任务默认隔离执行）；只有显式 non_isolated 才是
        非隔离（审批闸生效）。工作区拓扑 execution_context.workspace.mode
        = worktree 的会话同属边界依赖（隔离副本即免审批边界），isolation
        轴即使显式 non_isolated 也剥离。state 无 execution_context = 缺省
        隔离（fail-closed：宁可错剥不可漏剥）。
        """
        ec = state.get("execution_context")
        if not isinstance(ec, dict):
            return True
        iso = ec.get("isolation")
        level = iso.get("level") if isinstance(iso, dict) else None
        if str(level or "isolated") != "non_isolated":
            return True
        ws = ec.get("workspace")
        mode = ws.get("mode") if isinstance(ws, dict) else None
        return str(mode or "") == "worktree"

    async def _await_caller_wired(self) -> Callable[..., Awaitable[dict[str, Any]]] | None:
        """等待 capability caller 接线（有界），返回 caller 或 None（超时）。

        cohost 冷启动竞态：宿主组按成员序列初始化，on_load（注入 caller）晚于
        首个 execute 数百毫秒落地——生产 2026-09-15 16:52/17:02 两跑实证。
        等待期 await sleep 让出事件循环，on_load 通知处理器在同一 loop 落地。
        """
        if self._capability_caller is not None:
            return self._capability_caller
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wire_wait_seconds
        while self._capability_caller is None and loop.time() < deadline:
            await asyncio.sleep(_WIRE_POLL_SECONDS)
        caller = self._capability_caller
        if caller is not None:
            logger.warning(
                "[%s] capability caller 未就绪，等待接线后照常拉取工具面"
                "（cohost 冷启动竞态，wire_wait_seconds=%.0f）",
                self.name,
                self._wire_wait_seconds,
            )
        return caller

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

        # 隔离/worktree 会话默认工具面剥离宿主 GUI 自动化工具（BUG-65）：
        # state["tool_ids"] 保留 agent 原声明（审计面），剥离只作用于本次
        # tool-surface 白名单——被剥 id 不进 wanted，也就不触发工具面漂移告警。
        if self._is_boundary_reliant(ctx.state):
            stripped = sorted(t for t in set(wanted) if t in HOST_GUI_TOOL_IDS)
            if stripped:
                logger.warning(
                    "[%s] 隔离/worktree 会话剥离宿主 GUI 自动化工具"
                    "（隔离边界免审批会话不得触达宿主桌面）| stripped=%s",
                    self.name, stripped,
                )
                wanted = [t for t in wanted if t not in HOST_GUI_TOOL_IDS]

        caller = await self._await_caller_wired()
        if caller is None:
            # 通道断链 ≠ agent 声明零工具：静默空面会让 LLM 无工具声明出站，
            # 模型降级为正文输出原生工具标记且永不执行（BUG-17 实证失败链）。
            # 显式失败经引擎 warn+continue 落 _plugin_errors → WS 可见反馈。
            raise RuntimeError(
                f"[{self.name}] capability caller 未注入：tool-surface 调用通道未接线"
                f"（on_load 等待 {self._wire_wait_seconds:.0f}s 超时，server.py on_load "
                "应调用 set_capability_caller）"
            )

        # 调用失败（内核 tool-surface 不可达等）同上：错误上抛，不降级空面。
        # workspace 随调用上行（F-min 属地路由）：内核对同名多实例工具按工作
        # 空间属地换出宿主实例的 schema 面（工具名不变）；无 workspace = 主
        # 实例原样，既有行为不变。
        result = await caller(
            "schemas",
            {"tool_ids": wanted, "workspace": ctx.state.get("workspace")},
        )

        schemas = result.get("schemas") or []
        contracts = result.get("contracts") or {}

        # 工具面漂移检测：agent tool_ids 引用了注册表不存在的工具 = 配置错误/
        # 注册异常（被 G2 净化、插件未启用、名字写错），报警暴露而非静默缩面。
        # 只检 agent 声明的 wanted——框架强制工具（spill_retrieve）非 agent
        # 配置管辖，不计入漂移。
        # 分层：外部/用户空间提供方缺席（提供方未安装/未连接，见
        # EXTERNAL_PROVIDED_TOOL_IDS）与真漂移分开——真漂移 WARNING 保持报警，
        # 外部缺席 INFO（可观测但不冒报配置错误）。
        available = {
            (s.get("function") or {}).get("name")
            for s in schemas
            if isinstance(s, dict)
        }
        missing = sorted(t for t in set(wanted) if t not in available)
        drift = [t for t in missing if t not in EXTERNAL_PROVIDED_TOOL_IDS]
        external_missing = [t for t in missing if t in EXTERNAL_PROVIDED_TOOL_IDS]
        if drift:
            logger.warning(
                "[%s] 工具面漂移：agent tool_ids 引用的工具不在注册表"
                "（被 G2 净化/插件未启用/名字有误，需排查）| missing=%s",
                self.name, drift,
            )
        if external_missing:
            logger.info(
                "[%s] 外部提供方工具缺席：用户空间/外部 MCP 未注册"
                "（提供方未安装或未连接，非 agent 配置错误）| missing=%s",
                self.name, external_missing,
            )

        logger.info(
            "[%s] 工具面注入 | tool_ids=%d | schemas=%d",
            self.name, len(wanted), len(schemas),
        )
        # 卡4①：缺席面写 state（成功路径始终写入，恢复即空表自愈），供
        # prompt_build 注入「以下工具本轮不可用」模型可读通告——数组缩水
        # 不再无声，模型不必盲猜编造（2026-10-17 装机事故定因之一）。
        return {
            "tool_schemas": schemas,
            "tool_output_contracts": contracts,
            "tool_surface.unavailable": missing,
        }
