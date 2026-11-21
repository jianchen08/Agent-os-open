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
    _get_mxc_native_backend,
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
    _build_mxc_request,
    _map_linux_native_working_dir,
    _map_wsl_working_dir,
    _mxc_executor_argv,
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


class SpawnFirstLineTimeoutError(RuntimeError):
    """spawn 后首行 pid 上报在门控时间内未出现（命令未确认启动）。

    spawn 失败收尾已完成：宿主级杀树（不经后端传输通道）+ 日志留痕。
    attempt=max_attempts 时向上抛出；否则由 ProcessManager 做健康探测与重试。
    """

    def __init__(
        self,
        *,
        attempt: int,
        max_attempts: int,
        timeout: float,
        host_pid: int,
        log_file: Path | None,
    ) -> None:
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.host_pid = host_pid
        self.log_file = log_file
        super().__init__(
            f"spawn 首行超时（attempt {attempt}/{max_attempts}, {timeout:.1f}s, host_pid={host_pid}）"
        )


class BackendUnhealthyError(RuntimeError):
    """执行后端健康探测失败或处于熔断冷却期——快速失败，不再 spawn。

    快速失败是防泄漏的核心：2026-11-17 事故中后端已 wedged，每次调用仍继续
    spawn 新的 ``wsl.exe`` 客户端，4 小时累积 348 个，把会话通道彻底堵死。
    """

    def __init__(self, *, reason: str, backend_key: str) -> None:
        self.reason = reason
        self.backend_key = backend_key
        super().__init__(reason)


class ProcessManager:
    """进程管理器"""

    def __init__(
        self,
        log_dir: Path | None = None,
        *,
        spawn_first_line_timeout: float | None = None,
        spawn_max_attempts: int | None = None,
        backend_unhealthy_cooldown_secs: float | None = None,
    ):
        """初始化进程管理器

        Args:
            log_dir: 命令日志根目录（绝对路径）。缺省 = 项目根锚定的
                ``data/bash_logs``（见 _default_log_root）。单条命令日志再按
                会话键分子目录（_session_log_dir）。
            spawn_first_line_timeout: spawn 后等待首行 pid 上报的门控秒数。
                超时 = 命令未确认启动 → 宿主级杀树 + 探测门控重试。缺省取
                env ``AO_SPAWN_FIRST_LINE_TIMEOUT_SECS``（15s）。
            spawn_max_attempts: spawn 总尝试次数（含首次）。缺省取 env
                ``AO_SPAWN_MAX_ATTEMPTS``（2 = 首次 + 重试 1 次）。
            backend_unhealthy_cooldown_secs: 探测失败后的熔断冷却秒数，冷却期
                内同后端新调用快速失败不再 spawn。缺省取 env
                ``AO_BACKEND_UNHEALTHY_COOLDOWN_SECS``（60s）。
        """
        if log_dir is not None:
            self.log_dir = self._ensure_log_dir(log_dir)
        else:
            self.log_dir = self._ensure_log_dir(self._default_log_root())

        # 活跃进程映射: pid -> ProcessInfo
        self.active_processes: dict[int, ProcessInfo] = {}

        # 日志压缩器
        self.log_compressor = LogCompressor(max_lines=200)

        # 输入处理器
        self.input_handler = InputHandler()

        # ── spawn 门控 + 熔断（2026-11-17 WSL 挂死事故直修）──
        # 首行 pid 上报 = 执行环境已起来。N 秒拿不到 = 传输通道阻塞（如
        # wsl.exe 卡在服务连接），命令未确认启动：宿主级杀树（绝不走
        # backend.kill——那条路要再走一遍已坏的通道），然后健康探测决定
        # 重试（通道恢复）还是快速失败（通道仍坏 → 熔断冷却，防客户端泄漏）。
        self._spawn_first_line_timeout: float = float(
            os.environ.get("AO_SPAWN_FIRST_LINE_TIMEOUT_SECS", "15")
            if spawn_first_line_timeout is None
            else spawn_first_line_timeout
        )
        self._spawn_max_attempts: int = max(
            1,
            int(
                os.environ.get("AO_SPAWN_MAX_ATTEMPTS", "2")
                if spawn_max_attempts is None
                else spawn_max_attempts
            ),
        )
        self._backend_unhealthy_cooldown: float = float(
            os.environ.get("AO_BACKEND_UNHEALTHY_COOLDOWN_SECS", "60")
            if backend_unhealthy_cooldown_secs is None
            else backend_unhealthy_cooldown_secs
        )
        # 后端键 -> 冷却截止时刻（time.time 基准）
        self._backend_unhealthy_until: dict[str, float] = {}

        # ── 看门狗配置（内存水位驱动 + idle 排序杀）──
        # 触发条件（后台自动处理，不通知 Agent）：
        #   1. 内存高水位：采样内存使用率 ≥ high_watermark(85%) → 按 idle(最久没访问)
        #      排序，从最闲的开始杀，每杀一个重采样，回落到 low_watermark(70%) 即停。
        #      不一刀切，保 2-3G 容几个并发工作单元。
        #   2. 孤儿兜底：running 状态超 30 分钟无任何输出 → 无条件杀（ADR
        #      2026-11-07「只杀真失控」：判据 = 无输出时长而非无访问时长，
        #      有持续输出的长任务不被孤儿杀；无输出挂死仍按原超时杀）。
        # 内存压力判据看 idle(now - last_access_time) 不是 age(start_time)：
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
        # 启动时一次性清扫过期命令日志（只扫孤儿：>7 天的残留，含会话子目录）
        self._sweep_stale_logs()

    # 命令日志保留上限：>7 天的 bash_*.log 启动时清扫（只扫孤儿）
    _LOG_SWEEP_MAX_AGE_SECONDS: ClassVar[float] = 7 * 24 * 3600.0
    # 缺省日志根常量（相对项目根/CWD 锚展开为绝对路径，见 _default_log_root）：
    # 捕获面 = 绝对路径持久目录，禁止相对缺省（原缺陷：相对 logs/bash 落 sidecar CWD）
    _DEFAULT_LOG_ROOT_SUBPATH: ClassVar[tuple[str, ...]] = ("data", "bash_logs")
    # 缺省日志根锚点 env（SDK 既有项目根注入面，bridge_client 同源）
    _DEFAULT_LOG_ROOT_ENV: ClassVar[str] = "AGENTOS_PROJECT_ROOT"
    # 会话键目录名长度上限（owner 可能是长 pipeline id）
    _SESSION_KEY_MAX_LEN: ClassVar[int] = 80
    # 击杀后等待输出任务排空（EOF 驱动）的上限：终止行必须落在全部已产生输出之后
    _KILL_DRAIN_TIMEOUT_SECONDS: ClassVar[float] = 5.0

    @classmethod
    def _default_log_root(cls) -> Path:
        """缺省日志根（绝对路径）。

        AGENTOS_PROJECT_ROOT 注入时锚定 ``<项目根>/data/bash_logs``；缺失退化
        构造时刻 CWD 锚定并一次性解析为绝对路径（假设：sidecar 生命周期内 CWD
        稳定——构造即定根，不随命令执行期 CWD 漂移）。
        """
        project_root = os.environ.get(cls._DEFAULT_LOG_ROOT_ENV)
        base = Path(project_root) if project_root else Path.cwd()
        return base.joinpath(*cls._DEFAULT_LOG_ROOT_SUBPATH)

    @staticmethod
    def _session_key(owner: str | None) -> str | None:
        """owner → 会话键目录名（假设：owner 单值即会话键，取自内核注入面）。

        非法字符替换为 _、点串/空值退化为 None（调用方落缺省目录）、超长截断。
        头部 ``# Owner:`` 保留原始 owner 供越权校验，目录名仅作隔离命名。
        """
        if not owner:
            return None
        key = re.sub(r"[^A-Za-z0-9._-]", "_", owner.strip())
        if not key or key in {".", ".."}:
            return None
        return key[: ProcessManager._SESSION_KEY_MAX_LEN]

    def _session_log_dir(self, root: Path, owner: str | None) -> Path:
        """按会话键归位日志目录：键缺失退化到根目录（缺省目录，平铺布局）。

        假设：键缺失退化 = 根目录本身（与历史平铺布局同形，read_log_by_pid
        根目录直查即覆盖），不另设 default 子目录。
        """
        key = self._session_key(owner)
        return root / key if key else root

    def _sweep_stale_logs(self) -> None:
        """清扫超过保留期（7 天）的 bash_*.log（一次性行，带数量汇总）。

        只扫孤儿：根目录 + 各会话子目录下 mtime 超 7 天的残留（跨会话孤儿），
        活跃/近期会话日志不触碰。best-effort：目录不可读/单文件删除失败只告警
        保留现场，不影响命令执行。
        """
        try:
            candidates = [*self.log_dir.glob("bash_*.log"), *self.log_dir.glob("*/bash_*.log")]
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
        # 按会话键归位：日志落 <根>/<会话键>/bash_<pid>.log（键缺失退化根目录）。
        # 三后端一致（ADR 2026-11-07 决策 6）：local/container/wsl_native 的
        # stdout/stderr 均经管道由宿主侧 reader 落盘，捕获路径恒为宿主绝对路径，
        # 容器内 CWD/mount 语义不影响捕获（ContainerProcessBackend 无需路径适配）。
        session_log_dir = self._session_log_dir(effective_log_dir, owner)
        merged_env = {**os.environ, **(env or {})}
        is_windows = platform.system() == "Windows"

        if container_id:
            return await self._spawn_isolated_with_gate(
                command=command,
                working_dir=working_dir,
                container_id=container_id,
                owner=owner,
                session_log_dir=session_log_dir,
                on_output=on_output,
                exec_backend=exec_backend,
            )

        # ===== 本地路径：WSL/Bash/CMD 分支 =====
        command = self._normalize_local_command(command, is_windows)
        process = await spawn_local_process(command, working_dir, merged_env, is_windows)
        return self._register_process(
            command,
            process,
            session_log_dir,
            on_output,
            owner=owner,
            backend=_get_local_backend(),
            metadata=self._local_metadata(owner),
        )

    async def _spawn_isolated_with_gate(
        self,
        *,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        exec_backend: dict[str, Any] | None,
    ) -> tuple[int, Path]:
        """隔离后端 spawn 门控：首行超时 → 宿主级杀树 → 探测门控重试/熔断。

        判据边界：门控只在「首行 pid 上报未到达」时生效（命令未确认启动）。
        一旦收到首行 = 命令已启动，永不自动重试（防副作用双执行）；此类进程
        的收尾仍走既有 terminate/看门狗路径。
        """
        exec_kind = (exec_backend or {}).get("backend")
        # linux_native 无 pid 上报协议（--unshare-pid 内层 pid 宿主不可寻址）；
        # mxc_native 的执行器 stdout/stderr 归 workload、无常驻沙箱 pid 可寻址
        # （一次性 containment 会话）。两者的失败重试都无门控依据，保持单次
        # spawn（重试会让副作用双执行）。
        gate_enabled = exec_kind not in ("linux_native", "mxc_native")
        max_attempts = self._spawn_max_attempts if gate_enabled else 1
        backend_key = self._backend_key(container_id=container_id, exec_backend=exec_backend)

        retry_note: str | None = None
        for attempt in range(1, max_attempts + 1):
            self._raise_if_backend_cooling(backend_key)
            try:
                return await self._dispatch_isolated_spawn(
                    command=command,
                    working_dir=working_dir,
                    container_id=container_id,
                    owner=owner,
                    session_log_dir=session_log_dir,
                    on_output=on_output,
                    exec_backend=exec_backend,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry_note=retry_note,
                )
            except SpawnFirstLineTimeoutError as exc:
                if attempt >= max_attempts:
                    raise
                backend = self._backend_for_spawn(container_id, exec_backend)
                healthy = await backend.probe_health()
                if healthy is not True:
                    self._backend_unhealthy_until[backend_key] = (
                        time.time() + self._backend_unhealthy_cooldown
                    )
                    logger.error(
                        "[Spawn] 首行超时且后端探测未通过（attempt %d/%d host_pid=%s "
                        "probe=%s）→ 快速失败，熔断 %.0fs",
                        exc.attempt,
                        exc.max_attempts,
                        exc.host_pid,
                        healthy,
                        self._backend_unhealthy_cooldown,
                    )
                    raise BackendUnhealthyError(
                        reason=(
                            f"spawn 首行超时（{exc.timeout:.1f}s）且后端健康探测未通过"
                            f"（probe={healthy}）"
                        ),
                        backend_key=backend_key,
                    ) from exc
                logger.warning(
                    "[Spawn] 首行超时（attempt %d/%d host_pid=%s），探测健康 → 重试",
                    exc.attempt,
                    exc.max_attempts,
                    exc.host_pid,
                )
                retry_note = (
                    f"# Spawn retry: previous attempt {exc.attempt}/{exc.max_attempts} "
                    f"timed out after {exc.timeout:.1f}s (host_pid={exc.host_pid}, "
                    f"log={exc.log_file.name if exc.log_file else 'unknown'}); backend probe OK"
                )
        raise AssertionError("spawn 重试循环不可达：max_attempts 保证至少一次尝试")

    async def _dispatch_isolated_spawn(
        self,
        *,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        exec_backend: dict[str, Any] | None,
        attempt: int,
        max_attempts: int,
        retry_note: str | None,
    ) -> tuple[int, Path]:
        """按 exec_backend 分派到具体隔离后端 spawn。"""
        if exec_backend and exec_backend.get("backend") == "wsl_native":
            return await self._start_wsl_native_process(
                command,
                working_dir,
                container_id,
                owner,
                session_log_dir,
                on_output,
                exec_backend,
                attempt=attempt,
                max_attempts=max_attempts,
                retry_note=retry_note,
            )
        if exec_backend and exec_backend.get("backend") == "linux_native":
            return await self._start_linux_native_process(
                command, working_dir, container_id, owner, session_log_dir, on_output, exec_backend
            )
        if exec_backend and exec_backend.get("backend") == "mxc_native":
            return await self._start_mxc_native_process(
                command, working_dir, container_id, owner, session_log_dir, on_output, exec_backend
            )
        return await self._start_container_process(
            command,
            working_dir,
            container_id,
            owner,
            session_log_dir,
            on_output,
            attempt=attempt,
            max_attempts=max_attempts,
            retry_note=retry_note,
        )

    @staticmethod
    def _backend_key(*, container_id: str, exec_backend: dict[str, Any] | None) -> str:
        """熔断键：wsl_native/mxc_native 按后端配置内容，其余按容器 id。"""
        if exec_backend and exec_backend.get("backend") in ("wsl_native", "mxc_native"):
            pairs = repr(sorted((str(k), str(v)) for k, v in exec_backend.items()))
            return f"{exec_backend.get('backend')}:{pairs}"
        return f"container:{container_id}"

    def _backend_for_spawn(
        self, container_id: str, exec_backend: dict[str, Any] | None
    ) -> ProcessBackend:
        """spawn 阶段使用的后端实例（与注册收尾同一单例缓存）。"""
        if exec_backend and exec_backend.get("backend") == "wsl_native":
            return _get_wsl_native_backend(exec_backend)
        if exec_backend and exec_backend.get("backend") == "mxc_native":
            return _get_mxc_native_backend(exec_backend)
        return _get_container_backend(container_id)

    def _raise_if_backend_cooling(self, backend_key: str) -> None:
        """熔断冷却期内直接快速失败（不 spawn，防挂死客户端继续累积）。"""
        until = self._backend_unhealthy_until.get(backend_key, 0.0)
        remaining = until - time.time()
        if remaining > 0:
            raise BackendUnhealthyError(
                reason=f"后端处于探测失败熔断冷却期（剩余 {remaining:.0f}s）",
                backend_key=backend_key,
            )

    async def _kill_host_spawn(
        self, process: asyncio.subprocess.Process, backend: ProcessBackend
    ) -> None:
        """spawn 失败收尾：宿主级杀整棵客户端树（绝不经后端传输通道）。

        先走 backend.kill_host_tree（宿主 psutil/taskkill），再兜底 asyncio
        句柄强杀；任何失败都记日志但不阻断收尾（原始错误由调用方上报）。
        """
        host_pid = process.pid
        try:
            await backend.kill_host_tree(host_pid)
        except Exception as e:  # noqa: BLE001 - 收尾失败不掩盖原始 spawn 错误
            logger.warning("[Spawn] 宿主级杀树失败（host_pid=%s）: %s", host_pid, e)
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except (ProcessLookupError, OSError, TimeoutError) as e:
                logger.error("[Spawn] 宿主客户端强杀收尸失败（host_pid=%s）: %s", host_pid, e)

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
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        *,
        attempt: int = 1,
        max_attempts: int = 1,
        retry_note: str | None = None,
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
            session_log_dir,
            on_output,
            backend=_get_container_backend(container_id),
            metadata_extra={},
            attempt=attempt,
            max_attempts=max_attempts,
            retry_note=retry_note,
        )

    async def _start_wsl_native_process(
        self,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        exec_backend: dict[str, Any],
        *,
        attempt: int = 1,
        max_attempts: int = 1,
        retry_note: str | None = None,
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
            session_log_dir,
            on_output,
            backend=_get_wsl_native_backend(exec_backend),
            metadata_extra={"exec_backend": exec_backend},
            attempt=attempt,
            max_attempts=max_attempts,
            retry_note=retry_note,
        )

    async def _start_linux_native_process(
        self,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        session_log_dir: Path,
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
            session_log_dir,
            on_output,
            owner=owner,
            backend=_get_local_backend(),
            metadata=metadata,
        )

    async def _start_mxc_native_process(
        self,
        command: str,
        working_dir: str | None,
        container_id: str,
        owner: str | None,
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        exec_backend: dict[str, Any],
    ) -> tuple[int, Path]:
        """mxc_native 路径：一次性 MXC 执行器子进程（宿主进程树）。

        argv 形状：``[executor] + executor_args``（``{request}`` 替换为请求
        JSON 路径）。请求由 exec_backend（provider 下发的模式/策略/网络）
        构造；stdout/stderr 由执行器保留给 workload，本侧照常经管道落盘。

        与容器/wsl 路径的关键差异（pid 协议）：无 ``echo $$`` 上报——沙箱是
        一次性 containment 会话，内层 pid 宿主不可寻址；杀灭走宿主侧执行器
        进程树（psutil 整树杀），metadata 不含 container_pid。后台进程语义
        不承诺（MXC runner 前台退出杀后代），长跑任务应走 linux-bash。
        """
        import json as _json  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        request = _build_mxc_request(
            exec_backend,
            command,
            working_dir,
            timeout=0.0,
        )
        request_dir = Path(tempfile.gettempdir()) / "agentos_mxc_requests"
        await asyncio.to_thread(request_dir.mkdir, parents=True, exist_ok=True)
        request_path = request_dir / f"mxc-{container_id}-{time.time_ns()}.json"
        await asyncio.to_thread(
            request_path.write_text, _json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        argv = _mxc_executor_argv(exec_backend, str(request_path))
        logger.info("[MxcNative] exec argv=%r", argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
        except OSError:
            try:
                await asyncio.to_thread(request_path.unlink)
            except OSError:
                pass
            raise
        metadata: dict[str, Any] = {
            "container_id": container_id,
            "exec_backend": exec_backend,
            "mxc_request": str(request_path),
        }
        if owner is not None:
            metadata["owner"] = owner
        return self._register_process(
            command,
            process,
            session_log_dir,
            on_output,
            owner=owner,
            backend=_get_mxc_native_backend(exec_backend),
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
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        backend: ProcessBackend,
        metadata_extra: dict[str, Any],
        *,
        attempt: int = 1,
        max_attempts: int = 1,
        retry_note: str | None = None,
    ) -> tuple[int, Path]:
        """容器/wsl 共用的注册收尾：读容器内 pid、写日志头、登记进程。

        首行门控（2026-11-17 WSL 挂死事故直修）：``echo $$`` 的 pid 行是
        「执行环境已起来」的唯一客观证据。门控超时 = 命令未确认启动 →
        宿主级杀树（不经后端传输通道）+ 日志留痕 + 抛
        ``SpawnFirstLineTimeoutError``，由 ``_spawn_isolated_with_gate``
        决定探测门控重试或快速失败。
        """
        host_pid = process.pid
        # 先按 host pid 派生日志文件名（read_log 靠此规则），失败路径也要留痕。
        log_file = session_log_dir / self._generate_log_filename(command, host_pid)
        container_pid = await self._read_container_pid(
            process, timeout=self._spawn_first_line_timeout
        )
        if container_pid is None:
            self._write_log_header(log_file, command, host_pid, owner=owner)
            self._append_to_log(
                log_file,
                f"\n# Spawn attempt {attempt}/{max_attempts} timed out: "
                f"no pid line within {self._spawn_first_line_timeout:.1f}s\n",
            )
            await self._kill_host_spawn(process, backend)
            self._append_to_log(
                log_file, "# Host client tree killed (transport kill not attempted)\n"
            )
            raise SpawnFirstLineTimeoutError(
                attempt=attempt,
                max_attempts=max_attempts,
                timeout=self._spawn_first_line_timeout,
                host_pid=host_pid,
                log_file=log_file,
            )
        self._write_log_header(log_file, command, host_pid, owner=owner)
        metadata: dict[str, Any] = {
            "container_id": container_id,
            "container_pid": container_pid,
            **metadata_extra,
        }
        if owner is not None:
            metadata["owner"] = owner
        pid, registered_log = self._register_process(
            command,
            process,
            session_log_dir,
            on_output,
            owner=owner,
            backend=backend,
            metadata=metadata,
        )
        # 重试来源留痕必须落在 _register_process 之后：注册收尾会重写日志头
        # （mode=w），写在前面会被覆盖。
        if retry_note:
            self._append_to_log(registered_log, retry_note + "\n")
        return pid, registered_log

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
        session_log_dir: Path,
        on_output: Callable[[str], None] | None,
        backend: Any,
        metadata: dict[str, Any],
        owner: str | None = None,
    ) -> tuple[int, Path]:
        """注册已拉起的进程：派生日志文件、启动读取任务、入活跃表、
        确保看门狗运行。返回 (pid, log_file)。"""
        pid = process.pid

        # 先启动 process 拿到 pid，再按 pid 派生日志文件名（read_log 靠此规则）。
        log_file = session_log_dir / self._generate_log_filename(command, pid)
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
            last_output_time=time.time(),
            metadata=metadata,
        )
        # 确保看门狗在运行（首次启动进程时启动，幂等）
        self._ensure_watchdog()

        # 添加任务完成回调以清理引用
        output_task.add_done_callback(lambda t, p=pid: self._on_output_task_done(p, t))  # type: ignore[misc]

        return pid, log_file

    @staticmethod
    async def _read_container_pid(
        process: asyncio.subprocess.Process, *, timeout: float = 5.0
    ) -> int | None:
        """从 docker/wsl exec 子进程 stdout 第一行读执行环境内 pid。

        命令被包装成 `echo $$; exec <cmd>`，第一行即执行环境内 shell 的 pid。
        读不到（超时/空/非数字）返回 None——**绝不退回宿主 pid**：拿宿主
        pid 去执行环境里 kill 会命中一个数字相同、完全无关的进程（2026-11-17
        事故中该降级已存在，属误杀面）。调用方（首行门控）据 None 走上报+
        宿主级杀树 + 探测门控重试。
        """
        if process.stdout is None:
            # create_subprocess_exec 恒 PIPE；缺管道视为协议不可用
            return None
        try:
            first_line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except TimeoutError:
            return None
        text = first_line.decode(errors="replace").strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _mark_output(self, pid: int) -> None:
        """输出推进即刷新 last_output_time（孤儿判据「无输出时长」的基准）。"""
        info = self.active_processes.get(pid)
        if info is not None:
            info.last_output_time = time.time()

    async def _read_output(
        self,
        pid: int,
        process: asyncio.subprocess.Process,
        log_file: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        """异步读取进程输出（按字节块读 + 逐块落盘）。

        输出按字节块读取（stream.read(N)），不能用 readline：cargo/gcc/make 等
        检测到 stdout 不是 TTY 时切到块缓冲（4-8KB 攒满才 flush），无换行符时
        readline 会永久阻塞。自己按 \\n 切行 + 字节级半行缓存：
        切分点在 \\n（ASCII 0x0A，永远在多字节 UTF-8 字符外），不会切到
        字符中间，decode 在完整字节行上做，EncodingHandler 的多编码 fallback
        链照常生效。

        残留半行（流结束时无换行符结尾）会被 flush 到日志，不丢数据。

        落盘粒度 = 读取块（4KB 级）即刻落盘，不攒批（ADR 2026-11-07 捕获契约）：
        击杀/超时随时可能发生，「已读进内存但未落盘」的窗口必须趋近于零——
        被杀进程日志必须含全部已产生输出（53288 实证：击杀时攒批缓冲丢失）。
        原 512 行/64KB 攒批退役；磁盘写入从「每行 open/close」改为「每块
        open/close」，吞吐量级不变。

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
            """读取流并逐块写入日志，按 \\n 切行 + 字节级半行缓存。"""
            if stream is None:
                # create_subprocess_exec 恒 PIPE stdout/stderr；缺管道显式失败不静默
                raise ValueError("子进程流未建管道（PIPE 缺失）")
            pending = b""  # 上次 read 剩下的半行（字节级，防多字节字符跨块）

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
                        self._append_to_log(log_file, line)
                        _notify(line)
                        self._mark_output(pid)
                    break
                # 字节级拼接再切，保证切分点在 \n（ASCII 安全边界）
                full = pending + chunk
                parts = full.split(b"\n")
                # 最后一段可能是半行（无换行符结尾），缓存到下次
                *complete, pending = parts
                if complete:
                    lines = [prefix + EncodingHandler.decode_output_line(raw) + "\n" for raw in complete]
                    self._append_to_log(log_file, "".join(lines))
                    for line in lines:
                        _notify(line)
                    self._mark_output(pid)

        # 同时读取 stdout 和 stderr
        await asyncio.gather(
            read_stream(process.stdout),
            read_stream(process.stderr, "[stderr] "),
        )

        exit_code = await process.wait()

        if pid in self.active_processes:
            info = self.active_processes[pid]
            # 击杀路径（terminate/watchdog）已把状态定为 terminated——reader 收尾
            # 只补退出码，不把终态覆盖回 completed/error（kill 退出码非零会被
            # 误写成 error，终止原因随之失真）。
            if info.status == "running":
                info.status = "completed" if exit_code == 0 else "error"
            info.exit_code = exit_code

        self._append_to_log(log_file, f"\n# Process ended with exit code: {exit_code}\n")

    def _on_output_task_done(self, pid: int, task: asyncio.Task[None]) -> None:
        """输出读取任务完成时的回调。

        _read_output 任务结束 = 进程的 stdout/stderr 流已关闭 = 进程已退出
        （或 stdout/stderr 被对端关闭，但 _read_output 会在 await process.wait()
        后设置 exit_code/status）。此时立即清理 active_processes 中的内存记录，
        释放内存——不再等惰性 >100 触发（那个机制本来是为 running 进程堆积兜底）。

        日志持久性（ADR 2026-11-07 捕获契约）：正常退出（rc==0）与非正常退出
        （error/terminated）一律保留——退出不删，宽限删除机制已退役；跨会话
        残留由启动清扫兜底（_sweep_stale_logs，只扫 >7 天孤儿）。
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
            # mxc_native 一次性请求档随进程收尾清理（执行器启动时已读取）。
            request_path = info.metadata.get("mxc_request")
            if request_path:
                try:
                    Path(str(request_path)).unlink()
                except OSError:
                    pass
            self.active_processes.pop(pid, None)
            logger.debug(f"进程 {pid} 已结束（status={info.status}），清理内存记录（日志保留：{info.log_file.name}）")

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

    async def terminate_process(
        self,
        pid: int,
        force: bool = False,
        reason: str | None = None,
    ) -> tuple[bool, str | None]:
        """终止进程及其整棵进程树。

        优先用进程所属 backend 的 kill（整树杀）；本地无 backend 的旧进程
        回退到 LocalProcessBackend 的 psutil 整树杀。

        Args:
            pid: 进程句柄。
            force: True=强杀（SIGKILL/taskkill /F），False=先优雅终止。
            reason: 真实终止原因（调用方身份穿透到日志终止行，ADR 2026-11-07
                决策 3——self-heal/watchdog 不再统一伪装 user）。None 缺省
                写 "user"，保持既有行为向后兼容。
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
            if container_pid is None and proc_info.metadata.get("container_id"):
                # 容器/wsl 后端缺容器内 pid（协议异常/历史残留）：宿主级杀树。
                # 绝不能把宿主 pid 当容器 pid 传下去——那会在执行环境里命中
                # 一个数字相同、完全无关的进程（误杀面）。
                await backend.kill_host_tree(proc_info.process.pid)
            else:
                target_pid = (
                    container_pid if container_pid is not None else proc_info.process.pid
                )
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
            # 击杀后先排空已捕获输出（EOF 驱动，有界），再写终止行：
            # 日志序 = 已产生输出在前、终止行收尾（53288 场景根修）。
            await self._drain_output_before_terminate_line(proc_info)
            self._append_to_log(
                proc_info.log_file,
                f"\n# Process terminated by {self._sanitize_reason(reason)}\n",
            )
            return True, None

        except ProcessLookupError:
            proc_info.status = "terminated"
            return True, None
        except Exception as e:
            return False, f"终止进程失败: {str(e)}"

    @staticmethod
    def _sanitize_reason(reason: str | None) -> str:
        """终止原因单行化：缺省 user（向后兼容），换行清洗防日志结构注入。"""
        actor = (reason or "user").replace("\n", " ").replace("\r", " ").strip()
        return actor or "user"

    async def _drain_output_before_terminate_line(self, info: ProcessInfo) -> None:
        """击杀收尾前等待输出读取任务排空（EOF 驱动，有界）。

        进程被杀 → 管道 EOF → _read_output 把余量与攒留半行落盘后结束。
        本等待保证终止行落在全部已产生输出之后；超时放行（best-effort），
        极端情况下后续输出可能追加在终止行之后，告警留痕。
        """
        task = info.output_task
        if task is None or task.done():
            return
        done, _pending = await asyncio.wait({task}, timeout=self._KILL_DRAIN_TIMEOUT_SECONDS)
        if task not in done:
            logger.warning(
                "击杀后输出排空超时（%.0fs），终止行先落、后续输出可能追加其后 | pid=%s",
                self._KILL_DRAIN_TIMEOUT_SECONDS,
                info.pid,
            )

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

    def _find_log_file(self, pid: int) -> Path | None:
        """按 pid 定位磁盘日志：先根目录（键缺失退化布局/历史数据），再各会话子目录。

        pid 在 OS 层会复用，理论存在跨会话同号：sorted 取字典序首个（假设：
        同 pid 双日志极罕见，且任一命中都比「找不到」更接近真相——头部 Owner
        仍供调用方校验身份）。
        """
        flat = self.log_dir / f"bash_{pid}.log"
        if flat.exists():
            return flat
        try:
            matches = sorted(self.log_dir.glob(f"*/bash_{pid}.log"))
        except OSError as e:
            logger.warning(f"日志定位失败（会话子目录扫描） | dir={self.log_dir} | error={e}")
            return None
        return matches[0] if matches else None

    def read_log_by_pid(self, pid: int) -> dict[str, Any] | None:
        """按 pid 从磁盘读日志文件（read_log 的降级路径）。

        进程结束被清理后，active_processes 查不到 pid，但磁盘日志文件
        bash_<pid>.log 仍在（按会话键子目录组织，_find_log_file 负责定位）。
        read_log action 用此方法按 pid 定位读磁盘，完全不依赖
        active_processes，所以任何时候都能用。

        Args:
            pid: execute/continue 返回的 pid

        Returns:
            dict（含 output/summary/warnings/errors/command/terminated_by），
            文件不存在返回 None。
        """
        log_file = self._find_log_file(pid)
        if log_file is None:
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

        # 从尾部解析 exit_code 与真实终止行（# Process ended with exit code: N /
        # # Process terminated by <reason>）。进程已被即时清理后无法从内存读，
        # 只能从日志尾部解析——被杀进程的终止原因如实回传（ADR 2026-11-07 决策 3）。
        exit_code: int | None = None
        terminated_by: str | None = None
        for line in reversed(all_lines):
            if terminated_by is None and line.startswith("# Process terminated by "):
                terminated_by = line[len("# Process terminated by ") :].strip() or None
            if "# Process ended with exit code:" in line:
                try:
                    exit_code = int(line.rsplit(":", 1)[-1].strip())
                except ValueError:
                    pass
            if exit_code is not None and terminated_by is not None:
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
            "terminated_by": terminated_by,
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
    # 兜底：无输出超 30 分钟无条件杀（判据看 last_output_time 不看访问时间，
    # ADR 2026-11-07「只杀真失控」：有持续输出的长编译不被孤儿杀）。

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
        """单次巡检：内存高水位时按 idle 排序杀最闲进程；无输出超时兜底杀。"""
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

        # ── 判据2：孤儿兜底（无输出超 30 分钟无条件杀）──
        # 判据看「无输出时长」（ADR 2026-11-07：只杀真失控）不是「无访问时长」：
        # 有持续输出的长任务（cargo build）不被孤儿杀；无输出挂死（即使 agent
        # 还在轮询访问）仍按原超时杀。内存压力击杀（判据0/1）保留 idle 排序不变。
        for pid, info in live:
            quiet_secs = now - self._last_output_ts(info)
            if quiet_secs >= self._orphan_timeout:
                logger.error(
                    "[Watchdog] 孤儿进程终止 | pid=%s cmd=%.60s | 无输出 %.0fs（阈值 %.0fs）",
                    pid,
                    self._mask_secrets(info.command),
                    quiet_secs,
                    self._orphan_timeout,
                )
                await self._watchdog_kill(pid, info, "orphan")

    @staticmethod
    def _last_output_ts(info: ProcessInfo) -> float:
        """无输出时长基准：last_output_time（产出过输出 = 最后输出时刻）。

        0.0（从未产出过输出）退化 start_time——从启动时刻起算挂死时长。
        """
        return info.last_output_time if info.last_output_time > 0 else info.start_time

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
                if container_pid is None and info.metadata.get("container_id"):
                    # 缺容器内 pid（协议异常/历史残留）：宿主级杀树，绝不用宿主
                    # pid 冒充容器 pid（会在执行环境里误杀同号无关进程）。
                    await info.backend.kill_host_tree(pid)
                else:
                    target_pid = container_pid if container_pid is not None else pid
                    unit = WorkUnit(
                        pid=target_pid,
                        command=info.command,
                        pgid=info.metadata.get("pgid"),
                        metadata=info.metadata,
                    )
                    await info.backend.kill(unit, force=True)
                # 本地后端杀完后仍需 reap asyncio Process 并标记状态
                await self._reap_after_kill(pid, info, reason)
            else:
                # 回退路径经 terminate_process 收尾：真实原因穿透（watchdog+具体原因），
                # 终止行不再伪装 user（ADR 2026-11-07 决策 3）。
                await self.terminate_process(pid, force=True, reason=f"watchdog ({reason})")
            logger.info("[Watchdog] 已终止 pid=%s reason=%s", pid, reason)
        except Exception as e:
            logger.error("[Watchdog] 终止 pid=%s 失败: %s", pid, e)

    async def _reap_after_kill(self, pid: int, info: ProcessInfo, reason: str) -> None:
        """backend 整树杀后，reap 本地 asyncio Process 并更新状态。

        Args:
            reason: 看门狗击杀具体原因（orphan / unit_memory / memory_pressure），
                穿透到日志终止行（终止行 = ``# Process terminated by watchdog
                (<reason>)``，如实标注不再伪装 user）。

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
        # 击杀后先排空已捕获输出再写终止行（与 terminate_process 同一契约）
        await self._drain_output_before_terminate_line(info)
        self._append_to_log(info.log_file, f"\n# Process terminated by watchdog ({reason})\n")

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
                # 终止行如实（reason=shutdown），不伪装 user
                ok, err = await self.terminate_process(pid, force=force, reason="shutdown")
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
