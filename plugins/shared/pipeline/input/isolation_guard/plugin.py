"""隔离环境守卫 Input 插件。

在工具执行前根据安全策略决定是否在容器内执行，并负责**容器落地**：
优先使用 IsolationDecider 从 isolation_policy.yaml 决策隔离级别，
task metadata 可覆盖决策结果；决策为 docker 的 bash_execute 经
IsolationManager 按 workspace 幂等获取/创建容器并注入 _container_id
（吸收原 session_isolation 插件的会话级容器语义，见 GAP 合并）。

职责边界（SRP）：
- 决策"执行环境"（container / host / denied）+ 容器落地注入
- 不做审批（审批归 security_check 插件）
- 不写 security.decision（仅 security_check 写）
- blocked 信号通过 isolation.blocked 表达

容器落地安全底线：容器不可达（服务缺失/创建失败）→ 对应调用标 blocked，
**绝不降级 host 裸跑**——降级会让 security_check 的 task_isolated 审批豁免
放行危险命令。

State 命名空间：
    - execution_contexts : 各工具调用的执行上下文列表
    - isolation.blocked   : 被策略阻止时设置（供路由拦截）
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from decider import IsolationDecider
from isolation_types import IsolationLevel
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


def _ensure_isolation_path() -> None:
    """把 isolation 插件目录加入 sys.path（IsolationManager 所在）。"""
    _here = Path(__file__).resolve().parent
    _system_dir = _here.parents[2] / "system"
    _iso_dir = _system_dir / "isolation"
    for _p in (str(_system_dir), str(_iso_dir)):
        if _p not in sys.path:
            sys.path.insert(0, _p)


class IsolationGuard(IInputPlugin):
    """隔离环境守卫 Input 插件。

    根据工具类型和配置的安全策略，决定每个工具调用
    应在何种隔离级别下执行（docker 或 host）。

    决策优先级：
    1. task metadata 中的 isolation_level 覆盖
    2. IsolationDecider 基于 isolation_policy.yaml 策略决策
    3. Docker 不可用时根据策略的 fallback 字段降级

    优先级：40（在 level_guard 之后，security_check 之前）
    隔离决策失败不应阻断管道。
    """


    # Docker 可用性复检冷却窗口（秒）：仅自动检测来源在不可用时按此间隔复检，
    # 避免每次工具调用都 spawn subprocess 探测 daemon。
    _RECHECK_COOLDOWN = 30.0

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化隔离环境守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用隔离守卫（默认 True）
                - docker_available: Docker 是否可用（默认 False）
                - force_host: 强制所有工具在 host 执行（默认 False）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        # 服务不可用告警只打一次（低频留痕，避免每轮迭代刷屏）
        self._service_warned = False
        # Docker 可用性来源：配置显式指定（_docker_auto=False，信任不刷新）
        # vs 自动检测（_docker_auto=True，execute 入口按冷却窗口复检）。
        # 区分来源是为了避免：启动那一刻 daemon 假死被永久钉死为 False，
        # 此后即便 daemon 恢复、容器都在跑也无效——必须重启进程才解除。
        if "docker_available" in self._config:
            self._docker_available = self._config["docker_available"]
            self._docker_auto = False
        else:
            # 启动时真正检测 Docker，不依赖外部注入
            self._docker_available = self._detect_docker()
            self._docker_auto = True
        # 上次检测时间（自动检测来源按冷却窗口复检用）
        self._docker_checked_at = time.monotonic()
        if not self._docker_available:
            logger.warning(
                "[%s] docker_available=False, tool isolation will be degraded to host execution",
                self.name,
            )
        self._force_host = self._config.get("force_host", False)
        self._decider = IsolationDecider()
        self._enabled_by_agent: bool = True
        # 环境服务（IsolationManager，懒加载；容器落地用）
        self._manager: Any = None

    # ── 环境服务对象（懒加载，插件进程内自持）────────────────────

    def _get_manager(self) -> Any | None:
        """懒加载 IsolationManager（服务不可用时返回 None，容器落地降级为 blocked）。"""
        if self._manager is not None:
            return self._manager
        try:
            _ensure_isolation_path()
            from isolation.manager import IsolationManager  # noqa: PLC0415

            self._manager = IsolationManager(
                config_path=self._config.get("config_path"),
            )
            logger.info("[IsolationGuard] 环境服务已实例化")
        except Exception as exc:
            logger.warning(
                "[IsolationGuard] 环境服务实例化失败，容器落地降级为 blocked | error=%s",
                exc,
            )
            self._manager = None
        return self._manager

    def _ensure_engine(self) -> None:
        """引擎自愈：docker 不可达时尝试拉起 WSL 保活会话唤醒引擎。

        幂等（wsl_health.ensure_docker_engine 内部冷却），仅在复检路径
        （当前不可用 + 越过复检冷却）触发；失败只留日志不阻断复检。
        wsl_health 位于 system/isolation（本目录无副本），须先注入路径
        再延迟导入（同 _get_manager 的懒加载模式）。
        """
        try:
            _ensure_isolation_path()
            from wsl_health import ensure_docker_engine  # noqa: PLC0415

            ensure_docker_engine()
        except Exception as exc:  # noqa: BLE001 - 自愈失败不阻断管道
            logger.warning("[%s] 引擎自愈失败: %s", self.name, exc)

    @staticmethod
    def _detect_docker() -> bool:
        """同步检测 Docker 是否可用（CLI 存在 + daemon 运行）。

        用 subprocess.run 替代 asyncio subprocess，避免 Windows 静默失败。
        timeout 从 15s 降到 3s：daemon 不可达（如 DOCKER_HOST 指向离线地址）时，
        TCP i/o timeout 会卡满整个 timeout 窗口，15s 会让每条工具调用消息延迟 +15s。
        3s 足够区分"daemon 正常"与"不可达"，配合 _RECHECK_COOLDOWN 冷却避免频繁探测。
        """
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        if not shutil.which("docker"):
            return False
        try:
            # 用 docker version 替代 docker info（info 在某些 Docker Desktop 配置下会卡 stdin）
            result = subprocess.run(  # noqa: PLW1510
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=3,
            )
            return result.returncode == 0
        except Exception:
            return False

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "isolation_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 40)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行隔离环境决策。

        遍历当前管道状态中的工具调用列表，
        为每个工具调用决定隔离级别和执行上下文。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含 execution_contexts 状态更新的插件执行结果
        """
        self._apply_runtime_config(ctx)

        if not self._enabled or not self._enabled_by_agent:
            return PluginResult()

        state = ctx.state
        core_type = state.get(StateKeys.CORE_TYPE, "llm_call")

        # 纯 LLM 回复（无工具调用）根本不需要 docker 隔离决策——提前返回，
        # 避免 execute 入口为普通聊天也触发 docker 探测（daemon 不可达时会卡满
        # subprocess timeout，每条消息延迟 +15s）。docker 复检下移到确认
        # 有 tool_calls 之后再做。
        if core_type != "tool_execute":
            return PluginResult()

        tool_calls = state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        # 可用性复检：仅自动检测来源 + 当前不可用 + 越过冷却窗口时重新探测。
        # daemon 启动那一刻假死被钉死为 False 后，恢复后无需重启进程即可解除。
        # 放在 tool_execute + 有 tool_calls 之后：只有真正要决策工具隔离级别时才探测，
        # 纯文本对话路径完全不触发（避免无谓的 docker 子进程开销）。
        if self._docker_auto and not self._docker_available:
            now = time.monotonic()
            if now - self._docker_checked_at >= self._RECHECK_COOLDOWN:
                self._ensure_engine()  # 引擎自愈：先确保引擎存活再探测
                self._docker_available = self._detect_docker()
                self._docker_checked_at = now
                if self._docker_available:
                    logger.info(
                        "[%s] Docker 可用性复检通过，解除 host 降级",
                        self.name,
                    )

        execution_contexts = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            # 解析工具参数（可能为 JSON 字符串），供宿主路径检测使用
            tc_args = tc.get("args", tc.get("arguments", {}))
            if isinstance(tc_args, str):
                import json  # noqa: PLC0415

                try:
                    tc_args = json.loads(tc_args)
                except (json.JSONDecodeError, TypeError):
                    tc_args = {}
            context = self._decide_isolation(tool_name, ctx, tool_args=tc_args)
            execution_contexts.append(context)

        # 给每个 context 注入任务级隔离标志：isolation_level 是隔离的唯一真相源，
        # 隔离任务（isolated/None/空）的所有工具一律放行，不弹审批。
        # 0.2 薄化：优先读 execution_context.isolation.level（init 体解析），
        # task metadata 仅作兼容兜底（不再每次工具调用查 task_service 为主）。
        ec = ctx.state.get("execution_context") if isinstance(ctx.state, dict) else None
        ec_iso = (
            ec.get("isolation", {}).get("level")
            if isinstance(ec, dict) and isinstance(ec.get("isolation"), dict)
            else None
        )
        task_metadata = self._get_task_metadata(ctx)
        task_isolated = (ec_iso or task_metadata.get("isolation_level") or "isolated") == "isolated"
        for context in execution_contexts:
            context["task_isolated"] = task_isolated

        state_updates: dict[str, Any] = {
            "execution_contexts": execution_contexts,
        }

        # ── 容器落地（吸收原 session_isolation 的会话级容器语义）──
        # provider=docker 的 bash_execute 经 IsolationManager 按 workspace 幂等
        # 获取/创建容器并注入 _container_id（bash tool.py 据此走 docker exec 通路）。
        # 容器不可达（服务缺失/创建失败）→ 对应调用标 blocked，绝不降级 host 裸跑
        # （降级会让 security_check 的 task_isolated 审批豁免放行危险命令）。
        docker_ctxs = [c for c in execution_contexts if c.get("provider") == "docker"]
        if docker_ctxs:
            workspace = ctx.state.get("workspace") or task_metadata.get("workspace")
            container_id = await self._get_or_create_container(workspace, ctx)
            if container_id:
                injected_calls = self._inject_container_id(
                    tool_calls,
                    {c["tool_name"] for c in docker_ctxs},
                    container_id,
                )
                if injected_calls is not None:
                    state_updates[StateKeys.RAW_TOOL_CALLS] = injected_calls
            else:
                for c in docker_ctxs:
                    c["provider"] = "denied"
                    c["blocked"] = True
                    c["reason"] = "container_create_failed"
                logger.warning(
                    "[IsolationGuard] 容器落地失败，要求容器隔离的工具已阻止 | tools=%s",
                    [c["tool_name"] for c in docker_ctxs],
                )

        # 被策略阻止的工具写入 isolation.blocked，供路由拦截
        blocked_tools = [c for c in execution_contexts if c.get("blocked")]
        if blocked_tools:
            tool_names = ", ".join(c["tool_name"] for c in blocked_tools)
            state_updates["isolation.blocked"] = True
            state_updates["isolation.block_reason"] = f"隔离策略阻止: {tool_names}"
            logger.warning(
                "[IsolationGuard] 阻止工具执行 | tools=%s",
                tool_names,
            )

        return PluginResult(state_updates=state_updates)

    # ── 容器落地辅助 ────────────────────────────────────────────

    async def _get_or_create_container(
        self,
        workspace: str | None,
        ctx: PluginContext,
    ) -> str | None:
        """经 IsolationManager 幂等获取/创建 workspace 容器（同 workspace 复用）。

        Returns:
            容器 id（env_id = workspace 派生的容器名）；服务不可用或创建失败返回 None
            （调用方据此把对应调用标 blocked，不降级裸跑）。
        """
        if not workspace:
            return None
        manager = self._get_manager()
        if manager is None:
            return None
        try:
            # 用 isolation.* 命名空间路径 import，避免与插件本地 isolation_types 裸名冲突；
            # manager 可能被外部注入（测试/降级路径），此处自行确保包路径存在。
            _ensure_isolation_path()
            import isolation.isolation_types as iso_types  # noqa: PLC0415

            task_id = ctx.state.get(StateKeys.TASK_ID) or ""
            env = await manager.get_or_create_environment(
                task_id=task_id or "session",
                task_type=iso_types.TaskType.ATOMIC,
                operation_type=iso_types.OperationType.CODE_EXECUTION,
                workspace=workspace,
                isolation_level=iso_types.IsolationLevel.CONTAINER,
            )
            return getattr(env, "env_id", None) or getattr(env, "environment_id", None)
        except Exception as exc:
            logger.warning(
                "[IsolationGuard] 容器获取/创建失败 | workspace=%s | error=%s",
                workspace,
                exc,
            )
            return None

    @staticmethod
    def _inject_container_id(
        tool_calls: list[dict[str, Any]],
        docker_tool_names: set[str],
        container_id: str,
    ) -> list[dict[str, Any]] | None:
        """为 provider=docker 的 bash_execute 调用注入 _container_id。

        仅在确有注入时返回新 tool_calls 列表（否则返回 None，调用方不覆盖 state）。
        非 bash 工具不注入（_container_id 仅 bash tool.py 消费）；容器内固定挂载
        workspace → /workspace，working_dir 未显式指定时补 /workspace。
        """
        injected_calls: list[dict[str, Any]] = []
        injected = False
        for tc in tool_calls:
            new_tc = dict(tc)
            if tc.get("name") == "bash_execute" and tc.get("name") in docker_tool_names:
                args = new_tc.get("args", new_tc.get("arguments", {}))
                if isinstance(args, str):
                    import json  # noqa: PLC0415

                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                args = dict(args)
                args["_container_id"] = container_id
                # 容器内固定挂载 /workspace：未显式指定 working_dir 时补容器路径
                if not args.get("working_dir"):
                    args["working_dir"] = "/workspace"
                new_tc["args"] = args
                injected = True
            injected_calls.append(new_tc)
        return injected_calls if injected else None

    def _decide_isolation(
        self,
        tool_name: str,
        ctx: PluginContext,
        tool_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:  # noqa: PLR0911
        """决定工具的隔离级别。

        规则（按优先级）：
        1. 先查工具级 policy（isolation_policy.yaml）确定工具的隔离能力
        2. task metadata 的 isolation_level 只允许降级（isolated→non_isolated），
           不允许提升（non_isolated→isolated），避免把不支持容器的工具塞进容器
        3. Docker 不可用时：要求容器的工具一律拒绝（返回 blocked），
           不降级到 host——降级会让属于其它容器/工作区的任务静默落到本进程执行
        4. 命令含宿主路径（如 D:/...、C:\\...）时，即使 policy 要求容器，
           也路由到 host 执行——容器内没有宿主路径，进了容器必然报
           "No such file or directory"。路由到 host 后由 security_check
           审批把关（安全规则 host_path_access 命中即 needs_approval）。

        Args:
            tool_name: 工具名称
            ctx: 插件执行上下文
            tool_args: 工具参数字典（用于宿主路径检测，可选）

        Returns:
            执行上下文字典，包含 provider、level、tool_name、workspace 等信息
        """
        # 隔离与工作空间的运行时真相源（0.2 薄化）：
        # - execution_context.isolation.level（init 体 environment_lifecycle 解析）
        #   > task metadata.isolation_level（0.1 兼容兜底）
        # - state.workspace（init 体 workspace_lifecycle 解析）> task metadata.workspace
        ec = ctx.state.get("execution_context") if isinstance(ctx.state, dict) else None
        ec_iso = (
            ec.get("isolation", {}).get("level")
            if isinstance(ec, dict) and isinstance(ec.get("isolation"), dict)
            else None
        )
        state_workspace = ctx.state.get("workspace") if isinstance(ctx.state, dict) else None
        task_metadata = self._get_task_metadata(ctx)
        # isolation_level 是隔离的唯一真相源：None/空 = 默认隔离（isolated），
        # 只有显式 non_isolated 才表示非隔离。归一化后下游决策统一。
        metadata_isolation = ec_iso or task_metadata.get("isolation_level") or "isolated"
        metadata_workspace = state_workspace or task_metadata.get("workspace")

        # 先解析工具级 policy，作为决策基础
        policy = self._decider.resolve(tool_name)
        policy_isolation = policy.isolation

        if self._force_host:
            # P0-安全: force_host 不能把要求容器隔离的工具放到宿主机执行，
            # 一律拒绝（不降级）。force_host 仅对本身就走 host 的工具有效。
            if policy_isolation == IsolationLevel.CONTAINER:
                logger.warning(
                    "[IsolationGuard] force_host 被拒绝: 工具 %s 要求容器隔离，不降级到 host | tool=%s",
                    tool_name,
                    tool_name,
                )
                return self._build_context(
                    tool_name,
                    "denied",
                    "force_host_denied_by_policy",
                    workspace=metadata_workspace,
                    blocked=True,
                )
            return self._build_context(
                tool_name,
                "host",
                "force_host",
                workspace=metadata_workspace,
            )

        # ── L1 主 agent 前置路由（force_host 之后，所有 docker 决策之前）──
        # 主 agent 默认没有任务工作空间（它直接在 project_root 操作），强制进
        # 容器会因无 workspace 被拒（tool_core 报"工作空间未解析"）→ 路由到
        # host，由 security_check 按 root_task 策略触发审批（require_confirmation）。
        # 例外：主会话绑定了 workspace 且隔离级别为 isolated（会话级隔离语义，
        # 原 session_isolation 插件吸收）→ 允许进容器，与子任务同语义。
        if tool_name == "bash_execute" and self._is_main_agent(ctx.state):
            if metadata_workspace and metadata_isolation == "isolated":
                if self._docker_available:
                    logger.info(
                        "[IsolationGuard] L1 主 agent bash_execute 会话隔离进容器 | tool=%s | ws=%s",
                        tool_name,
                        metadata_workspace,
                    )
                    return self._build_context(
                        tool_name,
                        "docker",
                        "l1_main_agent_session_isolated",
                        workspace=metadata_workspace,
                    )
                logger.warning(
                    "[IsolationGuard] 主 agent 会话要求容器但 Docker 不可用，拒绝执行 | tool=%s",
                    tool_name,
                )
                return self._build_context(
                    tool_name,
                    "denied",
                    "docker_unavailable_container_required",
                    workspace=metadata_workspace,
                    blocked=True,
                )
            logger.info(
                "[IsolationGuard] L1 主 agent bash_execute 路由到 host（无任务工作空间，由 security_check 审批） | tool=%s",
                tool_name,
            )
            return self._build_context(
                tool_name,
                "host",
                "l1_main_agent_host",
                workspace=metadata_workspace,
            )

        # ── 宿主路径前置检测（所有 docker 决策之前）──
        # 命令/工作目录含 Windows 盘符路径时，容器内只有挂载的 /workspace，
        # 宿主路径必然不存在（返回 "No such file or directory"）。无论
        # isolation_level 是什么，含宿主路径的 bash_execute 都路由到 host 执行，
        # 由 security_check 的 host_path_access 规则触发用户审批。
        if (
            policy_isolation == IsolationLevel.CONTAINER
            and tool_name == "bash_execute"
            and tool_args
            and self._has_host_path(tool_args)
        ):
            logger.info(
                "[IsolationGuard] 命令含宿主路径，路由到 host 执行（等待审批） | tool=%s | reason=host_path_detected",
                tool_name,
            )
            return self._build_context(
                tool_name,
                "host",
                "host_path_detected",
                workspace=metadata_workspace,
            )

        # ── metadata 覆盖：只允许降级，不允许提升 ──
        # policy 是 non_isolated 的工具（如 file_write/task_submit），即使 metadata
        # 要求 isolated 也不路由到 docker（容器内没有工具代码，会报
        # "[isolated] tool=xxx not supported in container"）。
        # metadata_isolation 已归一化：None/空 = "isolated"（默认隔离）。
        if policy_isolation == IsolationLevel.CONTAINER:
            # policy 允许容器的工具（如 bash_execute），metadata 可控制实际级别
            if metadata_isolation == "isolated":
                if self._docker_available:
                    return self._build_context(
                        tool_name,
                        "docker",
                        "task_metadata",
                        workspace=metadata_workspace,
                    )
                # Docker 不可用：要求容器即拒绝，不降级到 host
                logger.warning(
                    "[IsolationGuard] metadata 要求容器但 Docker 不可用，拒绝执行 | tool=%s",
                    tool_name,
                )
                return self._build_context(
                    tool_name,
                    "denied",
                    "docker_unavailable_container_required",
                    workspace=metadata_workspace,
                    blocked=True,
                )
            # metadata 强制 host → 降级
            return self._build_context(
                tool_name,
                "host",
                "task_metadata_downgrade",
                workspace=metadata_workspace,
            )

        # ── 工具级 policy 决策（metadata 不适用或 policy 为 host）──
        if policy_isolation == IsolationLevel.CONTAINER and self._docker_available:
            return self._build_context(
                tool_name,
                "docker",
                "policy",
                workspace=metadata_workspace,
            )

        if policy_isolation == IsolationLevel.CONTAINER and not self._docker_available:
            # 要求容器但 Docker 不可用：一律拒绝，不降级
            logger.warning(
                "[IsolationGuard] Docker 不可用且工具要求容器隔离，拒绝执行 | tool=%s",
                tool_name,
            )
            return self._build_context(
                tool_name,
                "denied",
                "docker_unavailable_container_required",
                workspace=metadata_workspace,
                blocked=True,
            )

        return self._build_context(
            tool_name,
            "host",
            "policy",
            workspace=metadata_workspace,
        )

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 ctx.state 读取 Agent 覆盖的运行时配置。

        Args:
            ctx: 插件执行上下文
        """
        from pipeline.plugin import find_plugin_config  # noqa: PLC0415

        plugin_configs = ctx.state.get("plugin_configs", {})
        config = find_plugin_config("isolation_guard", plugin_configs)

        if not config.get("enabled", True):
            self._enabled_by_agent = False
            return

        self._enabled_by_agent = True
        if "docker_available" in config:
            self._docker_available = config["docker_available"]
        if "force_host" in config:
            self._force_host = config["force_host"]

    def _build_context(
        self,
        tool_name: str,
        provider: str,
        reason: str,
        workspace: str | None = None,
        blocked: bool = False,
    ) -> dict[str, Any]:
        """构建执行上下文字典。

        Args:
            tool_name: 工具名称
            provider: 执行提供者（docker / host / denied）
            reason: 决策原因
            workspace: 工作目录路径
            blocked: 是否被策略阻止执行

        Returns:
            执行上下文字典
        """
        context: dict[str, Any] = {
            "tool_name": tool_name,
            "provider": provider,
            "level": "denied" if blocked else ("isolated" if provider == "docker" else "non_isolated"),
            "reason": reason,
        }
        if blocked:
            context["blocked"] = True
        if workspace:
            context["workspace"] = workspace
        return context

    def _get_task_metadata(self, ctx: PluginContext) -> dict[str, Any]:
        """从 ctx.state 中获取当前 task 的 metadata。

        Args:
            ctx: 插件执行上下文

        Returns:
            task 的 metadata 字典，未找到时返回空字典
        """
        task_id = ctx.state.get(StateKeys.TASK_ID)
        if not task_id:
            return {}

        try:
            task_service = ctx.get_service("task_service")
        except KeyError:
            # 跨插件服务接线属后续架构任务：未接线时按无 metadata 处理（降级语义不变），
            # 低频 warning 留痕（只打一次）。
            if not self._service_warned:
                self._service_warned = True
                logger.warning("[IsolationGuard] task_service 未接线，task metadata 读取降级为空")
            return {}

        try:
            task = task_service.get_task(task_id)
            if task and task.metadata:
                return task.metadata
        except Exception as e:
            logger.debug(
                "[IsolationGuard] 读取 task metadata 失败 | task_id=%s | error=%s",
                task_id,
                e,
            )
        return {}

    @staticmethod
    def _is_main_agent(state: dict[str, Any]) -> bool:
        """判断当前调用方是否为 L1 主 agent。

        层级语义与 permission_policy.get_policy_name_for_agent_level 对齐：
        level <= 1（含 None/缺省/解析失败）即主 agent。主 agent 没有任务
        工作空间，bash_execute 不应进容器。

        Args:
            state: 插件上下文 state，读取 agent_level 字段。

        Returns:
            True 表示当前调用方是主 agent（L1 或未标层级）。
        """
        raw_level = state.get(StateKeys.AGENT_LEVEL)
        try:
            level = int(str(raw_level).upper().lstrip("L")) if raw_level else 1
        except (ValueError, TypeError):
            level = 1
        return level <= 1

    # 匹配 Windows 盘符绝对路径，如 D:/、D:\、C:\Users
    # 正则：盘符前须是行首/空白/引号/等号（排除 URL 中的 p:/、t:/ 片段），
    # 后跟字母+冒号+斜杠或反斜杠。
    _HOST_PATH_RE = re.compile(r"(?:^|[\s\"'=`])([A-Za-z]):[\\/]")

    @classmethod
    def _has_host_path(cls, tool_args: dict[str, Any]) -> bool:
        """检查工具参数中是否包含宿主机绝对路径（Windows 盘符模式）。

        检查 command 和 working_dir 两个参数。命中即说明命令意图访问
        宿主机文件系统——容器内只有挂载的 /workspace，宿主路径必然不存在。

        Args:
            tool_args: 工具参数字典

        Returns:
            是否包含宿主机路径
        """
        for key in ("command", "working_dir"):
            val = tool_args.get(key)
            if isinstance(val, str) and cls._HOST_PATH_RE.search(val):
                return True
        return False
