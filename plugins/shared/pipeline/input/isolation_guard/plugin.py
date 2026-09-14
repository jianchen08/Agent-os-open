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
    - isolation.container_name : 已落地的容器绑定（执行环境显式数据；
      后续轮凭绑定验活直读，不再重复推导/查找；失活才重新落地刷新）
    - isolation.blocked   : 被策略阻止时设置（供路由拦截）
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
import time
from typing import Any

from decider import IsolationDecider
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

from agentos_plugin_sdk.isolation_types import IsolationLevel

logger = logging.getLogger(__name__)

# 容器绑定在管道 state 里的键：执行环境（容器名）是显式数据，落地一次
# 全程直读，与 isolation.blocked 同命名空间。
_CONTAINER_STATE_KEY = "isolation.container_name"


# isolation 目录 sys.path 注入走共享单源（isolation_path.ensure_isolation_path）。
from isolation_path import ensure_isolation_path as _ensure_isolation_path


class IsolationGuard(IInputPlugin):
    """隔离环境守卫 Input 插件。

    根据工具类型和配置的安全策略，决定每个工具调用
    应在何种隔离级别下执行（docker 或 host）。

    决策优先级：
    1. task metadata 中的 isolation_level 覆盖
    2. IsolationDecider 基于 isolation_policy.yaml 策略决策
    3. Docker 不可用（或探测异常致可用性不可验证）时，容器要求型工具
       拒绝执行（fail-closed，不降级宿主）

    优先级：40（在 level_guard 之后，security_check 之前）
    隔离决策失败不应阻断管道。
    """


    # Docker 探测三态（_detect_docker 返回值）：available=可用；
    # absent=明确不可用（CLI 缺失或 daemon 明确回答不可达）；
    # probe_error=探测异常（超时/权限/瞬态，可用性不可验证，fail-closed）。
    _PROBE_AVAILABLE = "available"
    _PROBE_ABSENT = "absent"
    _PROBE_ERROR = "probe_error"

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
        # CONTAINER 后端配置（wsl_native 启用时探测目标从 docker 切到 WSL）
        self._wsl_native_cfg = self._load_wsl_native_config()
        # 服务不可用告警只打一次（低频留痕，避免每轮迭代刷屏）
        self._service_warned = False
        # Docker 可用性来源：配置显式指定（_docker_auto=False，信任不刷新）
        # vs 自动检测（_docker_auto=True，execute 入口按冷却窗口复检）。
        # 区分来源是为了避免：启动那一刻 daemon 假死被永久钉死为 False，
        # 此后即便 daemon 恢复、容器都在跑也无效——必须重启进程才解除。
        # _docker_probe_error 记录最近一次探测异常（超时/权限等，可用性不可
        # 验证）：与"明确不可用"分态，容器要求型工具按 fail-closed 拒绝。
        self._docker_probe_error: str | None = None
        if "docker_available" in self._config:
            self._docker_available = self._config["docker_available"]
            self._docker_auto = False
        else:
            # 启动时真正检测后端（docker 或 wsl_native），不依赖外部注入
            self._probe_backend()
            self._docker_auto = True
        # 上次检测时间（自动检测来源按冷却窗口复检用）
        self._docker_checked_at = time.monotonic()
        if self._docker_probe_error:
            logger.error(
                "[%s] Docker 探测异常（可用性不可验证），容器要求型工具将被拒绝 | %s",
                self.name,
                self._docker_probe_error,
            )
        if not self._docker_available:
            logger.warning(
                "[%s] docker 不可用，容器要求型工具拒绝执行，host 执行上下文标记 isolation_mode=host",
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
    def _detect_docker() -> tuple[str, str]:
        """同步探测 Docker，返回 (三态, 异常摘要)。

        三态区分安全语义（探测故障 ≠ 未安装，超时≠不可达）：
        - ("available", "")：CLI 存在且 daemon 应答正常；
        - ("absent", "")：CLI 缺失，或 daemon 明确回答不可达（returncode != 0，
          探测有确定答案）——按既有"明确不可用"语义处置；
        - ("probe_error", 异常摘要)：subprocess 异常（超时/权限/瞬态故障），
          可用性不可验证——容器要求型工具必须 fail-closed 拒绝，
          不得静默降级宿主执行。

        用 subprocess.run 替代 asyncio subprocess，避免 Windows 静默失败。
        timeout 从 15s 降到 3s：daemon 不可达（如 DOCKER_HOST 指向离线地址）时，
        TCP i/o timeout 会卡满整个 timeout 窗口，15s 会让每条工具调用消息延迟 +15s。
        3s 足够区分"daemon 正常"与"不可达"，配合 _RECHECK_COOLDOWN 冷却避免频繁探测。
        """
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        if not shutil.which("docker"):
            return (IsolationGuard._PROBE_ABSENT, "")
        try:
            # 用 docker version 替代 docker info（info 在某些 Docker Desktop 配置下会卡 stdin）
            result = subprocess.run(  # noqa: PLW1510
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=3,
            )
        except Exception as exc:
            return (IsolationGuard._PROBE_ERROR, str(exc))
        if result.returncode == 0:
            return (IsolationGuard._PROBE_AVAILABLE, "")
        return (IsolationGuard._PROBE_ABSENT, "")

    def _apply_probe_result(self, probe: tuple[str, str]) -> None:
        """把探测结果写回实例状态（available/absent/probe_error 三态归一）。"""
        status, detail = probe
        self._docker_available = status == self._PROBE_AVAILABLE
        self._docker_probe_error = detail if status == self._PROBE_ERROR else None

    def _load_wsl_native_config(self) -> dict[str, Any]:
        """wsl_native 后端配置：显式 config 优先，缺省读 ConfigCenter 单源
        （isolation/isolation_config.yaml，与 manager._load_provider_config
        同源），读取失败视为未启用（docker 后端）。"""
        providers_cfg = self._config.get("providers")
        if isinstance(providers_cfg, dict) and "wsl_native" in providers_cfg:
            cfg = providers_cfg.get("wsl_native")
            return cfg if isinstance(cfg, dict) else {}
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            iso = get_config_center().get("plugins/isolation/isolation_config.yaml") or {}
            cfg = (iso.get("providers") or {}).get("wsl_native")
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            pass
        # ConfigCenter 在 sidecar 不可达——直读仓库 yaml 兜底（与 manager
        # _load_provider_config 回退同源同文件）
        try:
            import yaml  # noqa: PLC0415

            yaml_path = (
                Path(__file__).resolve().parents[5] / "config" / "plugins" / "isolation" / "isolation_config.yaml"
            )
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            providers = data.get("providers") or {}
            cfg = providers.get("wsl_native") if isinstance(providers, dict) else None
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _probe_backend(self) -> None:
        """探测当前 CONTAINER 后端可用性：wsl_native 启用 → WSL，否则 docker。"""
        if self._wsl_native_cfg.get("enabled", False):
            self._apply_probe_result(self._detect_wsl_native())
        else:
            self._apply_probe_result(self._detect_docker())

    def _detect_wsl_native(self) -> tuple[str, str]:
        """同步探测 WSL 原生后端，三态语义与 _detect_docker 一致。

        只探轻量面（wsl.exe 在位 + 发行版在列）；用户/沙箱二进制的深度校验
        由 provider.is_available 在落地时做（fail-closed），此处保持与 docker
        探测同量级开销。发行版列表解码复用 WslNativeProvider 单源。
        """
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        try:
            _ensure_isolation_path()
            from providers.wsl_native_provider import WslNativeProvider  # noqa: PLC0415

            if not shutil.which("wsl"):
                return (self._PROBE_ABSENT, "")
            result = subprocess.run(  # noqa: PLW1510
                ["wsl", "-l", "-q"],
                capture_output=True,
                timeout=3,
            )
            if result.returncode != 0:
                return (self._PROBE_ABSENT, "")
            distro = str(self._wsl_native_cfg.get("distro", "Ubuntu"))
            distros = WslNativeProvider._decode_wsl_list(result.stdout or b"")
            if distro not in distros:
                return (self._PROBE_ABSENT, "")
            return (self._PROBE_AVAILABLE, "")
        except Exception as exc:
            return (self._PROBE_ERROR, str(exc))

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
                if not self._wsl_native_cfg.get("enabled", False):
                    # 引擎自愈（docker 专属）：先确保引擎存活再探测
                    self._ensure_engine()
                self._probe_backend()
                self._docker_checked_at = now
                if self._docker_available:
                    logger.info(
                        "[%s] Docker 可用性复检通过，解除 host 降级",
                        self.name,
                    )
                elif self._docker_probe_error:
                    logger.error(
                        "[%s] Docker 探测异常（超时/权限等，可用性不可验证），容器要求型工具拒绝执行 | %s",
                        self.name,
                        self._docker_probe_error,
                    )
                else:
                    logger.warning(
                        "[%s] Docker 可用性复检仍不可用，维持拒绝 + host 标记",
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
            container_id = await self._resolve_container(workspace, ctx)
            if container_id:
                exec_backend = await self._resolve_exec_backend(container_id)
                injected_calls = self._inject_container_id(
                    tool_calls,
                    {c["tool_name"] for c in docker_ctxs},
                    container_id,
                    exec_backend=exec_backend,
                )
                if injected_calls is not None:
                    state_updates[StateKeys.RAW_TOOL_CALLS] = injected_calls
                # 执行环境绑定显式落 state（幂等）：后续轮凭绑定直读
                state_updates[_CONTAINER_STATE_KEY] = container_id
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

    async def _resolve_container(
        self,
        workspace: str | None,
        ctx: PluginContext,
    ) -> str | None:
        """解析容器绑定：state 已有绑定则验活直读，缺失/失活才重新落地。

        执行环境是显式 state 数据（isolation.container_name，持久化字段）：
        首轮落地后写入，后续轮凭绑定验活（存在+running+探针通过）直接复用；
        绑定失活（容器被删/失活/daemon 慢超时）则重新走 get_or_create
        落地并刷新绑定。

        Returns:
            容器 id（env_id = workspace 派生的容器名）；服务不可用或落地失败
            返回 None（调用方据此把对应调用标 blocked，不降级裸跑）。
        """
        manager = self._get_manager()
        if manager is None:
            return None
        bound = ctx.state.get(_CONTAINER_STATE_KEY) if isinstance(ctx.state, dict) else None
        if bound:
            try:
                if await manager.ensure_container_alive(bound):
                    return bound
                logger.warning(
                    "[IsolationGuard] state 绑定的容器已失活，重新落地 | container=%s",
                    bound,
                )
            except Exception as exc:
                logger.warning(
                    "[IsolationGuard] state 绑定容器验活失败，重新落地 | container=%s | error=%s",
                    bound,
                    exc,
                )
        return await self._get_or_create_container(workspace, ctx)

    async def _resolve_exec_backend(self, container_id: str) -> dict[str, Any] | None:
        """取执行环境后端信息（wsl_native 环境才有；docker 环境无此键返回 None）。

        env 记录在 guard 进程内 manager（_get_manager 懒加载实例）的内存映射中，
        落地成功后必可查；查不到（重启后尚未重建等）不注入，bash 工具按 docker
        通路兜底——wsl_native 环境的 docker 通路必然失败并报环境不存在，不会
        误落到宿主裸跑。
        """
        manager = self._manager if self._manager is not None else self._get_manager()
        if manager is None:
            return None
        try:
            env = await manager.get_environment(container_id)
        except Exception as exc:
            logger.warning("[IsolationGuard] 读取环境后端信息失败 | container=%s | error=%s", container_id, exc)
            return None
        if env is None:
            return None
        backend = (env.provider_info or {}).get("exec_backend")
        return backend if isinstance(backend, dict) else None

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
            # isolation_types 已沉 SDK（单一真值源）；manager 可能被外部注入
            # （测试/降级路径），此处仍需自行确保 isolation 包路径存在。
            _ensure_isolation_path()
            from agentos_plugin_sdk import isolation_types as iso_types  # noqa: PLC0415

            task_id = ctx.state.get(StateKeys.TASK_ID) or ""
            env = await manager.get_or_create_environment(
                task_id=task_id or "session",
                task_type=iso_types.TaskType.ATOMIC,
                operation_type=iso_types.OperationType.CODE_EXECUTION,
                workspace=workspace,
                isolation_level=iso_types.IsolationLevel.CONTAINER,
            )
            # 创建失败时 manager 返回 ERROR 状态的占位环境（env_id 是内存假 id，
            # docker 里并无此容器）——注入它会让 bash 报"No such container"这种
            # 无法理解的错误；此处转 blocked 并留真实原因日志。
            if getattr(env, "status", "") == iso_types.EnvironmentStatus.ERROR.value:
                logger.warning(
                    "[%s] 容器创建失败（环境 ERROR 状态）| workspace=%s | detail=%s",
                    self.name,
                    workspace,
                    (getattr(env, "provider_info", {}) or {}).get("error", ""),
                )
                return None
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
        exec_backend: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]] | None:
        """为决策进容器的工具调用注入 _container_id。

        仅在确有注入时返回新 tool_calls 列表（否则返回 None，调用方不覆盖 state）。
        bash_execute 注入后由其 tool.py 走 docker exec 执行命令；browser_* 工具
        注入后经 bridge_client 在容器内发起 MCP 调用（沙箱内 MCP Client）。
        非 docker 决策工具不注入；容器内固定挂载 workspace → /workspace，
        bash 的 working_dir 未显式指定时补 /workspace（browser 工具的 workspace
        落盘目录由 bridge_client 自行翻译，见容器候选路径）。

        exec_backend：执行环境后端信息（wsl_native 环境才有，来自 env.provider_info；
        docker 环境为 None 不注入）。与 _container_id 同级的服务端信任链——
        bash 工具据其选择 wsl 传输；LLM 侧声明无效（param_inject 剥离下划线键）。
        """
        injected_calls: list[dict[str, Any]] = []
        injected = False
        for tc in tool_calls:
            new_tc = dict(tc)
            if tc.get("name", "") in docker_tool_names:
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
                if tc.get("name") == "bash_execute":
                    # exec_backend 是 bash 工具的传输通道（wsl_native 才有），
                    # browser 工具走 bridge 通路不消费
                    if exec_backend:
                        args["_exec_backend"] = exec_backend
                    # 容器内固定挂载 /workspace：未显式指定 working_dir 时补容器路径
                    if not args.get("working_dir"):
                        args["working_dir"] = "/workspace"
                new_tc["args"] = args
                injected = True
            injected_calls.append(new_tc)
        return injected_calls if injected else None

    def _deny_container_required(
        self,
        tool_name: str,
        workspace: str | None,
        log_hint: str,
    ) -> dict[str, Any]:
        """Docker 不可用/探测故障时拒绝容器要求型工具（fail-closed，不降级宿主）。

        probe_error（探测异常，隔离不可验证）与 absent（明确不可用）分态回显：
        probe_error 的拒绝原因携带探测异常原文——工具结果里的"工具被隔离策略
        拦截: docker_probe_error: ..."让 LLM 与用户能区分"docker 坏了"与
        "docker 没装"，而非同判静默拦截。
        """
        if self._docker_probe_error:
            logger.error(
                "[IsolationGuard] Docker 探测异常，隔离不可验证，拒绝执行 | tool=%s | %s | %s",
                tool_name,
                self._docker_probe_error,
                log_hint,
            )
            return self._build_context(
                tool_name,
                "denied",
                f"docker_probe_error: {self._docker_probe_error}",
                workspace=workspace,
                blocked=True,
            )
        logger.warning(
            "[IsolationGuard] Docker 不可用，拒绝执行（不降级宿主）| tool=%s | %s",
            tool_name,
            log_hint,
        )
        return self._build_context(
            tool_name,
            "denied",
            "docker_unavailable_container_required",
            workspace=workspace,
            blocked=True,
        )

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
                return self._deny_container_required(
                    tool_name, metadata_workspace, "主 agent 会话要求容器"
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
                return self._deny_container_required(
                    tool_name, metadata_workspace, "metadata 要求容器"
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
            return self._deny_container_required(
                tool_name, metadata_workspace, "policy 要求容器"
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
            # 配置显式声明可用性时以配置为准，清掉残留探测异常态
            self._docker_probe_error = None
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
        if provider == "host" and not self._docker_available:
            # Docker 不可用期间的宿主执行显式标记（降级不再无痕，下游可观测）
            context["isolation_mode"] = "host"
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
