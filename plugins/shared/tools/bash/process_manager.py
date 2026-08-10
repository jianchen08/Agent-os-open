"""进程管理器"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shlex
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from bash_types import ProcessBackend, ProcessInfo, WorkUnit
from encoding import EncodingHandler
from input_handler import InputHandler
from log_compressor import LogCompressor

logger = logging.getLogger(__name__)


class ProcessManager:
    """进程管理器"""

    def __init__(self, log_dir: Path | None = None):
        """初始化进程管理器"""
        self.log_dir = log_dir or Path("logs/bash")
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
        self._unit_memory_limit: int = int(
            os.environ.get("AO_UNIT_MEMORY_LIMIT_MB", "2048")
        ) * 1024 * 1024
        self._watchdog_task: asyncio.Task | None = None
        # 默认内存后端：本地宿主。容器隔离路径会注入 ContainerProcessBackend。
        # 看门狗策略层只依赖 backend.sample_memory()，不关心进程跑在哪。
        self._memory_backend: ProcessBackend | None = None

    def _generate_log_filename(self, command: str, pid: int) -> str:
        """生成日志文件名（pid 派生）。

        pid 是 LLM 调用 execute/continue 时永远拿到的稳定标识，直接做文件名。
        read_log 只认 pid 即可按规则算路径读磁盘——LLM 不需要额外记 log_file 路径，
        也不依赖 active_processes（进程结束后内存条目被清，但磁盘日志仍在）。

        历史：曾用 `bash_<timestamp>_<cmd>_<hash>.log`，但 LLM 算不出 hash，
        进程结束后无法回看日志。改成 pid 派生后 read_log 任何时候都能用。
        pid 在 OS 层唯一，不会冲突。
        """
        return f"bash_{pid}.log"

    def _write_log_header(self, log_file: Path, command: str, pid: int, owner: str | None = None):
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

    def _append_to_log(self, log_file: Path, content: str):
        """追加内容到日志（容错：日志写入失败不影响命令执行）"""
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(content)
        except OSError as e:
            logger.warning(f"日志追加写入失败（不影响命令执行） | file={log_file} | error={e}")

    def _read_log_lines(self, log_file: Path) -> list[str]:
        """读取日志所有行"""
        if not log_file.exists():
            return []

        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                # 过滤掉注释行
                return [line.rstrip() for line in lines if not line.startswith("#")]
        except Exception:
            return []

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
        except OSError:
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
    ) -> tuple[int, Path]:
        """启动新进程。

        container_id 非空时走容器路径：用 `docker exec` 起进程，命令包装成
        `echo $$; exec <cmd>` 以便从 stdout 第一行读出**容器内 pid**。
        宿主机 process.pid 只是 asyncio 句柄的 key（供 _read_output/wait 用），
        杀进程必须用容器内 pid（存在 ProcessInfo.metadata['container_pid']），
        否则 docker exec kill <host_pid> 在容器 namespace 里查无此 pid。

        owner: 会话身份（内核注入）。写入 ProcessInfo.metadata['owner'] 与
        日志头 `# Owner:`，供 pid 级操作（continue/input/terminate/read_log）
        的越权校验（工具层 _check_owner 执行）。
        """
        effective_log_dir = self._ensure_log_dir(log_dir) if log_dir else self.log_dir
        # 收到外部 log_dir 时同步 self.log_dir——start_process 写入目录与
        # read_log_by_pid/get_summary 降级读取必须用同一目录，否则即时清理后
        # 降级读磁盘找不到文件 → SUMMARY_ERROR（生产 bug 根因）。
        if log_dir is not None:
            self.log_dir = effective_log_dir

        merged_env = {**os.environ, **(env or {})}
        is_windows = platform.system() == "Windows"

        # ===== 容器路径：docker exec 起进程 =====
        if container_id:
            # 包装命令让容器内 sh 自己报 pid，并 exec 成用户命令——这样 $$ 报的
            # pid 就是用户命令本身（单条命令场景，如 cargo build），kill 干净。
            #
            # 复合命令（含 &&/;/||/|/() 等）：POSIX exec 只接受一个简单命令，
            # `exec cmd1 && cmd2` 里 exec 替换为 cmd1 后进程即退出，cmd2 被丢弃
            # → 后续段输出全部丢失（历史 bug：echo "标签" && ls 只返回标签）。
            # 故复合命令自动套 `exec sh -c '<cmd>'`（shlex.quote 整条引用，防
            # double-eval），让控制操作符在内层 sh 全部生效。代价：pid 指向内层 sh
            # 而非用户命令，kill 后 sh 的子进程可能成孤儿（容器销毁时兜底清理）。
            # 单条命令保持 `exec <cmd>`，pid 精准、无孤儿，向后兼容。
            #
            # working_dir 必须是容器内 POSIX 绝对路径（以 / 开头）。BashTool.get_working_dir
            # 返回的是宿主 task workspace（如 D:\myproject\xxx），直接传给
            # `docker exec -w` 会让 OCI 报 "Cwd must be an absolute path" 退 128。
            # 非 POSIX 绝对路径时强制用容器挂载点 /workspace（IsolationManager 约定）。
            container_workdir = working_dir if (working_dir and working_dir.startswith("/")) else "/workspace"
            if self._is_compound_command(command):
                # set -o pipefail 让管道里任一段失败/被信号杀时，退出码反映真实失败
                # （而非管道最后一段的成功码）。如 `cmd | grep` 里 cmd 被 OOM SIGKILL，
                # 旧实现退出码取 grep 的 0 → 工具误报成功。2>/dev/null 兜底：dash/纯 POSIX
                # sh 不支持 pipefail 选项时不报错中断（bash/ash 支持，容器镜像默认满足）。
                # 注意：pipefail 只修管道(|)，不修分号序列（cmd1; cmd2 取 cmd2 码）——
                # 后者是 shell 固有语义，靠 _build_failure_message 的信号提取兜底。
                wrapped = f"echo $$; exec sh -c {shlex.quote('set -o pipefail 2>/dev/null; ' + command)}"
            else:
                wrapped = f"echo $$; exec {command}"
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
            host_pid = process.pid
            # 同步读第一行（容器内 sh 的 $$），拿容器内 pid。
            container_pid = await self._read_container_pid(process)
            # 先启动 process 拿到 host_pid，再按 pid 派生日志文件名（read_log 靠此规则）。
            log_file = effective_log_dir / self._generate_log_filename(command, host_pid)
            self._write_log_header(log_file, command, host_pid, owner=owner)
            output_task = asyncio.create_task(self._read_output(host_pid, process, log_file))
            metadata: dict[str, Any] = {
                "container_id": container_id,
                "container_pid": container_pid,
            }
            if owner is not None:
                metadata["owner"] = owner
            self.active_processes[host_pid] = ProcessInfo(
                pid=host_pid,
                command=command,
                start_time=time.time(),
                log_file=log_file,
                process=process,
                status="running",
                output_task=output_task,
                backend=_get_container_backend(container_id),
                last_access_time=time.time(),
                metadata=metadata,
            )
            self._ensure_watchdog()
            output_task.add_done_callback(lambda t, p=host_pid: self._on_output_task_done(p, t))
            return host_pid, log_file

        # ===== 本地路径：原 WSL/Bash/CMD 分支 =====
        # Shell 检测优先级：
        # 1. WSL 命令 → _start_wsl_process 直连（use_wsl_direct）
        # 2. WSL + bash → wsl -e bash -c（避免 MSYS2 的 $VAR 参数转换 bug）
        # 3. Git Bash → bash -c（MSYS2 bash，有 $VAR 转义问题）
        # 4. CMD → cmd /c（最后手段，无 Unix shell 能力）
        use_bash_msys = is_windows and shutil.which("bash")
        wsl_available = is_windows and shutil.which("wsl")
        # MSYS2 bash 会将命令行参数中的 $VAR 展开为空（参数转换 bug），
        # WSL 的 bash 不存在此问题。因此同时可用时优先 WSL。
        use_wsl_bash = is_windows and wsl_available

        use_wsl_direct = is_windows and self._is_wsl_command(command)

        # WSL 环境下自动将 Windows 路径转换为 WSL 路径
        # 将 D:\path 等 Windows 路径自动转换为 /mnt/d/path，可通过 AO_BASH_WSL_PATH_CONVERT=0 关闭
        path_convert_enabled = os.environ.get("AO_BASH_WSL_PATH_CONVERT", "1") != "0"
        if path_convert_enabled and (use_wsl_direct or use_wsl_bash):
            command = self._convert_windows_paths_for_wsl(command)

        if use_wsl_direct:
            # WSL 直连
            process = await self._start_wsl_process(
                command=command,
                working_dir=working_dir,
                env=merged_env,
            )
        elif use_wsl_bash:
            # WSL -e bash -c：跳过登录 shell，$VAR 正确展开
            if "LANG" not in merged_env:
                merged_env["LANG"] = "en_US.UTF-8"
            # 复合命令加 pipefail（bash 原生支持），让管道失败退出码正确冒泡
            effective_cmd = f"set -o pipefail; {command}" if self._is_compound_command(command) else command
            process = await asyncio.create_subprocess_exec(
                "wsl",
                "-e",
                "bash",
                "-c",
                effective_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=merged_env,
            )
        elif use_bash_msys:
            # MSYS2 Git Bash（有 $VAR 参数转换问题，但作为后备）
            if "LANG" not in merged_env:
                merged_env["LANG"] = "en_US.UTF-8"
            # 复合命令加 pipefail，让管道失败退出码正确冒泡
            effective_cmd = f"set -o pipefail; {command}" if self._is_compound_command(command) else command
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                effective_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=merged_env,
            )
        else:
            # CMD 路径：使用 safe_cmd_encode 确保中文路径在 CMD 中正确编码
            safe_command = EncodingHandler.safe_cmd_encode(command)
            full_command = f'cmd /c "{safe_command}"' if is_windows else command
            process = await asyncio.create_subprocess_shell(
                full_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=merged_env,
            )

        pid = process.pid

        # 先启动 process 拿到 pid，再按 pid 派生日志文件名（read_log 靠此规则）。
        log_file = effective_log_dir / self._generate_log_filename(command, pid)
        # 写入日志头部（命令经掩码；owner 供磁盘降级路径越权校验）
        self._write_log_header(log_file, command, pid, owner=owner)

        # 启动日志读取任务并保存引用
        output_task = asyncio.create_task(self._read_output(pid, process, log_file))

        # 保存进程信息。注入本地后端，使看门狗的单进程内存维度判据生效
        # （sample_unit_memory 用 psutil 查该进程及其后代 RSS，超阈值按 idle 杀）。
        metadata: dict[str, Any] = {}
        if owner is not None:
            metadata["owner"] = owner
        self.active_processes[pid] = ProcessInfo(
            pid=pid,
            command=command,
            start_time=time.time(),
            log_file=log_file,
            process=process,
            status="running",
            output_task=output_task,
            backend=_get_local_backend(),
            last_access_time=time.time(),
            metadata=metadata,
        )
        # 确保看门狗在运行（首次启动进程时启动，幂等）
        self._ensure_watchdog()

        # 添加任务完成回调以清理引用
        output_task.add_done_callback(lambda t, p=pid: self._on_output_task_done(p, t))

        return pid, log_file

    @staticmethod
    def _is_compound_command(command: str) -> bool:
        """检测命令是否为 shell 复合命令（含顶层控制操作符）。

        用于容器路径决定是否套 ``exec sh -c '<cmd>'``：POSIX exec 只接受一个
        简单命令，若用户命令含 ``&&``/``;``/``||``/``|``/``()`` 等控制操作符，
        exec 会替换为第一条后丢弃后续段，导致输出丢失。

        判定规则（命中任一即视为复合）：
        - 顶层（非引号内）出现 ``;`` ``&``（单或双） ``|``（单或双） ``(`` ``)``
          换行、反引号
        - ``$(...)`` 命令替换（顶层或双引号内，与 bash 语义一致）

        单遍字符扫描 + 引号状态机，跳过单/双引号内字符（双引号内仍检测反引号
        和 ``$( ``），处理反斜杠转义。避免把 ``echo "a;b"`` 这类引号内的元字符
        误判为复合。不依赖 shlex（shlex 遇不完整语法会抛异常，不适合轻量判断）。

        fail-safe：宁可误判（多套一层 sh，命令仍正确，仅 pid 精度下降），
        也不漏判（漏判会丢输出，正是要修的 bug）。
        """
        in_single = False  # 单引号内（bash 内无任何转义/展开）
        in_double = False  # 双引号内（仅反引号/$( 生效）
        i = 0
        n = len(command)
        while i < n:
            ch = command[i]
            nxt = command[i + 1] if i + 1 < n else ""
            if in_single:
                if ch == "'":
                    in_single = False
                i += 1
                continue
            if in_double:
                if ch == "\\":
                    i += 2  # 双引号内反斜杠转义下一字符
                    continue
                if ch == '"':
                    in_double = False
                elif ch == "`":  # 双引号内反引号命令替换
                    return True
                elif ch == "$" and nxt == "(":  # $(...) 双引号内仍展开
                    return True
                i += 1
                continue
            # 顶层
            if ch == "\\":
                i += 2  # 转义下一字符
                continue
            if ch == "'":
                in_single = True
                i += 1
                continue
            if ch == '"':
                in_double = True
                i += 1
                continue
            if ch == "`":
                return True
            if ch == "$" and nxt == "(":
                return True
            if ch in (";", "&", "|", "(", ")", "\n"):
                return True
            i += 1
        return False

    @staticmethod
    async def _read_container_pid(process: asyncio.subprocess.Process) -> int:
        """从 docker exec 子进程 stdout 第一行读容器内 pid。

        命令被包装成 `echo $$; exec <cmd>`，第一行即容器内 sh 的 pid。
        读不到（空/非数字）时返回宿主机 process.pid 作为降级——此时
        docker exec kill 会失败但不会误杀，调用方据返回码处理。
        """
        try:
            first_line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
            text = first_line.decode(errors="replace").strip()
            return int(text)
        except (TimeoutError, ValueError, TypeError):
            return process.pid

    async def _read_output(self, pid: int, process: asyncio.subprocess.Process, log_file: Path):
        """异步读取进程输出（块缓冲根治版 + 批量落盘）。

        历史 bug：原用 stream.readline() 等换行符。但 cargo/gcc/make 等编译器
        检测到 stdout 不是 TTY 时切到块缓冲（4-8KB 攒满才 flush），导致长时间
        读不到任何行 → 日志文件为空 → LogCompressor 拿到空 lines → 平淡输出
        "[0行]"，LLM 误判为"正常无输出"。

        根治：改用 stream.read(N) 按字节块读，自己按 \\n 切行 + 字节级半行缓存。
        切分点在 \\n（ASCII 0x0A，永远在多字节 UTF-8 字符外），所以不会切到
        字符中间，decode 在完整字节行上做，EncodingHandler 的多编码 fallback
        链照常生效。

        残留半行（流结束时无换行符结尾）会被 flush 到日志，不丢数据。

        性能：逐行 open/write/close 在大量输出时开销高（评审 H-issue）。
        改为攒批写入——每 512 行或 64KB 落盘一次；流结束时强制 flush。
        """

        async def read_stream(stream, prefix: str = ""):
            """读取流并批量写入日志，按 \\n 切行 + 字节级半行缓存。"""
            pending = b""  # 上次 read 剩下的半行（字节级，防多字节字符跨块）
            batch: list[str] = []  # 攒批缓冲（行）
            batch_bytes = 0  # 攒批缓冲（字节数）

            async def flush_batch():
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
                        batch.append(prefix + text + "\n")
                        batch_bytes += len(batch[-1])
                    break
                # 字节级拼接再切，保证切分点在 \n（ASCII 安全边界）
                full = pending + chunk
                parts = full.split(b"\n")
                # 最后一段可能是半行（无换行符结尾），缓存到下次
                *complete, pending = parts
                for raw in complete:
                    text = EncodingHandler.decode_output_line(raw)
                    batch.append(prefix + text + "\n")
                    batch_bytes += len(batch[-1])
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

        # 等待进程结束
        exit_code = await process.wait()

        # 更新进程状态
        if pid in self.active_processes:
            self.active_processes[pid].status = "completed" if exit_code == 0 else "error"
            self.active_processes[pid].exit_code = exit_code

        # 写入结束标记
        self._append_to_log(log_file, f"\n# Process ended with exit code: {exit_code}\n")

    def _on_output_task_done(self, pid: int, task: asyncio.Task) -> None:
        """输出读取任务完成时的回调。

        _read_output 任务结束 = 进程的 stdout/stderr 流已关闭 = 进程已退出
        （或 stdout/stderr 被对端关闭，但 _read_output 会在 await process.wait()
        后设置 exit_code/status）。此时立即清理 active_processes 中的内存记录，
        释放内存——不再等惰性 >100 触发（那个机制本来是为 running 进程堆积兜底）。

        日志文件保留在磁盘：read_log 按 pid→文件名规则（bash_<pid>.log）随时可读。
        """
        # 清理任务引用
        if pid in self.active_processes:
            self.active_processes[pid].output_task = None

        # 检查任务是否有异常
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
            logger.debug(
                f"进程 {pid} 已结束（status={info.status}），清理内存记录"
                f"（日志保留：{info.log_file.name}）"
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
        except OSError:
            pass

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
        raw_output = "\n".join(lines)
        raw_output = raw_output.replace("\x00", "")
        return raw_output

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
        # [0行] bug 修复：长任务无输出时显式告警，不静默成"0行"。
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
        except OSError:
            return None

        # 从头部解析 command / owner（# Command: xxx / # Owner: xxx），
        # 供 LogCompressor 推断输出类型 + 磁盘降级路径的越权校验。
        # 头部只在文件开头几行，遍历前 32 行即可，无需全量扫描。
        command = ""
        owner: str | None = None
        header_window = all_lines[:32]
        for line in header_window:
            if line.startswith("# Command:"):
                command = line[len("# Command:"):].strip()
            elif line.startswith("# Owner:"):
                owner = line[len("# Owner:"):].strip() or None

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
        tail_window = content_lines[-self.TAIL_SUMMARY_LINES:]
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

    def _cleanup_if_needed(self):
        """需要时清理（懒惰策略）"""
        # 设置最大进程数限制
        MAX_PROCESSES = 100  # noqa: N806

        if len(self.active_processes) > MAX_PROCESSES:
            logger.info(f"进程数超过限制 ({len(self.active_processes)} > {MAX_PROCESSES})，开始清理")
            self.cleanup_finished()
            logger.info(f"清理后进程数: {len(self.active_processes)}")

    def cleanup_finished(self):
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
        now = time.time()
        # 快照当前 running 进程（迭代中 kill 会修改字典）
        running = [(pid, info) for pid, info in list(self.active_processes.items()) if info.status == "running"]
        if not running:
            return

        # 先同步清理已退出的，避免对死进程做无谓采样/杀
        for pid, info in running:
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
        live = [
            (pid, info)
            for pid, info in list(self.active_processes.items())
            if info.status == "running"
        ]

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
                live = [
                    (pid, info)
                    for pid, info in list(self.active_processes.items())
                    if info.status == "running"
                ]

        # ── 判据2：孤儿兜底（idle 超 30 分钟无条件杀）──
        for pid, info in live:
            idle_secs = now - info.last_access_time
            if idle_secs >= self._orphan_timeout:
                logger.error(
                    "[Watchdog] 孤儿进程终止 | pid=%s cmd=%.60s | 无访问 %.0fs（阈值 %.0fs）",
                    pid, self._mask_secrets(info.command), idle_secs, self._orphan_timeout,
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
                pid, self._mask_secrets(info.command), rss / 1024 / 1024,
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
                pid, self._mask_secrets(info.command), now - info.last_access_time,
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
        """backend 整树杀后，reap 本地 asyncio Process 并更新状态。"""
        if info.process is not None:
            try:
                await asyncio.wait_for(info.process.wait(), timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    info.process.kill()
                    await info.process.wait()
                except Exception:  # noqa: BLE001
                    pass
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
        # 停止看门狗（先停后杀，避免清理过程中被看门狗再次触发）
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watchdog_task = None

        pids = [
            pid
            for pid, info in list(self.active_processes.items())
            if info.status == "running"
        ]
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
        """同步检测进程是否已退出，更新 ProcessInfo 状态。"""
        process = proc_info.process
        if process is None:
            return

        # asyncio Process.returncode 可能已被 transport 设置
        rc = process.returncode
        if rc is not None:
            proc_info.exit_code = rc
            proc_info.status = "completed" if rc == 0 else "error"
            return

        # OS 级同步检测
        pid = proc_info.pid
        try:
            if platform.system() == "Windows":
                import _winapi  # noqa: PLC0415

                # SYNCHRONIZE 用于 WaitForSingleObject，QUERY_LIMITED_INFORMATION 获取退出码
                ACCESS = _winapi.SYNCHRONIZE | 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION  # noqa: N806
                handle = _winapi.OpenProcess(ACCESS, False, pid)
                if handle == 0:
                    return  # 无法打开进程，保守处理
                result = _winapi.WaitForSingleObject(handle, 0)
                if result == _winapi.WAIT_OBJECT_0:
                    # 进程已退出
                    proc_info.status = "completed"  # 无法获取精确退出码，保守标记
                    proc_info.exit_code = None
                _winapi.CloseHandle(handle)
            else:
                _pid, _status = os.waitpid(pid, os.WNOHANG)
                if _pid == pid:
                    proc_info.exit_code = os.WEXITSTATUS(_status) if os.WIFEXITED(_status) else 1
                    proc_info.status = "completed" if proc_info.exit_code == 0 else "error"
        except Exception:
            pass

    # ── WSL 直连支持 ──────────────────────────────────────────────

    # WSL 命令匹配模式（wsl 或 wsl.exe 开头）
    _WSL_COMMAND_RE: ClassVar[re.Pattern[str]] = re.compile(r"^\s*wsl(?:\.exe)?(?:\s+|$)", re.IGNORECASE)

    # WSL 自身标志（接受一个值参数，如 -d Ubuntu-20.04）
    _WSL_FLAGS_WITH_VALUE: ClassVar[frozenset[str]] = frozenset(
        {
            "-d",
            "--distribution",
            "-u",
            "--user",
        }
    )

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

    @classmethod
    def _is_wsl_command(cls, command: str) -> bool:
        """检测命令是否为 WSL 调用。"""
        if not cls._WSL_COMMAND_RE.match(command):
            return False
        return shutil.which("wsl") is not None

    @classmethod
    def _parse_wsl_args(cls, command: str) -> list[str]:
        """解析 WSL 命令行参数，始终使用 bash -c 包装以保留 shell 变量展开。"""
        stripped = cls._WSL_COMMAND_RE.sub("", command).strip()
        if not stripped:
            return ["wsl"]

        # 解析 token 以分离 WSL 标志
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            # 复杂 shell 语法（引号、变量等），整个交给 bash -c，使用 -e 跳过登录 shell
            return ["wsl", "-e", "bash", "-c", stripped]

        # 分离 WSL 自身标志
        wsl_opts: list[str] = []
        cmd_start = 0
        while cmd_start < len(tokens):
            t = tokens[cmd_start]
            if t in cls._WSL_FLAGS_WITH_VALUE:
                # -d Ubuntu-20.04 之类：标志 + 值
                wsl_opts.append(t)
                cmd_start += 1
                if cmd_start < len(tokens):
                    wsl_opts.append(tokens[cmd_start])
                    cmd_start += 1
            elif t.startswith("-") and t not in ("-c", "-e", "--exec"):
                # 其他 WSL 标志，不包括 -c/-e（属于后续命令）
                wsl_opts.append(t)
                cmd_start += 1
            else:
                break

        cmd_tokens = tokens[cmd_start:]
        if not cmd_tokens:
            return ["wsl", *wsl_opts] if wsl_opts else ["wsl"]

        # wsl -e bash -c：-e 跳过登录 shell，$VAR 正确展开
        cmd_str = cls._join_for_bash_c(cmd_tokens)
        return ["wsl", *wsl_opts, "-e", "bash", "-c", cmd_str]

    @classmethod
    def _join_for_bash_c(cls, tokens: list[str]) -> str:
        """将 token 列表拼接为 bash -c 的命令字符串。"""
        parts: list[str] = []
        for t in tokens:
            if any(c in t for c in (" ", "\t", "\n")):
                # 含空格 → 需要引号保护
                parts.append(shlex.quote(t))
            else:
                # 不含空格 → 保持原样，保留 $VAR、|、; 等 shell 元字符
                parts.append(t)
        return " ".join(parts)

    async def _start_wsl_process(
        self,
        command: str,
        working_dir: str | None,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        """直接启动 WSL 进程，绕过 cmd.exe 和 bash。"""
        wsl_args = self._parse_wsl_args(command)
        logger.debug("WSL direct exec: %s", wsl_args)

        # 确保 WSL 内的 locale 为 UTF-8
        if "LANG" not in env:
            env["LANG"] = "en_US.UTF-8"

        return await asyncio.create_subprocess_exec(
            *wsl_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )

    # ── Windows 路径转 WSL 路径方法 ───────────────────────────────

    @classmethod
    def _convert_windows_paths_for_wsl(cls, command: str) -> str:
        """将命令中的 Windows 路径自动转换为 WSL 路径。"""
        if not command or platform.system() != "Windows":
            return command

        def _replace_quoted(match: re.Match) -> str:
            quote = match.group("quote")
            path = match.group("path")
            converted = cls._convert_single_windows_path(path)
            return f"{quote}{converted}{quote}"

        def _replace_unquoted(match: re.Match) -> str:
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


class LocalProcessBackend(ProcessBackend):
    """本地宿主进程后端：psutil 递归杀进程树 + 宿主内存采样。

    杀进程树逻辑（原 ProcessManager._kill_process_tree）迁入此处：
    用 psutil 枚举后代叶子→根逐个终止，防孙子进程变孤儿。
    """

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        """psutil 递归杀整棵进程树（叶子→根），防 cargo/rustc 后代变孤儿。"""
        self._kill_tree_sync(unit.pid, force=force)

    @staticmethod
    def _kill_tree_sync(root_pid: int, force: bool) -> None:
        """同步杀树（原 _kill_process_tree，迁自 ProcessManager）。"""
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            return  # psutil 不可用时，交给调用方回退单进程杀

        try:
            parent = psutil.Process(root_pid)
        except psutil.NoSuchProcess:
            return

        # children(recursive=True) 已是叶子→...→根 的稳定顺序（psutil 保证）
        try:
            descendants = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []

        for proc in descendants:
            try:
                proc.kill() if force else proc.terminate()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                continue

        # 最后杀根进程
        try:
            parent.kill() if force else parent.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            pass

    async def sample_memory(self) -> float | None:
        """采样宿主内存使用率（系统级，0~1）。

        用 psutil 的 virtual_memory。返回 None 表示采样不可用。
        """
        try:
            import psutil  # noqa: PLC0415

            mem = psutil.virtual_memory()
            return mem.percent / 100.0
        except Exception:
            return None

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        """采样单个工作单元的内存占用(RSS 字节,含子进程)。

        用 psutil 查该进程及其所有后代的 RSS 之和——单个失控进程(cargo build
        fork 一堆 rustc 各吃内存)即使占系统比例低,自身 RSS 之和也会超阈值。
        这比系统级 sample_memory 对单进程失控灵敏得多。
        """
        try:
            import psutil  # noqa: PLC0415

            root = psutil.Process(unit.pid)
            total_rss = root.memory_info().rss
            for child in root.children(recursive=True):
                try:
                    total_rss += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return total_rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        except Exception:
            return None


# 本地后端单例（ProcessManager 默认后端，terminate_process 回退用它）
_local_backend: LocalProcessBackend | None = None


def _get_local_backend() -> LocalProcessBackend:
    """获取本地进程后端单例。"""
    global _local_backend  # noqa: PLW0603
    if _local_backend is None:
        _local_backend = LocalProcessBackend()
    return _local_backend


class ContainerProcessBackend(ProcessBackend):
    """容器内进程后端：`docker exec <cid> kill <pid>` 单进程杀 + 跳过内存采样。

    与本地后端的关键差异：
    - kill 只杀单 pid，不递归后代（容器内整组杀 kill -9 -- -PGID 会触发
      runc cgroup shim race 导致容器永久卡死，见 docker_provider.py 注释）；
      单进程杀在容器内是安全的，cargo/rustc 子进程在容器内独立，杀错最多
      该 task 重来。
    - sample_memory / sample_unit_memory 返回 None：容器有 cgroup -m OOM 兜底，
      不在宿主机侧采样容器进程内存。
    """

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id

    async def _run_cmd(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        """执行命令（默认真实 docker CLI；测试时替换此方法即可 mock）。

        与 DockerProvider._run_cmd 同签名，便于复用 mock 惯例。
        """
        import subprocess as _sp  # noqa: PLC0415

        proc = _sp.run(args, capture_output=True, timeout=timeout)  # noqa: S603
        return proc.returncode, proc.stdout, proc.stderr

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        """docker exec <cid> sh -c 'kill <sig> <pid>' —— 单进程杀，不整组。

        用 sh -c 包裹走 sh 内建 kill，而非 `docker exec cid kill ...`——
        精简镜像（python:3.11-slim 等）的 PATH 里没有 kill 二进制，直接调会
        `exec: kill: executable file not found`，sh 内建 kill 在所有 POSIX shell 都有。

        unit.metadata['container_id'] 优先（看门狗/terminate 透传），
        否则用后端实例的 self.container_id。缺 container_id 视为编程错误，抛 KeyError。
        """
        container_id = unit.metadata.get("container_id", self.container_id)
        if not container_id:
            raise KeyError("container_id 缺失，无法在容器内杀进程")

        sig = "-9" if force else "-15"
        # sh -c 'kill ...'：unit.pid 是容器内 pid（int），拼进命令安全
        inner_cmd = f"kill {sig} {unit.pid} 2>/dev/null || true"
        try:
            await self._run_cmd(
                ["docker", "exec", container_id, "sh", "-c", inner_cmd],
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            # kill 失败通常意味着进程已退出或 docker daemon 不可达；
            # 进程已退出是正常的，不抛。真实系统错误由上层兜底。
            logger.debug("[ContainerBackend] kill pid=%s 失败（可能已退出）: %s", unit.pid, e)

    async def sample_memory(self) -> float | None:
        """容器场景不做宿主内存采样（靠 docker -m OOM 兜底）。"""
        return None

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        """容器场景不做单进程内存采样（看门狗跳过内存判据，idle 兜底仍生效）。"""
        return None


# 容器后端缓存（按 container_id 单例；同一容器复用同一后端实例）
_container_backends: dict[str, ContainerProcessBackend] = {}


def _get_container_backend(container_id: str) -> ContainerProcessBackend:
    """获取/创建容器进程后端（按 container_id 缓存单例）。"""
    backend = _container_backends.get(container_id)
    if backend is None:
        backend = ContainerProcessBackend(container_id)
        _container_backends[container_id] = backend
    return backend
