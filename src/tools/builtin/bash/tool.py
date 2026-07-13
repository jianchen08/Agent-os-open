"""
增强版 Bash 命令执行工具

提供：
- 支持长时间运行的进程（30秒阈值 + 回调机制）
- 支持交互式输入（确认、密码等）
- 智能日志压缩（3-5行摘要）
- 自适应编码转换（Windows CMD GBK / Git Bash UTF-8 自动识别）

⚠️ 安全威胁模型（H3）
=================

bash 工具的设计语义就是"执行用户命令"，**无法靠输入过滤根治**——
shell 元字符（| $() `` ;）天然有效，黑名单不可穷举（python -c "import os;
os.system('...')" 即绕过 SecurityChecker）。

SecurityChecker 的正则黑名单只拦**不可逆灾难**（rm -rf /、mkfs、dd 等
手滑即无法挽回的操作），**不是安全边界**。curl | sh 这类"危险但合法"的
模式不在此层硬拦，而是降级为 warning + 管道层审批。真正的控制是**隔离**：

- 可信/本地单用户场景：宿主机执行可接受（SecurityChecker 防误操作即可）。
- 不可信/多租户场景：bash 调用必须经 IsolationCoordinator 路由到容器隔离
  （降权 + 只读根 + 限制能力），宿主机路径对不可信输入 fail-closed。
  容器模式下 SecurityChecker 被跳过是合理的（容器内黑名单无意义）。

暴露接口：
- SecurityChecker：防误操作黑名单（非安全边界，见上文）
- BashTool：工具主类，隔离决策由上层 IsolationCoordinator 统一处理
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from tools.builtin.base import BuiltinTool
from tools.builtin.workspace_aware import WorkspaceAwareMixin
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

from .input_handler import InputHandler
from .process_manager import ProcessManager
from .types import BashAction


class SecurityChecker:
    """命令防误操作检查器（**非安全边界**）。

    用正则黑名单匹配 rm -rf /、fork bomb 等**不可逆灾难**模式，防止手滑
    造成无法挽回的破坏。这是"防误操作"层级，**不能抵御恶意构造的命令**
    ——shell 元字符天然有效，黑名单不可穷举（见模块文档 H3 威胁模型）。
    抵御恶意命令依靠容器隔离，不靠这里。

    分层原则（按"可逆性"而非"看起来像不像黑客"）：
    - DANGEROUS_PATTERNS：**不可逆灾难**硬拦——一旦执行无法挽回（rm -rf、
      mkfs、dd、format、shutdown 等）。硬拦与审批层互不干涉，是最后兜底。
    - CAUTION_PATTERNS：**危险但合法**降级——curl/wget/管道到 shell 等
      是主流工具的官方用法，不应硬拦。命中只标 warning，是否放行交给管道
      层 SecurityCheckPlugin 的审批决策（用户批准即可执行）。

    与管道层的分工：管道层 SecurityCheckPlugin 基于危险工具声明 + 用户审批
    做精细决策；本层只在工具内部兜底挡不可逆灾难，不重复审批层的语义。
    """

    # 不可逆灾难命令：硬拦（一旦执行无法挽回）
    # 注意：管道到 shell（| sh / | bash 等）不在此列——它们是合法常见模式，
    # 由 CAUTION_PATTERNS 降级 + 管道层审批把关，避免"批了还过不去"。
    DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"\brm\s+-rf\b",  # rm -rf（词边界匹配，覆盖 rm -rf / 等所有变体）
        r";\s*rm\b",  # 分号连接 rm
        r";\s*del\b",  # 分号连接 del
        r";\s*format\b",  # 分号连接 format
        r"\bmkfs\b",  # 格式化命令
        r"\bdd\s+if=",  # dd 写入
        r">\s*/dev/sd[a-z]",  # 写入磁盘设备
        r":\(\)\s*\{\s*:\|:&\s*\};:",  # Fork bomb
        r"\bdel\s+/f\s+/s\s+/q\b",  # Windows 强制删除
        r"\brmdir\s+/s\s+/q\b",  # Windows 强制删除目录
        r"\bformat\s+[a-z]:",  # Windows 格式化
        r"\bshutdown\b",  # 关机
        r"\breboot\b",  # 重启
        r"\bpoweroff\b",  # 关机
        r"\bhalt\b",  # 停机
    ]

    # 危险但合法的命令：降级标 warning（不阻断），由管道层审批把关
    # 管道到 shell（rustup/homebrew/nvm 等官方安装方式）、curl/wget 等属此类。
    CAUTION_PATTERNS: ClassVar[list[str]] = [
        "curl",
        "wget",
        "rm ",
        "del ",
        "rmdir",
        "mv ",
        "move ",
        "cp ",
        "copy ",
        ">",
        ">>",
        "$(",  # 命令替换（脚本常用，不应阻断）
        "`",  # 反引号命令替换
        "| sh",  # 管道到 sh（合法常见模式，降级审批而非硬拦）
        "| bash",  # 管道到 bash
        "| zsh",  # 管道到 zsh
        "| fish",  # 管道到 fish
    ]

    def __init__(self, allowed_commands: list[str] | None = None):
        """初始化安全检查器"""
        self.allowed_commands = set(allowed_commands) if allowed_commands else None
        # 预编译正则表达式以提高性能
        self._compiled_dangerous = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]

    def check(self, command: str) -> tuple[bool, bool, str | None]:
        """
        检查命令安全性

        使用正则表达式模式匹配检测危险命令，防止命令注入绕过。

        Returns:
            tuple[bool, bool, str | None]: (是否安全, 是否需要警告, 错误消息)
        """
        cmd_stripped = command.strip()

        # 检查危险命令（使用正则表达式）
        for pattern, compiled in zip(self.DANGEROUS_PATTERNS, self._compiled_dangerous, strict=True):
            if compiled.search(cmd_stripped):
                return False, False, f"命令包含危险操作: {pattern}"

        # 检查白名单
        if self.allowed_commands is not None:
            base_cmd = cmd_stripped.split()[0] if cmd_stripped else ""
            if base_cmd not in self.allowed_commands:
                return False, False, f"命令不在允许列表中: {base_cmd}"

        # 检查需要警告的命令（保持简单字符串匹配）
        cmd_lower = cmd_stripped.lower()
        for pattern in self.CAUTION_PATTERNS:
            if pattern.lower() in cmd_lower:
                return True, True, f"命令包含潜在风险操作: {pattern}"

        return True, False, None


class BashTool(BuiltinTool, WorkspaceAwareMixin):
    """
    增强版 Bash 命令执行工具

    提供：
    - 支持长时间运行的进程（30秒阈值 + 回调机制）
    - 支持交互式输入（确认、密码等）
    - 智能日志压缩（3-5行摘要）

    注意：隔离决策由上层 IsolationCoordinator 统一处理，
    本工具只负责在宿主机上执行命令。
    """

    # 在主事件循环直接执行，避免 to_thread 每次创建独立循环
    # 导致的 execute/input/terminate 跨循环问题
    run_on_main_loop: ClassVar[bool] = True

    # 默认超时时间（秒）
    DEFAULT_TIMEOUT: ClassVar[int] = 30

    # 最大允许超时（秒），不得超过 ToolCore 外层超时 300 秒
    MAX_TIMEOUT: ClassVar[int] = 290

    # 回调触发阈值（秒）
    CALLBACK_THRESHOLD: ClassVar[int] = 30

    # summary 生效的输出行数阈值，短于此值不生成 summary
    SUMMARY_LINE_THRESHOLD: ClassVar[int] = 10

    @staticmethod
    def _compact_result_data(
        pid: int,
        output: str | None,
        summary_obj: dict[str, Any],
        exit_code: int,
    ) -> dict[str, Any]:
        """精简 result_data，去掉 LLM 不需要的字段以节省 token。

        保留策略：
        - pid: LLM 需要 pid 才能调用 continue/terminate/input
        - output: 核心输出
        - exit_code: 始终保留（评估框架 expect 条件依赖此字段）
        - status: 始终保留为 completed（评估框架 expect 条件依赖此字段）
        - summary: 仅长输出（>10行）时保留，短输出直接看 output
        - warnings/errors: 仅非空时保留
        """
        data: dict[str, Any] = {
            "pid": pid,
            "output": output,
            "status": "completed",
            "exit_code": exit_code,
        }

        warnings = summary_obj.get("warnings", [])
        errors = summary_obj.get("errors", [])
        if warnings:
            data["warnings"] = warnings
        if errors:
            data["errors"] = errors

        # 短输出不生成 summary（比 output 本身还长就失去意义）
        output_lines = (output or "").count("\n") + 1 if output else 0
        if output_lines > BashTool.SUMMARY_LINE_THRESHOLD:
            summary_lines = summary_obj.get("summary", [])
            if summary_lines:
                data["summary"] = summary_lines

        return data

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        allowed_commands: list[str] | None = None,
    ):
        """初始化 Bash 工具"""
        self.timeout = timeout

        # 安全组件
        self.security = SecurityChecker(allowed_commands)

        # 进程管理器
        self.process_manager = ProcessManager()

        # 输入处理器
        self.input_handler = InputHandler()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="bash_execute",
            description="执行 Shell 命令，支持长时间运行进程（30秒超时+回调机制）、交互式输入（确认/密码）。"
            "适用场景：执行系统命令（ls/cat/grep/pip/npm等）、运行脚本、查看系统信息、安装依赖、编译构建项目。"
            "不适用场景：仅需读取文件（使用file_read）、仅需搜索文件（使用code_search）、危险操作（rm -rf/format/dd等）、长期运行服务。"
            "注意事项：命令执行默认30秒超时，超时后触发回调机制；timeout参数最大值为290秒，不可超过此限制；"
            "危险命令会被安全检查拦截；Windows和Linux/Mac命令语法可能不同；"
            "需要审批才能执行；长时间运行命令会保存日志到logs/bash/目录；敏感输入会被自动掩码处理。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["execute", "continue", "terminate", "input", "read_log"],
                        "description": "操作类型：execute(执行新命令), continue(继续等待运行中的命令), terminate(终止命令), input(向进程发送输入), read_log(读取命令日志)",
                        "default": "execute",
                    },
                    "command": {
                        "type": "string",
                        "description": "要执行的Shell命令（当action=execute时必需）。例如：ls -la, npm install, python script.py",
                    },
                    "pid": {
                        "type": "integer",
                        "description": "进程ID（当action=continue/terminate/input/read_log时必需，由execute操作返回）",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "命令执行的超时时间（秒），默认30秒。最大值290秒，超过会被截断为290",
                        "default": 30,
                        "maximum": 290,
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "命令执行的工作目录，默认为当前目录",
                    },
                    "input_text": {
                        "type": "string",
                        "description": "要向运行中进程发送的输入文本（当action=input时必需）。例如：yes, 密码等",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "是否强制终止进程（当action=terminate时有效）。强制终止会立即结束进程，可能导致数据丢失",
                        "default": False,
                    },
                },
                "required": [],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["completed", "running", "terminated"]},
                    "exit_code": {"type": "integer"},
                    "output": {"type": "string"},
                },
                "required": ["status"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.USER,
            dangerous_operations=[
                "rm -rf",
                "format",
                "del /q",
                "shutdown",
                "reboot",
                "mkfs",
                "dd if=",
                "> /dev/sd",
                "curl",
                "wget",
            ],
            tags=["bash", "shell", "command", "dangerous", "interactive", "long-running"],
            injected_params=["workspace", "project_root"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行工具"""
        self._init_workspace(inputs)
        action = inputs.get("action", BashAction.EXECUTE)

        # 根据 action 分发到不同处理器
        handlers = {
            BashAction.EXECUTE: self._handle_execute,
            BashAction.CONTINUE: self._handle_continue,
            BashAction.TERMINATE: self._handle_terminate,
            BashAction.INPUT: self._handle_input,
            BashAction.READ_LOG: self._handle_read_log,
        }

        handler = handlers.get(action)
        if not handler:
            return create_failure_result(
                error=f"未知的 action: {action}",
                error_code="INVALID_ACTION",
            )

        return await handler(inputs)

    async def _handle_execute(self, inputs: dict[str, Any]) -> ToolResult:
        """处理 execute 操作"""
        command = inputs.get("command")
        if not command:
            return create_failure_result(
                error="command 不能为空",
                error_code="MISSING_COMMAND",
            )

        # 安全检查：容器隔离模式下跳过内部安全检查
        # 容器内执行已有独立的安全边界，反引号等 shell 特性是正常行为
        # 安全检查由管道层 SecurityCheckPlugin 和 ApprovalDecisionEngine 统一处理
        warning = None
        is_isolated = inputs.get("_isolation_provider") in ("docker", "isolated")
        if not is_isolated:
            is_safe, needs_warning, message = self.security.check(command)
            if not is_safe:
                return create_failure_result(
                    error=f"安全检查失败: {message}",
                    error_code="SECURITY_CHECK_FAILED",
                )
            warning = message if needs_warning else None

        timeout = min(inputs.get("timeout", self.timeout), self.MAX_TIMEOUT)
        wd = self.get_working_dir(inputs)
        working_dir = str(wd) if wd else None

        # 隔离决策由上层 IsolationCoordinator 统一处理
        # bash 工具只负责执行命令，不再自己决定是否隔离
        return await self._execute_command(
            command=command,
            timeout=timeout,
            working_dir=working_dir,
            warning=warning,
        )

    async def _execute_command(
        self,
        command: str,
        timeout: int,
        working_dir: str | None,
        warning: str | None = None,
    ) -> ToolResult:
        """
        统一的命令执行接口

        隔离决策由上层 IsolationCoordinator 统一处理，
        bash 工具只负责在宿主机上执行命令。

        Args:
            command: 要执行的命令
            timeout: 超时时间（秒）
            working_dir: 工作目录
            warning: 警告信息

        Returns:
            ToolResult: 统一格式的执行结果
        """
        return await self._execute_local_unified(
            command=command,
            timeout=timeout,
            working_dir=working_dir,
            warning=warning,
        )

    async def _execute_local_unified(
        self,
        command: str,
        timeout: int,
        working_dir: str | None,
        warning: str | None = None,
    ) -> ToolResult:
        """
        本地执行命令（统一返回格式）

        返回数据结构：
        {
            "status": "completed" | "running" | "terminated",
            "process_id": "12345",  # 统一为字符串
            "pid": 12345,           # 向后兼容，保留整数 pid
            "elapsed": 1.5,
            "output": "命令输出...",
            "summary": ["[800行]", "类型: pip install", "进度: 120/500"],
            "exit_code": 0,
            "warnings": [],
            "errors": [],
            "isolated": False,
        }
        """
        try:
            # 启动进程（传入项目根路径下的 logs/bash 作为日志目录）
            project_root = getattr(self, "_project_root", None)
            bash_log_dir = Path(project_root) / "logs" / "bash" if project_root else None
            pid, log_file = await self.process_manager.start_process(
                command=command,
                working_dir=working_dir,
                log_dir=bash_log_dir,
            )

            # 等待进程完成或超时
            start_time = time.time()

            while True:
                # 检查进程状态（需要在超时检查前获取 proc_info，以便使用 proc_info.start_time）
                proc_info = self.process_manager.get_process_info(pid)

                # 检查是否超时
                elapsed = time.time() - start_time

                if elapsed >= timeout:
                    # 触发回调机制
                    summary = self.process_manager.get_summary(pid)

                    if summary:
                        return create_success_result(
                            data={
                                "status": "running",
                                "pid": pid,
                                "elapsed": round(time.time() - proc_info.start_time, 1),
                                "summary": summary.get("summary", []),
                            },
                            metadata={
                                "action": "execute",
                                "command": command,
                                "warning": warning,
                            },
                        )
                if not proc_info or proc_info.status != "running":
                    break

                # 短暂等待
                await asyncio.sleep(0.5)

            # 进程已完成，获取摘要和输出
            summary = self.process_manager.get_summary(pid)
            output = self.process_manager.get_output(pid)

            if summary:
                exit_code = summary.get("exit_code", 0)
                result_data = self._compact_result_data(
                    pid=pid,
                    output=output,
                    summary_obj=summary,
                    exit_code=exit_code,
                )

                if exit_code != 0:
                    error_msg = (
                        output[-500:]
                        if output and len(output) > 500
                        else (output or f"命令执行失败，退出码: {exit_code}")
                    )
                    return create_failure_result(
                        error=error_msg,
                        error_code="COMMAND_FAILED",
                    )

                return create_success_result(
                    data=result_data,
                    metadata={
                        "action": "execute",
                        "command": command,
                        "warning": warning,
                    },
                )
            return create_failure_result(
                error="无法获取进程摘要",
                error_code="SUMMARY_ERROR",
            )

        except Exception as e:
            return create_failure_result(
                error=f"执行命令失败: {str(e)}",
                error_code="EXECUTION_FAILED",
            )

    async def _handle_continue(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """处理 continue 操作"""
        pid = inputs.get("pid")
        if not pid:
            return create_failure_result(
                error="pid 不能为空",
                error_code="MISSING_PID",
            )

        timeout = inputs.get("timeout", self.timeout)

        # 获取进程信息
        proc_info = self.process_manager.get_process_info(pid)
        if not proc_info:
            return create_failure_result(
                error=f"进程 {pid} 不存在",
                error_code="PROCESS_NOT_FOUND",
            )

        # 如果进程已完成，直接返回结果
        if proc_info.status != "running":
            summary = self.process_manager.get_summary(pid)
            exit_code = proc_info.exit_code
            if exit_code is not None and exit_code != 0:
                return create_failure_result(
                    error=f"命令执行失败，退出码: {exit_code}",
                    error_code="COMMAND_FAILED",
                )
            return create_success_result(
                data={
                    "status": proc_info.status,
                    "pid": pid,
                    "elapsed": summary.get("elapsed_seconds", 0) if summary else 0,
                    "summary": summary.get("summary", []) if summary else [],
                    "exit_code": exit_code,
                },
                metadata={"action": "continue"},
            )

        # 继续等待
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if elapsed >= timeout:
                # 再次触发回调
                summary = self.process_manager.get_summary(pid)

                return create_success_result(
                    data={
                        "status": "running",
                        "pid": pid,
                        "elapsed": round(time.time() - proc_info.start_time, 1),
                        "summary": summary.get("summary", []) if summary else [],
                    },
                    metadata={"action": "continue"},
                )

            # 检查状态
            proc_info = self.process_manager.get_process_info(pid)
            if not proc_info or proc_info.status != "running":
                break

            await asyncio.sleep(0.5)

        # 进程已完成
        summary = self.process_manager.get_summary(pid)
        exit_code = proc_info.exit_code if proc_info else None
        if exit_code is not None and exit_code != 0:
            return create_failure_result(
                error=f"命令执行失败，退出码: {exit_code}",
                error_code="COMMAND_FAILED",
            )

        return create_success_result(
            data={
                "status": "completed",
                "pid": pid,
                "elapsed": summary.get("elapsed_seconds", 0) if summary else 0,
                "summary": summary.get("summary", []) if summary else [],
                "exit_code": exit_code,
            },
            metadata={"action": "continue"},
        )

    async def _handle_terminate(self, inputs: dict[str, Any]) -> ToolResult:
        """处理 terminate 操作"""
        pid = inputs.get("pid")
        if not pid:
            return create_failure_result(
                error="pid 不能为空",
                error_code="MISSING_PID",
            )

        force = inputs.get("force", False)

        # 终止进程
        success, error = await self.process_manager.terminate_process(pid, force)

        if not success:
            return create_failure_result(
                error=error or "终止进程失败",
                error_code="TERMINATE_FAILED",
            )

        # 获取最终摘要
        summary = self.process_manager.get_summary(pid)

        return create_success_result(
            data={
                "status": "terminated",
                "pid": pid,
                "message": "进程已终止" + ("（强制）" if force else ""),
                "summary": summary.get("summary", []) if summary else [],
            },
            metadata={"action": "terminate", "force": force},
        )

    async def _handle_input(self, inputs: dict[str, Any]) -> ToolResult:
        """处理 input 操作"""
        pid = inputs.get("pid")
        if not pid:
            return create_failure_result(
                error="pid 不能为空",
                error_code="MISSING_PID",
            )

        input_text = inputs.get("input_text")
        if input_text is None:
            return create_failure_result(
                error="input_text 不能为空",
                error_code="MISSING_INPUT",
            )

        # 发送输入
        success, error = await self.process_manager.send_input(pid, input_text)

        if not success:
            return create_failure_result(
                error=error or "发送输入失败",
                error_code="INPUT_FAILED",
            )

        # 获取当前状态
        summary = self.process_manager.get_summary(pid)

        return create_success_result(
            data={
                "status": "running",
                "pid": pid,
                "message": "输入已发送",
                "summary": summary.get("summary", []) if summary else [],
            },
            metadata={"action": "input"},
        )

    async def _handle_read_log(self, inputs: dict[str, Any]) -> ToolResult:
        """处理 read_log 操作"""
        pid = inputs.get("pid")
        if not pid:
            return create_failure_result(
                error="pid 不能为空",
                error_code="MISSING_PID",
            )

        # 获取进程信息
        proc_info = self.process_manager.get_process_info(pid)
        if not proc_info:
            return create_failure_result(
                error=f"进程 {pid} 不存在",
                error_code="PROCESS_NOT_FOUND",
            )

        # 获取摘要 + 实际输出
        summary = self.process_manager.get_summary(pid)
        output = self.process_manager.get_output(pid)

        return create_success_result(
            data={
                "status": proc_info.status,
                "pid": pid,
                "output": output,  # 完整输出文本
                "summary": summary.get("summary", []) if summary else [],
                "warnings": summary.get("warnings", 0) if summary else 0,
                "errors": summary.get("errors", 0) if summary else 0,
            },
            metadata={"action": "read_log"},
        )
