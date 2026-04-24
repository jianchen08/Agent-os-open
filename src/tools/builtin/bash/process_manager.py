"""
进程管理器

暴露接口：
- get_process_info(self, pid: int) -> ProcessInfo | None：获取进程信息（含懒惰清理）
- get_output(self, pid: int) -> str：获取进程原始输出（含懒惰清理）
- get_summary(self, pid: int) -> dict[str, Any] | None：获取进程摘要（含懒惰清理）
- cleanup_finished(self)：清理已完成的进程记录
- _cleanup_if_needed(self)：懒惰清理（内部方法，超过100个进程时触发）
- ProcessManager：ProcessManager类
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.builtin.bash.input_handler import InputHandler
from tools.builtin.bash.log_compressor import LogCompressor
from tools.builtin.bash.types import ProcessInfo

logger = logging.getLogger(__name__)


class ProcessManager:
    """
    进程管理器

    管理长时间运行的进程，支持：
    - 进程跟踪
    - 日志记录
    - 输入发送
    - 优雅终止
    """

    def __init__(self, log_dir: Path | None = None):
        """初始化进程管理器"""
        self.log_dir = log_dir or Path("logs/bash")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 活跃进程映射: pid -> ProcessInfo
        self.active_processes: dict[int, ProcessInfo] = {}

        # 日志压缩器
        self.log_compressor = LogCompressor(max_lines=200)

        # 输入处理器
        self.input_handler = InputHandler()

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
        """写入日志头部"""
        header = [
            "# Bash Command Log",
            f"# Command: {command}",
            f"# PID: {pid}",
            f"# Started: {datetime.now(UTC).isoformat()}",
            f"# Platform: {platform.system()}",
            f"# {'='*50}",
            "",
        ]
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("\n".join(header))

    def _append_to_log(self, log_file: Path, content: str):
        """追加内容到日志"""
        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(content)

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

    async def start_process(
        self,
        command: str,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, Path]:
        """启动新进程"""
        # 生成日志文件
        log_filename = self._generate_log_filename(command)
        log_file = self.log_dir / log_filename

        # 确定平台特定的执行方式
        is_windows = platform.system() == "Windows"
        use_bash = is_windows and shutil.which("bash")

        if use_bash:
            # Windows + Git Bash: 直接用 bash -c 执行，支持 heredoc 等 Unix 语法
            process = await asyncio.create_subprocess_exec(
                "bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env={**os.environ, **(env or {})} if env else None,
            )
        else:
            full_command = f'cmd /c "{command}"' if is_windows else command
            process = await asyncio.create_subprocess_shell(
                full_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env={**os.environ, **(env or {})} if env else None,
            )

        pid = process.pid

        # 写入日志头部
        self._write_log_header(log_file, command, pid)

        # 启动日志读取任务并保存引用
        output_task = asyncio.create_task(self._read_output(pid, process, log_file))

        # 保存进程信息（包含任务引用）
        self.active_processes[pid] = ProcessInfo(
            pid=pid,
            command=command,
            start_time=time.time(),
            log_file=log_file,
            process=process,
            status="running",
            output_task=output_task,  # 保存任务引用防止垃圾回收
        )

        # 添加任务完成回调以清理引用
        output_task.add_done_callback(
            lambda t, p=pid: self._on_output_task_done(p, t)
        )

        return pid, log_file

    async def _read_output(self, pid: int, process: asyncio.subprocess.Process, log_file: Path):
        """异步读取进程输出"""
        async def read_stream(stream, prefix: str = ""):
            """读取流并写入日志"""
            while True:
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
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

    async def send_input(self, pid: int, input_text: str) -> tuple[bool, str | None]:
        """向进程发送输入"""
        if pid not in self.active_processes:
            return False, f"进程 {pid} 不存在或已结束"

        proc_info = self.active_processes[pid]

        if proc_info.status != "running":
            return False, f"进程状态为 {proc_info.status}，无法接受输入"

        if not proc_info.process or proc_info.process.stdin is None:
            return False, "进程标准输入不可用"

        # 处理输入
        handler = InputHandler()
        success, error, formatted = handler.process(input_text)

        if not success:
            return False, error

        try:
            # 写入输入
            proc_info.process.stdin.write(formatted.encode("utf-8"))
            await proc_info.process.stdin.drain()

            # 记录到日志（敏感信息会被掩码）
            is_sensitive, masked = handler.check_sensitive(input_text)
            log_entry = f"\n# [INPUT] {'*' * 8 if is_sensitive else masked}\n"
            self._append_to_log(proc_info.log_file, log_entry)

            return True, None

        except Exception as e:
            return False, f"发送输入失败: {str(e)}"

    async def terminate_process(self, pid: int, force: bool = False) -> tuple[bool, str | None]:
        """终止进程"""
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

            # 等待进程结束
            try:
                await asyncio.wait_for(proc_info.process.wait(), timeout=5.0)
            except TimeoutError:
                if not force:
                    # 优雅终止超时，强制终止
                    proc_info.process.kill()
                    await proc_info.process.wait()

            proc_info.status = "terminated"
            self._append_to_log(proc_info.log_file, "\n# Process terminated by user\n")

            return True, None

        except Exception as e:
            return False, f"终止进程失败: {str(e)}"

    def get_process_info(self, pid: int) -> ProcessInfo | None:
        """
        获取进程信息，顺便清理已完成进程

        BUG-FIX-fix_20260324_143000_process_cleanup
        修复: 添加懒惰清理调用，防止进程信息无限增长
        """
        # 先清理
        self._cleanup_if_needed()

        # 再获取
        return self.active_processes.get(pid)

    def get_output(self, pid: int) -> str:
        """
        获取进程原始输出

        BUG-FIX-fix_20260324_143000_process_cleanup
        修复: 添加懒惰清理调用，防止进程信息无限增长
        """
        # 先清理
        self._cleanup_if_needed()

        proc_info = self.active_processes.get(pid)
        if not proc_info:
            return ""

        # 读取日志内容
        lines = self._read_log_lines(proc_info.log_file)
        return "\n".join(lines)

    def get_summary(self, pid: int) -> dict[str, Any] | None:
        """
        获取进程摘要

        BUG-FIX-fix_20260324_143000_process_cleanup
        修复: 添加懒惰清理调用，防止进程信息无限增长
        """
        # 先清理
        self._cleanup_if_needed()

        proc_info = self.active_processes.get(pid)
        if not proc_info:
            return None

        # 读取日志
        lines = self._read_log_lines(proc_info.log_file)

        # 压缩
        summary = self.log_compressor.compress(lines, proc_info.command)

        # 计算运行时间
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
        """
        需要时清理（懒惰策略）

        BUG-FIX-fix_20260324_143000_process_cleanup
        问题根因: cleanup_finished() 方法定义但从未被调用，导致 active_processes 无限增长
        修复方案: 实现懒惰清理策略，在访问进程信息时检查并清理
        影响范围: 内存管理、进程信息查询
        修复日期: 2026-03-24
        """
        # 设置最大进程数限制
        MAX_PROCESSES = 100

        if len(self.active_processes) > MAX_PROCESSES:
            logger.info(f"进程数超过限制 ({len(self.active_processes)} > {MAX_PROCESSES})，开始清理")
            self.cleanup_finished()
            logger.info(f"清理后进程数: {len(self.active_processes)}")

    def cleanup_finished(self):
        """清理已完成的进程记录"""
        finished_pids = [
            pid for pid, info in self.active_processes.items()
            if info.status in ("completed", "error", "terminated")
        ]

        if finished_pids:
            logger.info(f"清理 {len(finished_pids)} 个已完成进程: {finished_pids}")

        for pid in finished_pids:
            del self.active_processes[pid]
