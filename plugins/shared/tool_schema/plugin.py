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
    - dynamic_tools : 动态工具定义集。动态类**不进 tool_schemas**（不进 LLM 原生
      tools 数组 → 前缀缓存稳定、白名单语义不被运行期扩大），只经薄转发壳
      call_tool 调用。定义来源 = 上一工具迭代 state["tool_results"] 里
      resource_search 返回的结构化 tool_defs（插件侧 state 字段，内核零感知）；
      按 name 累积合并、fail-closed 校验（缺 input_schema / 非 object 一律拒收，
      不补空对象）、隔离/worktree 会话对存量+新增整面剥离宿主 GUI 工具、条数
      封顶。持久键（不在 volatile_keys）：一次引入即长期有效，跨 run 保留
    - dynamic_tools_errors : 本轮被拒收条目 + 原因（模型可自纠、运维可观测）
    - tool_surface.unavailable : 声明但**真正不在工具面**的工具名排序表（成功
      路径始终写入；卡4① 数组缩水显式告知数据源，prompt_build 据此注入模型可读
      通告。G2 判定/观测失败的失败桩不在此列——其声明 schema 保留在面、调用时
      显式失败（ADR 2026-11-12），模型不应被告知「不要调用」。run 内 tool_ids
      静态，该差集只在可用性变化时变化；工具恢复即空表、通告自愈消失）
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
    # 桌面通道实际工具名（windows-mcp 上游真名）。旧抽象名在活注册表里不存在，
    # 若只登记旧名，隔离/worktree 会话的 GUI 剥离面会空转——desktop_* 留在面内
    # 等于 Type/Shortcut 进终端 ≈ Shell 等价物结构性绕开 bash 审批闸（BUG-65 同因）。
    "desktop_capture", "desktop_look", "desktop_windows", "desktop_tree",
    "desktop_click", "desktop_type", "desktop_key", "desktop_scroll",
    "desktop_drag", "desktop_window", "desktop_invoke", "desktop_read",
    "desktop_text", "desktop_wait", "desktop_sequence", "desktop_web",
    "desktop_course", "desktop_doctor", "desktop_log", "desktop_steer",
    "desktop_launch",
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
    "web_reader",        # external_mcp/omnisearch（URL→markdown，general/research 系消费）
}) | HOST_GUI_TOOL_IDS    # computer_use 外部 MCP 宿主桌面九件（上游原名，同属外部提供方）

# 动态工具定义的生产者工具名：只有它返回的结构化 `tool_defs` 参与动态集组装。
# 定义真值 = 插件 manifest 声明（resource_search 侧从 plugin.json 读取后原样
# 透出）；模型回传的任何描述不作为真值来源。
DYNAMIC_DEF_SOURCE_TOOL = "resource_search"

# 动态集条数上限（对齐 resource_search 的 `limit` 上限，避免上下文/state 膨胀）。
DYNAMIC_TOOL_LIMIT = 20

# 动态定义必填键：缺失即整条拒收（fail-closed，不静默补空对象——补 `{}` 等于
# 零参数盲调，与「external MCP 缺 input_schema 拒注册」同口径）。
_DYNAMIC_REQUIRED_KEYS: tuple[str, ...] = ("name", "description", "input_schema", "plugin_id")

# 动态定义直接透传的可选键（缺失仅降级，不阻断）。
_DYNAMIC_OPTIONAL_KEYS: tuple[str, ...] = ("output_schema", "render", "source")

#: 动态引用计数落库键（插件侧 state，**不写内核**）。语义见 ``_build_dynamic_refs``：
#: 纯观测/保活提示，**不控制回收**——回收仍由内核既有 idle GC 按空闲阈值决定。
DYNAMIC_REFS_KEY = "dynamic_tool_refs"


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
            return {
                "tool_schemas": [],
                "tool_output_contracts": {},
                "dynamic_tools": [],
                "dynamic_tools_errors": [],
                DYNAMIC_REFS_KEY: {},
            }

        wanted = ctx.state.get("tool_ids")
        if not isinstance(wanted, list):
            logger.warning(
                "[%s] state 无 tool_ids（context_build 应按 agent yaml 注入），"
                "工具面置空（K10 配置断链，不兜底全量）",
                self.name,
            )
            return {
                "tool_schemas": [],
                "tool_output_contracts": {},
                "dynamic_tools": [],
                "dynamic_tools_errors": [],
                DYNAMIC_REFS_KEY: {},
            }

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

        # 工具面漂移检测：agent tool_ids 引用了**不在工具面**的工具 = 配置错误/
        # 注册异常（插件未启用、名字写错、提供方未安装/未连接），报警暴露而非
        # 静默缩面。G2 判定/观测失败的失败桩不在注册表缺席列——其声明 schema
        # 保留在工具面（ADR 2026-11-12），调用时由内核显式失败，不进入本告警。
        # 只检 agent 声明的 wanted——插件 force_include_tools 声明的框架
        # 强制工具非 agent 配置管辖，不计入漂移。
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
                "[%s] 工具面漂移：agent tool_ids 引用的工具不在工具面"
                "（插件未启用/名字有误/提供方未注册，需排查）| missing=%s",
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
        # 卡4①：真缺席面写 state（成功路径始终写入，恢复即空表自愈），供
        # prompt_build 注入「以下工具不在本轮工具面」模型可读通告——真缺失
        # 不再无声，模型不必盲猜编造（2026-10-17 装机事故定因之一）。G2 失败桩
        # 在工具面（schema 保留、调用显式失败），不进此列（ADR 2026-11-12）。
        return {
            "tool_schemas": schemas,
            "tool_output_contracts": contracts,
            "tool_surface.unavailable": missing,
            **self._build_dynamic_tools(ctx.state),
        }

    # ── 动态工具集（插件侧 state 字段，内核零感知）──────────────────────

    def _build_dynamic_tools(self, state: dict[str, Any]) -> dict[str, Any]:
        """组装动态工具定义集，返回要写入 state 的两个键。

        数据源 = ``state["tool_results"]`` 里 ``resource_search`` 成功条目携带的
        结构化 ``tool_defs``（定义真值 = 该工具从插件 manifest 读出的声明原文，
        模型回传描述不作真值）。与既有 ``state["dynamic_tools"]`` 按 name **累积
        合并**：prepare 每轮执行，而 ``tool_results`` 只承载上一工具迭代的产出
        —— 不合并会导致「搜索后隔一轮再调用」时定义被后一轮无关工具结果冲掉。

        fail-closed 校验：``name`` / ``description`` / ``plugin_id`` 缺失或空、
        ``input_schema`` 缺失或非 object → 整条拒收并进 ``dynamic_tools_errors``，
        **不静默补空对象**（补 ``{}`` = 零参数盲调，与「external MCP 缺 schema
        拒注册」同口径）。隔离/worktree 会话按常驻面同一剥离清单对**合并后的整面**
        （存量 + 新增）剔除宿主 GUI 工具——动态面不得成为绕过隔离边界的暗道；
        ``dynamic_tools`` 不声明 volatile（跨 run 持久）后，存量定义同样必须受
        剥离约束，不能只检新增。

        Returns:
            ``{"dynamic_tools": [...], "dynamic_tools_errors": [...]}``
        """
        merged: list[dict[str, Any]] = [
            item
            for item in (state.get("dynamic_tools") or [])
            if isinstance(item, dict) and item.get("name")
        ]
        index = {str(item["name"]): i for i, item in enumerate(merged)}
        errors: list[dict[str, Any]] = []

        boundary_reliant = self._is_boundary_reliant(state)
        for raw in self._dynamic_source_defs(state):
            item, reason = self._validate_dynamic_def(raw, boundary_reliant=boundary_reliant)
            if item is None:
                errors.append({"name": str(raw.get("name") or "") if isinstance(raw, dict) else "", "reason": reason})
                continue
            name = str(item["name"])
            if name in index:
                merged[index[name]] = item
            else:
                index[name] = len(merged)
                merged.append(item)

        if boundary_reliant:
            # 隔离边界整面剥离（存量 + 新增统一过闸）：宿主 GUI 自动化工具触达
            # 隔离边界之外，跨 run 残留的存量定义与新引入定义同一清单剥离——
            # 剥离只作用于本插件产出面（state 原值不动），被剥条目进 errors 可观测。
            kept: list[dict[str, Any]] = []
            for item in merged:
                if str(item.get("name") or "") in HOST_GUI_TOOL_IDS:
                    errors.append({"name": str(item.get("name") or ""), "reason": "host_gui_tool_in_boundary_reliant_session"})
                else:
                    kept.append(item)
            merged = kept

        if len(merged) > DYNAMIC_TOOL_LIMIT:
            # 超限丢最旧（插入序在前的），本轮新引入的定义优先保留。
            overflow = merged[: len(merged) - DYNAMIC_TOOL_LIMIT]
            merged = merged[len(merged) - DYNAMIC_TOOL_LIMIT :]
            errors.extend(
                {"name": str(item.get("name") or ""), "reason": "dynamic_tools_over_limit"}
                for item in overflow
            )

        if errors:
            logger.warning("[%s] 动态工具定义拒收 | errors=%s", self.name, errors)
        if merged:
            logger.info("[%s] 动态工具集 | count=%d | names=%s", self.name, len(merged), [i["name"] for i in merged])
        return {
            "dynamic_tools": merged,
            "dynamic_tools_errors": errors,
            DYNAMIC_REFS_KEY: self._build_dynamic_refs(merged),
        }

    @staticmethod
    def _build_dynamic_refs(defs: list[dict[str, Any]]) -> dict[str, int]:
        """按 ``plugin_id`` 统计动态定义引用数（**只作观测/保活提示，不控制回收**）。

        方案 §6.2：动态类把 MCP 工具纳入后，MCP 会话生命周期需与「动态定义在
        state 中的存活期」对齐。但「用完即卸」已整体退役（§7.1 O1），故本计数
        刻意**不做回收决策**：

        - 计数从当前定义集**每轮重算**（含 -1 语义：定义被移除/覆盖即自然少计），
          不在插件侧累积闭包状态——漏归零的路径不存在，也就不会把宿主永久钉住；
        - 计数归零 **≠ 卸载**：回收仍由内核既有 idle GC（``inflight_gc.rs``
          ``run_idle_gc_pass``，缺省 300s 空闲阈值）决定；刚用过的 MCP 宿主其
          idle 时钟已被调用刷新，不会被立即回收；
        - 计数 > 0 = 该宿主当前仍被动态面引用（保活提示），供运维观测与后续
          「保活池」增强消费。

        Returns:
            ``{plugin_id: 引用条数}``；无定义时为空 dict（不写脏零值）。
        """
        refs: dict[str, int] = {}
        for item in defs:
            plugin_id = str(item.get("plugin_id") or "").strip()
            if plugin_id:
                refs[plugin_id] = refs.get(plugin_id, 0) + 1
        return refs

    @staticmethod
    def _dynamic_source_defs(state: dict[str, Any]) -> list[Any]:
        """合并本轮**全部** ``resource_search`` 成功结果里的结构化 ``tool_defs``。

        只认声明生产者（``DYNAMIC_DEF_SOURCE_TOOL``）+ 结构化键 ``tool_defs``：
        任意工具伪造同名字段不参与组装。同轮并行多个搜索时每条结果都是模型
        要的引入面，必须全部合并——只取最后一条会让先完成搜索的引入丢失
        （2026-11-18 矩阵 R2 §六.2 五轮复现：并行数越多丢越多）。同名覆盖与
        超限 FIFO 由 ``_build_dynamic_tools`` 统一处理（后到者胜）。
        """
        results = state.get("tool_results")
        if not isinstance(results, list):
            return []
        defs: list[Any] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("tool_name") or "") != DYNAMIC_DEF_SOURCE_TOOL:
                continue
            if not entry.get("success"):
                continue
            data = entry.get("data")
            raw = data.get("tool_defs") if isinstance(data, dict) else None
            if isinstance(raw, list):
                defs.extend(raw)
        return defs

    @staticmethod
    def _validate_dynamic_def(raw: Any, *, boundary_reliant: bool) -> tuple[dict[str, Any] | None, str]:
        """校验单条动态定义；不通过返回 ``(None, 原因码)``。"""
        if not isinstance(raw, dict):
            return None, "definition_not_object"
        name = str(raw.get("name") or "").strip()
        if not name:
            return None, "name_missing"
        description = str(raw.get("description") or "").strip()
        if not description:
            return None, "description_missing"
        plugin_id = str(raw.get("plugin_id") or "").strip()
        if not plugin_id:
            return None, "plugin_id_missing"
        schema = raw.get("input_schema")
        if schema is None:
            return None, "input_schema_missing"
        if not isinstance(schema, dict) or str(schema.get("type") or "") != "object":
            return None, "input_schema_not_object"
        if boundary_reliant and name in HOST_GUI_TOOL_IDS:
            return None, "host_gui_tool_in_boundary_reliant_session"

        item: dict[str, Any] = {
            "name": name,
            "description": description,
            "input_schema": schema,
            "plugin_id": plugin_id,
        }
        for key in _DYNAMIC_OPTIONAL_KEYS:
            if raw.get(key) is not None:
                item[key] = raw[key]
        # 可选契约缺失只降级（前端按 generic 渲染），不阻断可用性。
        item["degraded"] = any(item.get(k) is None for k in ("output_schema", "render"))
        return item, ""
