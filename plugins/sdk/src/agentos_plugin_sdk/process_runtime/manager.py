"""进程管理器（长进程代持：句柄注册表 + 日志真理源 + 看门狗）。

自 plugins/shared/bash/process_manager.py 迁入（工具隔离声明化 Slice 2，
ADR 2026-10-21-tool-isolation-capability-declaration）。传输辅助见
transports.py，进程后端见 backends.py；Tasks 状态机对外 API 见 runtime.py。
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shlex
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from agentos_plugin_sdk.process_runtime.backends import (
    _get_container_backend,
    _get_local_backend,
    _get_wsl_native_backend,
)
from agentos_plugin_sdk.process_runtime.encoding import EncodingHandler
from agentos_plugin_sdk.process_runtime.input_handler import InputHandler
from agentos_plugin_sdk.process_runtime.log_compressor import LogCompressor
from agentos_plugin_sdk.process_runtime.shell_route import (
    is_compound_command,
    is_wsl_command,
    spawn_local_process,
)
from agentos_plugin_sdk.process_runtime.transports import (
    _map_linux_native_working_dir,
    _map_wsl_working_dir,
    _wsl_bridge_env_pairs,
    _wsl_runtime_env_pairs,
    _wsl_transport,
)
from agentos_plugin_sdk.process_runtime.types import (
    ProcessBackend,
    ProcessInfo,
    WorkUnit,
)

logger = logging.getLogger(__name__)


class ProcessLogReadError(RuntimeError):
    """日志文件存在但读取失败（IO 层故障）。

    与「文件不存在（返回 None）」显式区分：调用方须把读取故障作为
    LOG_FILE_IO_ERROR 报给上层，不得静默当成"进程不存在/无日志"。
    """


class ProcessManager:
    """进程管理器"""

    def __init__(self, log_dir: Path | None = None, log_delete_grace_seconds: float | None = None):
        """初始化进程管理器

        Args:
            log_dir: 命令日志目录（默认 logs/bash）。
            log_delete_grace_seconds: 正常退出日志的删除宽限窗秒数
                （默认 300；测试可注入 0 验证删除行为）。
        """
        self.log_dir = log_dir or Path("logs/bash")
        self._log_delete_grace_seconds = (
            self._LOG_DELETE_GRACE_SECONDS_DEFAULT
            if log_delete_grace_seconds is None
            else float(log_delete_grace_seconds)
        )
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(f"日志目录创建失败（不影响命令执行） | dir={self.log_dir}")

        # 活跃进程映射: pid -> ProcessInfo
        self.active_processes: dict[int, ProcessInfo] = {}

        # 日志压缩器
        self.log_compressor = LogCompressor(max_lines=200)

        # 输入处理器
        self.input_handler = InputHandler()

        # ── 看门狗配置（内存水位驱动 + idle 排序杀）──
        # 触发条件（后台自动处理，不通知 Agent）：
        #   1. 内存高水位：采样内存使用率 ≥ high_watermark(85%) → 按 idle(最久没访问)
        #      排序，从最闲的开始杀，每杀一个重采样，回落到 low_watermark(70%) 即停。
        #      不一刀切，保 2-3G 容几个并发工作单元。
        #   2. 孤儿兜底：running 状态超 30 分钟无任何外部访问 → 无条件杀
        #      （内存没涨但进程确被遗忘的情况）。
        # 判据看 idle(now - last_access_time) 不是 age(start_time)：
        # 活跃 dev server 虽启动早但 agent 一直访问 → idle≈0 → 不杀；
        # 跑飞的 cargo build 无人管 → idle 涨 → 内存紧张时优先杀。
        self._watchdog_interval: float = 10.0  # 巡检间隔（秒）
        self._cleanup_high_watermark: float = 0.85  # 内存高水位：触发清理
        self._cleanup_low_watermark: float = 0.70  # 内存低水位：停止清理
        self._orphan_timeout: float = 1800.0  # 孤儿兜底：30 分钟无访问无条件杀
        # 单进程内存维度：某工作单元自身 RSS 超过此阈值即判为失控候选。
        # 比系统水位更灵敏——31GB 宿主上单进程吃 2GB 只占 6% 触发不了系统水位，
        # 但该进程自身已远超合理上限，应杀。默认 2GB(覆盖大部分 build 场景)。
        # 可经环境变量 AO_UNIT_MEMORY_LIMIT_MB 覆盖。
        self._unit_memory_limit: int = int(os.environ.get("AO_UNIT_MEMORY_LIMIT_MB", "2048")) * 1024 * 1024
        self._watchdog_task: asyncio.Task[None] | None = None
        # 默认内存后端：本地宿主。容器隔离路径会注入 ContainerProcessBackend。
        # 看门狗策略层只依赖 backend.sample_memory()，不关心进程跑在哪。
        self._memory_backend: ProcessBackend | None = None
        # 正常退出（rc==0）日志的延迟删除队列：[(log_file, 完成时刻)]。
        # 完成后不能立刻删——execute/continue 的轮询与 _on_output_task_done
        # 存在竞态，get_summary/read_log_by_pid 的磁盘降级路径以日志为真理源
        # （test_summary_race 锁定的契约），保留一个宽限窗再删。
        self._logs_pending_delete: list[tuple[Path, float]] = []

        # 启动时一次性清扫过期命令日志（运行期正常退出已随手清，此处兜历史残留）
        self._sweep_stale_logs()

    # 命令日志保留上限：>7 天的 bash_*.log 启动时清扫
    _LOG_SWEEP_MAX_AGE_SECONDS: ClassVar[float] = 7 * 24 * 3600.0
    # 正常退出日志的删除宽限窗：覆盖 execute 返回后的 read_log/summary 降级读取
    _LOG_DELETE_GRACE_SECONDS_DEFAULT: ClassVar[float] = 300.0

    def flush_expired_log_deletes(self) -> None:
        """删除超过宽限窗的正常退出日志（看门狗 tick / shutdown 调用）。

        best-effort：单文件删除失败告警保留，不阻塞其余删除。
        """
        if not self._logs_pending_delete:
            return
        now = time.monotonic()
        remaining: list[tuple[Path, float]] = []
        for log_file, done_at in self._logs_pending_delete:
            if now - done_at < self._log_delete_grace_seconds:
                remaining.append((log_file, done_at))
                continue
            self._unlink_command_log(log_file)
        self._logs_pending_delete = remaining

    def _sweep_stale_logs(self) -> None:
        """清扫 log_dir 下超过保留期（7 天）的 bash_*.log（一次性行，带数量汇总）。

        best-effort：目录不可读/单文件删除失败只告警保留现场，不影响命令执行。
        """
        try:
            candidates = list(self.log_dir.glob("bash_*.log"))
        except OSError as e:
            logger.warning(f"启动日志清扫失败（不影响命令执行） | dir={self.log_dir} | error={e}")
            return
        if not candidates:
            return
        cutoff = time.time() - self._LOG_SWEEP_MAX_AGE_SECONDS
        removed = 0
        for log_file in candidates:
            try:
                if log_file.is_file() and log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    removed += 1
            except OSError as e:
                logger.warning(f"过期日志删除失败（保留现场） | file={log_file} | error={e}")
        logger.info(
            f"启动日志清扫完成 | dir={self.log_dir} | scanned={len(candidates)} "
            f"| removed={removed} | retention_days={int(self._LOG_SWEEP_MAX_AGE_SECONDS // 86400)}"
        )

    def _unlink_command_log(self, log_file: Path) -> None:
        """删除单条命令日志（best-effort：失败告警保留，不吞）。"""
        try:
            log_file.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"命令日志清理失败（保留现场） | file={log_file} | error={e}")

    def _generate_log_filename(self, command: str, pid: int) -> str:
        """生成日志文件名（pid 派生）。

        pid 是 LLM 调用 execute/continue 时永远拿到的稳定标识，直接做文件名。
        read_log 只认 pid 即可按规则算路径读磁盘——LLM 不需要额外记 log_file 路径，
        也不依赖 active_processes（进程结束后内存条目被清，但磁盘日志仍在）。

        文件名必须由 pid 独自派生（不含 hash 等调用方算不出的成分），
        保证进程结束后 read_log 任何时候都能回读；pid 在 OS 层唯一，不会冲突。
        """
        return f"bash_{pid}.log"

    def _write_log_header(self, log_file: Path, command: str, pid: int, owner: str | None = None) -> None:
        """写入日志头部（容错：日志写入失败不影响命令执行）。

        命令先经 _mask_secrets 掩码（Authorization/Bearer/API key/密码等取值），
        避免日志落盘泄露敏感信息。owner（会话身份）一并写入头部，
        供进程清理后的磁盘降级路径（read_log_by_pid）做越权校验。
        """
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            header = [
                "# Bash Command Log",
                f"# Command: {self._mask_secrets(command)}",
                f"# PID: {pid}",
                f"# Started: {datetime.now(UTC).isoformat()}",
                f"# Platform: {platform.system()}",
                f"# {'=' * 50}",
                "",
            ]
            if owner is not None:
                # 插到 Started 之前，read_log_by_pid 解析头部时按行序读取
                header.insert(3, f"# Owner: {owner}")
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("\n".join(header))
        except OSError as e:
            logger.warning(f"日志头部写入失败（不影响命令执行） | file={log_file} | error={e}")

    # 敏感信息掩码模式：保留前缀，掩掉取值（命令日志落盘前调用）
    # 取值用 [^\s"']+（不含引号），避免吞掉值后的闭合引号。
    _SECRET_PATTERNS: ClassVar[list[tuple[re.Pattern[str], str]]] = [
        # Authorization: Bearer xxx / Basic xxx / digest xxx（curl -H 等）
        (
            re.compile(r"(?i)(authorization\s*[:=]\s*)((?:bearer|basic|digest)\s+)?[^\s\"']+"),
            r"\1\2***",
        ),
        # 裸 Bearer token
        (re.compile(r"(?i)(bearer\s+)[^\s\"']+"), r"\1***"),
        # key=value 赋值（API_KEY=xxx、password: xxx 等）
        (
            re.compile(r"(?i)((?:api[_-]?key|password|passwd|secret|token|credential)\s*[:=]\s*)[^\s\"']+"),
            r"\1***",
        ),
        # CLI 选项 --api-key xxx / --token xxx
        (
            re.compile(r"(?i)(--[a-z0-9_-]*(?:key|token|secret|password|auth)[a-z0-9_-]*\s+)[^\s\"']+"),
            r"\1***",
        ),
        # URL 内嵌凭据 https://user:pass@host
        (re.compile(r"(?i)(://[^/:\s@]+:)([^@\s/]+)(@)"), r"\1***\3"),
    ]

    @classmethod
    def _mask_secrets(cls, text: str) -> str:
        """掩码文本中的敏感取值（Authorization/Bearer/API key/密码/URL 凭据）。

        只掩码「取值」保留「键名/前缀」，尽量不破坏命令可读性。
        """
        if not text:
            return text
        for pattern, replacement in cls._SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _append_to_log(self, log_file: Path, content: str) -> None:
        """追加内容到日志（容错：日志写入失败不影响命令执行）"""
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(content)
        except OSError as e:
            logger.warning(f"日志追加写入失败（不影响命令执行） | file={log_file} | error={e}")

    def _read_log_lines(self, log_file: Path) -> list[str] | None:
        """读取日志所有行。

        返回 None = 文件存在但读取失败（OSError，权限/被占用等）——与
        「无日志」（文件缺失，返回 []）显式区分，调用方须给可识别的
        错误标识，不得把读取故障静默当成“进程无输出”。
        """
        if not log_file.exists():
            return []

        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning(f"日志读取失败 | file={log_file} | error={e}")
            return None
        # 过滤掉注释行
        return [line.rstrip() for line in lines if not line.startswith("#")]

    # 摘要压缩只喂尾部窗口的行数上限（防大日志全量读取，评审 H-issue）
    TAIL_SUMMARY_LINES: ClassVar[int] = 5000

    @staticmethod
    def _read_tail_lines(log_file: Path, max_lines: int = TAIL_SUMMARY_LINES) -> list[str]:
        """从文件尾部读取最多 max_lines 行（二进制尾部 seek，跨平台安全）。

        用于摘要压缩路径——大日志（几万行编译输出）只取尾部窗口喂
        LogCompressor，避免每次轮询全量读文件。返回过滤掉 # 注释行的文本行。
        """
        if not log_file.exists():
            return []
        try:
            with open(log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return []
                # 从尾部逐块向前扩展，直到凑够 max_lines 或读到文件头
                chunk_size = 65536
                pos = size
                buffer = b""
                while pos > 0:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    buffer = chunk + buffer
                    if buffer.count(b"\n") >= max_lines:
                        break
                lines = buffer.split(b"\n")[-max_lines:]
                result: list[str] = []
                for raw in lines:
                    text = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if text and not text.startswith("#"):
                        result.append(text)
                return result
        except OSError as exc:
            logger.debug("日志尾部读取失败（摘要按空窗口降级） | file=%s | error=%s", log_file, exc)
            return []

    def _ensure_log_dir(self, log_dir: Path) -> Path:
        """确保日志目录存在，返回绝对路径"""
        resolved = log_dir.resolve()
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(f"日志目录创建失败（不影响命令执行） | dir={resolved}")
        return resolved

    async def start_process(
        self,
        command: str,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        log_dir: Path | None = None,
        container_id: str | None = None,
        owner: str | None = None,
        on_output: Callable[[str], None] | None = None,
        exec_backend: dict[str, Any] | None = None,
    ) -> tuple[int, Path]:
        """启动新进程。

        container_id 非空时走容器路径：用 `docker exec` 起进程，命令包装成
        `echo $$; exec <cmd>` 以便从 stdout 第一行读出**容器内 pid**。
        宿主机 process.pid 只是 asyncio 句柄的 key（供 _read_output/wait 用），
        杀进程必须用容器内 pid（存在 ProcessInfo.metadata['container_pid']），
        否则 docker exec kill <host_pid> 在容器 namespace 里查无此 pid。

        exec_backend：执行环境后端信息（isolation_guard 服务端注入，与
        _container_id 同级信任链）。backend=wsl_native 时走 wsl 传输
        （wsl.exe -d 发行版 --cd 映射后工作目录 -- [env] [sandbox] sh -c），
        pid 读取协议与 docker 路径完全一致；None 时保持 docker exec 通路。

        owner: 会话身份（内核注入）。写入 ProcessInfo.metadata['owner'] 与
        日志头 `# Owner:`，供 pid 级操作（continue/input/terminate/read_log）
        的越权校验（工具层 _check_owner 执行）。

        on_output: 每行输出的同步回调（task_observability 任务 2——执行中
        进度推送。ProgressReporter.report，自带 1KB/2s 节流；None 时零开销）。
        """
        effective_log_dir = self._prepare_log_dir(log_dir)
        merged_env = {**os.environ, **(env or {})}
        is_windows = platform.system() == "Windows"

        if container_id:
            if exec_backend and exec_backend.get("backend") == "wsl_native":
                pid, log_file = await self._start_wsl_native_process(
                    command, working_dir, container_id, owner, effective_log_dir, on_output, exec_backend
                )
            elif exec_backend and exec_backend.get("backend") == "linux_native":
                pid, log_file = await self._start_linux_native_process(
                    command, working_dir, container_id, owner, effective_log_dir, on_output, exec_backend
                )
            else:
                pid, log_file = await self._start_container_process(
                    command, working_dir, container_id, owner, effective_log_dir, on_output
                )
            return pid, log_file

        # ===== 本地路径：WSL/Bash/CMD 分支 =====
        command = self._normalize_local_command(command, is_windows)
        process = await spawn_local_process(command, working_dir, merged_env, is_windows)
        return self._register_process(
            command,
            process,
            effective_log_dir,
            on_output,
            owner=owner,
            backend=_get_local_backend(),
            metadata=self._local_metadata(owner),
        )

    def _prepare_log_dir(self, log_dir: Path | None) -> Path:
        """归一化日志目录；收到外部 log_dir 时同步 self.log_dir——
        start_process 写入目录与 read_log_by_pid/get_summary 降级读取必须用
        同一目录，否则即时清理后降级读磁盘找不到文件 → SUMMARY_ERROR
        （生产 bug 根因）。"""
        effective_log_dir = self._ensure_log_dir(log_dir) if log_dir else self.log_dir
        if log_dir is not None:
            self.log_dir = effective_log_dir
        return effective_log_dir

    def _local_metadata(self, owner: str | None) -> dict[str, Any]:
        """本地进程 metadata（owner 供磁盘降级路径越权校验）。"""
        metadata: dict[str, Any] = {}
        if owner is not None:
            metadata["owner"] = owner
        return metadata

    async def _start_container_process(
        self,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        effective_log_dir: Path,
        on_output: Callable[[str], None] | None,
    ) -> tuple[int, Path]:
        """容器路径：docker exec 起进程，返回 (宿主句柄 pid, 日志文件)。"""
        # working_dir 必须是容器内 POSIX 绝对路径（以 / 开头）。BashTool.get_working_dir
        # 返回的是宿主 task workspace（如 D:\myproject\xxx），直接传给
        # `docker exec -w` 会让 OCI 报 "Cwd must be an absolute path" 退 128。
        # 非 POSIX 绝对路径时强制用容器挂载点 /workspace（IsolationManager 约定）。
        container_workdir = working_dir if (working_dir and working_dir.startswith("/")) else "/workspace"
        wrapped = self._wrap_container_command(command)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-w",
            container_workdir,
            container_id,
            "sh",
            "-c",
            wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        return await self._register_container_process(
            command,
            process,
            container_id,
            owner,
            effective_log_dir,
            on_output,
            backend=_get_container_backend(container_id),
            metadata_extra={},
        )

    async def _start_wsl_native_process(
        self,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        effective_log_dir: Path,
        on_output: Callable[[str], None] | None,
        exec_backend: dict[str, Any],
    ) -> tuple[int, Path]:
        """wsl_native 路径：wsl 传输起进程，pid 协议与 docker 路径一致。

        argv 形状（与 WslNativeProvider._build_exec_argv 同语义，漂移由
        tests/test_wsl_native_wiring.py::TestArgvDriftPin 钉死）：
        [wsl, -d distro, (-u user), --cd 映射后目录, --, [env k=v...],
         [sandbox_cmd...], sh, -c, wrapped]
        bridge 环境经 env 前缀注入（wsl_native 无常驻容器可挂 -e）。
        """
        working_dir_mapped = _map_wsl_working_dir(exec_backend, working_dir)
        # --exec：argv 逐字传递（`--` 会经登录 shell 重解析，$!/引号/管道符
        # 被外层改写——后台进程 echo $$ 协议全依赖逐字传递，禁用 `--`）
        argv = [*_wsl_transport(exec_backend), "--cd", working_dir_mapped, "--exec"]
        bridge_pairs = _wsl_bridge_env_pairs()
        runtime_pairs = _wsl_runtime_env_pairs(exec_backend)
        if bridge_pairs or runtime_pairs:
            argv.append("env")
            argv.extend(bridge_pairs)
            argv.extend(runtime_pairs)
        argv.extend(str(x) for x in exec_backend.get("sandbox_cmd", []))
        # 执行壳 bash：发行版 dash 对 set -o pipefail 致命退出（2026-09-14 实测）
        # noqa 下一行 S604 误报：shell 是自有包装函数关键字参数（pid 上报协议），非 subprocess shell=True
        argv.extend(["bash", "-c", self._wrap_container_command(command, shell="bash")])  # noqa: S604
        logger.info("[WslNative] exec argv=%r", argv)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        return await self._register_container_process(
            command,
            process,
            container_id,
            owner,
            effective_log_dir,
            on_output,
            backend=_get_wsl_native_backend(exec_backend),
            metadata_extra={"exec_backend": exec_backend},
        )

    async def _start_linux_native_process(
        self,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        effective_log_dir: Path,
        on_output: Callable[[str], None] | None,
        exec_backend: dict[str, Any],
    ) -> tuple[int, Path]:
        """linux_native 路径：bwrap 沙箱进程（宿主本地进程树）。

        argv 形状（隔离模板单点在 LinuxNativeProvider._sandbox_prefix，经
        exec_backend.sandbox_prefix 透传——本侧不重建模板，结构性无漂移）：
        [sandbox_prefix...] + --chdir 映射后目录 + -- + bash -c wrapped

        与容器/wsl 路径的关键差异（pid 协议）：不启用 echo $$ 上报——
        --unshare-pid 下内层 pid 宿主不可寻址，按内层 pid 杀会误杀同号宿主
        进程。杀灭走宿主侧 bwrap 进程树（本地后端 psutil 整树杀；pid-1 退出
        触发内核回收整 ns），metadata 不含 container_pid → terminate/看门狗
        按 host pid 杀。bridge/runtime env 已由 provider 经 --setenv 烘进
        sandbox_prefix，本侧零 env 处理。
        """
        working_dir_mapped = _map_linux_native_working_dir(exec_backend, working_dir)
        argv = [
            *(str(x) for x in exec_backend.get("sandbox_prefix", [])),
            "--chdir",
            working_dir_mapped,
            "--",
            "bash",
            "-c",
            self._wrap_sandbox_command(command),
        ]
        logger.info("[LinuxNative] exec argv=%r", argv)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        metadata: dict[str, Any] = {"container_id": container_id, "exec_backend": exec_backend}
        if owner is not None:
            metadata["owner"] = owner
        return self._register_process(
            command,
            process,
            effective_log_dir,
            on_output,
            owner=owner,
            backend=_get_local_backend(),
            metadata=metadata,
        )

    @staticmethod
    def _wrap_sandbox_command(command: str) -> str:
        """linux_native 沙箱命令包装：pipefail 复合命令包装，**无 pid 上报行**。

        与 _wrap_container_command 的差异点：不写 `echo $$`。--unshare-pid 下
        内层 pid 宿主不可寻址，按上报 pid 杀会误杀同号宿主进程；杀灭走宿主侧
        bwrap 进程树（本地后端整树杀），pid 上报无消费方。复合命令套内层
        bash（pipefail 语义与容器/wsl 路径一致）。
        """
        if is_compound_command(command):
            inner_shell = shlex.quote("bash")
            return f"exec {inner_shell} -c {shlex.quote('set -o pipefail 2>/dev/null; ' + command)}"
        return f"exec {command}"

    def _wrap_container_command(self, command: str, shell: str = "sh") -> str:
        """pid 上报包装：`echo $$; exec <cmd>`（复合命令套内层 shell，见下）。

        shell：内层 exec 的 shell（docker 路径默认 sh；wsl 路径传 bash——
        发行版 dash 对 set -o pipefail 致命退出）。"""
        inner_shell = shlex.quote(shell)
        # 包装命令让容器内 sh 自己报 pid，并 exec 成用户命令——这样 $$ 报的
        # pid 就是用户命令本身（单条命令场景，如 cargo build），kill 干净。
        #
        # 复合命令（含 &&/;/||/|/() 等）：POSIX exec 只接受一个简单命令，
        # `exec cmd1 && cmd2` 里 exec 替换为 cmd1 后进程即退出，cmd2 被丢弃
        # → 后续段输出全部丢失（如 echo "标签" && ls 只返回标签）。
        # 故复合命令自动套 `exec sh -c '<cmd>'`（shlex.quote 整条引用，防
        # double-eval），让控制操作符在内层 sh 全部生效。代价：pid 指向内层 sh
        # 而非用户命令，kill 后 sh 的子进程可能成孤儿（容器销毁时兜底清理）。
        # 单条命令保持 `exec <cmd>`，pid 精准、无孤儿，向后兼容。
        if is_compound_command(command):
            # set -o pipefail 让管道里任一段失败/被信号杀时，退出码反映真实失败
            # （而非管道最后一段的成功码）。pipefail 使退出码反映管道内真实失败段；
            # 2>/dev/null 兜底：dash/纯 POSIX sh 不支持 pipefail 选项时不报错中断
            # （bash/ash 支持，容器镜像默认满足）。
            # 注意：pipefail 只修管道(|)，不修分号序列（cmd1; cmd2 取 cmd2 码）——
            # 后者是 shell 固有语义，靠 _build_failure_message 的信号提取兜底。
            return f"echo $$; exec {inner_shell} -c {shlex.quote('set -o pipefail 2>/dev/null; ' + command)}"
        return f"echo $$; exec {command}"

    async def _register_container_process(
        self,
        command: str,
        process: asyncio.subprocess.Process,
        container_id: str,
        owner: str | None,
        effective_log_dir: Path,
        on_output: Callable[[str], None] | None,
        backend: ProcessBackend,
        metadata_extra: dict[str, Any],
    ) -> tuple[int, Path]:
        """容器/wsl 共用的注册收尾：读容器内 pid、写日志头、登记进程。"""
        host_pid = process.pid
        # 同步读第一行（容器内 sh 的 $$），拿容器内 pid。
        container_pid = await self._read_container_pid(process)
        # 先启动 process 拿到 host_pid，再按 pid 派生日志文件名（read_log 靠此规则）。
        log_file = effective_log_dir / self._generate_log_filename(command, host_pid)
        self._write_log_header(log_file, command, host_pid, owner=owner)
        metadata: dict[str, Any] = {
            "container_id": container_id,
            "container_pid": container_pid,
            **metadata_extra,
        }
        if owner is not None:
            metadata["owner"] = owner
        return self._register_process(
            command,
            process,
            effective_log_dir,
            on_output,
            owner=owner,
            backend=backend,
            metadata=metadata,
        )

    def _normalize_local_command(self, command: str, is_windows: bool) -> str:
        r"""WSL 环境下自动将 Windows 路径转换为 WSL 路径（D:\path → /mnt/d/path，
        可通过 AO_BASH_WSL_PATH_CONVERT=0 关闭）。"""
        path_convert_enabled = os.environ.get("AO_BASH_WSL_PATH_CONVERT", "1") != "0"
        if is_windows and path_convert_enabled and (is_wsl_command(command) or (shutil.which("wsl") is not None)):
            return self._convert_windows_paths_for_wsl(command)
        return command

    def _register_process(
        self,
        command: str,
        process: asyncio.subprocess.Process,
        effective_log_dir: Path,
        on_output: Callable[[str], None] | None,
        backend: Any,
        metadata: dict[str, Any],
        owner: str | None = None,
    ) -> tuple[int, Path]:
        """注册已拉起的进程：派生日志文件、启动读取任务、入活跃表、
        确保看门狗运行。返回 (pid, log_file)。"""
        pid = process.pid

        # 先启动 process 拿到 pid，再按 pid 派生日志文件名（read_log 靠此规则）。
        log_file = effective_log_dir / self._generate_log_filename(command, pid)
        # 写入日志头部（命令经掩码；owner 供磁盘降级路径越权校验）
        self._write_log_header(log_file, command, pid, owner=owner)

        # 启动日志读取任务并保存引用
        output_task = asyncio.create_task(self._read_output(pid, process, log_file, on_output))

        self.active_processes[pid] = ProcessInfo(
            pid=pid,
            command=command,
            start_time=time.time(),
            log_file=log_file,
            process=process,
            status="running",
            output_task=output_task,
            backend=backend,
            last_access_time=time.time(),
            metadata=metadata,
        )
        # 确保看门狗在运行（首次启动进程时启动，幂等）
        self._ensure_watchdog()

        # 添加任务完成回调以清理引用
        output_task.add_done_callback(lambda t, p=pid: self._on_output_task_done(p, t))  # type: ignore[misc]

        return pid, log_file

    @staticmethod
    async def _read_container_pid(process: asyncio.subprocess.Process) -> int:
        """从 docker exec 子进程 stdout 第一行读容器内 pid。

        命令被包装成 `echo $$; exec <cmd>`，第一行即容器内 sh 的 pid。
        读不到（空/非数字）时返回宿主机 process.pid 作为降级——此时
        docker exec kill 会失败但不会误杀，调用方据返回码处理。
        """
        if process.stdout is None:
            # docker exec 子进程以 PIPE 创建，stdout 恒非 None；缺管道回退宿主 pid
            return process.pid
        try:
            first_line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
            text = first_line.decode(errors="replace").strip()
            return int(text)
        except (TimeoutError, ValueError, TypeError):
            return process.pid

    async def _read_output(
        self,
        pid: int,
        process: asyncio.subprocess.Process,
        log_file: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        """异步读取进程输出（按字节块读 + 批量落盘）。

        输出按字节块读取（stream.read(N)），不能用 readline：cargo/gcc/make 等
        检测到 stdout 不是 TTY 时切到块缓冲（4-8KB 攒满才 flush），无换行符时
        readline 会永久阻塞。自己按 \\n 切行 + 字节级半行缓存：
        切分点在 \\n（ASCII 0x0A，永远在多字节 UTF-8 字符外），不会切到
        字符中间，decode 在完整字节行上做，EncodingHandler 的多编码 fallback
        链照常生效。

        残留半行（流结束时无换行符结尾）会被 flush 到日志，不丢数据。

        性能：逐行 open/write/close 在大量输出时开销高（评审 H-issue）。
        改为攒批写入——每 512 行或 64KB 落盘一次；流结束时强制 flush。

        on_output（task_observability 任务 2）：每行输出的同步回调，
        供执行中进度推送（ProgressReporter.report 节流后推前端）。
        """

        def _notify(text: str) -> None:
            """进度回调（异常隔离：进度失败不影响日志主流程）。"""
            if on_output is None:
                return
            try:
                on_output(text)
            except Exception:  # noqa: BLE001
                logger.debug("进度回调异常（忽略）", exc_info=True)

        async def read_stream(stream: asyncio.StreamReader | None, prefix: str = "") -> None:
            """读取流并批量写入日志，按 \\n 切行 + 字节级半行缓存。"""
            if stream is None:
                # create_subprocess_exec 恒 PIPE stdout/stderr；缺管道显式失败不静默
                raise ValueError("子进程流未建管道（PIPE 缺失）")
            pending = b""  # 上次 read 剩下的半行（字节级，防多字节字符跨块）
            batch: list[str] = []  # 攒批缓冲（行）
            batch_bytes = 0  # 攒批缓冲（字节数）

            async def flush_batch() -> None:
                """批量落盘（攒批到阈值或流结束时调用）。"""
                nonlocal batch, batch_bytes
                if batch:
                    self._append_to_log(log_file, "".join(batch))
                    batch = []
                    batch_bytes = 0

            while True:
                try:
                    chunk = await stream.read(4096)
                except Exception:
                    break
                if not chunk:
                    # 流结束，flush 残留的半行（如有）
                    if pending:
                        text = EncodingHandler.decode_output_line(pending)
                        line = prefix + text + "\n"
                        batch.append(line)
                        batch_bytes += len(line)
                        _notify(line)
                    break
                # 字节级拼接再切，保证切分点在 \n（ASCII 安全边界）
                full = pending + chunk
                parts = full.split(b"\n")
                # 最后一段可能是半行（无换行符结尾），缓存到下次
                *complete, pending = parts
                for raw in complete:
                    text = EncodingHandler.decode_output_line(raw)
                    line = prefix + text + "\n"
                    batch.append(line)
                    batch_bytes += len(line)
                    _notify(line)
                # 攒批阈值：512 行或 64KB，达到即落盘
                if len(batch) >= 512 or batch_bytes >= 64 * 1024:
                    await flush_batch()

            # 流结束：强制 flush 攒批
            await flush_batch()

        # 同时读取 stdout 和 stderr
        await asyncio.gather(
            read_stream(process.stdout),
            read_stream(process.stderr, "[stderr] "),
        )

        exit_code = await process.wait()

        if pid in self.active_processes:
            self.active_processes[pid].status = "completed" if exit_code == 0 else "error"
            self.active_processes[pid].exit_code = exit_code

        self._append_to_log(log_file, f"\n# Process ended with exit code: {exit_code}\n")

    def _on_output_task_done(self, pid: int, task: asyncio.Task[None]) -> None:
        """输出读取任务完成时的回调。

        _read_output 任务结束 = 进程的 stdout/stderr 流已关闭 = 进程已退出
        （或 stdout/stderr 被对端关闭，但 _read_output 会在 await process.wait()
        后设置 exit_code/status）。此时立即清理 active_processes 中的内存记录，
        释放内存——不再等惰性 >100 触发（那个机制本来是为 running 进程堆积兜底）。

        日志保留上限：正常退出（rc==0）入宽限删除队列（grace 后由看门狗
        tick 删除——保留窗覆盖 execute 返回后的 summary/read_log 磁盘降级
        读取）；非正常退出（error/terminated）保留便于排障；跨会话残留由
        启动清扫兜底（_sweep_stale_logs）。
        """
        if pid in self.active_processes:
            self.active_processes[pid].output_task = None

        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug(f"输出读取任务被取消 | pid={pid}")
        except Exception as e:
            logger.exception(f"输出读取任务异常 | pid={pid} | error={e}")

        # 进程已结束（output task done 且 status 非 running），立即清内存。
        # running 状态的进程不清（防御性：理论上 output task done 时进程必已退出，
        # 但极端情况下流可能提前关闭而进程仍在跑，保守不清，靠看门狗兜底）。
        info = self.active_processes.get(pid)
        if info and info.status in ("completed", "error", "terminated"):
            self.active_processes.pop(pid, None)
            if info.status == "completed":
                self._logs_pending_delete.append((info.log_file, time.monotonic()))
            logger.debug(
                f"进程 {pid} 已结束（status={info.status}），清理内存记录"
                + ("" if info.status == "completed" else f"（日志保留：{info.log_file.name}）")
            )

    async def send_input(self, pid: int, input_text: str) -> tuple[bool, str | None]:  # noqa: PLR0911
        """向进程发送输入。"""
        if pid not in self.active_processes:
            return False, f"进程 {pid} 不存在或已结束"

        proc_info = self.active_processes[pid]
        self._touch_access(pid)

        if proc_info.status != "running":
            return False, f"进程状态为 {proc_info.status}，无法接受输入"

        if not proc_info.process:
            return False, "进程对象不可用，可能已被回收"

        handler = InputHandler()
        success, error, formatted = handler.process(input_text)

        if not success:
            return False, error

        stdin = proc_info.process.stdin
        if stdin is None:
            return False, "进程标准输入已关闭"

        try:
            stdin.write(formatted.encode("utf-8"))
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            return False, f"发送输入失败（管道已断开）: {e}"
        except Exception as e:
            return False, f"发送输入失败: {e}"

        self._log_input(proc_info.log_file, input_text, handler)
        return True, None

    @staticmethod
    def _log_input(log_file: Path, input_text: str, handler: InputHandler) -> None:
        """记录输入到日志（敏感信息掩码）。"""
        is_sensitive, masked = handler.check_sensitive(input_text)
        log_entry = f"\n# [INPUT] {'*' * 8 if is_sensitive else masked}\n"
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(log_entry)
        except OSError as exc:
            logger.debug("输入记录写入失败（不影响命令执行） | file=%s | error=%s", log_file, exc)

    async def terminate_process(self, pid: int, force: bool = False) -> tuple[bool, str | None]:
        """终止进程及其整棵进程树。

        优先用进程所属 backend 的 kill（整树杀）；本地无 backend 的旧进程
        回退到 LocalProcessBackend 的 psutil 整树杀。
        """
        if pid not in self.active_processes:
            return False, f"进程 {pid} 不存在"

        proc_info = self.active_processes[pid]

        if proc_info.status != "running" or not proc_info.process:
            return False, "进程未在运行"

        try:
            # 整树杀：防止孙子进程(cargo/rustc/cc)变孤儿继续跑。
            # 本地进程走 LocalProcessBackend(psutil 递归杀树)。
            # 容器进程用容器内 pid（metadata['container_pid']），不是 host pid。
            backend = proc_info.backend or _get_local_backend()
            container_pid = proc_info.metadata.get("container_pid")
            target_pid = container_pid if container_pid is not None else proc_info.process.pid
            unit = WorkUnit(
                pid=target_pid,
                command=proc_info.command,
                metadata=proc_info.metadata,
            )
            await backend.kill(unit, force=force)

            try:
                await asyncio.wait_for(proc_info.process.wait(), timeout=5.0)
            except TimeoutError:
                if not force:
                    proc_info.process.kill()
                    await proc_info.process.wait()

            proc_info.status = "terminated"
            self._append_to_log(proc_info.log_file, "\n# Process terminated by user\n")
            return True, None

        except ProcessLookupError:
            proc_info.status = "terminated"
            return True, None
        except Exception as e:
            return False, f"终止进程失败: {str(e)}"

    def get_process_info(self, pid: int) -> ProcessInfo | None:
        """获取进程信息，顺便清理已完成进程"""
        self._cleanup_if_needed()
        info = self.active_processes.get(pid)
        if info is not None and info.status == "running":
            self._touch_access(pid)
            self._sync_poll_process(info)
        return info

    def get_output(self, pid: int) -> str:
        """获取进程原始输出。

        进程活跃时从 active_processes 读；进程已清（即时清理）时降级走磁盘
        （read_log_by_pid）——日志文件是真理来源，active_processes 只是缓存。
        """
        self._cleanup_if_needed()
        proc_info = self.active_processes.get(pid)
        if not proc_info:
            # 进程已被即时清理，从磁盘日志读
            file_data = self.read_log_by_pid(pid)
            return file_data["output"] if file_data else ""
        if proc_info.status == "running":
            self._touch_access(pid)
            self._sync_poll_process(proc_info)
        lines = self._read_log_lines(proc_info.log_file)
        if lines is None:
            return f"⚠️ 日志读取失败（IO 错误），不代表进程无输出 | log={proc_info.log_file}"
        raw_output = "\n".join(lines)
        raw_output = raw_output.replace("\x00", "")
        return raw_output

    async def wait_output_settled(self, pid: int, timeout: float = 5.0) -> None:
        """等待输出读取任务收尾，再放行 summary/output 读取。

        轮询路径的 _sync_poll_process 同步探测退出（returncode/waitpid），在快
        命令 + 事件循环高负载下会抢在 _read_output 把输出 flush 落盘之前把
        status 置为 completed——完成路径随即读 summary/output 拿到空内容。
        读取前显式等输出任务收尾；任务已清（即时清理竞态先到）或超时则直接
        放行，磁盘日志是兜底真理源。
        """
        proc_info = self.active_processes.get(pid)
        task = proc_info.output_task if proc_info else None
        if task is None or task.done():
            return
        try:
            await asyncio.wait({task}, timeout=timeout)
        except Exception:
            logger.debug("drain output task wait failed (pid=%s)", pid, exc_info=True)

    def get_summary(self, pid: int) -> dict[str, Any] | None:
        """获取进程摘要。

        进程活跃时从 active_processes 读；进程已被即时清理（_on_output_task_done
        在进程结束时触发）时降级走磁盘（read_log_by_pid）。

        关键：execute/continue 的轮询循环与 _on_output_task_done 回调存在竞态——
        快命令（如 echo）可能在轮询循环调 get_summary 前就被清理掉。此处降级
        保证竞态下也能拿到 summary，不再返回 None 触发 SUMMARY_ERROR。
        """
        self._cleanup_if_needed()
        proc_info = self.active_processes.get(pid)
        if not proc_info:
            # 进程已被即时清理，从磁盘日志降级读（与 get_output 对齐）
            return self._summary_from_disk(pid)
        if proc_info.status == "running":
            self._touch_access(pid)
            self._sync_poll_process(proc_info)
        # 摘要只取尾部窗口（大日志全量读会拖慢 0.5s 轮询循环）
        lines = self._read_tail_lines(proc_info.log_file)
        summary = self.log_compressor.compress(lines, proc_info.command)
        elapsed = time.time() - proc_info.start_time
        # 长任务无输出时显式告警，不静默成"0行"。
        # 子进程 stdout 块缓冲（cargo/gcc 等）下，readline 长时间读不到行，
        # LogCompressor 拿到空 lines → 平淡输出 "[0行]"，LLM 会误判为"正常无输出"。
        # 这里在 elapsed>15s 且 total_lines==0 时插入告警，让 LLM 知道是
        # 日志链路问题（块缓冲/未落盘），不代表进程卡死。
        if summary.total_lines == 0 and elapsed > 15:
            summary.lines.insert(
                0,
                f"⚠️ 已运行 {int(elapsed)}s 无输出（子进程 stdout 块缓冲或日志未落盘，不代表卡死）",
            )
        return {
            "pid": pid,
            "status": proc_info.status,
            "elapsed_seconds": round(elapsed, 1),
            "summary": summary.lines,
            "log_file": str(proc_info.log_file),
            "total_lines": summary.total_lines,
            "output_type": summary.output_type.value,
            "warnings": summary.warnings,
            "errors": summary.errors,
            "error_lines": summary.error_lines,
            "progress": summary.progress,
            "latest_message": summary.latest_message,
            "exit_code": proc_info.exit_code,
        }

    def _summary_from_disk(self, pid: int) -> dict[str, Any] | None:
        """从磁盘日志合成 summary（get_summary 的降级路径）。

        进程已被即时清理后，active_processes 查不到，但磁盘日志仍在。
        用 read_log_by_pid 读内容 + 从尾部解析 exit_code，合成 get_summary
        的返回结构。磁盘日志无 start_time，elapsed 无法精确计算，保守填 0。
        """
        file_data = self.read_log_by_pid(pid)
        if file_data is None:
            return None
        return {
            "pid": pid,
            "status": "completed",  # 能从磁盘读到的都是已结束的
            "elapsed_seconds": 0,  # 磁盘日志无 start_time，无法精确
            "summary": file_data["summary"],
            "log_file": file_data["log_file"],
            "total_lines": file_data["total_lines"],
            "output_type": "general",
            "warnings": file_data["warnings"],
            "errors": file_data["errors"],
            "error_lines": file_data.get("error_lines", []),
            "progress": None,
            "latest_message": "",
            "exit_code": file_data.get("exit_code"),
        }

    def read_log_by_pid(self, pid: int) -> dict[str, Any] | None:
        """按 pid 从磁盘读日志文件（read_log 的降级路径）。

        进程结束被清理后，active_processes 查不到 pid，但磁盘日志文件
        bash_<pid>.log 仍在。read_log action 用此方法按 pid 算路径读磁盘，
        完全不依赖 active_processes，所以任何时候都能用。

        Args:
            pid: execute/continue 返回的 pid

        Returns:
            dict（含 output/summary/warnings/errors/command），文件不存在返回 None。
        """
        log_file = self.log_dir / f"bash_{pid}.log"
        if not log_file.exists():
            return None

        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                all_lines = [line.rstrip("\n\r") for line in f]
        except OSError as e:
            logger.warning(f"日志文件读取失败（IO） | file={log_file} | error={e}")
            raise ProcessLogReadError(f"{log_file}: {e}") from e

        # 从头部解析 command / owner（# Command: xxx / # Owner: xxx），
        # 供 LogCompressor 推断输出类型 + 磁盘降级路径的越权校验。
        # 头部只在文件开头几行，遍历前 32 行即可，无需全量扫描。
        command = ""
        owner: str | None = None
        header_window = all_lines[:32]
        for line in header_window:
            if line.startswith("# Command:"):
                command = line[len("# Command:") :].strip()
            elif line.startswith("# Owner:"):
                owner = line[len("# Owner:") :].strip() or None

        content_lines = [line for line in all_lines if not line.startswith("#")]

        # 从尾部解析 exit_code（# Process ended with exit code: N）。
        # 进程已被即时清理后，exit_code 无法从内存读，只能从日志尾部解析。
        exit_code: int | None = None
        for line in reversed(all_lines):
            if "# Process ended with exit code:" in line:
                try:
                    exit_code = int(line.rsplit(":", 1)[-1].strip())
                except ValueError:
                    pass
                break

        # 摘要压缩只喂尾部窗口（评审 H-issue：避免大日志全量读）
        tail_window = content_lines[-self.TAIL_SUMMARY_LINES :]
        summary = self.log_compressor.compress(tail_window, command)
        output = "\n".join(content_lines).replace("\x00", "")
        return {
            "output": output,
            "summary": summary.lines,
            "warnings": summary.warnings,
            "errors": summary.errors,
            "error_lines": summary.error_lines,
            "total_lines": len(content_lines),  # 全量行数（压缩窗口可能截断）
            "command": command,
            "log_file": str(log_file),
            "exit_code": exit_code,
            "owner": owner,
        }

    def _cleanup_if_needed(self) -> None:
        """需要时清理（懒惰策略）"""
        MAX_PROCESSES = 100  # noqa: N806

        if len(self.active_processes) > MAX_PROCESSES:
            logger.info(f"进程数超过限制 ({len(self.active_processes)} > {MAX_PROCESSES})，开始清理")
            self.cleanup_finished()
            logger.info(f"清理后进程数: {len(self.active_processes)}")

    def cleanup_finished(self) -> None:
        """清理已完成的进程记录"""
        finished_pids = [
            pid for pid, info in self.active_processes.items() if info.status in ("completed", "error", "terminated")
        ]

        if finished_pids:
            logger.info(f"清理 {len(finished_pids)} 个已完成进程: {finished_pids}")

        for pid in finished_pids:
            del self.active_processes[pid]

    # ── 访问追踪 ──────────────────────────────────────────────────

    def _touch_access(self, pid: int) -> None:
        """记录进程被外部访问（看门狗据此判定是否孤儿）。"""
        info = self.active_processes.get(pid)
        if info is not None:
            info.last_access_time = time.time()

    # ── 看门狗：内存水位驱动 + idle 排序杀 ─────────────────────────
    # 周期采样内存水位，达高水位按 idle(最久没访问)排序杀最闲的，回落即停。
    # 兜底：idle 超 30 分钟无条件杀。判据看 idle 不看 age（活跃进程不杀）。

    def _ensure_watchdog(self) -> None:
        """确保看门狗后台任务在运行（幂等，重复调用安全）。"""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._watchdog_task = loop.create_task(self._watchdog_loop())
            logger.info("[Watchdog] 后台看门狗已启动")
        except RuntimeError:
            # 无事件循环（非异步上下文），跳过
            pass

    async def _watchdog_loop(self) -> None:
        """看门狗主循环：周期巡检，内存高水位时杀最闲进程。"""
        while True:
            await asyncio.sleep(self._watchdog_interval)
            try:
                await self._watchdog_check_once()
            except asyncio.CancelledError:
                logger.info("[Watchdog] 看门狗任务被取消，退出")
                return
            except Exception as e:
                # 看门狗自身异常不能崩，否则失去保护
                logger.error("[Watchdog] 看门狗巡检异常（非致命，继续）: %s", e, exc_info=True)

    async def _watchdog_check_once(self) -> None:
        """单次巡检：内存高水位时按 idle 排序杀最闲进程；idle 超时兜底杀。"""
        # 先删宽限窗到期的正常退出日志（与进程巡检无关，搭车执行）
        self.flush_expired_log_deletes()
        now = time.time()
        # 快照当前 running 进程（迭代中 kill 会修改字典）
        running = [(pid, info) for pid, info in list(self.active_processes.items()) if info.status == "running"]
        if not running:
            return

        # 先同步清理已退出的，避免对死进程做无谓采样/杀
        for _pid, info in running:
            self._sync_poll_process(info)

        live = [(pid, info) for pid, info in running if info.status == "running"]
        if not live:
            return

        # ── 判据0：单进程内存失控 → 某工作单元自身 RSS 超阈值即杀 ──
        # 比系统水位更灵敏：31GB 宿主上单进程吃 2GB 只占 6%，触发不了系统水位，
        # 但该进程自身已远超合理上限(_unit_memory_limit)。按 idle 排序，
        # 只杀超阈值且最久没访问的（活跃的超内存进程先观察，优先杀被遗忘的）。
        await self._cleanup_by_unit_memory(live, now)
        # 杀完后重新快照
        live = [(pid, info) for pid, info in list(self.active_processes.items()) if info.status == "running"]

        # ── 判据1：内存高水位 → 按 idle 排序杀最闲的，回落即停 ──
        if self._memory_backend is not None:
            try:
                mem_ratio = await self._memory_backend.sample_memory()
            except Exception as e:
                logger.warning("[Watchdog] 内存采样失败，跳过水位判据: %s", e)
                mem_ratio = None

            if mem_ratio is not None and mem_ratio >= self._cleanup_high_watermark:
                await self._cleanup_by_idle(live, now)
                # 杀完后重新快照（dict 可能已变），下面的孤儿兜底用新快照
                live = [(pid, info) for pid, info in list(self.active_processes.items()) if info.status == "running"]

        # ── 判据2：孤儿兜底（idle 超 30 分钟无条件杀）──
        for pid, info in live:
            idle_secs = now - info.last_access_time
            if idle_secs >= self._orphan_timeout:
                logger.error(
                    "[Watchdog] 孤儿进程终止 | pid=%s cmd=%.60s | 无访问 %.0fs（阈值 %.0fs）",
                    pid,
                    self._mask_secrets(info.command),
                    idle_secs,
                    self._orphan_timeout,
                )
                await self._watchdog_kill(pid, info, "orphan")

    async def _cleanup_by_unit_memory(self, live: list[tuple[int, ProcessInfo]], now: float) -> None:
        """单进程内存失控清理:某工作单元自身 RSS 超阈值即判为失控候选。

        比系统水位更灵敏——单进程吃 2GB 在大宿主上触发不了系统水位,但该进程
        自身已超 _unit_memory_limit。按 idle 排序,优先杀超阈值且最久没访问的:
        活跃的超内存进程(agent 在用的 dev server)先观察不杀,被遗忘的失控
        build 才杀。best-effort,采样失败跳过该单元。
        """
        if self._unit_memory_limit <= 0:
            return

        # 收集超内存阈值的工作单元(idle 降序)
        over_limit: list[tuple[int, ProcessInfo, int]] = []
        for pid, info in live:
            backend = info.backend
            if backend is None:
                continue
            try:
                unit = WorkUnit(pid=pid, command=info.command)
                rss = await backend.sample_unit_memory(unit)
            except Exception:
                rss = None
            if rss is not None and rss >= self._unit_memory_limit:
                over_limit.append((pid, info, rss))

        if not over_limit:
            return

        # 按 idle 降序（最久没访问的超内存进程先杀）
        over_limit.sort(key=lambda x: now - x[1].last_access_time, reverse=True)
        for pid, info, rss in over_limit:
            logger.warning(
                "[Watchdog] 单进程内存失控清理 | pid=%s cmd=%.60s | RSS=%.0fMB（阈值 %.0fMB）| idle=%.0fs",
                pid,
                self._mask_secrets(info.command),
                rss / 1024 / 1024,
                self._unit_memory_limit / 1024 / 1024,
                now - info.last_access_time,
            )
            await self._watchdog_kill(pid, info, "unit_memory")

    async def _cleanup_by_idle(self, live: list[tuple[int, ProcessInfo]], now: float) -> None:
        """内存高水位时：按 idle(最久没访问)排序，从最闲开始杀，回落低水位即停。

        进入此方法时内存已确认 ≥ high_watermark（调用方已采样判断）。
        第一次直接杀最闲的（已知高），杀完重采样，回落到 low_watermark 即停——
        不一刀切清空，保 2-3G 容几个并发工作单元。
        判据是 idle 不是 age：活跃进程 idle≈0 排最后。
        """
        if self._memory_backend is None:
            return
        # 按 idle 降序（最久没访问的排前）
        by_idle = sorted(live, key=lambda kv: now - kv[1].last_access_time, reverse=True)

        for idx, (pid, info) in enumerate(by_idle):
            # 第一次不采样（调用方已确认高水位）；后续每杀一个重采样判断回落
            if idx > 0:
                try:
                    mem_ratio = await self._memory_backend.sample_memory()
                except Exception:
                    break  # 采样失败，停止本轮清理（避免盲目杀）
                if mem_ratio is None or mem_ratio < self._cleanup_low_watermark:
                    break  # 已回落到低水位，停

            logger.warning(
                "[Watchdog] 内存高水位清理 | 杀最闲进程 pid=%s cmd=%.60s | idle=%.0fs",
                pid,
                self._mask_secrets(info.command),
                now - info.last_access_time,
            )
            await self._watchdog_kill(pid, info, "memory_pressure")

    async def _watchdog_kill(self, pid: int, info: ProcessInfo, reason: str) -> None:
        """看门狗强制终止进程（best-effort，失败仅记日志）。

        优先调进程所属 backend 的 kill（整树杀）；无 backend 则回退 terminate_process。
        """
        try:
            if info.backend is not None:
                # 容器进程用容器内 pid（metadata['container_pid']），不是 host pid
                container_pid = info.metadata.get("container_pid")
                target_pid = container_pid if container_pid is not None else pid
                unit = WorkUnit(
                    pid=target_pid,
                    command=info.command,
                    pgid=info.metadata.get("pgid"),
                    metadata=info.metadata,
                )
                await info.backend.kill(unit, force=True)
                # 本地后端杀完后仍需 reap asyncio Process 并标记状态
                await self._reap_after_kill(pid, info)
            else:
                await self.terminate_process(pid, force=True)
            logger.info("[Watchdog] 已终止 pid=%s reason=%s", pid, reason)
        except Exception as e:
            logger.error("[Watchdog] 终止 pid=%s 失败: %s", pid, e)

    async def _reap_after_kill(self, pid: int, info: ProcessInfo) -> None:
        """backend 整树杀后，reap 本地 asyncio Process 并更新状态。

        状态保真契约（审查 P1-23）：reap 成功（等到退出码）才置 terminated
        并把退出码落 exit_code。优雅等待超时升级强杀一次；强杀收尸仍失败
        时抛原异常交调用方（_watchdog_kill）按「终止失败」记账、保持原状态
        ——吞异常后照置 terminated 会谎报进程已死，未死进程脱离 running
        快照，从此失去看门狗兜底巡检。
        """
        if info.process is not None:
            try:
                exit_code = await asyncio.wait_for(info.process.wait(), timeout=3.0)
            except TimeoutError:
                logger.warning("[Reap] 进程收尸优雅等待超时（3s），升级强杀 | pid=%s", pid)
                try:
                    info.process.kill()
                    exit_code = await info.process.wait()
                except (ProcessLookupError, OSError) as e:
                    logger.error(
                        "[Reap] 进程强杀收尸失败，状态保持未定（不误报 terminated） | pid=%s | error=%s",
                        pid,
                        e,
                    )
                    raise
            info.exit_code = exit_code
        info.status = "terminated"
        self._append_to_log(info.log_file, "\n# Process terminated by watchdog\n")

    # ── 整体关闭（sidecar 卸载/退出）──────────────────────────

    async def shutdown_all(self, force: bool = True) -> int:
        """终止所有活动进程并停止看门狗（sidecar 卸载/退出前调用）。

        由 server.py 的 on_unload 生命周期钩子与 atexit 兜底调用，
        防止 sidecar 被卸载后残留进程变孤儿。best-effort：单进程失败
        仅记日志，不中断整体清理。

        Args:
            force: True=强制杀（SIGKILL/taskkill /F），False=先优雅终止

        Returns:
            成功终止的进程数
        """
        # 退出前把宽限窗到期的正常退出日志删掉（未到期的留盘，下次启动清扫兜底）
        self.flush_expired_log_deletes()
        # 停止看门狗（先停后杀，避免清理过程中被看门狗再次触发）
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass  # 上行刚 cancel() 本任务，等到的就是取消——停止流程预期路径
            except Exception:  # noqa: BLE001
                logger.exception("watchdog task raised during stop")
            self._watchdog_task = None

        pids = [pid for pid, info in list(self.active_processes.items()) if info.status == "running"]
        killed = 0
        for pid in pids:
            try:
                ok, err = await self.terminate_process(pid, force=force)
                if ok:
                    killed += 1
                else:
                    logger.warning("[shutdown_all] 终止失败 pid=%s: %s", pid, err)
            except Exception as e:  # noqa: BLE001
                logger.warning("[shutdown_all] 终止异常 pid=%s: %s", pid, e)
        if killed:
            logger.info("[shutdown_all] 已终止 %d/%d 个活动进程", killed, len(pids))
        return killed

    # ── 进程状态同步检测 ──────────────────────────────────────────

    @staticmethod
    def _sync_poll_process(proc_info: ProcessInfo) -> None:
        """同步 asyncio transport 的退出状态到 ProcessInfo。

        只读 process.returncode（asyncio child watcher 线程写入的权威值）。
        不做 OS 级 waitpid/OpenProcess 探测：Linux 上 waitpid(WNOHANG) 会与
        child watcher 竞争 reap，抢先 reap 后 watcher 线程 ECHILD，asyncio
        硬编码 returncode=255 写回 transport（ThreadedChildWatcher._do_waitpid
        的既定行为），真实退出码被污染成 255；Windows 分支探测命中时还会把
        exit_code 置 None 污染完成路径判断。轮询循环每轮都过事件循环 tick，
        transport 状态最迟下一轮即可见，OS 级探测无实际收益。
        """
        process = proc_info.process
        if process is None:
            return

        rc = process.returncode
        if rc is not None:
            proc_info.exit_code = rc
            proc_info.status = "completed" if rc == 0 else "error"

    # ── WSL 直连支持（路由单源在 shell_route.py）──────────────────

    # ── Windows 路径转 WSL 路径支持 ───────────────────────────────

    # 匹配命令中的 Windows 风格绝对路径，用于在 WSL 执行前自动转换。
    # 支持：D:\path, D:/path, \\?\D:\path, \\wsl$\Distro\path, \\wsl.localhost\Distro\path
    # 不匹配：相对路径、环境变量、网络共享 \\server\share、Unix 路径、URL。
    # 不带引号的 Windows 路径：遇到空格、引号、管道、重定向等 shell 元字符即终止。
    _WIN_UNQUOTED_PATH_CHARS: ClassVar[str] = r'[^\s"\'|&;<>$`]'
    # 带引号的 Windows 路径：引号内允许空格、括号等，只以对应引号终止。
    _WIN_QUOTED_PATH_RE: ClassVar[re.Pattern[str]] = re.compile(
        r'(?P<quote>[\'"])'
        r"(?P<path>"
        r'(?:\\\\\?\\)?[a-zA-Z]:[/\\][^\'"]*?'
        r'|\\\\wsl(?:\.localhost|\$)\\[^\'"]*?'
        r")"
        r"(?P=quote)"
    )
    _WIN_UNQUOTED_PATH_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?P<path>"
        r"\\\\\?\\[a-zA-Z]:[/\\]" + _WIN_UNQUOTED_PATH_CHARS + r"*"
        r"|\\\\wsl(?:\.localhost|\$)\\[^\\]+\\" + _WIN_UNQUOTED_PATH_CHARS + r"*"
        # 第三分支：裸盘符 X:/ 或 X:\。
        # (?<![a-zA-Z]) 负向回查：盘符字母前不能再有字母。
        # 否则会把 URL 当盘符误伤 —— https://sh.rustup.rs 里紧跟 ':' 的 's'
        # 会被当成盘符，整段 URL 被转成 /mnt/s//sh.rustup.rs 而破坏。
        # URL scheme（http/https/ftp/git/ssh/file...）末字母前必有字母，
        # 回查必失败；Windows 盘符前是空格/行首/引号/(/= 等，回查通过。
        # 见 tests/tools/builtin/bash/test_path_conversion.py URL 分组。
        r"|(?<![a-zA-Z])[a-zA-Z]:[/\\]" + _WIN_UNQUOTED_PATH_CHARS + r"*"
        r")"
    )

    # ── Windows 路径转 WSL 路径方法 ───────────────────────────────

    @classmethod
    def _convert_windows_paths_for_wsl(cls, command: str) -> str:
        """将命令中的 Windows 路径自动转换为 WSL 路径。"""
        if not command or platform.system() != "Windows":
            return command

        def _replace_quoted(match: re.Match[str]) -> str:
            quote = match.group("quote")
            path = match.group("path")
            converted = cls._convert_single_windows_path(path)
            return f"{quote}{converted}{quote}"

        def _replace_unquoted(match: re.Match[str]) -> str:
            path = match.group("path")
            return cls._convert_single_windows_path(path)

        # 先处理带引号路径（允许空格），再处理不带引号路径
        result = cls._WIN_QUOTED_PATH_RE.sub(_replace_quoted, command)
        return cls._WIN_UNQUOTED_PATH_RE.sub(_replace_unquoted, result)

    @classmethod
    def _convert_single_windows_path(cls, path: str) -> str:
        """将单个 Windows 路径转换为 WSL 路径。"""
        # Windows 长路径前缀 \\?\D:\path -> D:\path
        if path.startswith("\\\\?\\"):
            path = path[4:]

        # WSL UNC 路径：\\wsl$\Distro\path 或 \\wsl.localhost\Distro\path
        # 格式为 \\server\share\path，share 之后才是 WSL 内部路径
        if path.startswith("\\\\wsl$\\") or path.startswith("\\\\wsl.localhost\\"):
            parts = path.split("\\", 4)
            if len(parts) >= 5:
                inner = "\\".join(parts[4:]).replace("\\", "/")
                return inner if inner.startswith("/") else f"/{inner}"
            return path

        # 盘符路径：D:\path 或 D:/path
        match = re.match(r"^([a-zA-Z]):[/\\](.*)$", path)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2).replace("\\", "/").rstrip("/")
            if rest:
                return f"/mnt/{drive}/{rest}"
            return f"/mnt/{drive}"

        return path
