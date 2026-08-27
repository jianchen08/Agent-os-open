"""
增强版 Bash 命令执行工具（0.2 自包含版）

⚠️ 安全威胁模型（H3）
=================

bash 工具的设计语义就是"执行用户命令"，**无法靠输入过滤根治**——
shell 元字符（| $() `` ;）天然有效，黑名单不可穷举（python -c "import os;
os.system('...')" 即绕过 SecurityChecker）。

SecurityChecker 的正则黑名单只拦**不可逆灾难**（rm -rf /、mkfs、dd 等
手滑即无法挽回的操作），**不是安全边界**。curl | sh 这类"危险但合法"的
模式不在此层硬拦，而是降级为 warning + 管道层审批。真正的控制是**隔离**。

0.2 架构下本工具作为独立 sidecar 进程运行（MCP stdio），
与 0.1 src 树零依赖；隔离决策由内核统一处理，本工具只负责执行命令。

进程生命周期：
- 本模块被 server.py 以**模块级单例**持有——所有 MCP 调用共享同一个
  BashTool/ProcessManager，跨调用保留 active_processes（execute→input→
  continue→terminate 全链路可用）。
- 每个进程记录 owner（内核注入的会话身份，见 _owner_from_inputs），
  pid 级操作（continue/input/terminate/read_log）强制校验调用方身份，
  防跨会话越权。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from bash_types import BashAction
from input_handler import InputHandler
from process_manager import ProcessManager
from result_types import (
    ToolResult,
    create_failure_result,
    create_success_result,
)

from agentos_plugin_sdk.workspace_aware import WorkspaceAwareMixin

# POSIX 信号编号 → 名称映射。进程在 Linux 容器内运行，退出码遵循 POSIX
# 128+n 语义，但本工具的宿主可能是 Windows——Windows 的 signal.Signals 枚举
# 不含 SIGKILL(9)/SIGSEGV(11) 等 POSIX 信号（Windows 无这些信号），直接用
# signal.Signals(n) 反查会抛 ValueError。故内置 POSIX 标准信号名表，跨平台
# 一致，不依赖宿主 OS 的 signal 模块。
_POSIX_SIGNAL_NAMES: dict[int, str] = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    4: "SIGILL",
    6: "SIGABRT",
    8: "SIGFPE",
    9: "SIGKILL",
    11: "SIGSEGV",
    13: "SIGPIPE",
    14: "SIGALRM",
    15: "SIGTERM",
}


def describe_exit_code(exit_code: int | None) -> str | None:
    """把进程退出码解释为人类可读的终止原因（仅信号终止时返回非空）。

    POSIX 退出码语义：
    - 0        → 正常成功
    - 1..124   → 程序自身的错误码（含义因程序而异，不在此解释）
    - 128+n    → 被信号 n 终止（如 137 = 128 + 9 = SIGKILL）

    本函数**只在被信号终止时**返回描述字符串（含信号编号 + 名称），
    正常退出/程序自身错误码返回 None——避免给每个退出码硬编码提醒。

    信号名经内置 POSIX 信号表反查（SIGKILL/SIGTERM/SIGSEGV 等），查不到
    就只给编号。让 LLM 拿到的是「原始信号」而非编造的归因。

    Args:
        exit_code: process.wait() 返回的退出码（可能为 None，表示尚未结束）。

    Returns:
        被信号终止时返回 "信号 N (SIGXXX) 终止" 形式的描述；否则 None。
    """
    if exit_code is None or exit_code < 128 or exit_code > 192:
        return None
    signum = exit_code - 128
    name = _POSIX_SIGNAL_NAMES.get(signum, f"signal {signum}")
    return f"信号 {signum} ({name}) 终止"


# 失败时回传给 LLM 的错误消息由 LogCompressor 提取行组装（信号描述 + 错误行 +
# 最新消息），不做原始尾部截取——大输出兜底（原文存档 + 提取 + 定位符）已统一
# 移交 pipeline 的 spill_guard 输出插件（task_spill_guard.md 任务 2）。


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
      层审批决策（用户批准即可执行）。
    """

    # 不可逆灾难命令：硬拦（一旦执行无法挽回）
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

    # 手动后台化模式：降级标 warning（不阻断）。
    # 本工具自带后台执行+轮询语义，手动 nohup/setsid/disown/行尾& 会让进程
    # 脱离 ProcessManager 管理（continue/terminate 看不见），沦为孤儿。
    BACKGROUND_PATTERNS: ClassVar[list[str]] = [
        r"(?:^|[;|&]\s*)\s*nohup\b",  # nohup 作为命令 token
        r"(?:^|[;|&]\s*)\s*setsid\b",  # setsid 作为命令 token
        r"(?:^|[;|&]\s*)\s*disown\b",  # disown 作为命令 token
        r"\s&\s*$",  # 行尾独立的 & 后台符（不误伤 a&b / 2>&1 中段 &）
    ]

    def __init__(self, allowed_commands: list[str] | None = None):
        """初始化安全检查器"""
        self.allowed_commands = set(allowed_commands) if allowed_commands else None
        # 预编译正则表达式以提高性能
        self._compiled_dangerous = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]
        self._compiled_background = [re.compile(p, re.IGNORECASE) for p in self.BACKGROUND_PATTERNS]

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

        # 检查手动后台化模式（nohup/setsid/disown/行尾&）→ warning，不阻断
        for compiled in self._compiled_background:
            if compiled.search(cmd_stripped):
                return (
                    True,
                    True,
                    (
                        "命令包含手动后台化操作，本工具已自带后台执行+轮询，"
                        "手动后台化会使进程脱离管理；直接 execute 启动，"
                        "长任务返回 pid 后用 continue 轮询即可"
                    ),
                )

        return True, False, None


# ── 单一事实源导出（punch C17）──────────────────────────────
# 危险命令黑名单分类以本文件 SecurityChecker 为准；
# builtin_tools/src/agentos_builtin_tools/bash_tool.py 直接导入以下常量，
# 禁止再各维护一份正则清单（语义漂移：如 "| bash" 在此为 CAUTION 降级而非硬拦）。
DANGEROUS_PATTERNS: list[str] = SecurityChecker.DANGEROUS_PATTERNS
CAUTION_PATTERNS: list[str] = SecurityChecker.CAUTION_PATTERNS
BACKGROUND_PATTERNS: list[str] = SecurityChecker.BACKGROUND_PATTERNS


class BashTool(WorkspaceAwareMixin):
    """增强版 Bash 命令执行工具（0.2 sidecar）。

    隔离决策由内核统一处理，本工具只负责执行命令。
    进程状态由内部 ProcessManager 维护，**实例需在 sidecar 进程内长期存活**
    （server.py 以模块级单例持有），跨 MCP 调用保留 active_processes。
    """

    # 默认超时时间（秒）
    DEFAULT_TIMEOUT: ClassVar[int] = 30

    # 最大允许超时（秒），不得超过内核侧 MCP 调用外层超时
    MAX_TIMEOUT: ClassVar[int] = 290

    # 短输出阈值（字符数）：低于此值直接全量返回原始 output，不走任何提取——
    # 短输出本身信息量小、噪音少，提取反而可能切坏（如半行被截）。
    # 用字符数而非行数：行数受换行风格影响大（CRLF/LF、超长单行 vs 短行），
    # 字符数才是真实体积。约 2KB（~500 token）是 LLM 一次能轻松消化的量。
    SHORT_OUTPUT_CHAR_THRESHOLD: ClassVar[int] = 2000

    @staticmethod
    def _compact_result_data(
        pid: int,
        output: str | None,
        summary_obj: dict[str, Any],
        exit_code: int | None,
        status: str = "completed",
    ) -> dict[str, Any]:
        """精简 result_data，去掉 LLM 不需要的字段以节省 token。

        核心原则：**短输出全量返回，长输出附语义提取、原文不截断**。同一套逻辑
        覆盖 completed（进程结束）和 running（长任务轮询超时返回中间态）两种场景
        ——区别仅是 status 字段 + exit_code 有无，提取策略完全一致。

        - 短输出（<=SHORT_OUTPUT_CHAR_THRESHOLD 字符）：信息量小、噪音少，直接
          全量返回原始 output，不走提取——提取反而可能切坏（如半行被截）。
        - 长输出（>阈值）：**完整 output 原样返回**（大输出兜底由 pipeline 的
          spill_guard 统一负责：原文存档 + 头尾提取 + 定位符，task_spill_guard
          任务 2 已删除工具内截断），额外附 summary/error_lines/latest_message/
          progress（LogCompressor 已筛掉刷屏噪音的高价值信息）。

        保留字段：
        - pid/output/status：始终保留（exit_code 仅 completed 时有）
        - terminated_by_signal：信号终止时附加（如实暴露原始信号，不编造归因）
        - errors/warnings/error_lines：非空时保留（LogCompressor 过滤后的错误，去重）
        - summary/latest_message/progress：仅长输出时保留（统计摘要 + 最新输出行）
        """
        output_len = len(output or "")
        is_short = output_len <= BashTool.SHORT_OUTPUT_CHAR_THRESHOLD

        data: dict[str, Any] = {
            "pid": pid,
            "output": output,
            "status": status,
        }
        if status == "completed":
            data["exit_code"] = exit_code if exit_code is not None else 0

        # 信号终止时附加结构化字段：把原始信号（编号+名称）如实暴露给 LLM，
        # 而非让它从孤立数字猜。137=SIGKILL 常见但非唯一（OOM/timeout/外部 kill
        # 都可能发 SIGKILL，具体归因由调用方结合上下文判断，这里不编造）。
        if exit_code is not None:
            signal_desc = describe_exit_code(exit_code)
            if signal_desc:
                data["terminated_by_signal"] = signal_desc

        # errors/warnings 是 LogCompressor 提取的高价值信息（去重后的错误行），
        # 无论输出长短都应保留——短输出即便只有几行，只要命中 error 模式就该让
        # LLM 看到，避免它在一小段输出里还要自己找问题。
        # errors/warnings 是计数（int），error_lines 是具体错误行（list[str]）。
        # 计数让 LLM 知道"有几条错误"，error_lines 让它直接看到内容——两者互补。
        errors = summary_obj.get("errors", 0)
        warnings = summary_obj.get("warnings", 0)
        if errors:
            data["errors"] = errors
        if warnings:
            data["warnings"] = warnings
        error_lines = summary_obj.get("error_lines", [])
        if error_lines:
            data["error_lines"] = error_lines

        # 短输出到此为止：信息已全量在 output 里，summary（[N行]/类型/进度等
        # 统计性描述）对短输出是冗余噪音，跳过。
        if is_short:
            return data

        # 长输出：output 保持完整原文（spill_guard 统一兜底），附 summary/
        # latest_message/progress（LogCompressor 已筛掉刷屏噪音）。
        summary_lines = summary_obj.get("summary", [])
        if summary_lines:
            data["summary"] = summary_lines
        latest = summary_obj.get("latest_message") or ""
        if latest:
            data["latest_message"] = latest
        progress = summary_obj.get("progress")
        if progress:
            data["progress"] = progress

        return data

    @staticmethod
    def _build_failure_message(
        exit_code: int,
        output: str | None,
        summary_obj: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """组装失败时的 error_msg + metadata（execute/continue/read_log 共用）。

        按"短全量、长提取"原则：
        - 短输出（<=SHORT_OUTPUT_CHAR_THRESHOLD 字符）：直接全量原文，信息量小、
          截断反而可能切坏。
        - 长输出：信号描述 + LogCompressor 提取的错误列表（已去重筛噪音）+
          latest_message——不拼原始输出尾部（大输出兜底由 spill_guard 统一负责，
          task_spill_guard.md 任务 2：工具只保留"执行 + 语义提取"）。
        """
        signal_desc = describe_exit_code(exit_code)
        output_len = len(output or "")
        is_short = output_len <= BashTool.SHORT_OUTPUT_CHAR_THRESHOLD

        parts: list[str] = []
        if signal_desc:
            parts.append(f"{signal_desc}（退出码 {exit_code}）")
        else:
            parts.append(f"命令执行失败，退出码: {exit_code}")

        if is_short:
            if output:
                parts.append(f"输出：\n{output.rstrip()}")
        else:
            # error_lines 是 LogCompressor 提取的错误行原文列表（去重筛噪音）；
            # 注意与 errors（计数 int）区分——这里要的是具体行内容。
            error_lines = summary_obj.get("error_lines") or []
            latest = summary_obj.get("latest_message") or ""
            if error_lines:
                parts.append("错误行：\n" + "\n".join(error_lines[:20]))
            if latest:
                parts.append(f"最新输出：{latest}")

        error_msg = "\n".join(parts)
        fail_meta: dict[str, Any] = {"exit_code": exit_code}
        if signal_desc:
            fail_meta["terminated_by_signal"] = signal_desc
        return error_msg, fail_meta

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        allowed_commands: list[str] | None = None,
    ):
        """初始化 Bash 工具"""
        self.timeout = timeout

        # 安全组件
        self.security = SecurityChecker(allowed_commands)

        # 进程管理器（跨 MCP 调用共享——单例持有本实例时状态得以保留）
        self.process_manager = ProcessManager()

        # 输入处理器
        self.input_handler = InputHandler()

    # ── 会话身份（owner） ────────────────────────────────────────

    @staticmethod
    def _owner_from_inputs(inputs: dict[str, Any]) -> str | None:
        """从调用参数提取会话身份（owner）。

        0.2 内核在 tool-executor.invoke 时向 args 注入 `_owner`
        （thread_id/session_id 派生），插件据此做 pid 级越权防护。
        优先级：_owner > session_id > thread_id > workspace > project_root。
        全部缺失返回 None（无身份调用——仅双无身份场景放行）。
        """
        for key in ("_owner", "session_id", "thread_id", "workspace", "project_root"):
            value = inputs.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _check_owner(
        pid: int,
        caller_owner: str | None,
        proc_owner: str | None,
    ) -> tuple[bool, str | None]:
        """pid 级操作的 owner 校验。

        严格语义（防跨会话越权/劫持）：
        - 双方身份一致 → 放行
        - 双方皆无身份 → 放行（无身份可校验的兼容路径，0.2 注入后不出现）
        - 其余（身份缺失/不匹配）→ 拒绝
        """
        if caller_owner is None and proc_owner is None:
            return True, None
        if caller_owner is None:
            return False, f"进程 {pid} 属于已标识会话，无身份调用被拒绝（PROCESS_FORBIDDEN）"
        if proc_owner is None:
            return False, f"进程 {pid} 无归属会话，不允许跨身份操作（PROCESS_FORBIDDEN）"
        if caller_owner != proc_owner:
            return False, (
                f"进程 {pid} 属于其他会话，越权操作被拒绝（PROCESS_FORBIDDEN，"
                f"caller={caller_owner!r} vs owner={proc_owner!r}）"
            )
        return True, None

    # ── 主入口 ───────────────────────────────────────────────────

    async def execute(self, inputs: dict[str, Any], on_output: Callable[[str], None] | None = None) -> ToolResult:
        """执行工具（0.2 侧唯一入口，由 server.py 的 MCP handler 调用）。

        on_output（task_observability 任务 2）：execute action 的每行输出回调，
        供执行中进度推送（server.py 经 ProgressReporter 节流后推前端）。
        """
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

        if action == BashAction.EXECUTE and on_output is not None:
            return await self._handle_execute(inputs, on_output=on_output)
        return await handler(inputs)

    async def _handle_execute(
        self, inputs: dict[str, Any], on_output: Callable[[str], None] | None = None
    ) -> ToolResult:
        """处理 execute 操作"""
        command = inputs.get("command")
        if not command:
            return create_failure_result(
                error="command 不能为空",
                error_code="MISSING_COMMAND",
            )

        # 安全检查：容器隔离模式下跳过内部安全检查
        # 容器内执行已有独立的安全边界，反引号等 shell 特性是正常行为。
        # 注意：is_isolated 只信 _container_id（isolation_guard 服务端注入，
        # 且 param_inject 已剥离 LLM 夹带的下划线键）——不信任 _isolation_provider
        # 之类的声明式标记（历史版本允许其跳过黑名单，属提示注入伪造面）。
        warning = None
        container_id = inputs.get("_container_id")
        is_isolated = bool(container_id)
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
        owner = self._owner_from_inputs(inputs)

        return await self._execute_local_unified(
            command=command,
            timeout=timeout,
            working_dir=working_dir,
            warning=warning,
            container_id=container_id,
            owner=owner,
            on_output=on_output,
        )

    async def _execute_local_unified(
        self,
        command: str,
        timeout: int,
        working_dir: str | None,
        warning: str | None = None,
        container_id: str | None = None,
        owner: str | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> ToolResult:
        """
        本地执行命令（统一返回格式）

        返回数据结构（由 _compact_result_data 精简）：
        {
            "status": "completed" | "running",
            "pid": 12345,           # 后续 continue/read_log/terminate 凭此操作
            "output": "命令输出...",  # completed 时含完整结果
            "exit_code": 0,          # 始终保留
            "elapsed": 1.5,          # 已运行秒数
            "summary": [...],        # 仅长输出（>10行）时保留
            "warnings": [...],       # 仅非空时保留
            "errors": [...],         # 仅非空时保留
        }
        """
        try:
            # 启动进程。start_process 收到 log_dir 时会同步更新 process_manager.log_dir，
            # 保证后续 read_log_by_pid/get_summary 降级读磁盘时用同一目录。
            project_root = getattr(self, "_project_root", None)
            bash_log_dir = Path(project_root) / "logs" / "bash" if project_root else None
            pid, log_file = await self.process_manager.start_process(
                command=command,
                working_dir=working_dir,
                log_dir=bash_log_dir,
                container_id=container_id,
                owner=owner,
                on_output=on_output,
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
                        running_output = self.process_manager.get_output(pid)
                        running_data = BashTool._compact_result_data(
                            pid=pid,
                            output=running_output,
                            summary_obj=summary,
                            exit_code=None,
                            status="running",
                        )
                        running_data["elapsed"] = round(
                            time.time() - proc_info.start_time, 1
                        )
                        return create_success_result(
                            data=running_data,
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
                    error_msg, fail_meta = BashTool._build_failure_message(
                        exit_code, output, summary,
                    )
                    return create_failure_result(
                        error=error_msg,
                        error_code="COMMAND_FAILED",
                        metadata=fail_meta,
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

        caller_owner = self._owner_from_inputs(inputs)
        timeout = inputs.get("timeout", self.timeout)

        # 获取进程信息
        proc_info = self.process_manager.get_process_info(pid)
        if not proc_info:
            # 进程已被即时清理（completed 后 _on_output_task_done 触发）。
            # 降级走磁盘日志——先校验 owner（磁盘日志头 # Owner:）。
            file_data = self.process_manager.read_log_by_pid(pid)
            if file_data is not None:
                ok, err = self._check_owner(pid, caller_owner, file_data.get("owner"))
                if not ok:
                    return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")
                return create_success_result(
                    data={
                        "status": "completed",
                        "pid": pid,
                        "output": file_data["output"],
                        "summary": file_data["summary"],
                        "exit_code": file_data.get("exit_code") or 0,  # 磁盘日志可能无 exit_code
                    },
                    metadata={"action": "continue", "source": "file"},
                )
            return create_failure_result(
                error=(
                    f"进程 {pid} 不存在或已结束，且无对应日志文件（logs/bash/bash_{pid}.log）。"
                    "可能从未执行过或日志已被清理。"
                ),
                error_code="PROCESS_NOT_FOUND",
            )

        # owner 校验（内存进程）
        ok, err = self._check_owner(pid, caller_owner, proc_info.metadata.get("owner"))
        if not ok:
            return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")

        # 如果进程已完成，直接返回结果（对齐 execute 完成路径，带 output）
        if proc_info.status != "running":
            summary = self.process_manager.get_summary(pid)
            exit_code = proc_info.exit_code
            if exit_code is not None and exit_code != 0:
                output_fail = self.process_manager.get_output(pid)
                error_msg, fail_meta = BashTool._build_failure_message(
                    exit_code, output_fail, summary or {},
                )
                return create_failure_result(
                    error=error_msg,
                    error_code="COMMAND_FAILED",
                    metadata=fail_meta,
                )
            output = self.process_manager.get_output(pid)
            result_data = self._compact_result_data(
                pid=pid,
                output=output,
                summary_obj=summary or {},
                exit_code=exit_code if exit_code is not None else 0,
            )
            result_data["elapsed"] = summary.get("elapsed_seconds", 0) if summary else 0
            return create_success_result(
                data=result_data,
                metadata={"action": "continue"},
            )

        # 继续等待
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if elapsed >= timeout:
                # 再次触发回调
                summary = self.process_manager.get_summary(pid)

                running_output = self.process_manager.get_output(pid)
                running_data = BashTool._compact_result_data(
                    pid=pid,
                    output=running_output,
                    summary_obj=summary or {},
                    exit_code=None,
                    status="running",
                )
                running_data["elapsed"] = round(
                    time.time() - proc_info.start_time, 1
                )
                return create_success_result(
                    data=running_data,
                    metadata={"action": "continue"},
                )

            # 检查状态
            proc_info = self.process_manager.get_process_info(pid)
            if not proc_info or proc_info.status != "running":
                break

            await asyncio.sleep(0.5)

        # 进程已完成（对齐 execute 完成路径，带 output）
        summary = self.process_manager.get_summary(pid)
        exit_code = proc_info.exit_code if proc_info else None
        if exit_code is not None and exit_code != 0:
            output_fail = self.process_manager.get_output(pid)
            error_msg, fail_meta = BashTool._build_failure_message(
                exit_code, output_fail, summary or {},
            )
            return create_failure_result(
                error=error_msg,
                error_code="COMMAND_FAILED",
                metadata=fail_meta,
            )

        output = self.process_manager.get_output(pid)
        result_data = self._compact_result_data(
            pid=pid,
            output=output,
            summary_obj=summary or {},
            exit_code=exit_code if exit_code is not None else 0,
        )
        # 补回 elapsed（_compact_result_data 不带此字段）
        result_data["elapsed"] = summary.get("elapsed_seconds", 0) if summary else 0
        return create_success_result(
            data=result_data,
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

        caller_owner = self._owner_from_inputs(inputs)
        proc_info = self.process_manager.get_process_info(pid)
        if proc_info is None:
            return create_failure_result(
                error=f"进程 {pid} 不存在或已结束",
                error_code="PROCESS_NOT_FOUND",
            )
        # owner 校验（终止是破坏性操作，必须严格）
        ok, err = self._check_owner(pid, caller_owner, proc_info.metadata.get("owner"))
        if not ok:
            return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")

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

        caller_owner = self._owner_from_inputs(inputs)
        proc_info = self.process_manager.get_process_info(pid)
        if proc_info is None:
            return create_failure_result(
                error=f"进程 {pid} 不存在或已结束",
                error_code="PROCESS_NOT_FOUND",
            )
        # owner 校验（向进程注入输入是敏感操作，必须严格）
        ok, err = self._check_owner(pid, caller_owner, proc_info.metadata.get("owner"))
        if not ok:
            return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")

        # 获取当前状态。
        # ⚠️ 顺序说明（WSL 竞态）：send_input 后 ~ms 窗口内若读取该进程日志文件，
        # Windows WSL2 的 stdio relay 会丢失进程退出前的最后一批输出（预存在
        # 环境问题）。因此摘要在**发送前**取（发送前/后语义等价，都是"输入已
        # 发送"时刻的状态快照），发送后不再落盘任何读操作，避开竞态窗口。
        summary = self.process_manager.get_summary(pid)

        # 发送输入
        success, error = await self.process_manager.send_input(pid, input_text)

        if not success:
            return create_failure_result(
                error=error or "发送输入失败",
                error_code="INPUT_FAILED",
            )

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
        """处理 read_log 操作。

        双路径：
        1. 进程活跃（在 active_processes）→ 从内存读，返回实时 status + output + summary
        2. 进程已清（completed 后被 _on_output_task_done 清理）→ 按 pid 算日志文件名
           (logs/bash/bash_<pid>.log) 读磁盘
        """
        pid = inputs.get("pid")
        if not pid:
            return create_failure_result(
                error="pid 不能为空",
                error_code="MISSING_PID",
            )

        caller_owner = self._owner_from_inputs(inputs)

        # 路径1：进程活跃 → 从内存读（含实时 status、summary）
        proc_info = self.process_manager.get_process_info(pid)
        if proc_info:
            # owner 校验
            ok, err = self._check_owner(pid, caller_owner, proc_info.metadata.get("owner"))
            if not ok:
                return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")
            summary = self.process_manager.get_summary(pid)
            output = self.process_manager.get_output(pid)
            return create_success_result(
                data={
                    "status": proc_info.status,
                    "pid": pid,
                    "output": output,
                    "summary": summary.get("summary", []) if summary else [],
                    "warnings": summary.get("warnings", 0) if summary else 0,
                    "errors": summary.get("errors", 0) if summary else 0,
                },
                metadata={"action": "read_log", "source": "memory"},
            )

        # 路径2：进程已清 → 按 pid 算文件名读磁盘
        file_data = self.process_manager.read_log_by_pid(pid)
        if file_data is None:
            return create_failure_result(
                error=(
                    f"进程 {pid} 不存在且无对应日志文件（logs/bash/bash_{pid}.log）。"
                    "可能从未执行过，或日志文件已被外部清理。"
                ),
                error_code="LOG_FILE_NOT_FOUND",
            )
        # owner 校验（磁盘日志头 # Owner:）
        ok, err = self._check_owner(pid, caller_owner, file_data.get("owner"))
        if not ok:
            return create_failure_result(error=err, error_code="PROCESS_FORBIDDEN")
        return create_success_result(
            data={
                # 能从磁盘读到的都是已结束的进程（活跃的会在路径1处理）
                "status": "completed",
                "pid": pid,
                "output": file_data["output"],
                "summary": file_data["summary"],
                "warnings": file_data["warnings"],
                "errors": file_data["errors"],
            },
            metadata={"action": "read_log", "source": "file"},
        )
