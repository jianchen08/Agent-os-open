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
_SHARED_ROOT = str(Path(__file__).resolve().parents[3])
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from proc_tree import kill_process_tree  # noqa: E402

logger = logging.getLogger(__name__)

# metadata 档文件名后缀（state_dir/<env名>.json）
_METADATA_SUFFIX = ".json"

# find_environment_by_name 合成 context 的任务类型（与 docker 直查通路一致：
# 环境名反推，无真实任务归属）
_META_TASK_TYPE = TaskType.ATOMIC


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
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._distro = str(self._config.get("distro", "Ubuntu"))
        user = self._config.get("user")
        self._user = str(user) if user else None
        self._sandbox_cmd: list[str] = [str(x) for x in self._config.get("sandbox_cmd", [])]
        self._wsl_exe = str(self._config.get("wsl_exe", "wsl"))
        if self._config.get("state_dir"):
            self._state_dir = str(self._config["state_dir"])
        else:
            # 仓库根 = providers/ 上溯 5 级（providers→isolation→system→shared→plugins→根）
            self._state_dir = str(
                Path(__file__).resolve().parents[5] / ".ai_workspaces" / "wsl_native_envs"
            )
        self._bridge_url = str(self._config.get("bridge_url", "") or "")
        self._environments: dict[str, IsolationEnvironment] = {}
        # 当前环境 workspace 的 WSL 侧路径（create 时刷新；argv 构建用）
        self._workspace_wsl = str(self._config.get("workspace_wsl", ""))

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
                rc, _, err = await self._popen_run(
                    self._wsl_argv(["--exec", "true"]), timeout=15
                )
                if rc != 0:
                    return False, f"WSL 用户不可用: {self._user}: {self._decode(err)}"
            # bash 必须在位：执行壳用 bash（dash 对 set -o pipefail 致命退出，
            # 2026-09-14 实测；容器镜像内 dash 容忍——发行版语义不齐，统一 bash）
            rc_bash, _, _ = await self._popen_run(
                self._wsl_argv(["--exec", "sh", "-c", "command -v bash"]), timeout=15
            )
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
            return self._make_error_environment(
                context, now, f"工作空间路径不存在: {context.workspace}"
            )
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
            rc, _, err = await self._popen_run(
                self._wsl_argv(["--exec", "mkdir", "-p", workspace_wsl]), timeout=30
            )
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
        if rc != 0:
            return self._make_error_environment(
                context, now, f"工作空间目录创建失败: {self._decode(err)}"
            )

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
                rc, stdout, stderr = await self._popen_run(
                    self._wsl_argv(["--exec", "cat", path]), timeout=10
                )
                if rc != 0:
                    return ExecutionResult(
                        success=False, output=None, error=f"读取失败: {self._decode(stderr)}"
                    )
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
                            "mkdir -p \"$1\" && python3 -c "
                            "'import json,sys; open(sys.argv[1],\"w\").write(json.loads(sys.argv[2]))' "
                            "\"$2\" \"$3\"",
                            "--",
                            dir_path,
                            path,
                            encoded,
                        ]
                    ),
                    timeout=10,
                )
                if rc != 0:
                    return ExecutionResult(
                        success=False, output=None, error=f"写入失败: {self._decode(stderr)}"
                    )
                return ExecutionResult(success=True, output=None)

            if op == "exists":
                result = await self._exec(
                    {"command": f"test -e {path} && echo yes || echo no"}
                )
                stdout = (result.output or {}).get("stdout", "")
                return ExecutionResult(success=True, output={"exists": "yes" in stdout})

            return ExecutionResult(success=False, output=None, error=f"不支持的文件操作: {op}")
        except Exception as e:
            return ExecutionResult(success=False, output=None, error=f"文件操作失败: {e}")

    # ── argv 构建 ───────────────────────────────────────────────

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
        if bridge_env:
            args.append("env")
            args.extend(f"{k}={v}" for k, v in bridge_env.items())
        args.extend(self._sandbox_cmd)
        # 执行壳 bash：dash 对 set -o pipefail 致命退出（2026-09-14 实测），
        # bash 语义与 docker 路径的复合命令预期一致
        args.extend(["bash", "-c", command])
        return args

    def _build_kill_argv(self, pid: int, signal: int = 9) -> list[str]:
        """进程杀 argv：环境内 sh -c kill（kill 在沙箱外发——bwrap 默认不隔离
        pid namespace，同用户外部 kill 可达；无需 -u 以外的前缀）。"""
        return self._wsl_argv(["--exec", "bash", "-c", f"kill -{signal} {pid}"])

    def _map_working_dir(self, working_dir: str | None) -> str:
        """/workspace 约定路径 → 环境 workspace 的 WSL 路径。

        guard 对未显式指定 working_dir 的 bash 调用补 /workspace（容器挂载点
        约定）；wsl_native 无挂载点，映射到 workspace_wsl。其余 POSIX 路径与
        Windows 路径原样（wsl --cd 两者都接受）。

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
            return self._workspace_wsl + normalized[len("/workspace"):]
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
                    raise TimeoutError(
                        f"命令执行超时（{timeout}秒）；进程树清理失败: {'; '.join(failures)}"
                    ) from None
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
        text = (
            data.decode("utf-16-le", errors="replace") if b"\x00" in data else data.decode("utf-8", errors="replace")
        )
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

    # ── metadata 档（环境底层真相）──────────────────────────────

    def _metadata_path(self, name: str) -> Path:
        return Path(self._state_dir) / f"{name}{_METADATA_SUFFIX}"

    def _read_metadata(self, name: str) -> dict[str, Any] | None:
        """读 metadata 档；缺失返回 None，损坏清档返回 None（坏档≠存在）。"""
        path = self._metadata_path(name)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            logger.warning("[WslNativeProvider] metadata 损坏，清除 | name=%s", name)
            self._remove_metadata(name)
            return None

    def _write_metadata(self, name: str, workspace: str, workspace_wsl: str, now: datetime) -> bool:
        path = self._metadata_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "name": name,
                        "workspace": workspace,
                        "workspace_wsl": workspace_wsl,
                        "created_at": now.isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return True
        except OSError as e:
            logger.error("[WslNativeProvider] metadata 写入失败 | name=%s | error=%s", name, e)
            return False

    def _remove_metadata(self, name: str) -> bool:
        """删档：不存在 = 幂等成功；IO 失败如实 False（不谎报）。"""
        path = self._metadata_path(name)
        if not path.exists():
            return True
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.error("[WslNativeProvider] metadata 删除失败 | name=%s | error=%s", name, e)
            return False

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
