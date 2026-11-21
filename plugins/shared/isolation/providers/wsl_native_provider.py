"""WSL 原生隔离提供者（无 Docker 层）。

环境 = 「workspace 目录 + 一次性 wsl.exe 进程」：无常驻容器、无守护进程
依赖（不需要 docker daemon），环境随进程消亡、结构性无泄漏。隔离边界 =
WSL 虚拟机 + 专用 Linux 用户 + 可选 sandbox_cmd 前缀（如 bwrap/landlock
包装器，配置即启用，缺失则 is_available False fail-closed）。

与 DockerProvider 的契约对齐（可互换性）：
- env_id = container_name（workspace 确定性派生，同 workspace 同环境）；
- ExecutionResult.output 形状一致（stdout/stderr/return_code/command）；
- error env + query_unavailable 标记语义一致（D6② 存在性未知不推进）；
- 销毁按名兜底、失败如实返回 False（D6③ 不谎报）。

与 docker 的机制差异（有意为之）：
- 「容器记录」= state_dir 下的 JSON metadata 档（workspace 路径 + 创建时间），
  是收养/验活/按名销毁的底层真相；档不存在即环境不存在（wsl_native 无
  常驻进程可删，按名销毁删档即真删，幂等成功不是谎报）。
- 遗留环境的活性判定 = metadata 有效 + workspace 目录仍在；workspace 被外力
  删除对应 docker 的「坏容器探针失败」，清档返回 None 让上层重建。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
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

# 共享根（proc_tree 等共享裸模块所在）显式入 sys.path：本插件不经
# bootstrap_plugin 引导（server.py 只注入插件目录），实现模块自持解析。
_SHARED_ROOT = str(Path(__file__).resolve().parents[2])
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from _root_api import kill_process_tree  # noqa: E402

logger = logging.getLogger(__name__)

# `<X>:/mnt/<d>/Y`（污染变体）或 `/mnt/<d>/Y`（反斜杠归一后形态）。
_WS_MOUNT_RE = re.compile(r"^(?:[A-Za-z]:)?/mnt/([A-Za-z])/(.*)$", re.DOTALL)

# 盘符绝对形态判据（`D:/x`、`D:x` 归一后前缀；UNC 归一后以 `/` 开头）。
_WIN_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _is_relative_working_dir(normalized: str) -> bool:
    """归一后工作目录是否相对形态（既非 POSIX 绝对也非盘符/UNC 绝对）。"""
    return not normalized.startswith("/") and not _WIN_DRIVE_PREFIX_RE.match(normalized)


# metadata 档 IO 单源在 providers.env_metadata（D6 档真相，与 linux_native 共用）；
# 档文件名后缀随单源。

# find_environment_by_name 合成 context 的任务类型（与 docker 直查通路一致：
# 环境名反推，无真实任务归属）
_META_TASK_TYPE = TaskType.ATOMIC

# ── 工具链持久层（ADR 2026-11-07 决策 5）─────────────────────────

# cargo registry 源单源的仓根相对路径（与 docker_assets/agentos/Dockerfile COPY 的
# 是同一份文件；任何源变更只改该文件单点生效）。

# BASH_ENV 引导文件内容（静态）。$HOME 在发行版 bash 内展开——wsl --exec 下
# HOME 由 WSL init 按执行用户设置（2026-11-07 实测在案），无需宿主侧探测路径。
# 文件落 workspace 的 .task_runtime/（随任务空间管理），create 时幂等生成；
# 文件缺失（遗留环境/被清理）时非交互 bash 静默忽略 BASH_ENV（实测 stderr
# 干净、rc=0），不产生执行噪音——工具链 env 退化为仅有直接 env 对（如已注入）。
# PATH 追加只能在此承载：`env K=V` 前缀是整体替换，做不了追加。
_TOOLCHAIN_BOOTSTRAP_SH = (
    "# AgentOS wsl_native 工具链环境引导（ADR 2026-11-07 决策 5 工具链持久层）。\n"
    "# 由 WslNativeProvider 环境创建时生成；工具链持久层 = 发行版 home（VHDX\n"
    "# ext4 持久），本文件按 $HOME 展开指向。cargo 源配置以注入单源为准\n"
    "# （与容器镜像同语义；未注入则跳过 cargo 配置播种，官方源为默认）。\n"
    'export RUSTUP_HOME="$HOME/.rustup"\n'
    'export CARGO_HOME="$HOME/.cargo"\n'
    'export PATH="$CARGO_HOME/bin:$PATH"\n'
)

# 种子写入脚本（发行版侧 python3 执行；argv: [rt_dir, bootstrap, cargo_cfg]，
# 内容经 JSON 传递——与 _file_op write 同手法，杜绝注入）。cargo 配置取
# first-write-wins：$HOME 可能是用户领地，覆盖用户自有 cargo 配置比漏种
# 一次更伤（工具链 env 有直接 env 对 + BASH_ENV 引导两层兜底）。
_SEED_PY_SCRIPT = (
    "import json, os, sys\n"
    "rt_dir = json.loads(sys.argv[1])\n"
    "bootstrap = json.loads(sys.argv[2])\n"
    "cargo_cfg = json.loads(sys.argv[3])\n"
    "os.makedirs(rt_dir, exist_ok=True)\n"
    "with open(os.path.join(rt_dir, 'toolchain_env.sh'), 'w', encoding='utf-8') as f:\n"
    "    f.write(bootstrap)\n"
    "if cargo_cfg:\n"
    "    cfg = os.path.join(os.path.expanduser('~'), '.cargo')\n"
    "    dest = os.path.join(cfg, 'config.toml')\n"
    "    if not os.path.exists(dest):\n"
    "        os.makedirs(cfg, exist_ok=True)\n"
    "        with open(dest, 'w', encoding='utf-8') as f:\n"
    "            f.write(cargo_cfg)\n"
)

# 环境层零工具链管理（ADR 2026-11-07 决策 5 · 2026-11-10 修订二，CC/Codex
# 同构）：无探测、无预装、无版本钉子消费——agent 运行中撞到依赖缺失自己装
# （rustup 首次调用按工作区 rust-toolchain.toml 钉子自动装），持久层保证装
# 一次永久。环境层只做被动接线：BASH_ENV 引导 + RUSTUP_HOME/CARGO_HOME env
# 对钉发行版持久 home（见 _runtime_env_pairs 与 _TOOLCHAIN_BOOTSTRAP_SH）。


class WslNativeProvider(IsolationProvider):
    """WSL 原生隔离提供者。

    config 键：
    - distro: WSL 发行版名（默认 Ubuntu）
    - user: 执行用的 Linux 用户（默认 None = 发行版默认用户；建议配置
      非特权专用用户，与宿主默认用户隔离）
    - sandbox_cmd: 环境内命令前缀（如 ["bwrap", "--ro-bind", "/", "/", ...]），
      配置后 is_available 会校验首二进制在发行版内存在（缺失 fail-closed）
    - state_dir: metadata 档目录（默认 <仓库根>/.ai_workspaces/wsl_native_envs）
    - wsl_exe: wsl 可执行文件（默认 "wsl"；测试可注入替身）
    - bridge_url: AGENTOS_BRIDGE_URL 注入值（缺省读环境变量，与 docker 同源）
    - distro_home: 发行版持久 home 显式指定（缺省首次执行时经 wsl 探测并缓存；
      工具链直接 env 对的锚点）
    - cargo_source_config: cargo 源单源文件路径（注入交付；未注入=跳过播种，
      单源内容与 Dockerfile COPY 的那份一致）
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._distro = str(self._config.get("distro", "Ubuntu"))
        user = self._config.get("user")
        self._user = str(user) if user else None
        self._sandbox_cmd: list[str] = [str(x) for x in self._config.get("sandbox_cmd", [])]
        self._wsl_exe = str(self._config.get("wsl_exe", "wsl"))
        # 仓库根 = providers/ 上溯 4 级（providers→isolation→shared→plugins→根）；
        # 相对形态注入路径（钉子单源等）按此解析，绝对路径原样（dockerfile_path
        # 同一约定）
        self._repo_root = Path(__file__).resolve().parents[4]
        if self._config.get("state_dir"):
            self._state_dir = str(self._config["state_dir"])
        else:
            self._state_dir = str(self._repo_root / ".ai_workspaces" / "wsl_native_envs")
        self._bridge_url = str(self._config.get("bridge_url", "") or "")
        self._environments: dict[str, IsolationEnvironment] = {}
        # 当前环境 workspace 的 WSL 侧路径（create 时刷新；argv 构建用）
        self._workspace_wsl = str(self._config.get("workspace_wsl", ""))
        # 工具链持久层（ADR 2026-11-07 决策 5）：发行版持久 home 探测缓存
        # （None=未探测，""=探测失败/未知）。测试/部署可经 distro_home 显式指定。
        distro_home = self._config.get("distro_home")
        self._distro_home = str(distro_home) if distro_home else None
        # cargo 源单源文件路径：仅经注入（config 覆盖 cargo_source_config）交付；
        # 未注入 = None = 跳过 distro 侧播种（官方源即 cargo 默认，既有降级语义）。
        # 不回落仓根路径——插件禁止直读配置目录（ADR 2026-09-14 §2.6，直读守卫）。
        cargo_source = self._config.get("cargo_source_config")
        self._cargo_source_path: Path | None
        if cargo_source:
            _cs = Path(str(cargo_source))
            self._cargo_source_path = _cs if _cs.is_absolute() else self._repo_root / _cs
        else:
            self._cargo_source_path = None

    # ── 后端元数据（guard 注入 / bash 工具侧 argv 构建共用）──────

    def exec_backend_info(self, workspace_wsl: str) -> dict[str, Any]:
        """后端调度信息：随 env.provider_info 下发，bash 工具侧凭此构建 argv。"""
        return {
            "backend": "wsl_native",
            "wsl_exe": self._wsl_exe,
            "distro": self._distro,
            "user": self._user,
            "sandbox_cmd": list(self._sandbox_cmd),
            "state_dir": self._state_dir,
            "workspace_wsl": workspace_wsl,
        }

    @classmethod
    def from_backend_info(cls, info: dict[str, Any]) -> WslNativeProvider:
        """从 exec_backend_info 重建轻量实例（bash 工具侧只做 argv 构建）。"""
        if info.get("backend") != "wsl_native":
            raise ValueError(f"exec_backend 非 wsl_native: {info.get('backend')}")
        return cls(
            {
                "wsl_exe": info.get("wsl_exe", "wsl"),
                "distro": info.get("distro", "Ubuntu"),
                "user": info.get("user"),
                "sandbox_cmd": info.get("sandbox_cmd", []),
                "state_dir": info.get("state_dir", ""),
                "workspace_wsl": info.get("workspace_wsl", ""),
            }
        )

    # ── IsolationProvider 接口 ──────────────────────────────────

    def get_level(self) -> IsolationLevel:
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 WSL 后端可用性：wsl.exe → 发行版 → 用户 → sandbox 二进制。

        任一环失败即 (False, 原因)，不降级（decider 据此 fail-closed）。
        """
        if not shutil.which(self._wsl_exe):
            return False, f"WSL (wsl.exe) 未安装: {self._wsl_exe}"
        try:
            rc, out, err = await self._popen_run([self._wsl_exe, "-l", "-q"], timeout=15)
            if rc != 0:
                return False, f"WSL 发行版列表查询失败: {self._decode(out or err)}"
            distros = self._decode_wsl_list(out)
            if self._distro not in distros:
                return False, f"WSL 发行版不存在: {self._distro}（现有: {distros}）"
            if self._user:
                rc, _, err = await self._popen_run(self._wsl_argv(["--exec", "true"]), timeout=15)
                if rc != 0:
                    return False, f"WSL 用户不可用: {self._user}: {self._decode(err)}"
            # bash 必须在位：执行壳用 bash（dash 对 set -o pipefail 致命退出，
            # 2026-09-14 实测；容器镜像内 dash 容忍——发行版语义不齐，统一 bash）
            rc_bash, _, _ = await self._popen_run(self._wsl_argv(["--exec", "sh", "-c", "command -v bash"]), timeout=15)
            if rc_bash != 0:
                return False, "发行版缺少 bash（执行壳依赖）"
            if self._sandbox_cmd:
                bin_name = self._sandbox_cmd[0]
                rc, _, _ = await self._popen_run(
                    self._wsl_argv(["--exec", "sh", "-c", f"command -v {bin_name}"]),
                    timeout=15,
                )
                if rc != 0:
                    return False, f"沙箱命令在发行版中不可用: {bin_name}"
            return True, None
        except Exception as e:
            return False, f"WSL 探测失败: {e}"

    async def create_environment(
        self,
        context: IsolationContext,
        container_name: str | None = None,
    ) -> IsolationEnvironment:
        """创建（或按名收养）wsl_native 隔离环境。"""
        now = datetime.now(UTC)
        # manager 对 CONTAINER 恒注入 workspace 派生名；缺名兜底仅为类型完备
        name = container_name if container_name else f"cua-{context.task_id}"

        # 工作空间校验（镜像 DockerProvider：拒绝无挂载/不存在的 workspace，
        # 避免命令落到空目录却以 exit 0 静默通过）
        if not context.workspace:
            logger.error(
                "[WslNativeProvider] 拒绝创建环境：工作空间为空 | task=%s",
                context.task_id,
            )
            return self._make_error_environment(context, now, "工作空间为空，无法挂载到容器")
        if not Path(context.workspace).exists():
            logger.error(
                "[WslNativeProvider] 拒绝创建环境：工作空间路径不存在 | task=%s | path=%s",
                context.task_id,
                context.workspace,
            )
            return self._make_error_environment(context, now, f"工作空间路径不存在: {context.workspace}")

        # 环境层零工具链管理（ADR 2026-11-07 决策 5 · 2026-11-10 修订二）：
        # 工作区校验后直通收养/创建，不 gate 工具链——任务类型/工作区文件内容
        # 不参与环境选择（嗅探判据退役保持），agent 运行中撞到依赖缺失自己装，
        # 持久层保证装一次永久。

        workspace_wsl = self._to_wsl_path(context.workspace)

        # D6① 幂等收养：遗留 metadata 且 workspace 一致 → 直接收养复用，
        # 不再触发任何 wsl 调用（对应 docker start 按名收养）。
        meta = self._read_metadata(name)
        if meta is not None:
            if meta.get("workspace") == context.workspace:
                logger.info("[WslNativeProvider] 收养已有同名环境 | name=%s", name)
                return self._build_env(name, context, now, workspace_wsl)
            logger.warning(
                "[WslNativeProvider] 陈旧 metadata（workspace 漂移），清档重建 | name=%s",
                name,
            )
            self._remove_metadata(name)

        # mkdir -p：wsl 调用异常 = 存在性未知（D6②：不落 READY、不写档、
        # 带 query_unavailable 标记由上层软失败）；rc!=0 = 确定性失败。
        try:
            rc, _, err = await self._popen_run(self._wsl_argv(["--exec", "mkdir", "-p", workspace_wsl]), timeout=30)
        except Exception as exc:
            logger.error(
                "[WslNativeProvider] 工作空间探测不可用（防撞名，跳过创建） | name=%s | error=%s",
                name,
                exc,
            )
            return self._make_error_environment(
                context,
                now,
                f"隔离查询暂不可用，未创建环境（防撞名）: {exc}",
                query_unavailable=True,
            )
        if rc != 0 and not await self._ensure_workspace_dir(workspace_wsl, context.workspace):
            return self._make_error_environment(context, now, f"工作空间目录创建失败: {self._decode(err)}")
        if rc != 0:
            # mkdir 假失败容错（2026-11-13 装机实测形态）：rc!=0 但复核目录
            # 存在（_ensure_workspace_dir 已含 Windows 侧补建 + test -d 复核）
            # → 放行并留痕，不落 error environment。
            logger.warning(
                "[WslNativeProvider] mkdir rc=%s（stderr=%r）为假失败，目录复核存在，放行 | name=%s",
                rc,
                self._decode(err),
                name,
            )

        # 工具链持久层接线（ADR 2026-11-07 决策 5）：workspace 侧 BASH_ENV 引导
        # 文件 + distro $HOME/.cargo/config.toml（源配置单源注入时）。被动文件
        # 播种，无探测无安装；失败不阻断环境创建（工具链 env 有直接 env 对
        # 兜底），error 留痕不做静默降级。
        await self._seed_toolchain_env(workspace_wsl)

        if not self._write_metadata(name, context.workspace, workspace_wsl, now):
            return self._make_error_environment(context, now, "metadata 档写入失败")

        self._workspace_wsl = workspace_wsl
        env = self._build_env(name, context, now, workspace_wsl)
        logger.info("[WslNativeProvider] 环境已创建 | name=%s | workspace=%s", name, context.workspace)
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁环境：删 metadata 档即真删（无常驻进程/容器可清理）。

        D6③：无内存登记≠环境不存在——按名删档兜底；档不存在 = 底层真无
        资源，幂等成功（与 docker 的 rm -f 不同：wsl_native 的「底层存在」
        就是一份档文件，档没了就没了，不存在谎报空间）；删档失败（IO 错误）
        如实返回 False，保留登记供重试。
        """
        removed = self._remove_metadata(env_id)
        if not removed:
            logger.warning(
                "[WslNativeProvider] 销毁环境失败（metadata 删除异常，保留登记） | id=%s",
                env_id,
            )
            return False
        self._environments.pop(env_id, None)
        return True

    async def find_environment_by_name(self, name: str) -> IsolationEnvironment | None:
        """按名收养：metadata 有效且 workspace 仍在 → READY 环境。

        对应 manager._find_existing_container 的 docker 直查通路（服务重启后
        内存登记为空的收养场景）；workspace 消失/档损坏 = 坏环境，清档返回
        None 让上层走重建（对应 docker 坏容器探针删除重建）。
        """
        meta = self._read_metadata(name)
        if meta is None:
            return None
        workspace = meta.get("workspace")
        if not workspace or not Path(str(workspace)).exists():
            logger.warning(
                "[WslNativeProvider] 环境失效（workspace 消失），清档待重建 | name=%s",
                name,
            )
            self._remove_metadata(name)
            return None
        workspace_wsl = str(meta.get("workspace_wsl") or self._to_wsl_path(str(workspace)))
        now = datetime.now(UTC)
        context = IsolationContext(
            task_id=name,
            task_type=_META_TASK_TYPE,
            is_root_task=True,
            isolation_level=IsolationLevel.CONTAINER,
        )
        context.workspace = str(workspace)
        return self._build_env(name, context, now, workspace_wsl)

    async def execute_in_environment(
        self,
        env_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在环境中执行操作（一次性 wsl.exe 进程，无常驻环境可寻址）。"""
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
            return await self._exec(operation)
        if op_type == "file_operation":
            return await self._file_op(operation)
        return ExecutionResult(success=False, output=None, error=f"不支持的操作类型: {op_type}")

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """环境状态：wsl_native 环境无运行态（随用随起），登记即 READY。"""
        if env_id in self._environments:
            return EnvironmentStatus.READY
        return EnvironmentStatus.STOPPED

    # ── 执行内部 ────────────────────────────────────────────────

    async def _exec(self, operation: dict[str, Any]) -> ExecutionResult:
        """执行命令：argv 与超时纪律与 DockerProvider._exec_in_container 对齐。"""
        command = str(operation.get("command", ""))
        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")
        timeout = float(operation.get("timeout", 30))
        # 工具链直接 env 对依赖发行版 home 探测（异步、缓存；探测失败仅退化
        # 为无直接对，BASH_ENV 引导文件仍按 $HOME 展开，效果等价）
        await self._ensure_distro_home()
        args = self._build_exec_argv(
            working_dir=operation.get("working_dir"),
            command=command,
            bridge_env=self._bridge_env(),
            timeout=timeout,
        )
        try:
            rc, stdout, stderr = await self._popen_run(args, timeout=timeout)
        except TimeoutError as e:
            return ExecutionResult(success=False, output=None, error=str(e))
        except Exception as e:
            return ExecutionResult(success=False, output=None, error=f"执行命令失败: {e}")

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        success = rc == 0
        return ExecutionResult(
            success=success,
            output={
                "stdout": stdout_text,
                "stderr": stderr_text,
                "return_code": rc,
                "command": command,
            },
            error=None if success else (stderr_text or stdout_text or f"exit code {rc}"),
        )

    async def _file_op(self, operation: dict[str, Any]) -> ExecutionResult:
        """文件操作：read/write/exists 语义与 DockerProvider._file_op_in_container 对齐。"""
        op = operation.get("operation")
        # /workspace 约定路径映射到环境 workspace（与命令 working_dir 同规则）
        path = self._map_working_dir(str(operation.get("path", "")))
        try:
            if op == "read":
                rc, stdout, stderr = await self._popen_run(self._wsl_argv(["--exec", "cat", path]), timeout=10)
                if rc != 0:
                    return ExecutionResult(success=False, output=None, error=f"读取失败: {self._decode(stderr)}")
                return ExecutionResult(success=True, output=self._decode(stdout))

            if op == "write":
                content = str(operation.get("content", ""))
                dir_path = path.rsplit("/", 1)[0] if "/" in path else "."
                encoded = json.dumps(content)
                rc, _, stderr = await self._popen_run(
                    self._wsl_argv(
                        [
                            "--exec",
                            "sh",
                            "-c",
                            'mkdir -p "$1" && python3 -c '
                            "'import json,sys; open(sys.argv[1],\"w\").write(json.loads(sys.argv[2]))' "
                            '"$2" "$3"',
                            "--",
                            dir_path,
                            path,
                            encoded,
                        ]
                    ),
                    timeout=10,
                )
                if rc != 0:
                    return ExecutionResult(success=False, output=None, error=f"写入失败: {self._decode(stderr)}")
                return ExecutionResult(success=True, output=None)

            if op == "exists":
                result = await self._exec({"command": f"test -e {path} && echo yes || echo no"})
                stdout = (result.output or {}).get("stdout", "")
                return ExecutionResult(success=True, output={"exists": "yes" in stdout})

            return ExecutionResult(success=False, output=None, error=f"不支持的文件操作: {op}")
        except Exception as e:
            return ExecutionResult(success=False, output=None, error=f"文件操作失败: {e}")

    # ── argv 构建 ───────────────────────────────────────────────

    async def _ensure_workspace_dir(self, workspace_wsl: str, workspace_win: str) -> bool:
        """工作空间目录存在性复核 + 补建（mkdir 假失败容错）。

        ①wsl 侧 test -d 复核（存在即 True——mkdir rc!=0 但目录实际已建的
        假失败形态直接放行）；②不存在时 Windows 侧 Path.mkdir 补建（/mnt
        映射下与 wsl 视图同目录）；③再 test -d 复核。任一步 wsl 调用异常 =
        存在性未知 → False（调用方落 error environment，与 mkdir 异常同型的
        软失败面）。
        """
        try:
            if await self._wsl_test_dir(workspace_wsl):
                return True
            Path(workspace_win).mkdir(parents=True, exist_ok=True)
            return await self._wsl_test_dir(workspace_wsl)
        except Exception as exc:
            logger.warning(
                "[WslNativeProvider] 工作空间目录复核异常（按不存在处理） | dir=%s | error=%s",
                workspace_wsl,
                exc,
            )
            return False

    async def _wsl_test_dir(self, workspace_wsl: str) -> bool:
        """wsl 侧目录存在性单问（test -d，rc 即答案）。"""
        rc, _, _ = await self._popen_run(
            self._wsl_argv(["--exec", "test", "-d", workspace_wsl]),
            timeout=15,
        )
        return rc == 0

    def _wsl_argv(self, extra: list[str]) -> list[str]:
        """传输前缀：[wsl_exe, -d distro] + 可选 [-u user] + extra。

        extra 内命令一律走 `--exec`（argv 逐字传递）。`--` 形式会把参数拼接
        后经登录 shell 重解析——$!/引号/管道符会被外层改写（2026-09-14 实测
        $! 展开为空、stdin read 恒空），bash 后台进程协议（echo $$ 上报、
        exec 包装）全依赖逐字传递，禁用 `--`。
        """
        args = [self._wsl_exe, "-d", self._distro]
        if self._user:
            args.extend(["-u", self._user])
        args.extend(extra)
        return args

    def _build_exec_argv(
        self,
        working_dir: str | None,
        command: str,
        bridge_env: dict[str, str],
        timeout: float,
    ) -> list[str]:
        """执行 argv：前缀 + --cd(映射后) + -- + [env k=v] + [sandbox_cmd] + sh -c。

        working_dir 在此处统一做 /workspace → workspace_wsl 映射（bash 工具侧
        复用本 builder 时无需重复实现约定）。timeout 是命令语义的一部分
        （调用方 _popen_run 强制），不进入 argv。
        """
        args = self._wsl_argv(["--cd", self._map_working_dir(working_dir), "--exec"])
        bridge_pairs = [f"{k}={v}" for k, v in bridge_env.items()]
        runtime_pairs = self._runtime_env_pairs()
        if bridge_pairs or runtime_pairs:
            args.append("env")
            args.extend(bridge_pairs)
            args.extend(runtime_pairs)
        args.extend(self._sandbox_cmd)
        # 执行壳 bash：dash 对 set -o pipefail 致命退出（2026-09-14 实测），
        # bash 语义与 docker 路径的复合命令预期一致
        args.extend(["bash", "-c", command])
        return args

    def _runtime_env_pairs(self) -> list[str]:
        """任务运行时 env：副作用约束进 workspace（发行版 home 治本）。

        发行版 home 是持久面（VHDX），pip --user / XDG 缓存 / npm 缓存写进去
        无清理机制且 Windows 侧不可见。注入后主流工具的副作用落到 workspace
        的 .task_runtime/（NTFS，随任务工作空间管理）：
        - XDG_CACHE_HOME：pip/huggingface/torch/matplotlib 等缓存（自动建目录）
        - npm_config_cache：npm 缓存（npm 自建，不认 XDG）
        - PYTHONUSERBASE：pip install --user 落点（pip/python site 自建自识别）
        工具链持久层（ADR 2026-11-07 决策 5）：
        - BASH_ENV：非交互 bash 启动时 source 的引导文件（create 时生成于
          .task_runtime/toolchain_env.sh），承载 PATH 追加 ~/.cargo/bin 等
          env 前缀做不了的语义；文件缺失时 bash 静默忽略（实测在案）。
        - RUSTUP_HOME/CARGO_HOME：直接 env 对把工具链标准路径钉到发行版持久
          home（VHDX ext4 持久，跨任务可见）；home 未知（未探测/探测失败）
          不注入——引导文件仍按 $HOME 展开等价兜底。与 docker 后端的
          _TOOLCHAIN_ENV 标准路径同源（容器 = /usr/local/*，wsl = $HOME/*）。
        workspace_wsl 未知（异常形态）不注入，防 env 值指向文件系统根。
        TMPDIR/HOME/git/ssh 不动：/tmp 随 WSL 重启清空；HOME 是工具链持久层
        锚点本身（有意持久），git 身份与凭据是有意共享面，重定向得不偿失。
        """
        base = self._workspace_wsl
        if not base:
            return []
        rt = f"{base.rstrip('/')}/.task_runtime"
        pairs = [
            f"XDG_CACHE_HOME={rt}/cache",
            f"npm_config_cache={rt}/npm-cache",
            f"PYTHONUSERBASE={rt}/pyuser",
            f"BASH_ENV={rt}/toolchain_env.sh",
        ]
        home = self._distro_home
        if home:
            pairs.append(f"RUSTUP_HOME={home}/.rustup")
            pairs.append(f"CARGO_HOME={home}/.cargo")
        return pairs

    async def _ensure_distro_home(self) -> str:
        """探测发行版持久 home（工具链直接 env 对的锚点），结果缓存。

        wsl --exec 下 HOME 由 WSL init 按执行用户设置（2026-11-07 实测在案），
        `sh -c printf` 读取即可。失败/非绝对路径按 "" 缓存（不反复重试）：
        直接 env 对跳过注入，BASH_ENV 引导文件仍按 $HOME 展开等价兜底，debug
        留痕。超时 30s：wsl VM 冷启动实测可达 9~15s（_wsl_dir_exists 同教训）。
        """
        if self._distro_home is not None:
            return self._distro_home
        try:
            rc, out, _ = await self._popen_run(self._wsl_argv(["--exec", "sh", "-c", 'printf %s "$HOME"']), timeout=30)
            home = self._decode(out) if rc == 0 else ""
        except Exception as exc:
            logger.debug(
                "[WslNativeProvider] distro home 探测失败（工具链直接 env 对跳过） | error=%s",
                exc,
            )
            home = ""
        if not home.startswith("/"):
            home = ""
        self._distro_home = home
        return home

    async def _seed_toolchain_env(self, workspace_wsl: str) -> None:
        """workspace 侧工具链引导种子（create 时一次，被动文件播种）。

        写两个文件，一次 wsl 调用（发行版侧 python3 执行，内容 JSON 传递）：
        - <workspace>/.task_runtime/toolchain_env.sh：BASH_ENV 引导（PATH 追加等）
        - $HOME/.cargo/config.toml：cargo 源单源（与镜像 COPY 同一份文件内容；
          first-write-wins，不覆盖用户自有配置）——agent 后装 rustup 时源配置
          已在位
        失败不抛（环境创建继续，工具链 env 退化为直接 env 对），error 留痕。
        环境层零工具链管理（ADR 2026-11-10 修订二）：本步只做被动文件播种，
        无探测、无安装，不影响环境可用性。
        """
        rt_dir = f"{workspace_wsl.rstrip('/')}/.task_runtime"
        cargo_cfg = self._load_cargo_source_config()
        args = self._wsl_argv(
            [
                "--exec",
                "python3",
                "-c",
                _SEED_PY_SCRIPT,
                json.dumps(rt_dir),
                json.dumps(_TOOLCHAIN_BOOTSTRAP_SH),
                json.dumps(cargo_cfg),
            ]
        )
        try:
            rc, _, err = await self._popen_run(args, timeout=30)
        except Exception as exc:
            logger.error(
                "[WslNativeProvider] 工具链种子写入异常（环境继续创建，工具链 env 退化） | error=%s",
                exc,
            )
            return
        if rc != 0:
            logger.error(
                "[WslNativeProvider] 工具链种子写入失败（环境继续创建，工具链 env 退化） | rc=%s | err=%s",
                rc,
                self._decode(err),
            )

    def _load_cargo_source_config(self) -> str:
        """读 cargo 源单源文件（与 Dockerfile COPY 同一份）；不可达返回空串。

        空串时 distro 侧跳过 cargo 配置播种：单源钉的是官方 crates.io sparse
        （cargo ≥1.70 默认行为），缺文件语义不变，warning 留痕。
        """
        if not self._cargo_source_path:
            logger.warning("[WslNativeProvider] cargo 源单源未注入（跳过 distro 侧播种） | path=None")
            return ""
        try:
            return self._cargo_source_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[WslNativeProvider] cargo 源单源文件不可读（跳过 distro 侧播种） | path=%s | error=%s",
                self._cargo_source_path,
                exc,
            )
            return ""

    def _build_kill_argv(self, pid: int, signal: int = 9) -> list[str]:
        """进程杀 argv：环境内 sh -c kill（kill 在沙箱外发——bwrap 默认不隔离
        pid namespace，同用户外部 kill 可达；无需 -u 以外的前缀）。"""
        return self._wsl_argv(["--exec", "bash", "-c", f"kill -{signal} {pid}"])

    def _map_working_dir(self, working_dir: str | None) -> str:
        r"""/workspace 约定路径 → 环境 workspace 的 WSL 路径；相对路径按 workspace 根解析。

        guard 对未显式指定 working_dir 的 bash 调用补 /workspace（容器挂载点
        约定）；wsl_native 无挂载点，映射到 workspace_wsl。绝对 POSIX 路径与
        Windows 路径原样（wsl --cd 两者都接受）。

        相对路径（`.`/`frontend`）不得直透：`wsl --cd` 只接受绝对路径，相对
        形态实测报 Wsl/E_INVALIDARG、exit -1（2026-10-17 装机实测）。容器
        语义下相对路径以 workspace 根为基准（docker -w 同源），按
        workspace_wsl 拼接；workspace_wsl 未知（异常形态）保持原样交 wsl
        显式失败（不猜路径）。

        注意反斜杠形态：working_dir 在工具层可能被 ntpath 规整成 `\workspace`
        （2026-09-14 管道实测：wsl 收到 `--cd \workspace` 报 E_INVALIDARG），
        匹配前统一归一为正斜杠。
        """
        if not working_dir:
            return self._workspace_wsl
        normalized = working_dir.replace("\\", "/")
        if normalized == "/workspace":
            return self._workspace_wsl
        if normalized.startswith("/workspace/"):
            return self._workspace_wsl + normalized[len("/workspace") :]
        # WSL 挂载形态归一（与 process_manager._map_wsl_working_dir 同语义，
        # 漂移钉 tests/test_wsl_native_wiring.py）：`\mnt\d\X` / `D:\mnt\d\X`
        # 直透 wsl --cd 会被按盘符相对路径解析为不存在的 D:\mnt\d\X。
        m = _WS_MOUNT_RE.match(normalized)
        if m:
            return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
        base = self._workspace_wsl.rstrip("/")
        if _is_relative_working_dir(normalized) and base:
            rel = normalized[2:] if normalized.startswith("./") else normalized
            return base if rel in ("", ".") else f"{base}/{rel}"
        return working_dir

    def _bridge_env(self) -> dict[str, str]:
        """bridge 通路环境变量（与 DockerProvider._bridge_url_for_container 同源）。"""
        env: dict[str, str] = {}
        url = self._bridge_url or os.environ.get("AGENTOS_BRIDGE_URL") or ""
        if url:
            env["AGENTOS_BRIDGE_URL"] = url
        token = os.environ.get("AGENTOS_BRIDGE_TOKEN", "")
        if token:
            env["AGENTOS_BRIDGE_TOKEN"] = token
        return env

    # ── wsl 子进程 ──────────────────────────────────────────────

    async def _popen_run(self, args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        """同步 Popen 放线程池执行（Windows 兼容），超时杀本地进程树。

        超时纪律与 HostProvider 对齐：kill_process_tree 杀 wsl.exe 进程树，
        杀失败不吞——抛出的 TimeoutError 消息携带失败清单；杀净后 wait 回收。
        wsl 侧残留进程随 WSL 会话终止兜底，不构成环境泄漏（环境=档文件）。
        """

        def _sync() -> tuple[int, bytes, bytes]:
            proc = subprocess.Popen(  # noqa: S603 - args 为受控 argv，无 shell
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                out, err = proc.communicate(timeout=timeout)
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                failures = kill_process_tree(proc.pid)
                if failures:
                    raise TimeoutError(f"命令执行超时（{timeout}秒）；进程树清理失败: {'; '.join(failures)}") from None
                proc.wait()
                raise TimeoutError(f"命令执行超时（{timeout}秒）") from None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync)

    @staticmethod
    def _decode(data: bytes | None) -> str:
        return (data or b"").decode("utf-8", errors="replace").strip()

    @staticmethod
    def _decode_wsl_list(data: bytes) -> list[str]:
        """wsl -l -q 输出解码：wsl.exe 自身输出为 UTF-16-LE，Linux 侧为 UTF-8。

        以是否含 NUL 区分（UTF-16 每字符低字节后跟 \\x00）。
        """
        if not data:
            return []
        text = data.decode("utf-16-le", errors="replace") if b"\x00" in data else data.decode("utf-8", errors="replace")
        return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]

    @staticmethod
    def _to_wsl_path(workspace: str | None) -> str:
        """Windows 盘符路径 → WSL 路径（D:\\x\\y → /mnt/d/x/y）；其余原样。"""
        if not workspace:
            return ""
        normalized = workspace.replace("\\", "/")
        m = re.match(r"^([A-Za-z]):/(.*)$", normalized)
        if not m:
            return workspace
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"

    # ── metadata 档（环境底层真相；IO 单源 providers.env_metadata）──

    def _read_metadata(self, name: str) -> dict[str, Any] | None:
        return env_metadata.read_metadata(self._state_dir, name, logger=logger)

    def _write_metadata(self, name: str, workspace: str, workspace_wsl: str, now: datetime) -> bool:
        return env_metadata.write_metadata(
            self._state_dir,
            name,
            {
                "name": name,
                "workspace": workspace,
                "workspace_wsl": workspace_wsl,
                "created_at": now.isoformat(),
            },
            logger=logger,
        )

    def _remove_metadata(self, name: str) -> bool:
        return env_metadata.remove_metadata(self._state_dir, name, logger=logger)

    # ── 环境构造 ────────────────────────────────────────────────

    def _build_env(
        self,
        name: str,
        context: IsolationContext,
        now: datetime,
        workspace_wsl: str,
    ) -> IsolationEnvironment:
        env = IsolationEnvironment(
            env_id=name,
            level=IsolationLevel.CONTAINER,
            provider_type="wsl_native",
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "container_name": name,
                "workspace": context.workspace,
                "workspace_wsl": workspace_wsl,
                "exec_backend": self.exec_backend_info(workspace_wsl),
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[name] = env
        self._workspace_wsl = workspace_wsl
        return env

    def _make_error_environment(
        self,
        context: IsolationContext,
        now: datetime,
        error_msg: str,
        query_unavailable: bool = False,
    ) -> IsolationEnvironment:
        """构造错误状态环境（形状与 DockerProvider._make_error_environment 对齐）。"""
        env_id = f"wsl-{context.task_id}"
        provider_info: dict[str, Any] = {"error": error_msg}
        if query_unavailable:
            # 存在性查询未知（非确定性失败）：调用方据此不计入创建失败熔断。
            provider_info["query_unavailable"] = True
        env = IsolationEnvironment(
            env_id=env_id,
            level=IsolationLevel.CONTAINER,
            provider_type="wsl_native",
            status=EnvironmentStatus.ERROR.value,
            context=context,
            provider_info=provider_info,
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[env_id] = env
        return env
