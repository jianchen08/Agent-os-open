"""MXC 原生隔离提供者（Windows ProcessContainer / WSLC 双后端）。

设计真值：``docs/working/MXC隔离后端_设计草案_20261117.md``；决策取舍见
``docs/working/MXC隔离后端_ADR草稿_20261117.md``。

环境 = 「workspace 目录 + metadata 档 + 一份 MXC 策略 JSON」：无常驻容器
（wslc 档由执行器按请求创建/销毁），隔离边界由 MXC 引擎的 ProcessContainer
（Windows PE 受限令牌）或 WSLC（Linux 容器）承载。

后端选择 = **注册表**（``providers.mxc_native.backends``，profile→containment），
装载期由 manager 按平台门 + 配置决定是否占据 CONTAINER 槽位；运行期不因
探测结果改选后端（探测只回答「已注册后端现在能不能用」，ADR 2026-10-09）。

本 provider 不实现「按任务声明切工具面」：``execution_profile`` 只决定
用哪个 MXC backend（tool 面分叉在 bash/process_runtime 侧）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import locale
import logging
import os
import re
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from providers import env_metadata
from providers.base import IsolationProvider

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    TaskType,
)

logger = logging.getLogger(__name__)

PROVIDER_TYPE = "mxc_native"

PROFILE_LINUX_BASH = "linux-bash"
PROFILE_NATIVE_WIN = "native-win"
DEFAULT_PROFILE = PROFILE_LINUX_BASH

#: profile → 允许的 MXC containment 值（注册表值的合法域）。
_ALLOWED_BACKENDS: dict[str, str] = {
    PROFILE_LINUX_BASH: "wslc",
    PROFILE_NATIVE_WIN: "processcontainer",
}

#: 注册表缺省（配置缺席时的出厂注册表；enabled 由 manager 段控制）。
_DEFAULT_BACKENDS: dict[str, str] = dict(_ALLOWED_BACKENDS)

_ERROR_EXECUTOR_MISSING = "MXC_EXECUTOR_MISSING"
_ERROR_BACKEND_NOT_REGISTERED = "MXC_BACKEND_NOT_REGISTERED"
_ERROR_BACKEND_UNAVAILABLE = "MXC_BACKEND_UNAVAILABLE"
_ERROR_WSLC_WSL_TOO_OLD = "WSLC_WSL_TOO_OLD"

_REQUEST_VERSION = "1.0.0"

#: MXC 要求的最低 WSL 版本（WSLC backend；docs/backends/wslc）。
_MIN_WSL_VERSION = (3, 0, 5)

#: 平台门谓词与 manager 同口径（os.name == "nt"）：native-win 只在 Windows 注册。
_IS_WINDOWS = os.name == "nt"


def _as_path_list(value: Any) -> list[str]:
    """配置路径数组归一（None/非列表 → 空）。"""
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if str(x).strip()]


def _default_denied_paths() -> list[str]:
    """敏感目录 deny 清单（home 下凭据 + Windows 配置库）。

    只 deny 整目录：MXC 的硬链接名保留独立权限，单文件 deny 不是对象级安全
    边界（设计草案 §6.1 硬性注意）。

    路径必须是**绝对路径**：Git Bash 的 SYSTEMDRIVE 常为 ``C:``（无尾部分隔
    符），``Path("C:") / "Windows"`` 得到盘符相对路径 ``C:Windows\\...``，
    MXC ProcessContainer 建安全环境时按路径不存在失败（真机实证
    HRESULT 0x80070003）。Windows 配置库优先用 SystemRoot 派生。
    """
    home = Path.home()
    candidates = [home / ".ssh", home / ".aws", home / ".config"]
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if system_root:
        candidates.append(Path(system_root) / "System32" / "config")
    return [str(p) for p in candidates if str(p).strip()]


class MxcNativeProvider(IsolationProvider):
    """MXC 原生隔离提供者（Windows CONTAINER 槽位候选）。

    config 键：
    - backends: profile→containment 注册表（缺省两条全注册）；
    - default_profile: 未声明 execution_profile 的任务使用的 profile（缺省
      linux-bash，与 0.2 现状兼容）；
    - executor: MXC 执行器路径或 PATH 名（缺省读 AGENTOS_MXC_EXECUTOR；
      空 = 不可用 fail-closed，不静默回落其它后端）；
    - executor_args: 执行器 argv 模板（{request} 替换为请求 JSON 路径）；
    - state_dir: metadata/策略档目录（缺省 <仓库根>/.ai_workspaces/mxc_native_envs）；
    - cache_root: 编译缓存根（只读基线 + 任务可写层；空 = 不注入）；
    - image: wslc 镜像（缺省 agentos-linux:latest）；
    - network: MXC network 段（缺省 egress deny）；
    - ui: ProcessContainer ui 段（缺省 disable=false：锁 Win32k 会让控制台
      程序 DLL 初始化失败；wslc 不下发）；
    - require_wsl: linux-bash 档是否校验 WSL ≥3.0.5（缺省 true）；
    - denied_paths: 覆盖缺省敏感目录 deny 清单。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._repo_root = Path(__file__).resolve().parents[4]
        self._backends = self._load_registry(self._config.get("backends"))
        self._default_profile = str(self._config.get("default_profile") or DEFAULT_PROFILE)
        if self._default_profile not in self._backends:
            # 缺省 profile 不在注册表 = 装载期配置错误：退回首条注册项（仍显式
            # 留痕），不静默变成无后端。
            fallback_profile = next(iter(self._backends), DEFAULT_PROFILE)
            logger.error(
                "[MxcNativeProvider] default_profile=%s 未注册，退回首条注册项 %s",
                self._default_profile,
                fallback_profile,
            )
            self._default_profile = fallback_profile
        executor = self._config.get("executor")
        if executor is None:
            executor = os.environ.get("AGENTOS_MXC_EXECUTOR", "")
        self._executor = str(executor or "")
        self._executor_args = [str(x) for x in self._config.get("executor_args", ["{request}"])]
        if self._config.get("state_dir"):
            self._state_dir = str(self._config["state_dir"])
        else:
            self._state_dir = str(self._repo_root / ".ai_workspaces" / "mxc_native_envs")
        self._cache_root = str(self._config.get("cache_root") or "")
        self._image = str(self._config.get("image") or "agentos-linux:latest")
        self._network = self._normalize_network(self._config.get("network"))
        self._ui = self._normalize_ui(self._config.get("ui"))
        self._require_wsl = bool(self._config.get("require_wsl", True))
        self._denied_paths = (
            _as_path_list(self._config.get("denied_paths"))
            if isinstance(self._config.get("denied_paths"), list)
            else _default_denied_paths()
        )
        self._environments: dict[str, IsolationEnvironment] = {}
        # WSL 版本探测缓存：None=未探测；""=不可用；否则版本串
        self._wsl_version: str | None = None

    # ── 注册表（后端选择真值）────────────────────────────────────

    @staticmethod
    def _load_registry(raw: Any) -> dict[str, str]:
        """加载 profile→containment 注册表；非法条目装载期剔除并留痕。

        非法 = profile 未知 / containment 不是该 profile 的合法 backend。
        注册表由配置单源持有，provider 不按探测结果增删条目。
        """
        if not isinstance(raw, dict) or not raw:
            return dict(_DEFAULT_BACKENDS)
        registry: dict[str, str] = {}
        for profile, backend in raw.items():
            name = str(profile).strip()
            value = str(backend).strip()
            expected = _ALLOWED_BACKENDS.get(name)
            if expected is None:
                logger.error("[MxcNativeProvider] 注册表含未知 profile，剔除 | profile=%s", name)
                continue
            if value != expected:
                logger.error(
                    "[MxcNativeProvider] 注册表 backend 非法，剔除 | profile=%s | backend=%s | 合法值=%s",
                    name,
                    value,
                    expected,
                )
                continue
            registry[name] = value
        if not registry:
            logger.error("[MxcNativeProvider] 注册表为空，回退出厂注册表")
            return dict(_DEFAULT_BACKENDS)
        return registry

    def registered_profiles(self) -> dict[str, str]:
        """注册表快照（诊断/测试读面）。"""
        return dict(self._backends)

    def default_profile(self) -> str:
        return self._default_profile

    def resolve_backend(self, profile: str | None) -> tuple[str | None, str | None]:
        """profile → (backend, error_code)。未注册返回 (None, MXC_BACKEND_NOT_REGISTERED)。"""
        name = str(profile or self._default_profile)
        backend = self._backends.get(name)
        if backend is None:
            return None, _ERROR_BACKEND_NOT_REGISTERED
        return backend, None

    def env_name_suffix(self, metadata: dict[str, Any]) -> str:
        """环境名后缀：默认 profile 不加后缀（同 workspace 同环境语义不变），
        非默认 profile 加 ``--<profile>``，避免同 workspace 两种后端互相收养。
        """
        profile = str((metadata or {}).get("execution_profile") or self._default_profile)
        if profile == self._default_profile:
            return ""
        return f"--{profile}"

    # ── IsolationProvider 接口 ──────────────────────────────────

    def get_level(self) -> IsolationLevel:
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """执行器在位检查（注册表可用性的最小判据）。

        更细的 backend 就绪探测在 create/execute 时按 profile 执行（WSL 版本
        等），不在此处按探测结果改选后端——选择真值只来自注册表。
        """
        executor = self._resolve_executor()
        if not executor:
            return False, f"{_ERROR_EXECUTOR_MISSING}: MXC 执行器未配置（executor/AGENTOS_MXC_EXECUTOR）"
        return True, None

    async def create_environment(
        self,
        context: IsolationContext,
        container_name: str | None = None,
    ) -> IsolationEnvironment:
        """创建（或按名收养）MXC 隔离环境（metadata + 策略档）。"""
        now = datetime.now(UTC)
        name = container_name if container_name else f"cua-{context.task_id}"
        profile = str((context.metadata or {}).get("execution_profile") or self._default_profile)
        backend, registry_error = self.resolve_backend(profile)
        if registry_error or backend is None:
            logger.error(
                "[MxcNativeProvider] profile 未注册，拒绝创建 | name=%s | profile=%s",
                name,
                profile,
            )
            return self._make_error_environment(
                context,
                now,
                f"{_ERROR_BACKEND_NOT_REGISTERED}: profile={profile} 未在 providers.mxc_native.backends 注册",
            )

        if not context.workspace:
            return self._make_error_environment(context, now, "工作空间为空，无法建立 MXC 隔离环境")
        if not await asyncio.to_thread(Path(context.workspace).exists):
            return self._make_error_environment(
                context, now, f"工作空间路径不存在: {context.workspace}"
            )

        runtime_error = await self._check_runtime(profile)
        if runtime_error:
            logger.error(
                "[MxcNativeProvider] 后端不可用，拒绝创建 | name=%s | profile=%s | error=%s",
                name,
                profile,
                runtime_error,
            )
            return self._make_error_environment(context, now, runtime_error)

        # 幂等收养：metadata 同 workspace 同 profile → 直接复用
        meta = env_metadata.read_metadata(self._state_dir, name, logger=logger)
        if meta is not None:
            same_workspace = meta.get("workspace") == context.workspace
            same_profile = meta.get("execution_profile") == profile
            if same_workspace and same_profile:
                logger.info("[MxcNativeProvider] 收养已有同名环境 | name=%s", name)
                return self._build_env(name, context, now, profile, backend, meta.get("policy", {}))
            logger.warning(
                "[MxcNativeProvider] 陈旧 metadata（workspace/profile 漂移），清档重建 | name=%s",
                name,
            )
            env_metadata.remove_metadata(self._state_dir, name, logger=logger)

        policy = self._render_policy(profile, context.workspace, context.task_id)
        policy_path = self._write_policy(name, policy)
        if policy_path is None:
            return self._make_error_environment(context, now, "MXC 策略档写入失败")

        if not self._write_metadata(name, context.workspace, profile, backend, policy, policy_path, now):
            return self._make_error_environment(context, now, "metadata 档写入失败")

        env = self._build_env(name, context, now, profile, backend, policy)
        logger.info(
            "[MxcNativeProvider] 环境已创建 | name=%s | profile=%s | backend=%s | workspace=%s",
            name,
            profile,
            backend,
            context.workspace,
        )
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁环境：删 metadata + 策略档（无常驻容器可清；幂等）。"""
        removed = env_metadata.remove_metadata(self._state_dir, env_id, logger=logger)
        policy_removed = self._remove_policy(env_id)
        if not removed or not policy_removed:
            logger.warning(
                "[MxcNativeProvider] 销毁环境失败（metadata/policy 删除异常，保留登记） | id=%s",
                env_id,
            )
            return False
        self._environments.pop(env_id, None)
        return True

    async def find_environment_by_name(self, name: str) -> IsolationEnvironment | None:
        """按名收养（服务重启后内存登记为空的恢复通路）。"""
        meta = env_metadata.read_metadata(self._state_dir, name, logger=logger)
        if meta is None:
            return None
        workspace = meta.get("workspace")
        if not workspace or not await asyncio.to_thread(Path(str(workspace)).exists):
            logger.warning("[MxcNativeProvider] 环境失效（workspace 消失），清档待重建 | name=%s", name)
            env_metadata.remove_metadata(self._state_dir, name, logger=logger)
            return None
        profile = str(meta.get("execution_profile") or self._default_profile)
        backend = str(meta.get("backend") or self.resolve_backend(profile)[0] or "")
        if not backend:
            return None
        now = datetime.now(UTC)
        context = IsolationContext(
            task_id=name,
            task_type=TaskType.ATOMIC,
            is_root_task=True,
            isolation_level=IsolationLevel.CONTAINER,
            metadata={"execution_profile": profile},
        )
        context.workspace = str(workspace)
        return self._build_env(name, context, now, profile, backend, meta.get("policy", {}))

    async def execute_in_environment(
        self,
        env_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在环境中执行操作（一次一个 MXC 执行器子进程）。"""
        env = self._environments.get(env_id)
        if not env:
            return ExecutionResult(success=False, output=None, error=f"环境不存在: {env_id}")
        if env.status == EnvironmentStatus.ERROR.value:
            return ExecutionResult(
                success=False,
                output=None,
                error=str(env.provider_info.get("error", "环境处于错误状态")),
            )

        op_type = operation.get("type")
        if op_type == "command":
            return await self._exec_command(env, operation)
        if op_type == "file_operation":
            return await self._file_op(env, operation)
        return ExecutionResult(success=False, output=None, error=f"不支持的操作类型: {op_type}")

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        return EnvironmentStatus.READY if env_id in self._environments else EnvironmentStatus.STOPPED

    # ── 后端元数据（bash/process_runtime 共用）───────────────────

    def exec_backend_info(
        self,
        *,
        env_name: str,
        profile: str,
        backend: str,
        workspace: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """后端调度信息：随 env.provider_info 下发，process_runtime 凭此建请求。"""
        return {
            "backend": PROVIDER_TYPE,
            "profile": profile,
            "mxc_backend": backend,
            "executor": self._executor,
            "executor_args": list(self._executor_args),
            "request_version": _REQUEST_VERSION,
            "state_dir": self._state_dir,
            "env_name": env_name,
            "workspace": workspace,
            "policy": policy,
            "network": self._network,
            "image": self._image if backend == "wslc" else None,
        }

    # ── 策略渲染 ────────────────────────────────────────────────

    def _normalize_network(self, raw: Any) -> dict[str, Any]:
        """network 段归一（MXC 1.0.0 schema：egress.allow = 规则对象数组）。

        配置里的裸 CIDR 字符串按「该目的地全端口 allow」展开为规则对象；
        已是规则对象的条目原样透传（契约见 docs/containment-policy/1.0.0）。
        """
        cfg = raw if isinstance(raw, dict) else {}
        egress: dict[str, Any] = {"default": str(cfg.get("default", "deny"))}
        allow: list[dict[str, Any]] = []
        for item in cfg.get("allow") or []:
            if isinstance(item, str) and item.strip():
                allow.append({"to": [{"cidr": item.strip()}]})
            elif isinstance(item, dict):
                allow.append(item)
        if allow:
            egress["allow"] = allow
        return {"egress": egress}

    def _normalize_ui(self, raw: Any) -> dict[str, Any]:
        """ProcessContainer ui 段（MXC 1.0.0 只认 disable/clipboard/injection）。

        缺省 ``{"disable": False}``：ProcessContainer 默认锁 Win32k，git/
        powershell 等控制台程序会 STATUS_DLL_INIT_FAILED（真机实证），
        而 native-win 档的定位就是跑 Windows 原生命令。剪贴板/合成输入
        仍保持 MXC 缺省（none/false）。wslc 不接受 ui 段，故只在
        processcontainer 请求里下发（见 _build_request）。
        """
        cfg = raw if isinstance(raw, dict) else {}
        ui: dict[str, Any] = {"disable": bool(cfg.get("disable", False))}
        if "clipboard" in cfg:
            ui["clipboard"] = str(cfg["clipboard"])
        if "injection" in cfg:
            ui["injection"] = bool(cfg["injection"])
        return ui

    def _render_policy(self, profile: str, workspace: str, task_id: str) -> dict[str, Any]:
        """渲染 MXC filesystem 策略（逐目录授权，可写卷根不递归信任）。

        缓存两层（设计 §7）：共享基线只读（存在才登记，避免引擎对不存在
        路径报错）、任务可写层按 task_id 隔离；workspace 恒读写。
        """
        writable = [str(workspace)]
        readonly: list[str] = []
        if self._cache_root:
            root = Path(self._cache_root)
            writable.append(str(root / "tasks" / str(task_id)))
            for name in ("registry", "pip", "cargo-home", "npm", "pnpm-store", "go"):
                candidate = root / name
                if candidate.exists():
                    readonly.append(str(candidate))
        return {
            "readwritePaths": writable,
            "readonlyPaths": readonly,
            # wslc 只挂显式授权路径，未授权路径容器内不存在 → deny 清单无增益，
            # 反而要求 MXC 解析宿主受保护目录（System32\config）并因权限拒绝
            # 整体拒跑（真机实证）。deny 只对 native-win（ACL 隐式授权）下发。
            **({"deniedPaths": list(self._denied_paths)} if profile == PROFILE_NATIVE_WIN else {}),
        }

    def _write_policy(self, name: str, policy: dict[str, Any]) -> Path | None:
        path = env_metadata.metadata_path(self._state_dir, f"{name}.policy")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
            return path
        except OSError as e:
            logger.error("[MxcNativeProvider] 策略档写入失败 | name=%s | error=%s", name, e)
            return None

    def _remove_policy(self, name: str) -> bool:
        path = env_metadata.metadata_path(self._state_dir, f"{name}.policy")
        if not path.exists():
            return True
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.error("[MxcNativeProvider] 策略档删除失败 | name=%s | error=%s", name, e)
            return False

    # ── 运行时检查 ──────────────────────────────────────────────

    def _resolve_executor(self) -> str | None:
        """解析执行器可执行路径；未配置/不可达返回 None。"""
        if not self._executor:
            return None
        if os.sep in self._executor or (os.altsep and os.altsep in self._executor):
            return self._executor if Path(self._executor).is_file() else None
        return shutil.which(self._executor)

    async def _check_runtime(self, profile: str) -> str | None:
        """已注册 backend 的就绪检查（fail-closed，错误码随消息返回）。"""
        if not self._resolve_executor():
            return f"{_ERROR_EXECUTOR_MISSING}: 执行器未配置或不可达: {self._executor or '(未配置)'}"
        if profile == PROFILE_LINUX_BASH and self._require_wsl:
            version = await self._probe_wsl_version()
            if version is None:
                return f"{_ERROR_BACKEND_UNAVAILABLE}: WSL 不可用（WSLC backend 依赖）"
            if version < _MIN_WSL_VERSION:
                return (
                    f"{_ERROR_WSLC_WSL_TOO_OLD}: WSL {'.'.join(map(str, version))} "
                    f"< {'.'.join(map(str, _MIN_WSL_VERSION))}"
                )
        return None

    async def _probe_wsl_version(self) -> tuple[int, int, int] | None:
        """``wsl --version`` 版本探测（缓存；不可用返回 None）。"""
        if self._wsl_version is not None:
            return self._parse_wsl_version(self._wsl_version) if self._wsl_version else None
        argv = ["wsl", "--version"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except (OSError, TimeoutError) as e:
            logger.warning("[MxcNativeProvider] WSL 版本探测失败 | error=%s", e)
            self._wsl_version = ""
            return None
        text = self._decode_wsl_output(out)
        version = self._parse_wsl_version(text)
        self._wsl_version = text if version is not None else ""
        return version

    @staticmethod
    def _decode_wsl_output(data: bytes | None) -> str:
        """wsl.exe 输出解码：自身输出 UTF-16-LE，其它 UTF-8（含 NUL 判据）。"""
        if not data:
            return ""
        raw = data
        return (
            raw.decode("utf-16-le", errors="replace")
            if b"\x00" in raw
            else raw.decode("utf-8", errors="replace")
        )

    @staticmethod
    def _parse_wsl_version(text: str) -> tuple[int, int, int] | None:
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
        if not m:
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    # ── 执行 ────────────────────────────────────────────────────

    async def _exec_command(
        self, env: IsolationEnvironment, operation: dict[str, Any]
    ) -> ExecutionResult:
        command = str(operation.get("command", ""))
        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")
        timeout = float(operation.get("timeout", 30))
        working_dir = self._map_working_dir(env, operation.get("working_dir"))
        request = self._build_request(env, command, working_dir, timeout)
        try:
            rc, stdout, stderr = await self._invoke_executor(request, timeout)
        except TimeoutError as e:
            return ExecutionResult(success=False, output=None, error=str(e))
        except Exception as e:
            return ExecutionResult(success=False, output=None, error=f"执行命令失败: {e}")

        stdout_text, stderr_text, exit_code = self._parse_executor_output(rc, stdout, stderr)
        success = rc == 0 and exit_code == 0
        return ExecutionResult(
            success=success,
            output={
                "stdout": stdout_text,
                "stderr": stderr_text,
                "return_code": exit_code,
                "command": command,
            },
            error=None if success else (stderr_text or stdout_text or f"exit code {exit_code}"),
        )

    def _build_request(
        self,
        env: IsolationEnvironment,
        command: str,
        working_dir: str,
        timeout: float,
    ) -> dict[str, Any]:
        """构造 MXC 1.0.0 stable schema 请求（设计 §2 的执行器消费形态）。

        ``process.cwd`` 按宿主路径解释（MXC 引擎负责 wslc 侧映射）——这是
        执行器契约未文档化处的假设，真机 smoke 时校准（设计 §13）。
        """
        info = env.provider_info
        backend = str(info.get("mxc_backend") or "")
        request: dict[str, Any] = {
            "version": _REQUEST_VERSION,
            "process": {
                "commandLine": command,
                "cwd": working_dir,
                "env": [],
                "inheritDefaultEnv": True,
                # MXC 1.0.0 schema 的 timeout 单位是毫秒（容器侧契约），
                # provider 接口的 timeout 是秒（0.2 既有语义）。
                "timeout": int(timeout * 1000),
            },
            "filesystem": info.get("policy") or {},
            "network": info.get("network") or {"egress": {"default": "deny"}},
            "containment": backend,
            "lifecycle": {"destroyOnExit": True},
        }
        if backend == "wslc":
            request["wslc"] = {"image": self._image}
        else:
            # ProcessContainer 需要 cmd 语义（&&/管道/内建命令）；MXC 对
            # commandLine 直接 CreateProcess，不套 shell。ui 见 _normalize_ui。
            request["process"]["commandLine"] = f'cmd.exe /d /s /c "{command}"'
            request["ui"] = dict(self._ui)
        return request

    async def _invoke_executor(
        self, request: dict[str, Any], timeout: float
    ) -> tuple[int, bytes, bytes]:
        """调用执行器：请求 JSON 落临时档，argv 由 executor_args 模板生成。

        执行器 stdout/stderr 归 workload（MXC 约定），因此结果解析必须容忍
        「纯文本输出 + rc」形态，不假设 JSON 结果信封（见 _parse_executor_output）。
        """
        executor = self._resolve_executor()
        if not executor:
            raise RuntimeError(f"{_ERROR_EXECUTOR_MISSING}: 执行器不可达")
        state_dir = Path(self._state_dir)
        await asyncio.to_thread(state_dir.mkdir, parents=True, exist_ok=True)
        digest = hashlib.md5(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        request_path = state_dir / f"request-{digest}.json"
        await asyncio.to_thread(
            request_path.write_text, json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        argv = [executor] + [arg.replace("{request}", str(request_path)) for arg in self._executor_args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(f"命令执行超时（{timeout}秒）") from None
            return proc.returncode or 0, out, err
        finally:
            try:
                await asyncio.to_thread(request_path.unlink)
            except OSError:
                pass

    @staticmethod
    def _parse_executor_output(
        rc: int, stdout: bytes, stderr: bytes
    ) -> tuple[str, str, int]:
        """执行器输出 → (stdout, stderr, exit_code)。

        容忍两种形态：①执行器自身返回 JSON 结果信封（字段名假设为
        exitCode/exit_code/return_code + stdout/stderr）；②stdout/stderr 直接
        就是 workload 输出（MXC 「流保留给 workload」约定）——此时 rc 即命令
        退出码。
        """
        out_text = _decode_process_output(stdout)
        err_text = _decode_process_output(stderr)
        stripped = out_text.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and "error" in data:
                # 执行器自身的错误信封（如 malformed_request）即使 rc=0 也
                # 不是 workload 成功：归一到非零退出 + stderr 承载原因。
                err = data.get("error")
                if isinstance(err, dict):
                    detail = f"{err.get('code', 'error')}: {err.get('message', '')}"
                else:
                    detail = str(err)
                return "", detail, 1
            if isinstance(data, dict) and any(
                key in data for key in ("exitCode", "exit_code", "return_code", "stdout", "stderr")
            ):
                exit_code = data.get("exitCode", data.get("exit_code", data.get("return_code", rc)))
                try:
                    exit_code_int = int(exit_code) if exit_code is not None else rc
                except (TypeError, ValueError):
                    exit_code_int = rc
                return (
                    str(data.get("stdout", out_text)),
                    str(data.get("stderr", err_text)),
                    exit_code_int,
                )
        return out_text, err_text, rc

    async def _file_op(
        self, env: IsolationEnvironment, operation: dict[str, Any]
    ) -> ExecutionResult:
        """文件操作：经 profile 对应 shell 走同一 MXC 沙箱（宿主不直读）。"""
        op = str(operation.get("operation") or "")
        raw_path = str(operation.get("path", ""))
        if not raw_path:
            return ExecutionResult(success=False, output=None, error="文件路径不能为空")
        path = self._map_workspace_path(env, raw_path)
        profile = str(
            env.provider_info.get("execution_profile")
            or env.provider_info.get("profile")
            or self._default_profile
        )
        if profile == PROFILE_LINUX_BASH:
            # wslc 只挂载宿主路径到 /mnt/<drive>/...；文件命令在容器内执行，
            # 必须用容器路径（宿主 E:\ws 在容器内不可见）。
            path = _to_wslc_path(path)
        if op == "read":
            command = self._read_command(profile, path)
            result = await self._exec_command(env, {"command": command, "timeout": 30})
            return ExecutionResult(
                success=result.success,
                output=(result.output or {}).get("stdout", "") if result.success else None,
                error=result.error,
            )
        if op == "write":
            content = str(operation.get("content", ""))
            command = self._write_command(profile, path, content)
            result = await self._exec_command(env, {"command": command, "timeout": 30})
            return ExecutionResult(success=result.success, output=None, error=result.error)
        if op == "exists":
            command = self._exists_command(profile, path)
            result = await self._exec_command(env, {"command": command, "timeout": 30})
            stdout = (result.output or {}).get("stdout", "") if result.success else ""
            return ExecutionResult(success=True, output={"exists": "yes" in stdout})
        return ExecutionResult(success=False, output=None, error=f"不支持的文件操作: {op}")

    @staticmethod
    def _map_workspace_path(env: IsolationEnvironment, path: str) -> str:
        """``/workspace`` 约定路径 → 环境 workspace 真实宿主路径。"""
        workspace = str(env.context.workspace or "")
        normalized = path.replace("\\", "/")
        if normalized == "/workspace":
            return workspace or path
        if normalized.startswith("/workspace/"):
            return (workspace or "/workspace") + normalized[len("/workspace") :]
        return path

    def _map_working_dir(self, env: IsolationEnvironment, working_dir: str | None) -> str:
        """/workspace 约定/相对 working_dir → 宿主 workspace 路径（与 process_runtime
        ``_map_mxc_working_dir`` 同语义）。空落 workspace；其它 POSIX/Windows
        绝对路径原样。"""
        workspace = str(env.context.workspace or "")
        if not working_dir:
            return workspace
        normalized = working_dir.replace("\\", "/")
        if normalized == "/workspace":
            return workspace or working_dir
        if normalized.startswith("/workspace/"):
            return (workspace or "/workspace") + normalized[len("/workspace") :]
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            return working_dir
        base = workspace.rstrip("/\\")
        return f"{base}/{normalized}" if base else working_dir

    @staticmethod
    def _read_command(profile: str, path: str) -> str:
        if profile == PROFILE_NATIVE_WIN:
            return f'powershell -NoProfile -Command "[IO.File]::ReadAllText({_ps_quote(path)})"'
        return f"cat -- {shlex.quote(path)}"

    @staticmethod
    def _write_command(profile: str, path: str, content: str) -> str:
        import base64

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        if profile == PROFILE_NATIVE_WIN:
            parent = str(Path(path).parent)
            return (
                f'powershell -NoProfile -Command "'
                f"New-Item -ItemType Directory -Force -Path {_ps_quote(parent)} | Out-Null; "
                f"$b=[Convert]::FromBase64String('{encoded}'); "
                f'[IO.File]::WriteAllBytes({_ps_quote(path)},$b)"'
            )
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        return (
            f"mkdir -p {shlex.quote(parent)} && "
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        )

    @staticmethod
    def _exists_command(profile: str, path: str) -> str:
        if profile == PROFILE_NATIVE_WIN:
            return (
                f'powershell -NoProfile -Command "'
                f"if (Test-Path -LiteralPath {_ps_quote(path)}) {{ 'yes' }} else {{ 'no' }}\""
            )
        return f"test -e {shlex.quote(path)} && echo yes || echo no"

    # ── 环境构造 ────────────────────────────────────────────────

    def _write_metadata(
        self,
        name: str,
        workspace: str,
        profile: str,
        backend: str,
        policy: dict[str, Any],
        policy_path: Path,
        now: datetime,
    ) -> bool:
        return env_metadata.write_metadata(
            self._state_dir,
            name,
            {
                "name": name,
                "workspace": workspace,
                "execution_profile": profile,
                "backend": backend,
                "policy_path": str(policy_path),
                "policy": policy,
                "created_at": now.isoformat(),
            },
            logger=logger,
        )

    def _build_env(
        self,
        name: str,
        context: IsolationContext,
        now: datetime,
        profile: str,
        backend: str,
        policy: dict[str, Any],
    ) -> IsolationEnvironment:
        env = IsolationEnvironment(
            env_id=name,
            level=IsolationLevel.CONTAINER,
            provider_type=PROVIDER_TYPE,
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "container_name": name,
                "workspace": context.workspace,
                "execution_profile": profile,
                "mxc_backend": backend,
                "policy": policy,
                "exec_backend": self.exec_backend_info(
                    env_name=name,
                    profile=profile,
                    backend=backend,
                    workspace=str(context.workspace or ""),
                    policy=policy,
                ),
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[name] = env
        return env

    def _make_error_environment(
        self,
        context: IsolationContext,
        now: datetime,
        error_msg: str,
    ) -> IsolationEnvironment:
        env_id = f"mxc-{context.task_id}"
        env = IsolationEnvironment(
            env_id=env_id,
            level=IsolationLevel.CONTAINER,
            provider_type=PROVIDER_TYPE,
            status=EnvironmentStatus.ERROR.value,
            context=context,
            provider_info={"error": error_msg},
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[env_id] = env
        return env


def _decode_process_output(data: bytes | None) -> str:
    """子进程输出解码：UTF-8 优先，回退系统首选编码，坏字节替换。

    MXC 自身错误信封是 UTF-8；cmd/powershell 在中文 Windows 上输出
    GBK/CP936，直接按 UTF-8 replace 会得到替换符（真机实证）。
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(locale.getpreferredencoding(False), errors="replace")
    except (LookupError, TypeError, ValueError):
        return data.decode("utf-8", errors="replace")


def _to_wslc_path(path: str) -> str:
    """宿主 Windows 路径 → wslc 容器内挂载路径（``E:\\ws`` → ``/mnt/e/ws``）。"""
    normalized = path.replace("\\", "/")
    match = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if match:
        return f"/mnt/{match.group(1).lower()}/{match.group(2)}"
    return normalized


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字面量（内部单引号双写）。"""
    return "'" + value.replace("'", "''") + "'"


def _install_shared_root() -> None:
    """共享根入 sys.path（与 wsl_native 同约定；providers 包经 server 注入）。"""
    shared_root = str(Path(__file__).resolve().parents[2])
    if shared_root not in sys.path:
        sys.path.insert(0, shared_root)


_install_shared_root()
