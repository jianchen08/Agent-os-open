"""Linux 原生隔离提供者（bubblewrap 沙箱，免 daemon，Codex 式）。

环境 = 「workspace 目录 + 一次性 bwrap 沙箱进程」：无常驻容器、无守护进程
依赖（不需要 docker daemon），沙箱随进程生灭、结构性无泄漏。隔离边界 =
Linux namespace 文件系统（根 ro-bind + workspace rw-bind）+ 独立
proc/dev/tmpfs + pid namespace + 默认 ``--unshare-net`` 禁网（network 配置
可开，对齐 Codex 的 network_access 开关精神）。

资源限额边界（如实声明，与 2026-10-09 ADR「后端各自语义承担」口径一致）：
bwrap 给不了 cgroup 级内存/CPU 限额；能给的域是 pid namespace（沙箱内进程
组与宿主隔离）、文件系统写域（仅 workspace rw + 沙箱内 tmpfs）、网络域
（默认禁）。进程级失控由 bash 工具侧看门狗按宿主 RSS 采样兜底（沙箱进程
是宿主进程树，psutil 可采样可杀灭）。

与 DockerProvider/wsl_native 的契约对齐（可互换性）：
- env_id = container_name（workspace 确定性派生，同 workspace 同环境）；
- ExecutionResult.output 形状一致（stdout/stderr/return_code/command）；
- error env + query_unavailable 标记语义一致（本后端无 daemon 查询边界，
  D6② 无触发面——创建/验活均为本地确定性操作，结构性差异非语义缺口）；
- 销毁按名幂等、失败如实返回 False（D6③ 不谎报）。

与 wsl_native 的机制差异（有意为之）：
- 「容器记录」= state_dir 下的 JSON metadata 档（IO 单源 providers.env_metadata，
  两后端共用）：workspace 路径 + 创建时间，是收养/验活/按名销毁的底层真相；
  档不存在即环境不存在。
- 执行 = 直接 spawn bwrap（宿主本机，无 wsl.exe 传输层）；working_dir 的
  /workspace 挂载点约定由 --bind 建立，无跨机路径转换面。
- 进程杀灭 = 宿主侧 bwrap 进程树（--unshare-pid 下内层 pid 宿主不可寻址，
  echo $$ 上报协议不适用；pid-1 退出触发内核回收整 ns），bash 工具侧按
  宿主 pid 走 psutil 整树杀。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

# find_environment_by_name 合成 context 的任务类型（与 docker/wsl 直查通路
# 一致：环境名反推，无真实任务归属）
_META_TASK_TYPE = TaskType.ATOMIC


class LinuxNativeProvider(IsolationProvider):
    """Linux 原生隔离提供者。

    config 键：
    - network: 是否放开网络（默认 False = --unshare-net 禁网）
    - bwrap_path: bwrap 可执行文件（默认 "bwrap"；测试可注入替身）
    - state_dir: metadata 档目录（默认 <仓库根>/.ai_workspaces/linux_native_envs）
    - bridge_url: AGENTOS_BRIDGE_URL 注入值（缺省读环境变量，与 docker 同源）
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._network_enabled = bool(self._config.get("network", False))
        self._bwrap_path = str(self._config.get("bwrap_path", "bwrap"))
        if self._config.get("state_dir"):
            self._state_dir = str(self._config["state_dir"])
        else:
            # 仓库根 = providers/ 上溯 5 级（providers→isolation→system→shared→plugins→根）
            self._state_dir = str(
                Path(__file__).resolve().parents[4] / ".ai_workspaces" / "linux_native_envs"
            )
        self._bridge_url = str(self._config.get("bridge_url", "") or "")
        self._environments: dict[str, IsolationEnvironment] = {}
        # 当前环境 workspace 的主机路径（create 时刷新；沙箱模板构建用）
        self._workspace = str(self._config.get("workspace", ""))

    # ── 后端元数据（guard 注入 / bash 工具侧执行共用）────────────

    def exec_backend_info(self) -> dict[str, Any]:
        """后端调度信息：随 env.provider_info 下发，bash 工具侧凭此拼执行 argv。

        sandbox_prefix 携带完整 bwrap 选项段（隔离模板 + env 注入）；bash 侧
        只续 --chdir 与命令、不重建模板——隔离模板单点在本类，传输面无漂移。
        """
        return {
            "backend": "linux_native",
            "bwrap_path": self._bwrap_path,
            "network": self._network_enabled,
            "state_dir": self._state_dir,
            "workspace": self._workspace,
            "sandbox_prefix": self._sandbox_prefix(),
        }

    # ── IsolationProvider 接口 ──────────────────────────────────

    def get_level(self) -> IsolationLevel:
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 bwrap 后端可用性：二进制在位 → 真实沙箱自检（spawn true）。

        bwrap 缺失时指引两条出路（装 bubblewrap / 显式置 linux_native.enabled
        =false 回退 Docker），不静默回退——CONTAINER 槽位已被本后端占据，
        fail-closed 由 decider 执法（F11：禁止运行期静默兜底）。真实自检
        （而非仅 which）验证内核 userns/mount 能力：部分发行版关闭
        unprivileged userns，二进制在位也会在 spawn 时失败——误报可用的
        代价（容器要求型工具批量假可用）高于误报不可用。
        """
        if not shutil.which(self._bwrap_path):
            return False, (
                f"bubblewrap 未安装: {self._bwrap_path}。"
                "安装 bubblewrap（如 sudo apt install bubblewrap），"
                "或在 isolation 配置显式置 linux_native.enabled=false 回退 Docker 后端"
            )
        if not shutil.which("bash"):
            return False, "宿主缺少 bash（执行壳依赖）"
        try:
            rc, out, err = await self._popen_run(self._probe_argv(), timeout=15)
        except Exception as e:
            return False, f"bwrap 探测失败: {e}"
        if rc != 0:
            return False, f"bwrap 沙箱自检失败（rc={rc}）: {self._decode(err or out)}"
        return True, None

    async def create_environment(
        self,
        context: IsolationContext,
        container_name: str | None = None,
    ) -> IsolationEnvironment:
        """创建（或按名收养）linux_native 隔离环境。

        无 daemon 查询边界：workspace 校验与 metadata 写档均为本地确定性
        操作（D6② 无触发面）；IO 失败 = 确定性失败（error env，不带
        query_unavailable）。
        """
        now = datetime.now(UTC)
        # manager 对 CONTAINER 恒注入 workspace 派生名；缺名兜底仅为类型完备
        name = container_name if container_name else f"cua-{context.task_id}"

        # 工作空间校验（镜像 DockerProvider/wsl_native：拒绝无挂载/不存在的
        # workspace，避免命令落到空目录却以 exit 0 静默通过）
        if not context.workspace:
            logger.error(
                "[LinuxNativeProvider] 拒绝创建环境：工作空间为空 | task=%s",
                context.task_id,
            )
            return self._make_error_environment(context, now, "工作空间为空，无法挂载到沙箱")
        if not Path(context.workspace).exists():
            logger.error(
                "[LinuxNativeProvider] 拒绝创建环境：工作空间路径不存在 | task=%s | path=%s",
                context.task_id,
                context.workspace,
            )
            return self._make_error_environment(
                context, now, f"工作空间路径不存在: {context.workspace}"
            )

        # D6① 幂等收养：遗留 metadata 且 workspace 一致 → 直接收养复用
        # （对应 docker start 按名收养；本后端创建面无任何子进程）。
        meta = env_metadata.read_metadata(self._state_dir, name, logger=logger)
        if meta is not None:
            if meta.get("workspace") == context.workspace:
                logger.info("[LinuxNativeProvider] 收养已有同名环境 | name=%s", name)
                return self._build_env(name, context, now)
            logger.warning(
                "[LinuxNativeProvider] 陈旧 metadata（workspace 漂移），清档重建 | name=%s",
                name,
            )
            env_metadata.remove_metadata(self._state_dir, name, logger=logger)

        payload = {
            "name": name,
            "workspace": context.workspace,
            "created_at": now.isoformat(),
        }
        if not env_metadata.write_metadata(self._state_dir, name, payload, logger=logger):
            return self._make_error_environment(context, now, "metadata 档写入失败")

        env = self._build_env(name, context, now)
        logger.info(
            "[LinuxNativeProvider] 环境已创建 | name=%s | workspace=%s",
            name,
            context.workspace,
        )
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁环境：删 metadata 档即真删（无常驻进程/容器可清理）。

        D6③：无内存登记≠环境不存在——按名删档兜底；档不存在 = 底层真无
        资源，幂等成功（该后端的「底层存在」就是一份档文件，档没了就没了，
        不存在谎报空间）；删档失败（IO 错误）如实返回 False，保留登记供重试。
        """
        removed = env_metadata.remove_metadata(self._state_dir, env_id, logger=logger)
        if not removed:
            logger.warning(
                "[LinuxNativeProvider] 销毁环境失败（metadata 删除异常，保留登记） | id=%s",
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
        meta = env_metadata.read_metadata(self._state_dir, name, logger=logger)
        if meta is None:
            return None
        workspace = meta.get("workspace")
        if not workspace or not Path(str(workspace)).exists():
            logger.warning(
                "[LinuxNativeProvider] 环境失效（workspace 消失），清档待重建 | name=%s",
                name,
            )
            env_metadata.remove_metadata(self._state_dir, name, logger=logger)
            return None
        now = datetime.now(UTC)
        context = IsolationContext(
            task_id=name,
            task_type=_META_TASK_TYPE,
            is_root_task=True,
            isolation_level=IsolationLevel.CONTAINER,
        )
        context.workspace = str(workspace)
        return self._build_env(name, context, now)

    async def execute_in_environment(
        self,
        env_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在环境中执行操作（一次性 bwrap 沙箱进程，无常驻环境可寻址）。"""
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
        """环境状态：沙箱环境无运行态（随用随起），登记即 READY。"""
        if env_id in self._environments:
            return EnvironmentStatus.READY
        return EnvironmentStatus.STOPPED

    # ── 执行内部 ────────────────────────────────────────────────

    async def _exec(self, operation: dict[str, Any]) -> ExecutionResult:
        """执行命令：argv 与超时纪律与 DockerProvider/wsl_native 对齐。"""
        command = str(operation.get("command", ""))
        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")
        timeout = float(operation.get("timeout", 30))
        args = self._build_exec_argv(
            working_dir=operation.get("working_dir"),
            command=command,
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
        """文件操作：read/write/exists 语义与 DockerProvider/wsl_native 对齐。

        走沙箱 exec（而非宿主直读直写）：写域（仅 workspace rw）由 bwrap
        挂载模板强制，宿主侧不重抄边界逻辑（防两套边界漂移）。
        """
        op = operation.get("operation")
        # /workspace 约定路径在沙箱内由 --bind 建立，映射即缺省补挂载点
        path = self._map_working_dir(str(operation.get("path", "")))
        try:
            if op == "read":
                rc, stdout, stderr = await self._popen_run(
                    [*self._sandbox_prefix(), "--", "cat", path], timeout=10
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
                    [
                        *self._sandbox_prefix(),
                        "--",
                        "sh",
                        "-c",
                        "mkdir -p \"$1\" && python3 -c "
                        "'import json,sys; open(sys.argv[1],\"w\").write(json.loads(sys.argv[2]))' "
                        "\"$2\" \"$3\"",
                        "--",
                        dir_path,
                        path,
                        encoded,
                    ],
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

    def _sandbox_prefix(self) -> list[str]:
        """bwrap 选项段（隔离模板）：根 ro + workspace 同路径 rw + 独立
        proc/dev/tmpfs + pid ns + 默认禁网 + die-with-parent + env 注入。

        - --ro-bind / /：根文件系统只读视图（宿主工具链复用，写域仅 workspace
          与沙箱内 tmpfs）——后挂点遮蔽先挂点，workspace bind 必须在其后；
        - --bind <ws> <ws>：workspace 以**真实路径**读写覆盖（同路径挂载点
          即真实目录，bwrap 只在 tmpfs 上自动建挂载点，根视图无 /workspace
          这个目录可挂）；guard 的 /workspace 约定由 working_dir 映射层翻译；
        - --unshare-pid：独立 pid namespace（沙箱内进程组与宿主互不可见）；
          代价是内层 pid 宿主不可寻址，进程杀灭走宿主侧 bwrap 进程树；
        - --die-with-parent：宿主侧父进程消亡即全家带葬（沙箱随进程生灭）；
        - --setenv：bridge 通路 + 任务运行时 env（与 wsl_native 同语义）。
        """
        args = [
            self._bwrap_path,
            "--ro-bind", "/", "/",
            "--bind", self._workspace, self._workspace,
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--unshare-pid",
            "--die-with-parent",
        ]
        if not self._network_enabled:
            args.append("--unshare-net")
        merged_env = {**self._bridge_env(), **self._runtime_env_pairs()}
        for key, value in merged_env.items():
            args.extend(["--setenv", key, value])
        return args

    def _probe_argv(self) -> list[str]:
        """可用性自检 argv：最小真实沙箱跑 /bin/true（验 userns/mount 能力）。"""
        return [
            self._bwrap_path,
            "--ro-bind", "/", "/",
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-pid",
            "--unshare-net",
            "--die-with-parent",
            "--",
            "/bin/true",
        ]

    def _build_exec_argv(self, working_dir: str | None, command: str) -> list[str]:
        """执行 argv：[沙箱选项段] + --chdir <映射后目录> + -- + bash -c <command>。

        working_dir 在此处统一做 /workspace → 环境真实路径映射（bash 工具侧
        复用 sandbox_prefix 时同规则）；timeout 是命令语义的一部分（调用方
        _popen_run 强制），不进入 argv。
        """
        return [
            *self._sandbox_prefix(),
            "--chdir", self._map_working_dir(working_dir),
            "--",
            "bash", "-c", command,
        ]

    def _map_working_dir(self, working_dir: str | None) -> str:
        """/workspace 约定路径 → 环境 workspace 真实路径（同路径 rw-bind，
        沙箱内可见可写）。guard 对未显式指定 working_dir 的 bash 调用补
        /workspace（容器挂载点约定）；其余绝对路径原样（沙箱内路径与宿主
        1:1——根 ro-bind，无跨机路径转换面）；相对路径落 workspace（cwd
        必须存在）。"""
        workspace = self._workspace or "/workspace"
        if not working_dir:
            return workspace
        normalized = working_dir.replace("\\", "/")
        if normalized == "/workspace":
            return workspace
        if normalized.startswith("/workspace/"):
            return workspace + normalized[len("/workspace"):]
        if normalized.startswith("/"):
            return normalized
        return workspace

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

    def _runtime_env_pairs(self) -> dict[str, str]:
        """任务运行时 env：工具链缓存副作用约束进 workspace（与 wsl_native 同语义）。

        区别：沙箱内除 workspace 外全盘 ro，写宿主 home 结构性不可能——注入
        runtime pairs 是让 pip/npm/XDG 缓存落 rw 域（写不进去会走慢路径或刷
        告警），非防污染。workspace 未知（异常形态）不注入，防 env 值指向
        文件系统根。
        """
        if not self._workspace:
            return {}
        rt = f"{self._workspace.rstrip('/')}/.task_runtime"
        return {
            "XDG_CACHE_HOME": f"{rt}/cache",
            "npm_config_cache": f"{rt}/npm-cache",
            "PYTHONUSERBASE": f"{rt}/pyuser",
        }

    # ── bwrap 子进程 ────────────────────────────────────────────

    async def _popen_run(self, args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        """同步 Popen 放线程池执行（双平台兼容），超时杀本地进程树。

        超时纪律与 wsl_native/HostProvider 对齐：kill_process_tree 杀宿主侧
        bwrap 进程树（沙箱全部进程是其后代，--unshare-pid 下 pid-1 退出触发
        内核回收整 ns），杀失败不吞——抛出的 TimeoutError 消息携带失败清单；
        杀净后 wait 回收。
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

    # ── 环境构造 ────────────────────────────────────────────────

    def _build_env(
        self,
        name: str,
        context: IsolationContext,
        now: datetime,
    ) -> IsolationEnvironment:
        self._workspace = context.workspace or ""
        env = IsolationEnvironment(
            env_id=name,
            level=IsolationLevel.CONTAINER,
            provider_type="linux_native",
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "container_name": name,
                "workspace": context.workspace,
                "exec_backend": self.exec_backend_info(),
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
        """构造错误状态环境（形状与 DockerProvider/wsl_native 对齐）。

        query_unavailable（存在性未知）标记在本后端不存在：无 daemon 查询
        边界，创建/验活均为本地确定性操作，D6② 无触发面——所有 error env
        均为确定性失败（消费方语义：计入创建失败熔断）。
        """
        env_id = f"linux-{context.task_id}"
        provider_info: dict[str, Any] = {"error": error_msg}
        env = IsolationEnvironment(
            env_id=env_id,
            level=IsolationLevel.CONTAINER,
            provider_type="linux_native",
            status=EnvironmentStatus.ERROR.value,
            context=context,
            provider_info=provider_info,
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[env_id] = env
        return env
