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

import contextlib
import logging
import os
import re
import urllib.parse
from typing import Any

from human_interaction.models import Priority, ResponseType  # noqa: F401
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
_PROJECT_ROOT = os.path.dirname(  # noqa: PTH120
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: PTH100,PTH120
)

# 隔离策略加载器（模块级单例，构造时即从 isolation_policy.yaml 缓存）。
# Host 模式审批以 policy.execution 为单一事实源：
#   command_in_container → 命令执行类，HOST 降级需用户审批
#   host_direct          → 内部 API 工具，免审批
_policy_loader = IsolationPolicyLoader()

# 审批选项单一事实源：发起处（create_choice_request）与消费处（wait_for_choice 返回值
# 解析）共用这一份常量，消除两处硬编码漂移。
#
# 消费侧约定：前端优先提交选项 label（见 InteractionPanel.tsx 的
# respondChoice：optionLabel || optionId），因此 wait_for_choice 返回的
# selected_option 通常是 label。消费处先按 label 反查出稳定 id 再做分支判断，
# label 文案随意调整都不影响指纹记忆等内部逻辑。auto_confirm_runner 等不经
# 前端的路径仍提交 id，反查时回退按 id 直接匹配兜底。
_APPROVAL_OPTIONS: list[dict[str, str]] = [
    {"id": "approved_once", "label": "仅本次执行"},
    {"id": "approved_remember", "label": "本管道内同命令免批"},
    {"id": "denied", "label": "拒绝执行"},
]
# label → id 反查表（消费处用）。
_APPROVAL_LABEL_TO_ID = {opt["label"]: opt["id"] for opt in _APPROVAL_OPTIONS}
_APPROVAL_ID_TO_LABEL = {opt["id"]: opt["label"] for opt in _APPROVAL_OPTIONS}


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
        self._path_params = self._config.get(
            "path_params",
            [
                "path",
                "file_path",
                "directory",
                "dest",
                "target",
                "output_path",
                "working_dir",
            ],
        )
        # 命令参数名列表：基础检查里用于 CMD 风格 nul 重定向检测
        self._command_params = self._config.get(
            "command_params",
            [
                "command",
                "cmd",
            ],
        )
        self._allowed_base_paths = self._config.get("allowed_base_paths", ["skills"])

        # 本管道内已记忆"通过"的命令指纹集合。
        # 作用域为单管道实例：每个 PipelineEngine 创建时 fork() 出独立插件实例
        # （registry.py fork 对每个插件深拷贝重建），新实例集合为空，天然按管道隔离，
        # 跨管道/跨会话不泄漏。仅记忆用户选择"本管道内同命令免批"的精确指纹，拒绝绝不记忆。
        self._approved_signatures: set[str] = set()

        # 连续拒绝计数（按工具签名）：防止模型反复提交同一被拦请求导致死循环。
        # 与 _approved_signatures 同作用域（单管道隔离）。一旦模型换了请求（签名变化），
        # 新签名计数从 0 开始。偶发拒绝（1-2 次）靠 messages 写回反馈给模型改正；
        # 达到阈值仍重复 → 模型无法自我纠正，终止任务（死循环最终防线）。
        self._rejected_signatures: dict[str, int] = {}
        self._reject_threshold = self._config.get("reject_threshold", 3)

        # 加载安全规则，并启动期预编译校验所有正则（fail-fast，见 _validate_rules）
        self._rules = self._load_rules()
        self._validate_rules(self._rules)

    def _load_rules(self) -> list[dict[str, Any]]:
        """从配置或 YAML 文件加载安全规则。

        优先使用 config 中直接提供的 rules 列表，
        否则从 rules_path 指定的 YAML 文件加载。

        Fail-closed 语义（关键）：
        - 本规则集是黑名单语义（mode: blacklist）——"没有规则"等于"全部放行"。
          若配置加载失败却静默返回空列表，安全检查会退化为"全放行"，
          属于危险态。因此加载失败时抛 ValueError，让管道启动期就暴露问题，
          而不是带着空规则静默运行。
        - 仅当配置明确返回"空规则集"（合法的、用户主动选择全放行）时才返回空列表。
          YAML 文件缺失 / ConfigCenter 未初始化 / 数据无 rules 字段都视为加载失败。

        Returns:
            安全规则列表，每条规则包含 name、tools、params、action、patterns

        Raises:
            ValueError: 规则文件加载失败或数据无效时抛出（fail-closed）
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
            from config.config_center import get_config_center  # noqa: PLC0415

            data = get_config_center().get(rel)
        except Exception as e:
            # 加载异常（ConfigCenter 未初始化 / IO 错误）→ fail-closed
            msg = f"安全规则文件加载失败（{rules_path}）: {e}"
            logger.error("[%s] %s", self.name, msg)
            raise ValueError(msg) from e

        if not data or "rules" not in data:
            # 数据无效：视为加载失败而非"用户主动选择空规则"
            msg = f"安全规则文件无有效 rules 字段（{rules_path}）"
            logger.error("[%s] %s", self.name, msg)
            raise ValueError(msg)

        return data["rules"]

    def _validate_rules(self, rules: list[dict[str, Any]]) -> None:
        """启动期预编译校验所有 regex 规则，编译错立即暴露。

        Fail-fast 语义：正则规则配错时，运行期 _match_rules 只会 warning 然后把
        该 pattern 当作不匹配（fail-open）。对于安全规则，这会让"该拦截的没拦"
        静默发生。本方法在构造时一次性编译所有 type=regex 的 pattern，编译失败
        立即抛 ValueError，让配置错误在管道启动期就暴露，绝不带到运行期。

        Args:
            rules: 已加载的规则列表

        Raises:
            ValueError: 任何 regex pattern 编译失败时抛出，含规则名与错误 pattern
        """
        for rule in rules:
            rule_name = rule.get("name", "?")
            for pattern_def in rule.get("patterns", []):
                if pattern_def.get("type", "keyword") != "regex":
                    continue
                pat_value = pattern_def.get("value", "")
                try:
                    re.compile(pat_value)
                except re.error as e:
                    msg = (
                        f"安全规则 '{rule_name}' 含非法正则 {pat_value!r}: {e}。"
                        f"该 pattern 在运行期会被静默跳过（fail-open），请在配置中修正。"
                    )
                    logger.error("[%s] %s", self.name, msg)
                    raise ValueError(msg) from e

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
        # 每轮工具调用独立检查，不可短路（否则审批通过后硬底线被跳过，安全闸门失效）。
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """执行安全检查逻辑。

        隔离即放行，裸操作才审批：
        1. 基础安全检查（路径遍历 / 敏感系统目录黑名单）→ 任何模式都必须执行，
           这是防注入、防触碰 OS 核心目录的底线，隔离不能绕过
        2. 已 docker 隔离（所有工具 provider 均为 docker）
           → 基础检查通过后一路绿灯
        3. 非 docker 隔离按工具是否危险决定：
           - 非危险工具 → 放行
           - 危险工具（command_in_container 或声明了 dangerous_operations）：
             参数命中白名单（action=allow）→ 放行（allow 优先于其它规则）
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

        # ── 第一道：基础安全检查（路径遍历 + 敏感系统目录黑名单 + nul 重定向）──
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
                    self.name,
                    tool_name,
                    traversal_reason,
                )
                return self._soft_block(ctx, tool_name, f"路径遍历攻击被拦截: {traversal_reason}")

            # 2. 敏感系统目录黑名单（禁止触碰 OS 核心目录）
            sensitive_reason = self._check_sensitive_paths(args)
            if sensitive_reason:
                logger.warning(
                    "[%s] Blocked sensitive system path | tool=%s | reason=%s",
                    self.name,
                    tool_name,
                    sensitive_reason,
                )
                return self._soft_block(ctx, tool_name, f"敏感系统目录被拦截: {sensitive_reason}")

            # 3. CMD 风格 nul 重定向检测（Git Bash/Linux 下会创建名为 nul 的垃圾文件）
            nul_reason = self._check_nul_redirect(args)
            if nul_reason:
                logger.warning(
                    "[%s] Blocked CMD-style nul redirect | tool=%s | reason=%s",
                    self.name,
                    tool_name,
                    nul_reason,
                )
                return self._soft_block(
                    ctx,
                    tool_name,
                    f"CMD 风格重定向被拦截: {nul_reason}（Git Bash 下请用 2>/dev/null）",
                )

        # ── 第二道：按模式分流 ──

        # 隔离任务即放行：isolation_level 是隔离唯一真相源，隔离任务（isolated/None/空）
        # 的所有工具一律放行，不弹审批——无论工具走 docker 还是 host。
        if self._is_isolated(execution_contexts):
            logger.info("[%s] 隔离任务，基础检查通过，放行", self.name)
            return {"security.decision": {"allowed": True, "reason": "isolated task, base checks passed"}}

        # 非隔离任务：逐个检查工具调用的参数
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
                    self.name,
                    tool_name,
                    rule_name,
                )
                continue

            # 命中本管道内已记忆的命令指纹 → 放行（用户此前选了"同命令免批"）
            signature = self._make_signature(tool_name, args)
            if signature and signature in self._approved_signatures:
                logger.info(
                    "[%s] 危险工具命中本管道已记忆指纹，放行 | tool=%s",
                    self.name,
                    tool_name,
                )
                continue

            # 未命中白名单/记忆指纹 → 一律弹审批
            reason = "参数未命中安全白名单" + (f"（匹配规则: {rule_name}）" if rule_name else "")
            logger.warning(
                "[%s] 危险工具参数未命中白名单，弹审批 | tool=%s | rule=%s",
                self.name,
                tool_name,
                rule_name or "none",
            )
            return await self._await_approval(
                ctx,
                tool_name,
                reason,
                signature=signature,
            )

        return {"security.decision": {"allowed": True, "reason": "all checks passed"}}

    async def _await_approval(
        self,
        ctx: PluginContext,
        tool_name: str,
        rule_name: str,
        *,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """等待用户审批并返回审批结果。

        通过 human_interaction 服务创建审批请求并 await 等待用户响应。
        优先用 engine 注入的服务（ctx.get_service），取不到时回退到
        全局单例 get_human_interaction_service()（覆盖 websocket 等未注入
        服务的场景）。审批通过后写入 allowed=True；
        拒绝/超时/取消走 _soft_block（软拦截，反馈给 LLM）。

        三个审批选项：
        - approved_once   : 仅本次执行通过（不记忆指纹）
        - approved_remember: 本管道内同命令（同工具+同指纹）后续免审批；
                            仅当 signature 非空时记忆，无指纹的命令退化为"仅本次"
        - denied          : 拒绝（软拦截）

        Args:
            ctx: 插件执行上下文
            tool_name: 触发审批的工具名称
            rule_name: 触发审批的规则/原因描述
            signature: 当前命令的归一化指纹，用于"同命令免批"记忆；None 表示无法记忆

        Returns:
            安全决策结果字典
        """
        # 优先用 engine 注入的服务（测试可 mock），回退全局单例
        try:
            interaction_svc = ctx.get_service("human_interaction_service")
        except KeyError:
            from human_interaction import get_human_interaction_service  # noqa: PLC0415

            interaction_svc = get_human_interaction_service()

        # 提取工具调用的具体参数，显示给用户审批
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        args_preview = self._format_args_for_approval(tool_calls, tool_name)

        session_id = ctx.state.get(StateKeys.SESSION_ID, "")
        # request_id 预初始化：create_choice_request 抛异常时尚未赋值，
        # 但 except 链日志需引用它，占位为 "-" 表示请求未创建成功。
        request_id = "-"
        # try 覆盖创建请求+等待响应，避免服务异常上抛中断管道。
        try:
            request_id = await interaction_svc.create_choice_request(
                session_id=session_id,
                thread_id=session_id,
                tab_id="",
                title=f"安全审批: {tool_name}",
                description=args_preview,
                options=list(_APPROVAL_OPTIONS),
                priority=Priority.HIGH,
                user_id=ctx.state.get("user_id"),
            )

            logger.info(
                "[%s] Approval request created | request_id=%s | tool=%s | rule=%s",
                self.name,
                request_id,
                tool_name,
                rule_name,
            )

            result = await interaction_svc.wait_for_choice(request_id)
            response_type = result.get("response_type", "")
            # response_type 表示响应类型（answered/denied/cancelled），
            # 用户的具体选择在 selected_option 字段中。
            # DENIED 和 CANCELLED 已由 wait_for_choice 抛异常处理，
            # 到这里 response_type 通常是 "answered"，需检查 selected_option。
            #
            # selected_option 可能是 label（前端优先传 label）也可能是 id
            # （auto_confirm_runner 等不经前端的路径传 id）。这里归一到稳定 id
            # 再做分支判断——文案怎么改都不影响指纹记忆逻辑。
            raw_selected = result.get("selected_option", "")
            # 归一到稳定 id：前端传 label（走 _LABEL_TO_ID），不经前端的路径传 id
            # （raw_selected 本身就是 id，直接用）。两者都查不到则保守视为拒绝。
            resolved_id = _APPROVAL_LABEL_TO_ID.get(raw_selected) or (
                raw_selected if raw_selected in _APPROVAL_ID_TO_LABEL else ""
            )

            if resolved_id in ("approved_once", "approved_remember"):
                # "同命令免批"：记忆精确指纹，本管道内同工具+同命令后续免审批。
                # 仅当指纹可计算时才记忆；无指纹（如无法归一化的参数）退化为"仅本次"。
                if resolved_id == "approved_remember" and signature:
                    self._approved_signatures.add(signature)
                    logger.info(
                        "[%s] Approval granted (remember signature) | request_id=%s | tool=%s | sig=%s",
                        self.name,
                        request_id,
                        tool_name,
                        signature,
                    )
                else:
                    logger.info(
                        "[%s] Approval granted (once) | request_id=%s | tool=%s | option=%s",
                        self.name,
                        request_id,
                        tool_name,
                        resolved_id,
                    )
                return {
                    "security.decision": {
                        "allowed": True,
                        "reason": "approved",
                        "tool": tool_name,
                    },
                }

            logger.warning(
                "[%s] Approval denied | request_id=%s | tool=%s | response=%s | raw=%s | resolved=%s",
                self.name,
                request_id,
                tool_name,
                response_type,
                raw_selected,
                resolved_id,
            )
            # 审批拒绝属于软拦截——用户主观选择拒绝，不应结束管道，
            # 而是把拒绝结果作为 tool_result 返回给 LLM，让 LLM 决定下一步。
            return self._soft_block(ctx, tool_name, f"用户拒绝执行: {rule_name}")

        except InteractionDeniedError as e:
            logger.warning(
                "[%s] Approval denied (exception) | request_id=%s | tool=%s | reason=%s",
                self.name,
                request_id,
                tool_name,
                e.reason,
            )
            return self._soft_block(ctx, tool_name, f"用户拒绝执行: {e.reason or rule_name}")

        except InteractionTimeoutError:
            logger.warning(
                "[%s] Approval timed out | request_id=%s | tool=%s",
                self.name,
                request_id,
                tool_name,
            )
            return self._soft_block(ctx, tool_name, f"审批超时未响应: {rule_name}")

        except InteractionCancelledError as e:
            logger.warning(
                "[%s] Approval cancelled | request_id=%s | tool=%s | reason=%s",
                self.name,
                request_id,
                tool_name,
                e.reason,
            )
            return self._soft_block(ctx, tool_name, f"审批被取消: {e.reason or rule_name}")

        except Exception as e:
            logger.error(
                "[%s] Approval error | request_id=%s | tool=%s | error=%s",
                self.name,
                request_id,
                tool_name,
                e,
            )
            return self._soft_block(ctx, tool_name, f"审批服务异常: {e}")

    def _make_signature(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """计算命令指纹，用于"本管道内同命令免批"记忆。

        指纹 = 工具名 + 全部非空参数的归一化字符串的 sha256 短摘要。

        安全边界（关键）：
        - 精确匹配，不做前缀/语义模糊。字符串参数仅做空白归一化（统一连续空白为
          单空格、去首尾），不替换路径、不截断、不解析重排。保证 "rm -rf /tmp/x"
          ≠ "rm -rf /"。
        - 命令前后差异（哪怕只多一个空格）也会产生不同指纹 → 用户重审，宁可多问。
        - 无任何可归一化参数时返回 None，调用方退化为"仅本次"。

        全参数入哈希（非白名单参数键）：
        - 早期实现只对 command/code/content 与写死的 7 个路径键做哈希，新增工具若
          用别的键承载关键内容（如 url/src/dst/filename），指纹会漏掉这些参数 →
          "仅 url 不同"的两个 download 调用算同一指纹 → 用户授权一次后不同目标全
          部免批，方向上是"少问/漏放"，违反"宁可多问"。
        - 现改为：对所有非空参数（string 归一化；非 string 走 repr 稳定表示）都
          入哈希。list/dict 等结构类型用 repr() 参与签名，保证内容差异能区分。

        Args:
            tool_name: 工具名称
            args: 工具参数字典

        Returns:
            形如 "tool_name:ab12cd34" 的指纹；无法计算返回 None
        """
        import hashlib  # noqa: PLC0415

        parts: list[str] = [tool_name]

        # 全参数入哈希：按键名排序保证稳定性（dict 顺序不影响指纹），
        # 字符串走空白归一化，其余类型用 repr() 稳定表示。
        for key in sorted(args.keys()):
            val = args[key]
            if val is None:
                continue
            if isinstance(val, str):
                if not val.strip():
                    continue
                parts.append(f"{key}=" + " ".join(val.split()))
            elif isinstance(val, set):
                # set 无序：先排序再 repr，保证元素顺序不影响指纹稳定性
                if not val:
                    continue
                try:
                    parts.append(f"{key}={sorted(val)!r}")
                except TypeError:
                    # 含不可排序元素（如 dict）时退化为 repr，接受可能的顺序敏感
                    parts.append(f"{key}={val!r}")
            elif isinstance(val, (list, dict, tuple)):
                # list/tuple 有序、dict 保序：空集合跳过，否则直接 repr
                if not val:
                    continue
                parts.append(f"{key}={val!r}")
            else:
                # 数值/布尔等标量：空字符串跳过，否则入哈希
                if isinstance(val, str) and not val:  # pragma: no cover - 上面已处理 str
                    continue
                parts.append(f"{key}={val!r}")

        # 只有工具名、无任何可归一化参数 → 无法稳定记忆，返回 None
        if len(parts) == 1:
            return None

        digest_source = "|".join(parts)
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
        return f"{tool_name}:{digest}"

    def _soft_block(
        self,
        ctx: PluginContext,
        tool_name: str,
        reason: str,
    ) -> dict[str, Any]:
        """软拦截：把拒绝原因作为 tool_result 返回给 LLM，不结束管道。

        审批拒绝/路径遍历拦截等属于可恢复情况，不应直接结束整个管道。
        通过清空 raw_tool_calls + 注入拒绝 tool_result，让管道走到 output
        路由的 next_llm 分支，LLM 收到拒绝反馈后自行决定下一步策略。

        关键：拒绝结果必须同步 append 为 role=tool 消息到 messages，且
        tool_call_id 与被拒的 assistant(tool_calls) 配对。否则 messages 末尾
        会留下无配对 tool 结果的孤儿 assistant，被 normalize Phase B 整条删除，
        模型永远收不到拒绝反馈、反复重试同一请求 → 死循环。

        连续拒绝保护：按工具签名计数，同一请求被连续拦截超阈值时，说明
        模型无法自我纠正，直接终止管道并上报，避免无限重试。

        Args:
            ctx: 插件执行上下文
            tool_name: 被拒绝的工具名称
            reason: 拒绝原因

        Returns:
            状态更新字典（raw_tool_calls 清空，tool_results 注入拒绝结果，
            messages 追加配对的 tool 消息）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        rejected_results: list[dict[str, Any]] = []
        # 同步把拒绝结果写回 messages：每个被拒 tool_call 对应一条 role=tool 消息，
        # tool_call_id 必须与 assistant(tool_calls) 的 id 一致，才能通过
        # normalize 的配对校验（否则 assistant 会被当孤儿删除）。
        messages = list(ctx.state.get("messages", []))
        for tc in tool_calls:
            tc_name = tc.get("name", "")
            tc_call_id = tc.get("id")
            block_reason = f"[审批拒绝] {reason}" if tc_name == tool_name else f"[关联拒绝] {reason}"
            rejected_results.append(
                {
                    "tool_name": tc_name,
                    "success": False,
                    "error": block_reason,
                    "call_id": tc_call_id,
                }
            )
            if tc_call_id:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_call_id,
                        "content": f"Error: {block_reason}",
                    }
                )

        # 连续拒绝计数：同一工具签名被反复拦截时累加，换请求即重置。
        signature = self._make_signature(tool_name, self._first_args(tool_calls, tool_name))
        updates: dict[str, Any] = {
            StateKeys.RAW_TOOL_CALLS: [],
            StateKeys.TOOL_RESULTS: rejected_results,
            StateKeys.RAW_RESULT: f"工具 {tool_name} 被拒绝: {reason}",
            "messages": messages,
            "security.decision": {"allowed": True, "reason": f"soft_block: {reason}", "tool": tool_name},
        }
        if signature:
            count = self._rejected_signatures.get(signature, 0) + 1
            self._rejected_signatures[signature] = count
            if count >= self._reject_threshold:
                # 同一请求连续被拦超阈值：模型已陷入死循环、无法自我纠正。
                # 明确终止管道并上报，而非无限重试。这是"工具重复上限"的最终防线：
                # P0 的 messages 写回已保证偶发拒绝能反馈给模型改正；到这一步说明
                # 模型无视反馈反复提交同一被拦请求，继续重试只会空耗资源。
                fatal_reason = (
                    f"工具 {tool_name} 连续被安全检查拦截 {count} 次"
                    f"（阈值 {self._reject_threshold}），疑似死循环，终止任务"
                )
                logger.warning(
                    "[%s] %s | sig=%s",
                    self.name,
                    fatal_reason,
                    signature,
                )
                updates[StateKeys.RAW_RESULT] = fatal_reason
                updates[StateKeys.RAW_ERROR] = fatal_reason
                updates[StateKeys.ENDED] = True
                # 重置该签名计数：管道即将结束，避免残留
                self._rejected_signatures[signature] = 0

        logger.info(
            "[%s] Soft-block: 拒绝转为 tool_result 反馈给 LLM | tool=%s",
            self.name,
            tool_name,
        )
        return updates

    @staticmethod
    def _first_args(
        tool_calls: list[dict[str, Any]],
        tool_name: str,
    ) -> dict[str, Any]:
        """取出触发拒绝的工具调用参数，用于计算连续拒绝指纹。

        Args:
            tool_calls: 当前轮次的工具调用列表
            tool_name: 触发拒绝的工具名称

        Returns:
            触发工具的 args 字典；找不到时返回空字典
        """
        for tc in tool_calls:
            if tc.get("name", "") == tool_name:
                args = tc.get("args", tc.get("arguments", {}))
                if isinstance(args, str):
                    return {}
                return args if isinstance(args, dict) else {}
        return {}

    @staticmethod
    def _format_args_for_approval(tool_calls: list[dict[str, Any]], triggered_tool: str) -> str:  # noqa: ARG004,PLR0912
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
                import json  # noqa: PLC0415

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

        优先级：allow > block / needs_approval。
        先扫一遍所有规则，命中 action=allow 即立即放行（白名单优先，
        避免 dangerous_commands 抢先于 safe_commands 误伤安全命令，
        如 `wc -l ... 2>/dev/null` 被 2>/dev/null 关键词误判）。
        无 allow 命中时，返回首个 block/needs_approval 规则。

        Args:
            tool_name: 工具名称
            args: 工具参数字典

        Returns:
            元组 (action, rule_name)：
            - action 为 "allow" 表示白名单命中（放行）
            - action 为 "block" 或 "needs_approval" 表示匹配到拦截/审批规则
            - action 为空字符串表示未匹配到任何规则
            - rule_name 为匹配到的规则名称
        """
        first_reject: tuple[str, str] = ("", "")
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
                                self.name,
                                rule.get("name", "?"),
                                pat_value,
                            )

                    if matched:
                        action = rule.get("action", "block")
                        rule_name = rule.get("name", "unknown")
                        # allow 白名单优先：命中即放行，不再查其它规则
                        if action == "allow":
                            return (action, rule_name)
                        # 记录首个拦截/审批规则，无 allow 命中时返回它
                        if not first_reject[0]:
                            first_reject = (action, rule_name)

        return first_reject

    def _check_path_traversal(self, args: dict[str, Any]) -> str:
        """检查路径遍历攻击（增强版）。

        检测 ../ 等路径遍历模式，防止通过相对路径绕过工作目录限制。
        使用 Path.resolve() 解析绝对路径，同时检测 URL 编码绕过和符号链接。
        """
        from pathlib import Path  # noqa: PLC0415

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

        allowed_bases = [os.path.realpath(effective_workspace)]
        for extra in self._allowed_base_paths:
            abs_extra = extra if os.path.isabs(extra) else os.path.join(_PROJECT_ROOT, extra)  # noqa: PTH117
            allowed_bases.append(os.path.realpath(abs_extra))

        for key in self._path_params:
            if key in args:
                path = str(args[key])
                if os.path.isabs(path):  # noqa: PTH117
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

    def _check_nul_redirect(self, args: dict[str, Any]) -> str:
        """检查命令参数是否含 CMD 风格 nul 重定向（内置安全机制）。

        Windows CMD 的 ``>nul`` / ``2>nul`` 把输出丢弃到 NUL 设备，语法合法；
        但同样的字符串在 Git Bash / Linux bash 下不识别 NUL 设备，反而会
        创建一个名为 ``nul`` 的真实文件（垃圾文件）。本检查拦截此类命令，
        命中后走软拦截反馈给 LLM，提示改用 ``2>/dev/null``。

        匹配范围限定在 command/cmd 参数（命令字符串），不影响路径参数。
        正则 ``(?:2\\s*)?>+\\s*&?\\s*nul(?!\\w)`` 覆盖 ``>nul`` / ``2>nul`` /
        ``>>nul`` / ``2>>nul`` / ``>&nul`` 等变体，大小写不敏感；负向断言
        ``(?!\\w)`` 排除 ``null`` / ``nullable`` 等合法标识符，也确保
        ``>/dev/null`` 不被误命中（``dev/null`` 里 nul 前无重定向符 ``>``）。

        Args:
            args: 工具参数

        Returns:
            拦截原因字符串，空字符串表示通过
        """
        for key in self._command_params:
            if key not in args:
                continue
            value = args[key]
            if not isinstance(value, str):
                continue
            # (?:2\s*)?  可选的 fd 前缀 2
            # \s*>+      一个或多个 >（覆盖 >>）
            # \s*&?\s*   可选的 &（覆盖 >&nul 这种合并写法）
            # nul        目标 nul
            # (?!\w)     后面不能跟单词字符（排除 null/nullable/nuls 等合法标识符）
            m = re.search(r"(?:2\s*)?>+\s*&?\s*nul(?!\w)", value, re.IGNORECASE)
            if m:
                return f"CMD-style nul redirect detected: {m.group(0)!r} in command"
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
        return bool(dangerous_ops)

    def _is_isolated(self, execution_contexts: list[dict[str, Any]]) -> bool:
        """判断当前任务是否为隔离模式。

        isolation_level 是隔离的唯一真相源：隔离任务（isolated/None/空）的所有
        工具一律放行，不弹审批——无论工具自身走 docker 还是 host（如 delete_file）。
        task_isolated 由 isolation_guard 按任务 isolation_level 归一化后注入每个 context。

        Args:
            execution_contexts: isolation_guard 写入的工具执行上下文列表

        Returns:
            True=隔离任务（放行），False=非隔离任务（危险工具需审批）
        """
        return bool(execution_contexts) and all(c.get("task_isolated") for c in execution_contexts)

    @staticmethod
    def _get_dangerous_operations(ctx: PluginContext, tool_name: str) -> list[str]:
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
        with contextlib.suppress(KeyError):
            registry = ctx.get_service("tool_registry")

        if registry is None:
            try:
                from tools.global_registry import get_global_tool_registry_sync  # noqa: PLC0415

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
