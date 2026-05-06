"""安全检查 Input 插件 — 配置驱动的统一参数匹配引擎。

负责在管道循环的输入阶段检查工具执行的安全性，
采用 YAML 规则配置驱动，支持关键词匹配和正则匹配两种模式。

安全检查维度：
1. 路径遍历检测（内置，不依赖配置规则）
2. 工作目录边界检查（内置，workspace 路径从 config 读取）
3. 配置规则匹配（通用引擎，从 YAML 加载）

对于 needs_approval 规则，插件内部通过 human_interaction 服务
创建审批请求并 await 等待用户响应，审批通过后写入 allowed=True。

State 命名空间：
    - security.decision : 本插件写入的安全决策结果
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Any

import yaml

from human_interaction.models import Priority, ResponseType
from human_interaction.service import (
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# 项目根目录：src/plugins/input/ → 向上 4 级到项目根
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


class SecurityCheckPlugin(IInputPlugin):
    """安全检查 Input 插件。

    配置驱动的统一参数匹配引擎，支持：
    - 黑名单模式（默认）：匹配到规则则拦截
    - 白名单模式：未匹配到规则则拦截

    检查顺序：
    1. 路径遍历检测（内置）
    2. 工作目录边界检查（内置）
    3. 配置规则匹配（通用引擎）

    优先级：70（校验级，在参数注入之后）
    错误策略：ABORT（安全不确定必须停止）

    Attributes:
        _config: 插件配置字典
        _rules: 从 YAML 加载的安全规则列表
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化安全检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用安全检查（默认 True）
                - workspace: 允许的工作目录
                - max_path_depth: 最大路径深度（默认 10）
                - rules_path: 规则配置文件路径（默认 config/isolation/security_rules.yaml）
                - rules: 直接传入规则列表（如果提供则不从文件加载）
                - path_params: 路径参数名列表（默认 6 个常见路径参数）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._workspace = self._config.get("workspace", "")
        self._max_path_depth = self._config.get("max_path_depth", 10)
        self._path_params = self._config.get("path_params", [
            "path", "file_path", "directory", "dest", "target", "output_path", "working_dir",
        ])
        self._allowed_base_paths = self._config.get("allowed_base_paths", ["skills"])

        # 加载安全规则
        self._rules = self._load_rules()

    def _load_rules(self) -> list[dict[str, Any]]:
        """从配置或 YAML 文件加载安全规则。

        优先使用 config 中直接提供的 rules 列表，
        否则从 rules_path 指定的 YAML 文件加载。

        Returns:
            安全规则列表，每条规则包含 name、tools、params、action、patterns
        """
        # 优先使用 config 中直接传入的规则
        if "rules" in self._config and self._config["rules"]:
            return self._config["rules"]

        # 从 YAML 文件加载
        rules_path = self._config.get("rules_path", "config/isolation/security_rules.yaml")
        # 将相对路径转换为基于项目根目录的绝对路径
        if not os.path.isabs(rules_path):
            rules_path = os.path.join(_PROJECT_ROOT, rules_path)
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and "rules" in data:
                return data["rules"]
            logger.warning("[%s] No rules found in %s", self.name, rules_path)
            return []
        except FileNotFoundError:
            logger.warning("[%s] Rules file not found: %s", self.name, rules_path)
            return []
        except yaml.YAMLError as e:
            logger.error("[%s] Failed to parse rules file: %s", self.name, e)
            return []

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
        包括路径遍历、工作目录边界和配置规则匹配。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含安全决策状态更新的插件执行结果。
            如果检查不通过，会设置 security.decision 为 blocked。
        """
        # 幂等检查：如果已有安全决策则跳过，避免 YAML 继承场景下重复执行
        if ctx.state.get("security.decision"):
            return PluginResult()

        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行安全检查逻辑。

        检查顺序：
        1. 路径遍历检测（内置）
        2. 工作目录边界检查（内置）
        3. 配置规则匹配（通用引擎）

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

            # 1. 路径遍历检测（内置，检查 6 个路径参数）
            traversal_reason = self._check_path_traversal(args)
            if traversal_reason:
                logger.warning(
                    "[%s] Blocked path traversal | tool=%s | reason=%s",
                    self.name, tool_name, traversal_reason,
                )
                decision = {"allowed": False, "reason": traversal_reason, "tool": tool_name}
                return {"security.decision": decision}

            # 2. 工作目录边界检查（内置，优先从 state 动态获取 workspace）
            # 动态获取 workspace：优先使用 state 中的值（支持 worktree 模式），
            # 回退到配置中的静态值
            workspace = ctx.state.get("workspace", self._workspace)
            if workspace:
                workspace_reason = self._check_workspace_boundary(args, workspace)
                if workspace_reason:
                    logger.warning(
                        "[%s] Blocked out-of-workspace access | tool=%s | reason=%s",
                        self.name, tool_name, workspace_reason,
                    )
                    decision = {"allowed": False, "reason": workspace_reason, "tool": tool_name}
                    return {"security.decision": decision}

            # 3. 配置规则匹配（通用引擎）
            action, rule_name = self._match_rules(tool_name, args)
            if action == "block":
                logger.warning(
                    "[%s] Blocked by rule '%s' | tool=%s",
                    self.name, rule_name, tool_name,
                )
                decision = {
                    "allowed": False,
                    "reason": f"Blocked by security rule: {rule_name}",
                    "tool": tool_name,
                }
                return {"security.decision": decision}

            if action == "needs_approval":
                logger.warning(
                    "[%s] Needs approval by rule '%s' | tool=%s",
                    self.name, rule_name, tool_name,
                )
                return await self._await_approval(
                    ctx, tool_name, rule_name
                )

        return {"security.decision": {"allowed": True, "reason": "all checks passed"}}

    async def _await_approval(
        self,
        ctx: PluginContext,
        tool_name: str,
        rule_name: str,
    ) -> dict[str, Any]:
        """等待用户审批并返回审批结果。

        通过 human_interaction 服务创建审批请求并 await 等待用户响应。
        审批通过后写入 allowed=True，拒绝/超时/取消后写入 allowed=False。

        Args:
            ctx: 插件执行上下文
            tool_name: 触发审批的工具名称
            rule_name: 触发审批的规则名称

        Returns:
            安全决策结果字典
        """
        try:
            interaction_svc = ctx.get_service("human_interaction")
        except KeyError:
            interaction_svc = None
        if interaction_svc is None:
            logger.error(
                "[%s] Approval required but no human_interaction service available, blocking",
                self.name,
            )
            return {
                "security.decision": {
                    "allowed": False,
                    "reason": f"Needs approval by security rule: {rule_name} (no approval service)",
                    "tool": tool_name,
                },
            }

        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        request_id = await interaction_svc.create_choice_request(
            session_id=session_id,
            thread_id=session_id,
            tab_id="",
            title="安全审批请求",
            description=f"工具 {tool_name} 触发安全规则 {rule_name}，需要您的审批才能继续执行。",
            options=[
                {"value": "approved", "label": "允许执行"},
                {"value": "denied", "label": "拒绝执行"},
            ],
            priority=Priority.HIGH,
        )

        logger.info(
            "[%s] Approval request created | request_id=%s | tool=%s | rule=%s",
            self.name, request_id, tool_name, rule_name,
        )

        try:
            result = await interaction_svc.wait_for_choice(request_id)
            response_type = result.get("response_type", "")

            if response_type == ResponseType.APPROVED.value:
                logger.info(
                    "[%s] Approval granted | request_id=%s | tool=%s",
                    self.name, request_id, tool_name,
                )
                return {
                    "security.decision": {
                        "allowed": True,
                        "reason": "approved",
                        "tool": tool_name,
                    },
                }

            logger.warning(
                "[%s] Approval denied | request_id=%s | tool=%s | response=%s",
                self.name, request_id, tool_name, response_type,
            )
            return {
                "security.decision": {
                    "allowed": False,
                    "reason": f"Approval {response_type}: {rule_name}",
                    "tool": tool_name,
                },
            }

        except InteractionDeniedError as e:
            logger.warning(
                "[%s] Approval denied (exception) | request_id=%s | tool=%s | reason=%s",
                self.name, request_id, tool_name, e.reason,
            )
            return {
                "security.decision": {
                    "allowed": False,
                    "reason": f"Approval denied: {e.reason or rule_name}",
                    "tool": tool_name,
                },
            }

        except InteractionTimeoutError as e:
            logger.warning(
                "[%s] Approval timed out | request_id=%s | tool=%s",
                self.name, request_id, tool_name,
            )
            return {
                "security.decision": {
                    "allowed": False,
                    "reason": f"Approval timed out: {rule_name}",
                    "tool": tool_name,
                },
            }

        except InteractionCancelledError as e:
            logger.warning(
                "[%s] Approval cancelled | request_id=%s | tool=%s | reason=%s",
                self.name, request_id, tool_name, e.reason,
            )
            return {
                "security.decision": {
                    "allowed": False,
                    "reason": f"Approval cancelled: {e.reason or rule_name}",
                    "tool": tool_name,
                },
            }

        except Exception as e:
            logger.error(
                "[%s] Approval error | request_id=%s | tool=%s | error=%s",
                self.name, request_id, tool_name, e,
            )
            return {
                "security.decision": {
                    "allowed": False,
                    "reason": f"Approval error: {e}",
                    "tool": tool_name,
                },
            }

    def _match_rules(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        """通用规则匹配引擎。

        遍历所有配置规则，对工具调用参数进行匹配。
        规则的 tools 为 ["*"] 或包含当前工具名时适用。
        支持关键词子串匹配（大小写不敏感）和正则匹配两种模式。

        Args:
            tool_name: 工具名称
            args: 工具参数字典

        Returns:
            元组 (action, rule_name)：
            - action 为 "block" 或 "needs_approval" 表示匹配到规则
            - action 为空字符串表示未匹配到任何规则
            - rule_name 为匹配到的规则名称
        """
        for rule in self._rules:
            # 检查工具是否匹配
            tools = rule.get("tools", [])
            if "*" not in tools and tool_name not in tools:
                continue

            # 遍历规则关注的参数
            params = rule.get("params", [])
            for param_name in params:
                value = args.get(param_name)
                if value is None:
                    continue
                value_str = str(value)

                # 遍历规则的模式列表
                patterns = rule.get("patterns", [])
                for pattern_def in patterns:
                    pat_type = pattern_def.get("type", "keyword")
                    pat_value = pattern_def.get("value", "")

                    matched = False
                    if pat_type == "keyword":
                        # 关键词子串匹配，大小写不敏感
                        if pat_value.lower() in value_str.lower():
                            matched = True
                    elif pat_type == "regex":
                        # 正则匹配
                        try:
                            if re.search(pat_value, value_str):
                                matched = True
                        except re.error:
                            logger.warning(
                                "[%s] Invalid regex in rule '%s': %s",
                                self.name, rule.get("name", "?"), pat_value,
                            )

                    if matched:
                        action = rule.get("action", "block")
                        return (action, rule.get("name", "unknown"))

        return ("", "")

    def _check_path_traversal(self, args: dict[str, Any]) -> str:
        """检查路径遍历攻击（增强版）。

        检测 ../ 等路径遍历模式，防止通过相对路径绕过工作目录限制。
        使用 Path.resolve() 解析绝对路径，同时检测 URL 编码绕过和符号链接。
        """
        from pathlib import Path

        for key in self._path_params:
            if key not in args:
                continue

            path = str(args[key])

            # 1. 检查原始路径中的遍历模式
            if ".." in path.replace("\\", "/"):
                return f"Path traversal detected in raw path: {path}"

            # 2. 使用 Path.resolve() 解析绝对路径（处理符号链接）
            try:
                resolved = Path(path).resolve()
                if ".." in str(resolved):
                    return f"Path traversal in resolved path: {path} -> {resolved}"
            except (OSError, ValueError) as e:
                return f"Invalid path: {path} ({e})"

            # 3. 检查编码绕过（URL 编码、双重编码等）
            if "%" in path:
                try:
                    decoded = urllib.parse.unquote(path)
                    decoded2 = urllib.parse.unquote(decoded)
                    if ".." in decoded.replace("\\", "/") or ".." in decoded2.replace("\\", "/"):
                        return f"Encoded path traversal detected: {path}"
                except Exception:
                    pass

            # 4. 检查空字节注入（Windows）
            if "\x00" in path:
                return f"Null byte injection detected: {path}"

        return ""

    def _check_workspace_boundary(self, args: dict[str, Any], workspace: str | None = None) -> str:
        """检查文件操作是否在允许的目录边界内（内置安全机制）。

        对绝对路径检查是否在 workspace 或 allowed_base_paths 目录范围内，
        使用 realpath 解析符号链接，防止通过符号链接绕过边界。

        支持动态 workspace：优先使用传入的 workspace 参数（来自 state），
        回退到实例属性 self._workspace（来自配置），确保 worktree 模式下
        不会因路径与配置不一致而误判为越界。

        allowed_base_paths 支持项目内相对目录（如 skills），
        用于允许 Agent 访问项目级资源（如 Skill 脚本）。

        Args:
            args: 工具参数
            workspace: 动态 workspace 路径（来自 state），为 None 时回退到配置值

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        effective_workspace = workspace or self._workspace
        if not effective_workspace:
            return ""

        # BUG-FIX-fix_20260506_bash_security: 支持多基路径边界检查
        # 问题根因: working_dir 参数未被检查；Skill 脚本路径在 workspace 外被阻止
        # 修复方案: 新增 working_dir 参数检查；支持 allowed_base_paths 多基路径
        # 影响范围: bash_execute 的 working_dir 参数、Skill 脚本执行路径
        allowed_bases = [os.path.realpath(effective_workspace)]
        for extra in self._allowed_base_paths:
            abs_extra = extra if os.path.isabs(extra) else os.path.join(_PROJECT_ROOT, extra)
            allowed_bases.append(os.path.realpath(abs_extra))

        for key in self._path_params:
            if key in args:
                path = str(args[key])
                if os.path.isabs(path):
                    real_path = os.path.normcase(os.path.realpath(path))
                    if not any(
                        real_path == os.path.normcase(base) or real_path.startswith(os.path.normcase(base) + os.sep)
                        for base in allowed_bases
                    ):
                        return f"Path outside allowed boundaries: {path}"

        return ""
