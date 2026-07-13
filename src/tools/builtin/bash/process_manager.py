"""进程管理器"""

from __future__ import annotations

import asyncio
import hashlib
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

from tools.builtin.bash.encoding import EncodingHandler
from tools.builtin.bash.input_handler import InputHandler
from tools.builtin.bash.log_compressor import LogCompressor
from tools.builtin.bash.types import ProcessInfo

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

        # ── 看门狗配置（防止失控进程拖垮系统）──
        # 触发条件（满足任一即杀，后台自动处理，不通知 Agent）：
        #   1. 资源失控：句柄 > HANDLE_THRESHOLD 且连续 HANDLE_GROW_ROUNDS 次采样都在增长
        #      （双条件避免误杀 build：build 飙高后会回落，不满足"持续增长"）
        #   2. 孤儿进程：running 状态超过 ORPHAN_TIMEOUT 秒无任何外部访问
        #      （合法长期进程只要 Agent 周期性 continue/input/read_log 就不会被判孤儿）
        self._watchdog_interval: float = 10.0  # 采样间隔（秒）
        self._handle_threshold: int = 100000  # 句柄绝对阈值
        self._handle_grow_rounds: int = 3  # 连续增长采样次数
        self._orphan_timeout: float = 1800.0  # 孤儿判定：30 分钟无访问
        self._watchdog_task: asyncio.Task | None = None

    def _generate_log_filename(self, command: str) -> str:
        """生成日志文件名"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 提取命令关键词
        cmd_parts = command.strip().split()
        if cmd_parts:
            base_cmd = cmd_parts[0].replace("/", "_").replace("\\", "_")
            if len(base_cmd) > 20:
                base_cmd = base_cmd[:20]
        else:
            base_cmd = "unknown"

        # 添加哈希后缀防止冲突
        hash_suffix = hashlib.md5(f"{command}{time.time()}".encode()).hexdigest()[:6]

        return f"bash_{timestamp}_{base_cmd}_{hash_suffix}.log"

    def _write_log_header(self, log_file: Path, command: str, pid: int):
        """写入日志头部（容错：日志写入失败不影响命令执行）"""
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            header = [
                "# Bash Command Log",
                f"# Command: {command}",
                f"# PID: {pid}",
                f"# Started: {datetime.now(UTC).isoformat()}",
                f"# Platform: {platform.system()}",
                f"# {'=' * 50}",
                "",
            ]
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("\n".join(header))
        except OSError as e:
            logger.warning(f"日志头部写入失败（不影响命令执行） | file={log_file} | error={e}")

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
    ) -> tuple[int, Path]:
        """启动新进程"""
        effective_log_dir = self._ensure_log_dir(log_dir) if log_dir else self.log_dir
        log_filename = self._generate_log_filename(command)
        log_file = effective_log_dir / log_filename

        merged_env = {**os.environ, **(env or {})}
        is_windows = platform.system() == "Windows"

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
            process = await asyncio.create_subprocess_exec(
                "wsl",
                "-e",
                "bash",
                "-c",
                command,
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
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
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

        # 捕获 stdin 原始管道句柄，绕过 asyncio transport 层直写
        # process.stdin._transport._sock 可能被 asyncio 置 None
        stdin_fd = self._capture_stdin_fd(process)

        # 写入日志头部
        self._write_log_header(log_file, command, pid)

        # 启动日志读取任务并保存引用
        output_task = asyncio.create_task(self._read_output(pid, process, log_file))

        # 保存进程信息
        self.active_processes[pid] = ProcessInfo(
            pid=pid,
            command=command,
            start_time=time.time(),
            log_file=log_file,
            process=process,
            status="running",
            output_task=output_task,
            stdin_fd=stdin_fd,
            last_access_time=time.time(),
        )
        # 确保看门狗在运行（首次启动进程时启动，幂等）
        self._ensure_watchdog()

        # 添加任务完成回调以清理引用
        output_task.add_done_callback(lambda t, p=pid: self._on_output_task_done(p, t))

        return pid, log_file

    async def _read_output(self, pid: int, process: asyncio.subprocess.Process, log_file: Path):
        """异步读取进程输出"""

        async def read_stream(stream, prefix: str = ""):
            """读取流并写入日志，使用自适应编码解码。"""
            while True:
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    # 自适应解码：UTF-8 → 系统编码 → replace 兜底
                    text = EncodingHandler.decode_output_line(line)
                    self._append_to_log(log_file, prefix + text)
                except Exception:
                    break

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
        """输出读取任务完成时的回调"""
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
        """终止进程。"""
        if pid not in self.active_processes:
            return False, f"进程 {pid} 不存在"

        proc_info = self.active_processes[pid]

        if proc_info.status != "running" or not proc_info.process:
            return False, "进程未在运行"

        try:
            if force:
                proc_info.process.kill()
            else:
                proc_info.process.terminate()

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
        """获取进程原始输出"""
        self._cleanup_if_needed()
        proc_info = self.active_processes.get(pid)
        if not proc_info:
            return ""
        if proc_info.status == "running":
            self._touch_access(pid)
            self._sync_poll_process(proc_info)
        lines = self._read_log_lines(proc_info.log_file)
        raw_output = "\n".join(lines)
        raw_output = raw_output.replace("\x00", "")
        return raw_output

    def get_summary(self, pid: int) -> dict[str, Any] | None:
        """获取进程摘要"""
        self._cleanup_if_needed()
        proc_info = self.active_processes.get(pid)
        if not proc_info:
            return None
        if proc_info.status == "running":
            self._touch_access(pid)
            self._sync_poll_process(proc_info)
        lines = self._read_log_lines(proc_info.log_file)
        summary = self.log_compressor.compress(lines, proc_info.command)
        elapsed = time.time() - proc_info.start_time
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
            "progress": summary.progress,
            "latest_message": summary.latest_message,
            "exit_code": proc_info.exit_code,
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

    # ── 看门狗：监控失控进程 ──────────────────────────────────────
    # 周期采样所有 running 进程，满足任一条件直接杀（不通知 Agent）：
    #   1. 资源失控：句柄 > 阈值 且 连续 N 次采样都在增长
    #   2. 孤儿进程：running 超 30 分钟无任何外部访问

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
        """看门狗主循环：周期采样 running 进程，发现失控即杀。"""
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
        """单次巡检：扫描所有 running 进程，判定失控并处理。"""
        now = time.time()
        # 快照当前 running 进程（迭代中 terminate 会修改字典）
        running = [(pid, info) for pid, info in list(self.active_processes.items()) if info.status == "running"]
        if not running:
            return

        for pid, info in running:
            # 先同步检测是否已退出（避免对已死进程做无谓采样）
            self._sync_poll_process(info)
            if info.status != "running":
                continue

            # ── 判据1：孤儿进程（无访问超时）──
            idle_secs = now - info.last_access_time
            if idle_secs >= self._orphan_timeout:
                logger.error(
                    "[Watchdog] 孤儿进程终止 | pid=%s cmd=%.60s | 无访问 %.0fs（阈值 %.0fs）| 句柄历史=%s",
                    pid,
                    info.command,
                    idle_secs,
                    self._orphan_timeout,
                    info.handle_samples[-3:],
                )
                await self._watchdog_kill(pid, info, "orphan")
                continue

            # ── 判据2：资源失控（句柄超阈值 + 持续增长）──
            handles = self._sample_handles(pid)
            if handles is not None:
                info.handle_samples.append(handles)
                # 只保留最近 N+1 次采样，防止无限增长
                keep = self._handle_grow_rounds + 1
                if len(info.handle_samples) > keep:
                    info.handle_samples = info.handle_samples[-keep:]

                if self._is_resource_out_of_control(info.handle_samples):
                    logger.error(
                        "[Watchdog] 资源失控进程终止 | pid=%s cmd=%.60s | 句柄=%d（阈值 %d）| 采样历史=%s",
                        pid,
                        info.command,
                        handles,
                        self._handle_threshold,
                        info.handle_samples,
                    )
                    await self._watchdog_kill(pid, info, "resource")
                    continue

    def _sample_handles(self, pid: int) -> int | None:
        """采样进程句柄数。失败返回 None（不作为失控判据）。"""
        try:
            import psutil  # noqa: PLC0415

            p = psutil.Process(pid)
            # num_handles 是 Windows 专属；其他平台用 num_fds 兜底
            if hasattr(p, "num_handles"):
                try:
                    return p.num_handles()
                except Exception:
                    pass
            if hasattr(p, "num_fds"):
                try:
                    return p.num_fds()
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _is_resource_out_of_control(self, samples: list[int]) -> bool:
        """判定资源是否失控：超阈值 且 连续 N 次都在增长。"""
        rounds = self._handle_grow_rounds
        if len(samples) < rounds + 1:
            return False  # 采样不足，不判定
        recent = samples[-(rounds + 1) :]
        if recent[-1] < self._handle_threshold:
            return False  # 没超阈值
        # 连续 rounds 次都在增长
        return all(recent[i] > recent[i - 1] for i in range(1, len(recent)))

    async def _watchdog_kill(self, pid: int, info: ProcessInfo, reason: str) -> None:
        """看门狗强制终止进程（best-effort，失败仅记日志）。"""
        try:
            # 复用现有 terminate 逻辑（含 process.terminate/kill）
            await self.terminate_process(pid, force=True)
            logger.info("[Watchdog] 已终止 pid=%s reason=%s", pid, reason)
        except Exception as e:
            logger.error("[Watchdog] 终止 pid=%s 失败: %s", pid, e)

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

    # ── stdin 直写支持 ────────────────────────────────────────────

    @staticmethod
    def _capture_stdin_fd(process: asyncio.subprocess.Process) -> int | None:
        """从进程对象中捕获 stdin 管道的文件描述符。"""
        try:
            stdin = process.stdin
            if stdin is None:
                return None
            transport = getattr(stdin, "_transport", None)
            if transport is None:
                return None
            sock = getattr(transport, "_sock", None)
            if sock is None or not hasattr(sock, "fileno"):
                return None
            return sock.fileno()
        except Exception:
            return None

    @staticmethod
    def _raw_stdin_write(data: bytes, proc_info: ProcessInfo) -> bool:
        """使用原始管道句柄直写 stdin，完全绕过 asyncio。"""
        fd = proc_info.stdin_fd
        if fd is None:
            return False

        try:
            if platform.system() == "Windows":
                import _winapi  # noqa: PLC0415

                _winapi.WriteFile(fd, data)
            else:
                os.write(fd, data)
            return True
        except OSError:
            # fd 可能已关闭，尝试从 process.stdin 写
            try:
                if proc_info.process and proc_info.process.stdin:
                    proc_info.process.stdin.write(data)
                    return True
            except Exception:
                pass
        return False

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
