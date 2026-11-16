"""隔离环境守卫 Input 插件。

在工具执行前根据安全策略决定是否在容器内执行；决策为 docker 的调用经
isolation 服务正门（`isolation.create_env`）落地容器并注入 _container_id
（吸收原 session_isolation 插件的会话级容器语义，见 GAP 合并）。
优先使用 IsolationDecider 从 isolation_policy.yaml 决策隔离级别，
task metadata 可覆盖决策结果。

职责边界（SRP）：
- 决策"执行环境"（container / host / denied）+ 容器落地注入
- 容器获取唯一属主是 isolation 服务（sidecar 单例自持环境管理器，配额/
  熔断按服务进程单实例计）：guard 不私建管理器，落地一律走
  `isolation.create_env` 能力调用（tool-executor 正门，显式 plugin_id）
- 不做审批（审批归 security_check 插件）
- 不写 security.decision（仅 security_check 写）
- blocked 调用经结果预填直出拒绝结果（ADR 2026-09-28：写
  pre_decided_results，tool_core 命中即跳过执行）

容器落地安全底线：容器不可达（服务缺失/调用异常/创建失败）→ 对应调用标
blocked，**绝不降级 host 裸跑**——降级会让 security_check 的 task_isolated
审批豁免放行危险命令。

State 命名空间：
    - execution_contexts : 各工具调用的执行上下文列表（security_check
      判 isolated 态仍读，保留）
    - isolation.container_name : 已落地的容器绑定（执行环境显式数据；
      持久化字段，environment_lifecycle exit 凭绑定按名销毁——服务重启
      后内存登记为空也能按名删除；落地每轮幂等回写）
    - pre_decided_results : 被策略阻止调用的预定拒绝结果（与其它 guard
      经 SDK merge_pre_decided 合并）
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from _libs.pipeline_base.plugin import IInputPlugin, PluginContext, PluginResult
from _libs.pipeline_base.types import StateKeys
from _root_api import ensure_isolation_path as _ensure_isolation_path
from decider import IsolationDecider

from agentos_plugin_sdk.isolation_types import IsolationLevel
from agentos_plugin_sdk.tool_result_protocol import (
    merge_pre_decided,
    tool_result_entry,
)

logger = logging.getLogger(__name__)

# 环境服务调用方（server.py on_load 注入）：async (tool_name, args) -> Any，
# 经 tool-executor.invoke 以显式 plugin_id 直达 isolation_service——系统插件
# 工具不在 LLM 工具注册表，反查必失败，须带 plugin_id（同 environment_lifecycle
# 的销毁调用方同款形态）。
EnvCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
_env_caller: EnvCaller | None = None


def set_env_caller(caller: EnvCaller | None) -> None:
    """注入/清除环境服务调用方（server.py on_load 构造；None = 能力未注入，容器落地降级 blocked）。"""
    global _env_caller
    _env_caller = caller


def _is_linux_platform() -> bool:
    """linux_native 平台门谓词：bwrap/namespace 是 Linux 内核机制，仅 Linux 生效。

    os.name 不足以判定（posix 混含 macOS——macOS 无 namespace/bwrap，结构性
    不可实例化），sys.platform == "linux" 精确。与 isolation manager 同名谓词
    双端一致（跨插件不可 import，一致性由接线测试钉死，ADR 2026-10-12）。
    """
    return sys.platform == "linux"

# 容器绑定在管道 state 里的键：执行环境（容器名）是显式数据，落地一次
# 全程直读。
_CONTAINER_STATE_KEY = "isolation.container_name"

# 宿主路径逃逸拒绝话术（隔离任务直出给模型的结果文本）：说清拦截原因、
# 越位事实与改法（工作空间内相对路径重试），模型自行纠正，不经宿主审批。
_HOST_PATH_REJECT_MESSAGE = (
    "本次调用被拦截：命令包含工作空间边界之外的宿主绝对路径，"
    "你已经越位——宿主路径不在容器文件系统内。"
    "当前工作空间就是你的当前目录，请改用相对路径（或容器内路径，如 /workspace/...）重新调用。"
)


# isolation 目录 sys.path 注入走共享单源（isolation_path.ensure_isolation_path，
# 顶部导入；引擎自愈/WSL 探测的懒加载点运行时调用）。


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
        # CONTAINER 后端配置（探测目标按后端分派：Linux 默认 linux_native、
        # Windows 启用态 wsl_native、其余 docker）；返回值 = 配置注入链断裂
        # 说明（None = 配置就绪）
        self._config_probe_error = self._load_wsl_native_config()
        self._load_linux_native_config()
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
        elif self._config_probe_error:
            # 配置注入链断裂 → 后端可用性不可判定：按 probe_error fail-closed
            # （容器要求型工具拒绝），绝不静默回落 docker 探测（F11：禁止
            # 静默兜底——yaml 已启用 wsl_native 的部署无声走 docker 即
            # 2026-09-14 管道实测事故形态）。
            self._apply_probe_result((self._PROBE_ERROR, self._config_probe_error))
            self._docker_auto = True
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

    def _ensure_engine(self) -> None:
        """引擎自愈：docker 不可达时尝试拉起 WSL 保活会话唤醒引擎。

        幂等（wsl_health.ensure_docker_engine 内部冷却），仅在复检路径
        （当前不可用 + 越过复检冷却）触发；失败只留日志不阻断复检。
        wsl_health 位于 system/isolation（本目录无副本），须先注入路径
        再延迟导入。
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

    def _load_wsl_native_config(self) -> str | None:
        """wsl_native 后端配置：注入单通道装载，返回配置注入链断裂说明。

        取数源（均为注入面，无任何运行时读取回退）：
        - 构造 config 的 ``providers`` 键（调用方/step config 显式传入）；
        - manifest config_files 注入命名空间 ``isolation_config``（内核按
          config_files[].id 注入，与 isolation 服务的 get_config() 同源）。

        两源皆缺 = 配置注入链断裂（manifest 声明未生效/内核未携带注入）→
        返回断裂说明（调用方按 probe_error fail-closed，容器面拒绝、不静默
        回落 docker 探测——F11 禁止静默兜底，2026-09-14 管道实测踩中过
        「yaml 已启用 wsl_native 而落地走 cua 容器」的同型事故）。源在而
        ``providers.wsl_native`` 缺席 = 未配置该后端（合法缺省，docker 后端）。

        平台门（装载出口单点归一）：``os.name != "nt"`` 时把 ``_wsl_native_cfg``
        置空（等价「未配置该后端」的合法缺省形态）。wsl_native 是 Windows 专属
        后端，非 Windows 平台 ``enabled`` 语义收敛为「不参与后端竞争」——这是
        装载期平台适配，非 F11 禁止的运行期静默兜底（与 manager 注册端同一
        ``os.name`` 谓词，双端一致）。单点归一后全部下游消费位（_probe_backend
        探测分派、复检路径 _ensure_engine 引擎自愈门、未来新增 enabled 消费位）
        自动继承，无需各自加门。
        """
        providers_cfg = self._config.get("providers")
        notice: str | None = None
        if isinstance(providers_cfg, dict) and "wsl_native" in providers_cfg:
            cfg = providers_cfg.get("wsl_native")
            self._wsl_native_cfg = cfg if isinstance(cfg, dict) else {}
        elif isinstance(self._config.get("isolation_config"), dict):
            injected = self._config["isolation_config"]
            providers = injected.get("providers")
            cfg = providers.get("wsl_native") if isinstance(providers, dict) else None
            self._wsl_native_cfg = cfg if isinstance(cfg, dict) else {}
        else:
            self._wsl_native_cfg = {}
            notice = (
                "isolation_config 未注入（manifest config_files 未生效）且无显式 "
                "providers 配置——容器后端可用性不可判定"
            )
        # 装载出口平台门（语义见 docstring）：单点覆盖全部取数路径
        if os.name != "nt":
            self._wsl_native_cfg = {}
        return notice

    def _load_linux_native_config(self) -> None:
        """linux_native 后端配置：注入单通道装载（与 wsl_native 同取数源）。

        取数源（均为注入面，无任何运行时读取回退）：构造 config 的
        ``providers`` 键 > manifest config_files 注入命名空间
        ``isolation_config``。源在而 ``providers.linux_native`` 缺席 =
        未配置该后端（合法形态，enabled 缺省 **true**——linux_native 是
        Linux 平台的默认 CONTAINER 后端，零配置部署即生效）。两源皆缺 =
        配置注入链断裂（断裂说明由 _load_wsl_native_config 返回并按
        probe_error fail-closed，本装载不再重复报）。

        平台门（装载出口单点归一）：非 Linux 平台把 ``_linux_native_cfg``
        收敛为显式关闭形态 ``{"enabled": False}``。enabled 缺省 true 与
        wsl_native 相反，置空 dict 盖不过缺省，必须显式 False——语义等价
        「后端在此平台结构性不可用」（bwrap/namespace 为 Linux 机制）。
        单点归一后全部下游消费位（_probe_backend 分派、复检路径引擎自愈门、
        未来新增 enabled 消费位）自动继承，无需各自加门。
        """
        providers_cfg = self._config.get("providers")
        if isinstance(providers_cfg, dict) and "linux_native" in providers_cfg:
            cfg = providers_cfg.get("linux_native")
            self._linux_native_cfg = cfg if isinstance(cfg, dict) else {}
        elif isinstance(self._config.get("isolation_config"), dict):
            injected = self._config["isolation_config"]
            providers = injected.get("providers")
            cfg = providers.get("linux_native") if isinstance(providers, dict) else None
            self._linux_native_cfg = cfg if isinstance(cfg, dict) else {}
        else:
            self._linux_native_cfg = {}
        if not _is_linux_platform():
            self._linux_native_cfg = {"enabled": False}

    def _native_backend_active(self) -> bool:
        """wsl_native/linux_native 是否承载 CONTAINER 后端（引擎自愈门）。

        引擎自愈（ensure_docker_engine）是 docker 后端专属的 WSL 保活动作；
        原生后端无 docker 引擎可救（wsl_native 走 wsl.exe 传输、linux_native
        走宿主 bwrap），复检路径跳过。
        """
        return self._wsl_native_cfg.get("enabled", False) or self._linux_native_cfg.get(
            "enabled", True
        )

    def _probe_backend(self) -> None:
        """探测当前 CONTAINER 后端可用性：linux_native（Linux 默认）→
        wsl_native（Windows 启用态）→ docker。

        分派优先级即槽位竞争顺序（与 manager 注册端一致）；平台门使 linux/
        wsl 两分支互斥（Linux 上 wsl cfg 归空、非 Linux 上 linux cfg 收敛
        关闭）。配置注入链断裂（_config_probe_error 在场）→ probe_error
        三态，不探测（可用性不可判定的面不猜测，容器要求型工具按
        fail-closed 拒绝）。
        """
        if self._config_probe_error:
            self._apply_probe_result((self._PROBE_ERROR, self._config_probe_error))
        elif self._linux_native_cfg.get("enabled", True):
            self._apply_probe_result(self._detect_linux_native())
        elif self._wsl_native_cfg.get("enabled", False):
            self._apply_probe_result(self._detect_wsl_native())
        else:
            self._apply_probe_result(self._detect_docker())

    def _detect_linux_native(self) -> tuple[str, str]:
        """同步探测 Linux 原生后端，三态语义与 _detect_docker 一致。

        只探轻量面（bwrap 在 PATH）；内核 userns 支持等深度校验由
        provider.is_available 真实沙箱自检承担（fail-closed），此处保持与
        docker 探测同量级开销。
        """
        import shutil  # noqa: PLC0415

        bwrap_path = str(self._linux_native_cfg.get("bwrap_path", "bwrap"))
        try:
            if not shutil.which(bwrap_path):
                return (self._PROBE_ABSENT, "")
            return (self._PROBE_AVAILABLE, "")
        except Exception as exc:
            return (self._PROBE_ERROR, str(exc))

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

        # 纯 LLM 回复（无待执行调用）根本不需要 docker 隔离决策——提前返回，
        # 避免 execute 入口为普通聊天也触发 docker 探测（daemon 不可达时会卡满
        # subprocess timeout，每条消息延迟 +15s）。docker 复检下移到确认
        # 有 tool_calls 之后再做。raw_tool_calls 非空 ⟺ 本轮有调用待执行
        # （静态化管道下的调用轮判据，ADR 2026-10-02-loopconfig-parallel-foreach）。
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
                if not self._native_backend_active():
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
        self._apply_task_isolation(execution_contexts, task_isolated)

        state_updates: dict[str, Any] = {
            "execution_contexts": execution_contexts,
        }

        # ── 容器落地（吸收原 session_isolation 的会话级容器语义）──
        # provider=docker 的 bash_execute 经 isolation 服务正门（create_env）
        # 按 workspace 幂等获取/创建容器并注入 _container_id（bash tool.py 据
        # 此走 docker exec 通路）。容器不可达（服务缺失/调用异常/创建失败）→
        # 对应调用标 blocked，绝不降级 host 裸跑（降级会让 security_check 的
        # task_isolated 审批豁免放行危险命令）。
        docker_ctxs = [c for c in execution_contexts if c.get("provider") == "docker"]
        if docker_ctxs:
            workspace = self._resolve_landing_workspace(ctx, task_metadata)
            container_id, exec_backend = await self._get_or_create_container(workspace, ctx)
            if container_id:
                if not isinstance(state.get("tool_output_contracts"), dict):
                    logger.warning(
                        "[%s] state.tool_output_contracts 缺失，隔离能力声明不可查"
                        "（_exec_backend 不注入；管道缺 tool_schema 步骤或状态断链）",
                        self.name,
                    )
                injected_calls = self._inject_container_id(
                    tool_calls,
                    {c["tool_name"] for c in docker_ctxs},
                    container_id,
                    exec_backend=exec_backend,
                    exec_backend_capable=self._isolation_capable_tools(state),
                )
                if injected_calls is not None:
                    state_updates[StateKeys.RAW_TOOL_CALLS] = injected_calls
                # 执行环境绑定显式落 state（幂等）：environment_lifecycle exit
                # 凭绑定按名销毁（服务重启后内存登记为空也能按名删除）
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

        # 被策略阻止的调用直出预定拒绝结果（ADR 2026-09-28 结果预填）：
        # tool_core 按 call_id（无 id 按工具名兜底）命中即跳过执行。
        # execution_contexts 与 tool_calls 按下标对齐（同一循环构造）。
        blocked_pairs = [
            (tc, c)
            for tc, c in zip(tool_calls, execution_contexts, strict=True)
            if c.get("blocked")
        ]
        if blocked_pairs:
            entries = [
                tool_result_entry(
                    tc.get("name", ""),
                    call_id=tc.get("id"),
                    success=False,
                    error=(
                        _HOST_PATH_REJECT_MESSAGE
                        if c.get("reason") == "host_path_detected"
                        else f"工具被隔离策略拦截: {c.get('reason', '隔离策略阻止')}"
                    ),
                    metadata={
                        "decided_by": "isolation_guard",
                        "provider": c.get("provider", ""),
                    },
                )
                for tc, c in blocked_pairs
            ]
            state_updates["pre_decided_results"] = merge_pre_decided(
                state.get("pre_decided_results"), entries
            )
            tool_names = ", ".join(c["tool_name"] for _, c in blocked_pairs)
            logger.warning(
                "[IsolationGuard] 阻止工具执行 | tools=%s",
                tool_names,
            )

        return PluginResult(state_updates=state_updates)

    # ── 容器落地辅助 ────────────────────────────────────────────

    # 容器挂载源兜底来源：随管道投影恒在的任务域坐标键（task_state_writer
    # 直写 state；会话域 ws_meta 不列——任务管道不写会话域键）。
    _WS_META_FALLBACK_KEYS: ClassVar[tuple[str, ...]] = (
        "task.ws_meta",
        "lineage.parent_ws_meta",
    )

    def _resolve_landing_workspace(
        self,
        ctx: PluginContext,
        task_metadata: dict[str, Any],
    ) -> str | None:
        """容器落地挂载源解析：state.workspace → task metadata → 任务坐标键。

        task_service 跨插件接线未落前 metadata 恒空（_get_task_metadata 降级为空），
        任务管道在部分路径（shared 模式/出生契约缺 workspace 键）不落
        state.workspace——单一来源缺失会让容器落地全程失败、要求容器隔离的工具
        全被拦（2026-10-17 装机实测：整条任务生命期 bash_execute 全 blocked）。
        task.ws_meta / lineage.parent_ws_meta 是任务域坐标键，与 state.workspace
        同源（父工作区路径），作兼容兜底；全部缺失仍走 blocked（不降级裸跑，
        悬空挂载防护在 _get_or_create_container 内按真实存在性复核）。
        """
        direct = ctx.state.get("workspace")
        if direct:
            return str(direct)
        metadata_ws = task_metadata.get("workspace")
        if metadata_ws:
            return str(metadata_ws)
        for key in self._WS_META_FALLBACK_KEYS:
            coord = ctx.state.get(key)
            if isinstance(coord, str):
                import json  # noqa: PLC0415

                try:
                    coord = json.loads(coord)
                except (json.JSONDecodeError, TypeError):
                    coord = None
            if isinstance(coord, dict) and coord.get("path"):
                return str(coord["path"])
        return None

    async def _get_or_create_container(
        self,
        workspace: str | None,
        ctx: PluginContext,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """经 isolation 服务正门（create_env）幂等获取/创建 workspace 容器。

        容器获取唯一属主是 isolation 服务（sidecar 单例 manager）：每轮按
        workspace 调 create_env，服务端幂等（同 workspace 复用内存登记或收养
        既有容器，env_id = workspace 派生的容器名，稳定不变）；guard 侧不再
        持 manager，配额/熔断计数归服务进程单实例。state 绑定由调用方落库
        （isolation.container_name，销毁锚），落地路径不读绑定。

        Returns:
            (env_id, exec_backend)；服务不可用/调用异常/创建失败返回
            (None, None)——调用方据此把对应调用标 blocked，不降级裸跑。
            exec_backend 是 bash 传输通道信息（仅原生环境有；docker 环境为
            None 不注入，bash 按 docker 通路兜底）。
        """
        if not workspace:
            return None, None
        # 悬空挂载防护：workspace 是容器挂载源，挂载前必须真实存在。state/
        # metadata 里的 workspace 可能指向已删除或从未创建的目录（如按陈旧
        # 提示词锚定的路径），缺此预检时 docker daemon 会对不存在的 bind
        # mount 源静默自动建空目录，命令落空目录却以成功假象通过。
        if not Path(workspace).exists():
            logger.warning(
                "[%s] 拒绝创建容器：工作空间路径不存在（悬空挂载防护）| workspace=%s",
                self.name,
                workspace,
            )
            return None, None
        caller = _env_caller
        if caller is None:
            logger.warning(
                "[%s] 环境服务调用方未注入（tool-executor 能力缺失），容器落地降级 blocked",
                self.name,
            )
            return None, None
        task_id = ctx.state.get(StateKeys.TASK_ID) or ""
        try:
            raw = await caller(
                "isolation.create_env",
                {
                    # 无任务挂 "session" 主会话容器（会话级容器语义）；
                    # isolation_level 服务 enum "isolated" 即 SDK CONTAINER 的值。
                    "task_id": task_id or "session",
                    "task_type": "atomic",
                    "operation_type": "code_execution",
                    "workspace": workspace,
                    "isolation_level": "isolated",
                },
            )
        except Exception as exc:
            logger.warning(
                "[%s] 容器获取/创建调用失败 | workspace=%s | error=%s",
                self.name,
                workspace,
                exc,
            )
            return None, None
        return self._parse_create_env_response(raw, workspace)

    @staticmethod
    def _parse_create_env_response(
        raw: Any,
        workspace: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """isolation.create_env 信封 → (env_id, exec_backend)；业务失败 → (None, None)。

        失败形态全覆盖（任一命中即 blocked，原因留日志）：
        - 信封 success=False：服务端显式失败（manager 未初始化/配额超限等
          manager 抛错，error 键携原因）；
        - data 带 error 键：服务端裸返 error dict 的旧形态（内核 ③ 归一会包成
          success=true，须读业务载荷）；
        - data.status == "error"：创建失败的 ERROR 占位环境（env_id 是内存假
          id，docker 里并无此容器）——不注入假 id，provider_error 留真实原因。
        """
        if not isinstance(raw, dict):
            logger.warning(
                "[IsolationGuard] 环境服务返回非映射信封 | workspace=%s", workspace
            )
            return None, None
        if raw.get("success") is False:
            logger.warning(
                "[IsolationGuard] 容器创建被服务拒绝 | workspace=%s | error=%s",
                workspace,
                raw.get("error", ""),
            )
            return None, None
        data = raw.get("data")
        if not isinstance(data, dict):
            logger.warning(
                "[IsolationGuard] 环境服务返回缺 data 载荷 | workspace=%s", workspace
            )
            return None, None
        if data.get("error"):
            logger.warning(
                "[IsolationGuard] 环境服务业务失败 | workspace=%s | error=%s",
                workspace,
                data["error"],
            )
            return None, None
        if data.get("status") == "error":
            logger.warning(
                "[IsolationGuard] 容器创建失败（环境 ERROR 状态）| workspace=%s | detail=%s",
                workspace,
                data.get("provider_error", ""),
            )
            return None, None
        env_id = data.get("env_id")
        if not env_id:
            logger.warning(
                "[IsolationGuard] 环境服务返回缺 env_id | workspace=%s", workspace
            )
            return None, None
        backend = data.get("exec_backend")
        return env_id, backend if isinstance(backend, dict) else None

    @staticmethod
    def _isolation_capable_tools(state: dict[str, Any] | None) -> set[str]:
        """声明了隔离执行能力的工具名集合（契约表单源）。

        真相源 = state.tool_output_contracts（tool_schema 插件逐字透传内核
        tool-surface.schemas 契约表；ToolCapability.isolation_capable 服务端
        声明，LLM 侧无对应参数位）。状态缺失/条目未声明 → 不注入——
        声明缺席 = 能力未声明（fail-closed），不回退硬编码名单。
        """
        contracts = state.get("tool_output_contracts") if isinstance(state, dict) else None
        if not isinstance(contracts, dict):
            return set()
        return {
            name
            for name, entry in contracts.items()
            if isinstance(entry, dict) and entry.get("isolation_capable") is True
        }

    @staticmethod
    def _inject_container_id(
        tool_calls: list[dict[str, Any]],
        docker_tool_names: set[str],
        container_id: str,
        exec_backend: dict[str, Any] | None = None,
        exec_backend_capable: set[str] | None = None,
    ) -> list[dict[str, Any]] | None:
        """为决策进容器的工具调用注入 _container_id。

        仅在确有注入时返回新 tool_calls 列表（否则返回 None，调用方不覆盖 state）。
        bash_execute 注入后由其 tool.py 走 docker exec 执行命令；其他声明了
        isolation_capable 的工具按各自通路消费（browser_* 经 bridge_client 在
        容器/沙箱环境内发起 MCP 调用）。非 docker 决策工具不注入；容器内固定
        挂载 workspace → /workspace，bash 的 working_dir 未显式指定时补
        /workspace（bash 专属缺省，其余工具工作目录语义各异不代填）。

        exec_backend：执行环境后端信息（原生环境才有，来自 env.provider_info；
        docker 环境为 None 不注入）。与 _container_id 同级的服务端信任链——
        声明了 isolation_capable 的工具据其选择沙箱内传输通道（wsl/bwrap/
        docker）；LLM 侧声明无效（param_inject 剥离下划线键）。
        注入条件 = exec_backend 存在且工具在 exec_backend_capable 声明集
        （契约表单源，见 _isolation_capable_tools）。
        """
        injected_calls: list[dict[str, Any]] = []
        injected = False
        capable = exec_backend_capable or set()
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
                if exec_backend and tc.get("name", "") in capable:
                    args["_exec_backend"] = exec_backend
                if tc.get("name") == "bash_execute" and not args.get("working_dir"):
                    # bash 专属缺省：容器挂载点约定 /workspace
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

    def _apply_task_isolation(self, execution_contexts: list[dict[str, Any]], task_isolated: bool) -> None:
        """给每个 context 注入任务级隔离标志（S1 安全整改，评估 2026-09-24）。

        "隔离容器即安全边界"裁定（2026-09-15）的前提是执行面在容器内。host
        裸跑的 context（force_host、主 agent L1 路由、宿主路径路由、policy
        host）不承载免审批默认——task_isolated=False 使 security_check 的
        all() 语义对含 host 调用的批次 fail-closed（回落所选权限档审批）。
        worktree/容器 context 不受影响，裁定语义保持。
        """
        for context in execution_contexts:
            context["task_isolated"] = task_isolated and context.get("provider") != "host"

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
        4. 命令含宿主路径（如 D:/...、C:\\...）时，即使 policy 要求容器也不进
           容器——容器内只有挂载的 /workspace，宿主路径必然不存在（返回
           "No such file or directory"）。隔离任务就地拒绝：调用标 blocked，
           经结果预填把改道话术直出给模型（不路由 host、不弹审批）；非隔离
           任务路由到 host 执行，由 security_check 的 host_path_access 规则
           触发审批。

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
        # 宿主路径必然不存在（返回 "No such file or directory"）。
        # 隔离任务：容器逃逸企图就地拒绝——调用标 blocked，经结果预填
        # （ADR 2026-09-28）把拒绝话术直出给模型，模型改用工作空间内路径
        # 重试，不路由 host、不弹审批卡。
        # 非隔离任务：路由到 host 执行，由 security_check 的 host_path_access
        # 规则触发用户审批。
        if (
            policy_isolation == IsolationLevel.CONTAINER
            and tool_name == "bash_execute"
            and tool_args
            and self._has_host_path(tool_args)
        ):
            if metadata_isolation == "isolated":
                logger.info(
                    "[IsolationGuard] 隔离任务命令含宿主路径，就地拒绝并预填改道话术 | tool=%s | reason=host_path_detected",
                    tool_name,
                )
                return self._build_context(
                    tool_name,
                    "denied",
                    "host_path_detected",
                    workspace=metadata_workspace,
                    blocked=True,
                )
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

        # ── 工具级 policy 决策（policy 为 host）──
        # （policy==CONTAINER 的 docker/deny 决策已由上方 metadata 块全部
        # return——container-required 不降级与 metadata 强制 host 降级均在
        # 该块内闭环，此处不可能再收到 CONTAINER）
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
        from _libs.pipeline_base.plugin import find_plugin_config  # noqa: PLC0415

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
