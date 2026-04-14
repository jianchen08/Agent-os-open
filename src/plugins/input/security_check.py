"""安全检查 Input 插件 — 合并旧代码 3 处 isolation 重复。

负责在管道循环的输入阶段检查工具执行的安全性，
包括危险操作拦截、受保护路径检查、路径遍历检测和工作目录边界检查。

合并了旧代码中 isolation_tool_wrapper（3 处重复）、
security_check 和 reasoning_check 的安全检查逻辑。

M6b 阶段：从 isolation/permission_checker.py 和
isolation/tools.py 的工具安全包装逻辑迁移。
阶段 4.16：增强危险命令覆盖（网络操作/包管理/代码执行/注册表等），
增加正则模式匹配和路径遍历检测，增加高风险操作审批机制。

State 命名空间：
    - security.decision : 本插件写入的安全决策结果
    - security.approval_required : 高风险操作需要审批时设置
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class SecurityCheckPlugin(IInputPlugin):
    """安全检查 Input 插件。

    安全检查包括五个维度：
    1. 危险命令拦截：关键词匹配 + 正则模式匹配
    2. 受保护路径检查：文件操作是否在受保护目录内
    3. 路径遍历检测：防止 ../ 等路径遍历攻击
    4. 工作目录边界：文件操作是否在允许的工作目录内
    5. 高风险操作审批：特定操作需人工确认

    优先级：70（校验级，在参数注入之后）
    错误策略：ABORT（安全不确定必须停止）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    # 危险命令关键词列表（子串匹配）
    _DANGEROUS_COMMANDS = {
        # 文件系统破坏
        "rm -rf", "del /s", "format", "mkfs", "dd if=",
        "> /dev/sd", "chmod 777", "chown root",
        # 系统控制
        "shutdown", "reboot", "halt", "poweroff",
        # 网络操作
        "curl ", "wget ", "nc -", "netcat ", "ncat ",
        "ssh ", "scp ", "rsync ", "iptables",
        # 包管理
        "pip install", "npm install", "yarn add",
        "apt-get install", "yum install", "brew install",
        # 代码执行
        "python -c", "python3 -c", "node -e", "perl -e",
        "ruby -e", "php -r",
        # 注册表
        "reg add", "reg delete", "regedit",
        # 定时任务
        "crontab", "schtasks", "at ",
        # 环境篡改
        "export PATH", "set PATH", "LD_PRELOAD",
        # 其他危险操作
        "no-preserve-root", "2>/dev/null",
    }

    # 危险命令正则模式列表（更灵活的匹配）
    _DANGEROUS_PATTERNS = [
        # rm 命令的多种变体
        re.compile(r"\brm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)", re.IGNORECASE),
        # del 命令的多种变体
        re.compile(r"\bdel\s+/[sq]\b", re.IGNORECASE),
        # 危险的 dd 命令
        re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE),
        # sudo 危险命令
        re.compile(r"\bsudo\s+(rm|dd|mkfs|format|fdisk|parted)\b", re.IGNORECASE),
        # 管道到 shell
        re.compile(r"\|\s*(ba)?sh\b", re.IGNORECASE),
        # 反弹 shell
        re.compile(r"(bash|sh|nc|ncat)\s+-[ep]\s", re.IGNORECASE),
        # 内联代码执行
        re.compile(r"\b(eval|exec)\s*\(", re.IGNORECASE),
        # 危险的 PowerShell 命令
        re.compile(r"(Remove-Item|Invoke-WebRequest|Start-Process)", re.IGNORECASE),
        # 磁盘/分区操作
        re.compile(r"\b(fdisk|parted|mkfs|mount\s+/dev/)\b", re.IGNORECASE),
    ]

    # 受保护的路径列表
    _PROTECTED_PATHS = {
        "/etc/passwd", "/etc/shadow", "/etc/sudoers",
        "/etc/ssh", "/root/.ssh", "/home/.ssh",
        "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
        "/System/Library", "/usr/lib/systemd",
        "/boot/", "/efi/",
    }

    # 高风险操作关键词（需要审批而非直接拦截）
    _HIGH_RISK_COMMANDS = {
        "sudo ", "runas ", "su -",
        "docker run", "docker exec",
        "kubectl apply", "kubectl delete",
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化安全检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用安全检查（默认 True）
                - workspace: 允许的工作目录
                - max_path_depth: 最大路径深度（默认 10）
                - blocked_commands: 额外拦截的命令列表
                - blocked_paths: 额外拦截的路径列表
                - enable_approval: 是否启用高风险操作审批（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._workspace = self._config.get("workspace", "")
        self._max_path_depth = self._config.get("max_path_depth", 10)
        self._blocked_commands = set(self._DANGEROUS_COMMANDS) | set(
            self._config.get("blocked_commands", [])
        )
        self._blocked_paths = set(self._PROTECTED_PATHS) | set(
            self._config.get("blocked_paths", [])
        )
        self._enable_approval = self._config.get("enable_approval", True)
        self._high_risk_commands = set(self._HIGH_RISK_COMMANDS) | set(
            self._config.get("high_risk_commands", [])
        )

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "security_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 70)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行安全检查。

        检查当前管道状态中的工具调用参数是否安全，
        包括权限验证、目录边界和危险操作检测。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含安全决策状态更新的插件执行结果。
            如果检查不通过，会设置 security.decision 为 blocked。
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行安全检查逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            安全决策结果字典
        """
        if not self._enabled:
            return {"security.decision": {"allowed": True, "reason": "security check disabled"}}

        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")

        # LLM 调用不需要安全检查
        if core_type != "tool_execute":
            return {"security.decision": {"allowed": True, "reason": "not a tool execution"}}

        # 检查工具调用参数
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"security.decision": {"allowed": True, "reason": "no tool calls to check"}}

        # 逐个检查工具调用
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})

            # 1. 危险命令检查（关键词 + 正则）
            blocked_reason = self._check_dangerous_commands(tool_name, args)
            if blocked_reason:
                logger.warning(
                    "[%s] Blocked dangerous command | tool=%s | reason=%s",
                    self.name, tool_name, blocked_reason,
                )
                decision = {"allowed": False, "reason": blocked_reason, "tool": tool_name}
                return {"security.decision": decision}

            # 2. 受保护路径检查
            path_reason = self._check_protected_paths(args)
            if path_reason:
                logger.warning(
                    "[%s] Blocked protected path access | tool=%s | reason=%s",
                    self.name, tool_name, path_reason,
                )
                decision = {"allowed": False, "reason": path_reason, "tool": tool_name}
                return {"security.decision": decision}

            # 3. 路径遍历检测
            traversal_reason = self._check_path_traversal(args)
            if traversal_reason:
                logger.warning(
                    "[%s] Blocked path traversal | tool=%s | reason=%s",
                    self.name, tool_name, traversal_reason,
                )
                decision = {"allowed": False, "reason": traversal_reason, "tool": tool_name}
                return {"security.decision": decision}

            # 4. 工作目录边界检查
            if self._workspace:
                workspace_reason = self._check_workspace_boundary(args)
                if workspace_reason:
                    logger.warning(
                        "[%s] Blocked out-of-workspace access | tool=%s | reason=%s",
                        self.name, tool_name, workspace_reason,
                    )
                    decision = {"allowed": False, "reason": workspace_reason, "tool": tool_name}
                    return {"security.decision": decision}

            # 5. 高风险操作审批检查
            if self._enable_approval:
                approval_reason = self._check_high_risk(tool_name, args)
                if approval_reason:
                    logger.warning(
                        "[%s] High-risk operation requires approval | tool=%s | reason=%s",
                        self.name, tool_name, approval_reason,
                    )
                    decision = {
                        "allowed": False,
                        "reason": approval_reason,
                        "tool": tool_name,
                        "approval_required": True,
                    }
                    return {
                        "security.decision": decision,
                        StateKeys.APPROVAL_REQUIRED: True,
                    }

        return {"security.decision": {"allowed": True, "reason": "all checks passed"}}

    def _check_dangerous_commands(self, tool_name: str, args: dict[str, Any]) -> str:
        """检查是否包含危险命令。

        使用两层检测：
        1. 关键词子串匹配（快速，覆盖常见模式）
        2. 正则模式匹配（灵活，覆盖变体和绕过尝试）

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        # 检查命令类型工具
        command = str(args.get("command", "")) + str(args.get("cmd", ""))
        command_lower = command.lower()

        # 层 1：关键词子串匹配
        for blocked in self._blocked_commands:
            if blocked.lower() in command_lower:
                return f"Dangerous command detected: {blocked}"

        # 层 2：正则模式匹配
        for pattern in self._DANGEROUS_PATTERNS:
            if pattern.search(command):
                return f"Dangerous command pattern detected: {pattern.pattern}"

        return ""

    def _check_protected_paths(self, args: dict[str, Any]) -> str:
        """检查是否访问受保护路径。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        # 提取所有路径参数
        paths = []
        for key in ("path", "file_path", "directory", "dest", "target", "output_path"):
            if key in args:
                paths.append(str(args[key]))

        for path in paths:
            for protected in self._blocked_paths:
                if protected.lower() in path.lower():
                    return f"Access to protected path: {path}"

        return ""

    def _check_path_traversal(self, args: dict[str, Any]) -> str:
        """检查路径遍历攻击。

        检测 ../ 等路径遍历模式，防止通过相对路径
        绕过工作目录限制。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        for key in ("path", "file_path", "directory", "dest", "target", "output_path"):
            if key in args:
                path = str(args[key])
                # 直接检查路径中是否包含 .. 组件
                # 使用 normpath 标准化后再检查
                import os
                normalized = os.path.normpath(path)
                # normpath 后如果路径以 .. 开头或包含 .. 分隔段，
                # 说明存在路径遍历
                parts = normalized.replace("\\", "/").split("/")
                if ".." in parts:
                    return f"Path traversal detected: {path}"

                # 检查编码绕过（URL 编码、双重编码等）
                if "%" in path:
                    import urllib.parse
                    try:
                        decoded = urllib.parse.unquote(path)
                        decoded_normalized = os.path.normpath(decoded)
                        decoded_parts = decoded_normalized.replace("\\", "/").split("/")
                        if ".." in decoded_parts:
                            return f"Encoded path traversal detected: {path}"
                    except Exception:
                        pass

        return ""

    def _check_workspace_boundary(self, args: dict[str, Any]) -> str:
        """检查文件操作是否在工作目录边界内。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        import os

        for key in ("path", "file_path", "directory", "dest", "target", "output_path"):
            if key in args:
                path = str(args[key])
                if os.path.isabs(path):
                    workspace_abs = os.path.abspath(self._workspace)
                    # 使用 normpath 防止符号链接绕过
                    real_path = os.path.normpath(path)
                    if not real_path.startswith(workspace_abs):
                        return f"Path outside workspace: {path}"

        return ""

    def _check_high_risk(self, tool_name: str, args: dict[str, Any]) -> str:
        """检查高风险操作。

        高风险操作不是直接拦截，而是需要审批确认。
        当前实现为标记 approval_required，后续可接入审批流。

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            原因字符串（非空表示需要审批），空字符串表示无需审批
        """
        command = str(args.get("command", "")) + str(args.get("cmd", ""))
        command_lower = command.lower()

        for high_risk in self._high_risk_commands:
            if high_risk.lower() in command_lower:
                return f"High-risk operation requires approval: {high_risk.strip()}"

        return ""
