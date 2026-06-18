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

from human_interaction.models import Priority, ResponseType
from human_interaction.service import (
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)
from isolation.policy import IsolationPolicyLoader
from isolation.sensitive_paths import is_sensitive_path
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# 项目根目录：src/plugins/input/ → 向上 4 级到项目根
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# 隔离策略加载器（模块级单例，构造时即从 isolation_policy.yaml 缓存）。
# Host 模式审批以 policy.execution 为单一事实源：
#   command_in_container → 命令执行类，HOST 降级需用户审批
#   host_direct          → 内部 API 工具，免审批
_policy_loader = IsolationPolicyLoader()


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

        # 从 YAML 文件加载（通过 ConfigCenter 统一缓存）
        # 注意：ConfigCenter.get() 内部已捕获 yaml.YAMLError 和 IO 错误，
        # 这里只需兜底防御 ConfigCenter 抛出意外异常（如未初始化）。
        rules_path = self._config.get("rules_path", "config/isolation/security_rules.yaml")
        rel = rules_path.replace("config/", "", 1) if rules_path.startswith("config/") else rules_path
        try:
            from config.config_center import get_config_center
            data = get_config_center().get(rel)
            if data and "rules" in data:
                return data["rules"]
            logger.warning("[%s] No rules found in %s", self.name, rules_path)
            return []
        except Exception:
            logger.warning("[%s] Rules file load failed: %s", self.name, rules_path)
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
        # state 中 security.decision 已展开为嵌套字典 state["security"]["decision"]
        security_decision = ctx.state.get("security", {}).get("decision")
        if security_decision:
            return PluginResult()

        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行安全检查逻辑。

        Host 模式统一规则：不限制工作目录边界（不管路径），只保留三道底线：
        1. 基础安全检查（路径遍历 / 敏感系统目录黑名单）→ 任何模式都必须执行，
           这是防注入、防触碰 OS 核心目录的底线，容器不能绕过
        2. 容器模式（provider=docker）→ 基础检查通过后一路绿灯
        3. host 模式按工具是否危险决定：
           - 非危险工具 → 放行
           - 危险工具（command_in_container 或声明了 dangerous_operations）：
             参数命中白名单（action=allow）→ 放行
             参数命中黑名单（action=block）→ 软拦截反馈 LLM
             参数需要审批（action=needs_approval）→ 弹审批
             危险工具的未知参数 → 弹审批（兜底）

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

        # 判断执行模式
        execution_contexts = ctx.state.get("execution_contexts", [])
        all_docker = bool(execution_contexts) and all(
            c.get("provider") == "docker" for c in execution_contexts
        )

        # ── 第一道：基础安全检查（路径遍历 + 敏感系统目录黑名单）──
        # 任何模式都必须执行，这是防注入、防触碰 OS 核心目录的底线。
        # host 模式不再做工作目录越界检查（不管路径）。
        # 违规时软拦截——反馈给 LLM 让它改正，不杀引擎。
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})

            # 1. 路径遍历检测
            traversal_reason = self._check_path_traversal(args)
            if traversal_reason:
                logger.warning(
                    "[%s] Blocked path traversal | tool=%s | reason=%s",
                    self.name, tool_name, traversal_reason,
                )
                return self._soft_block(ctx, tool_name, f"路径遍历攻击被拦截: {traversal_reason}")

            # 2. 敏感系统目录黑名单（禁止触碰 OS 核心目录）
            sensitive_reason = self._check_sensitive_paths(args)
            if sensitive_reason:
                logger.warning(
                    "[%s] Blocked sensitive system path | tool=%s | reason=%s",
                    self.name, tool_name, sensitive_reason,
                )
                return self._soft_block(ctx, tool_name, f"敏感系统目录被拦截: {sensitive_reason}")

        # ── 第二道：按模式分流 ──

        # 容器模式：基础检查通过 → 一路绿灯
        if all_docker:
            logger.info("[%s] 容器模式，基础检查通过，放行", self.name)
            return {"security.decision": {"allowed": True, "reason": "container mode, base checks passed"}}

        # host 模式：逐个检查工具调用的参数
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})

            # 判定是否危险工具：
            # - policy.execution == command_in_container（bash 等命令执行类）
            # - 或工具声明了 dangerous_operations（delete/move/copy/file_write 等）
            is_dangerous_tool = self._is_dangerous_tool(ctx, tool_name)

            # 非危险工具 → 直接放行
            if not is_dangerous_tool:
                continue

            # ── 危险工具：参数命中白名单才放行，否则一律审批 ──
            action, rule_name = self._match_rules(tool_name, args)
            if action == "allow":
                logger.info(
                    "[%s] 危险工具参数命中白名单，放行 | tool=%s | rule=%s",
                    self.name, tool_name, rule_name,
                )
                continue

            # 未命中白名单 → 一律弹审批
            reason = f"参数未命中安全白名单" + (f"（匹配规则: {rule_name}）" if rule_name else "")
            logger.warning(
                "[%s] 危险工具参数未命中白名单，弹审批 | tool=%s | rule=%s",
                self.name, tool_name, rule_name or "none",
            )
            return await self._await_approval(
                ctx, tool_name, reason
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
        优先用 engine 注入的服务（ctx.get_service），取不到时回退到
        全局单例 get_human_interaction_service()（覆盖 websocket 等未注入
        服务的场景）。审批通过后写入 allowed=True，
        拒绝/超时/取消后写入 allowed=False。

        Args:
            ctx: 插件执行上下文
            tool_name: 触发审批的工具名称
            rule_name: 触发审批的规则名称

        Returns:
            安全决策结果字典
        """
        # 优先用 engine 注入的服务（测试可 mock），回退全局单例
        try:
            interaction_svc = ctx.get_service("human_interaction_service")
        except KeyError:
            from human_interaction import get_human_interaction_service
            interaction_svc = get_human_interaction_service()

        # 提取工具调用的具体参数，显示给用户审批
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        args_preview = self._format_args_for_approval(tool_calls, tool_name)

        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        request_id = await interaction_svc.create_choice_request(
            session_id=session_id,
            thread_id=session_id,
            tab_id="",
            title=f"安全审批: {tool_name}",
            description=args_preview,
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
            # response_type 表示响应类型（answered/denied/cancelled），
            # 用户的具体选择在 selected_option 字段中。
            # DENIED 和 CANCELLED 已由 wait_for_choice 抛异常处理，
            # 到这里 response_type 通常是 "answered"，需检查 selected_option。
            selected_option = result.get("selected_option", "")

            if selected_option == "approved":
                logger.info(
                    "[%s] Approval granted | request_id=%s | tool=%s | option=%s",
                    self.name, request_id, tool_name, selected_option,
                )
                return {
                    "security.decision": {
                        "allowed": True,
                        "reason": "approved",
                        "tool": tool_name,
                    },
                }

            logger.warning(
                "[%s] Approval denied | request_id=%s | tool=%s | response=%s | option=%s",
                self.name, request_id, tool_name, response_type, selected_option,
            )
            # 审批拒绝属于软拦截——用户主观选择拒绝，不应结束管道，
            # 而是把拒绝结果作为 tool_result 返回给 LLM，让 LLM 决定下一步。
            return self._soft_block(ctx, tool_name, f"用户拒绝执行: {rule_name}")

        except InteractionDeniedError as e:
            logger.warning(
                "[%s] Approval denied (exception) | request_id=%s | tool=%s | reason=%s",
                self.name, request_id, tool_name, e.reason,
            )
            return self._soft_block(ctx, tool_name, f"用户拒绝执行: {e.reason or rule_name}")

        except InteractionTimeoutError:
            logger.warning(
                "[%s] Approval timed out | request_id=%s | tool=%s",
                self.name, request_id, tool_name,
            )
            return self._soft_block(ctx, tool_name, f"审批超时未响应: {rule_name}")

        except InteractionCancelledError as e:
            logger.warning(
                "[%s] Approval cancelled | request_id=%s | tool=%s | reason=%s",
                self.name, request_id, tool_name, e.reason,
            )
            return self._soft_block(ctx, tool_name, f"审批被取消: {e.reason or rule_name}")

        except Exception as e:
            logger.error(
                "[%s] Approval error | request_id=%s | tool=%s | error=%s",
                self.name, request_id, tool_name, e,
            )
            return self._soft_block(ctx, tool_name, f"审批服务异常: {e}")

    def _soft_block(
        self, ctx: PluginContext, tool_name: str, reason: str,
    ) -> dict[str, Any]:
        """软拦截：把拒绝原因作为 tool_result 返回给 LLM，不结束管道。

        审批拒绝/超时/取消属于用户主观行为，不应直接结束整个管道。
        通过清空 raw_tool_calls + 注入拒绝 tool_result，让管道走到 output
        路由的 next_llm 分支，LLM 收到拒绝反馈后自行决定下一步策略。

        Args:
            ctx: 插件执行上下文
            tool_name: 被拒绝的工具名称
            reason: 拒绝原因

        Returns:
            状态更新字典（raw_tool_calls 清空，tool_results 注入拒绝结果）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        rejected_results: list[dict[str, Any]] = []
        for tc in tool_calls:
            tc_name = tc.get("name", "")
            tc_call_id = tc.get("id")
            rejected_results.append({
                "tool_name": tc_name,
                "success": False,
                "error": f"[审批拒绝] {reason}" if tc_name == tool_name else f"[关联拒绝] {reason}",
                "call_id": tc_call_id,
            })

        logger.info(
            "[%s] Soft-block: 审批拒绝转为 tool_result 反馈给 LLM | tool=%s",
            self.name, tool_name,
        )
        return {
            StateKeys.RAW_TOOL_CALLS: [],
            StateKeys.TOOL_RESULTS: rejected_results,
            StateKeys.RAW_RESULT: f"工具 {tool_name} 被拒绝: {reason}",
            "security.decision": {"allowed": True, "reason": f"soft_block: {reason}", "tool": tool_name},
        }

    @staticmethod
    def _format_args_for_approval(tool_calls: list[dict[str, Any]], triggered_tool: str) -> str:
        """格式化工具调用参数，生成审批描述（含具体命令/路径/内容）。

        Args:
            tool_calls: 当前轮次的工具调用列表
            triggered_tool: 触发审批的工具名

        Returns:
            格式化的审批描述文本，包含工具名 + 具体操作内容预览
        """
        lines = ["请审批以下工具执行请求：", ""]

        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", tc.get("arguments", {}))

            # args 可能是 JSON 字符串
            if isinstance(args, str):
                import json
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}

            if not isinstance(args, dict):
                args = {}

            lines.append(f"工具: {name}")

            # 提取关键参数：命令、路径、代码等
            # 命令类（bash_execute 等）
            cmd = args.get("command") or args.get("cmd")
            if cmd:
                # 限制长度，避免超长命令刷屏
                cmd_preview = str(cmd)[:500]
                if len(str(cmd)) > 500:
                    cmd_preview += "\n... (命令过长，已截断)"
                lines.append(f"命令:\n{cmd_preview}")

            # 路径类（file_write/read/delete 等）
            for path_key in ("path", "file_path", "directory", "dest", "target", "output_path", "working_dir"):
                val = args.get(path_key)
                if val:
                    lines.append(f"{path_key}: {val}")

            # 文件写入内容
            content = args.get("content")
            if content:
                content_preview = str(content)[:300]
                if len(str(content)) > 300:
                    content_preview += "\n... (内容过长，已截断)"
                lines.append(f"内容预览:\n{content_preview}")

            # 代码类
            code = args.get("code")
            if code:
                code_preview = str(code)[:300]
                if len(str(code)) > 300:
                    code_preview += "\n... (代码过长，已截断)"
                lines.append(f"代码预览:\n{code_preview}")

            # URL 类
            url = args.get("url")
            if url:
                lines.append(f"URL: {url}")

            lines.append("")

        return "\n".join(lines).strip()

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

    def _check_sensitive_paths(self, args: dict[str, Any]) -> str:
        """检查路径参数是否命中敏感系统目录黑名单（内置安全机制）。

        Host 模式不再限制工作目录边界，但 OS 核心目录（Windows 的
        C:/Windows/System32、Linux 的 /etc /proc 等）始终禁止触碰。
        使用共享的 is_sensitive_path 统一判定。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        for key in self._path_params:
            if key in args:
                value = args[key]
                if not isinstance(value, str):
                    continue
                hit, matched = is_sensitive_path(value)
                if hit:
                    return f"Path hits sensitive system dir: {value} (matched: {matched})"
        return ""

    def _is_dangerous_tool(self, ctx: PluginContext, tool_name: str) -> bool:
        """判定工具是否属于危险工具（host 模式下需要参数审批把关）。

        双轨判定（满足任一即为危险工具）：
        1. policy.execution == "command_in_container" —— bash 等命令执行类
           （容器不可用时降级到 host，命令任意性高，必须审批）
        2. 工具声明了 dangerous_operations —— delete_file/move_file/copy_file/
           file_write 等破坏性操作工具

        tool_definition 从 tool_registry 服务获取；registry 不可用时回退到
        只看 policy.execution（保持原有兜底行为）。

        Args:
            ctx: 插件执行上下文
            tool_name: 工具名称

        Returns:
            是否为危险工具
        """
        # 轨道 1：命令执行类（policy.execution 判定）
        policy = _policy_loader.resolve(tool_name)
        if policy.execution == "command_in_container":
            return True

        # 轨道 2：声明了 dangerous_operations 的工具
        dangerous_ops = self._get_dangerous_operations(ctx, tool_name)
        if dangerous_ops:
            return True

        return False

    @staticmethod
    def _get_dangerous_operations(
        ctx: PluginContext, tool_name: str
    ) -> list[str]:
        """从 tool_registry 获取工具声明的 dangerous_operations。

        优先用 engine 注入的 tool_registry 服务，取不到时回退全局单例。
        registry 不可用或工具不存在时返回空列表（兜底，不影响主流程）。

        Args:
            ctx: 插件执行上下文
            tool_name: 工具名称

        Returns:
            工具声明的 dangerous_operations 列表，无声明或不可用时为空列表
        """
        registry = None
        try:
            registry = ctx.get_service("tool_registry")
        except KeyError:
            pass

        if registry is None:
            try:
                from tools.global_registry import get_global_tool_registry_sync
                registry = get_global_tool_registry_sync()
            except Exception:
                return []

        try:
            tool_def = registry.get(tool_name)
        except Exception:
            return []

        if tool_def is None:
            return []

        return getattr(tool_def, "dangerous_operations", None) or []

