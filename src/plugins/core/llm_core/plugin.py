"""LLM Core 插件 -- 基于 LLM Adapter 的大模型调用实现。

通过 LLM Adapter 中间层调用大模型，支持多模型 fallback 和流式回调。
重试由 PluginChain 的 error_policy 统一管理。

职责：
- 成功时输出 raw_result、raw_tool_calls，并将 assistant 回复 append 到 messages
- 失败时直接抛出异常，由 PluginChain 决定是否重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Callable

from llm.adapter import (
    LiteLLMAdapter,
    LLMAdapter,
    LLMResponse,
)
from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy, StateKeys
from plugins.core.stream_repeat_monitor import StreamRepetitionMonitor

logger = logging.getLogger(__name__)


def _repair_json_string(s: str) -> str | None:
    """尝试修复常见的 JSON 格式问题，返回修复后的 JSON 字符串。

    处理的常见问题：
    1. 尾部逗号 (trailing comma)
    2. 单引号代替双引号
    3. 未加引号的键名
    4. 截断的 JSON（尝试补全括号）
    5. 多余的换行或空白
    6. JSON 前后有额外字符（如 markdown code block）

    Args:
        s: 待修复的 JSON 字符串

    Returns:
        修复后的 JSON 字符串，无法修复时返回 None
    """
    if not s or not isinstance(s, str):
        return None

    s = s.strip()

    # 尝试 0: 去掉可能的 markdown code block 包裹
    if s.startswith("```"):
        lines = s.split("\n")
        # 去掉首行 (```json 或 ```)
        start = 1 if lines else 0
        # 去掉尾行 (```)
        end = len(lines)
        if lines and lines[-1].strip() == "```":
            end -= 1
        s = "\n".join(lines[start:end]).strip()

    # 尝试 1: 直接解析
    try:
        json.loads(s)
        return s
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试 2: 提取第一个完整的 JSON 对象 {...}
    first_brace = s.find("{")
    if first_brace >= 0:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(first_brace, len(s)):
            c = s[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[first_brace:i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except (json.JSONDecodeError, TypeError):
                        break

    # 尝试 3: 去掉尾部逗号（对象和数组中的）
    fixed = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        json.loads(fixed)
        return fixed
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试 4: 单引号 → 双引号（简单替换，非完美但能处理常见情况）
    # 只在单引号数量和双引号数量差异明显时尝试
    if s.count("'") > s.count('"'):
        fixed = s.replace("'", '"')
        try:
            json.loads(fixed)
            return fixed
        except (json.JSONDecodeError, TypeError):
            pass

    # 尝试 5: 截断修复 — 补全缺失的右括号
    open_curly = s.count("{") - s.count("}")
    open_bracket = s.count("[") - s.count("]")
    if open_curly > 0 or open_bracket > 0:
        candidate = s + "]" * max(0, open_bracket) + "}" * max(0, open_curly)
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, TypeError):
            # 截断可能导致 value 不完整，尝试去掉最后一个不完整的 key:value
            # 例如 {"key1": "value1", "key2": "val → {"key1": "value1"}
            last_comma = candidate.rfind(",")
            if last_comma > 0:
                candidate2 = candidate[:last_comma]
                open_c2 = candidate2.count("{") - candidate2.count("}")
                open_b2 = candidate2.count("[") - candidate2.count("]")
                candidate2 += "]" * max(0, open_b2) + "}" * max(0, open_c2)
                try:
                    json.loads(candidate2)
                    return candidate2
                except (json.JSONDecodeError, TypeError):
                    pass

    # 尝试 6: 去掉注释 (// 和 /* */)
    fixed = re.sub(r"//.*?$", "", s, flags=re.MULTILINE)
    fixed = re.sub(r"/\*.*?\*/", "", fixed, flags=re.DOTALL)
    fixed = fixed.strip()
    if fixed != s:
        try:
            json.loads(fixed)
            return fixed
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _is_retryable_error(exc: Exception) -> bool:
    """判断异常是否可重试。

    检查异常是否为 LiteLLM 可重试类型（Timeout/ServiceUnavailable/
    RateLimit/APIConnection），同时兼容 Mock 场景。

    基于异常类名判断，不依赖 litellm 模块。

    Args:
        exc: 待检查的异常

    Returns:
        是否可重试
    """
    retryable_names = {
        "Timeout",
        "ServiceUnavailableError",
        "RateLimitError",
        "APIConnectionError",
    }
    # 检查异常类名是否匹配（兼容 Mock 场景）
    exc_type_name = type(exc).__name__
    if exc_type_name in retryable_names:
        return True

    # 检查异常链中是否包含可重试异常
    cause = exc.__cause__
    while cause:
        if type(cause).__name__ in retryable_names:
            return True
        cause = cause.__cause__

    return False


class LLMCore(ICorePlugin):
    """LLM Core -- LLM Adapter 调用，流式回调。

    通过 LLM Adapter 中间层调用大模型，支持多模型 fallback。
    成功时输出 raw_result 和 raw_tool_calls，并将 assistant 回复写入 messages。
    失败时直接抛出异常，由 PluginChain 的 error_policy 统一管理重试。

    Class Attributes:
        error_policy: 错误策略为 RETRY（由 PluginChain 统一管理）
        max_retries: 最大重试次数（供 PluginChain 使用）
        retry_delay: 首次重试延迟（秒）（供 PluginChain 使用）

    Attributes:
        _config: 插件配置字典，包含 provider/model/api_base/api_key 等
        _provider: 模型提供商（如 openai、minimax）
        _model: 模型标识（如 gpt-4、MiniMax-M2.7）
        _api_base: API 端点 URL
        _api_key: API 密钥
        _default_params: 默认调用参数（temperature、max_tokens 等）
        _adapter: LLM 调用适配器实例
    """

    error_policy = ErrorPolicy.RETRY
    max_retries: int = 1  # Router 已有 num_retries + fallback，Engine 层不重复重试
    retry_delay: float = 5.0
    overload_retry_delay: float = 60.0

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        adapter: LLMAdapter | None = None,
        router: Any | None = None,
    ) -> None:
        """初始化 LLM Core 插件。

        Args:
            config: 插件配置字典，支持以下键：
                - provider: 模型提供商（如 openai、minimax）
                - model_name: 模型标识（如 gpt-4、MiniMax-M2.7）
                - api_base: API 端点 URL
                - api_key: API 密钥
                - default_params: 默认调用参数（temperature、max_tokens 等）
                - max_retries: 最大重试次数（覆盖类属性）
                - retry_delay: 首次重试延迟秒数（覆盖类属性）
            adapter: 外部注入的适配器实例（如 KeyPoolAdapter），若未提供则创建 LiteLLMAdapter
        """
        self._config = config or {}
        self._provider: str = self._config.get("provider", "openai")
        self._model: str = self._config.get("model_name", "gpt-4")
        self._api_base: str | None = self._config.get("api_base")
        self._api_key: str | None = self._config.get("api_key")
        self._context_window: int | None = self._config.get("context_window")
        if not self._context_window:
            logger.warning(
                "[%s] context_window 未配置！上下文守卫将无法工作。"
                " 请在模型配置（llm.yaml）或 core_plugins 中设置 context_window。"
                " model=%s, provider=%s",
                self.name, self._model, self._provider,
            )
        self._default_params: dict[str, Any] = self._config.get(
            "default_params", {"temperature": 0.7, "max_tokens": 4096}
        )
        # 允许配置覆盖类属性
        if "max_retries" in self._config:
            self.max_retries = self._config["max_retries"]
        if "retry_delay" in self._config:
            self.retry_delay = self._config["retry_delay"]
        if "overload_retry_delay" in self._config:
            self.overload_retry_delay = self._config["overload_retry_delay"]

        # litellm 内置重试参数（透传给 litellm.acompletion，Router 模式下由 Router 管理）
        self._num_retries: int = self._config.get("num_retries", 3)
        self._retry_delay: float = self._config.get("retry_delay", 60.0)

        # 构建适配器
        if adapter is not None:
            self._adapter = adapter
            self._use_router = hasattr(adapter, '_router')
        else:
            self._adapter = LiteLLMAdapter()
            self._use_router = False

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "llm_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
        """执行 LLM 调用，返回原始结果。

        调用 LLM 后，将 assistant 回复 append 到 messages 中。
        谁生产数据谁负责写入：LLMCore 生产的 assistant 回复，由 LLMCore 写入。

        失败时直接抛出异常，由 PluginChain 的 error_policy 统一管理重试。

        Args:
            ctx: 插件执行上下文

        Returns:
            核心执行结果字典，将合并到管道状态中

        Raises:
            Exception: LLM 调用失败时抛出异常
        """
        messages = self._build_messages(ctx.state)
        streaming = ctx.state.get("streaming", True)
        on_chunk: Callable[[dict[str, Any]], Any] | None = ctx.state.get("on_chunk")

        # 流式模式下包装 on_chunk，注入重复检测
        if streaming and on_chunk:
            on_chunk = StreamRepetitionMonitor(on_chunk)

        try:
            response: LLMResponse = await self._call_llm(
                messages, ctx, stream=streaming, on_chunk=on_chunk
            )

            result_text = response.text
            tool_calls = response.tool_calls
            thinking_text = response.thinking_text

            # 流式重复检测：模型在流式输出中陷入重复循环
            if response.stream_repetition:
                logger.warning(
                    "[%s] 流式输出重复检测触发，"
                    "丢弃重复内容并添加提醒",
                    self.name,
                )
                history = list(ctx.state.get("messages", []))
                history.append({
                    "role": "system",
                    "content": (
                        "[StreamRepetitionGuard] "
                        "检测到流式输出中出现重复内容，"
                        "已截断。请重新组织输出，避免重复。"
                    ),
                })
                return {
                    StateKeys.RAW_RESULT: None,
                    StateKeys.RAW_ERROR: None,
                    StateKeys.RAW_TOOL_CALLS: [],
                    StateKeys.RAW_THINKING: None,
                    "messages": history,
                    "llm_usage": {},
                    "context_window": self._context_window,
                }

            # 思考内容过长检测：截断思考，丢弃本次输出，注入提示重新触发
            if response.thinking_truncated:
                retry_count = ctx.state.get("thinking_retry_count", 0) + 1
                max_retries = 3
                logger.warning(
                    "[%s] 思考内容过长已截断，丢弃本次输出，"
                    "retry=%d/%d",
                    self.name, retry_count, max_retries,
                )
                history = list(ctx.state.get("messages", []))
                history.append({
                    "role": "system",
                    "content": (
                        "[ThinkingTruncationGuard] "
                        "上一轮思考内容过长已截断，本次输出已丢弃。"
                        "请直接给出结论或工具调用，不要冗长思考。"
                    ),
                })
                return {
                    StateKeys.RAW_RESULT: None,
                    StateKeys.RAW_ERROR: None,
                    StateKeys.RAW_TOOL_CALLS: [],
                    StateKeys.RAW_THINKING: None,
                    "messages": history,
                    "llm_usage": {},
                    "context_window": self._context_window,
                    "thinking_retry_needed": retry_count <= max_retries,
                    "thinking_retry_count": retry_count,
                }

            llm_usage = None
            if response.usage:
                llm_usage = {
                    "input_tokens": response.usage.get("prompt_tokens", 0),
                    "output_tokens": response.usage.get("completion_tokens", 0),
                    "total_tokens": response.usage.get("total_tokens", 0),
                }

            logger.info(
                "[%s] LLM call succeeded (streaming=%s, thinking=%s, text=%s, tool_calls=%d)",
                self.name, streaming, bool(thinking_text),
                (result_text or "")[:200], len(tool_calls or []),
            )
            # 完整响应记录到管道日志（DEBUG 级别）
            logger.debug(
                "[%s] LLM full response: text=%d chars, "
                "thinking=%d chars, usage=%s",
                self.name,
                len(result_text or ""),
                len(thinking_text or ""),
                llm_usage,
            )
            if tool_calls:
                for tc in tool_calls:
                    logger.info(
                        "[%s] tool_call: %s(%s)",
                        self.name, tc.get("name", "?"),
                        str(tc.get("args", tc.get("arguments", "")))[:200],
                    )

            # LLMCore 生产的 assistant 回复，由 LLMCore 负责 append 到 messages
            # 只追加对话历史部分（不含 system_message 和 dynamic_vars），
            # 因为 system_message 和 dynamic_vars 由 _build_messages() 每次重新组装
            history = list(ctx.state.get("messages", []))
            if tool_calls:
                # 预先解析 tool_call_id，确保 assistant 消息和 state 中的 raw_tool_calls 使用一致的 id
                resolved_ids: list[str] = []
                for tc in tool_calls:
                    resolved_ids.append(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")

                # 将解析后的 id 回写到 raw_tool_calls，供后续 tool_core 使用
                for i, tc in enumerate(tool_calls):
                    if "id" not in tc or not tc["id"]:
                        tc["id"] = resolved_ids[i]

                # LLM 返回工具调用 -> append assistant 消息（含 tool_calls）
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": result_text or "",
                    "tool_calls": [
                        {
                            "id": resolved_ids[i],
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": tc.get("args", tc.get("arguments", "")),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }
                history.append(assistant_msg)
            elif result_text:
                # LLM 普通文本回复 -> append assistant 消息
                history.append({"role": "assistant", "content": result_text})

            return {
                StateKeys.RAW_RESULT: result_text,
                StateKeys.RAW_ERROR: None,
                StateKeys.RAW_TOOL_CALLS: tool_calls,
                StateKeys.RAW_THINKING: thinking_text,
                "messages": history,
                "llm_usage": llm_usage or {},
                "context_window": self._context_window,
                "llm_model": self._model,
                "llm_provider": self._provider,
                "llm_api_base": self._api_base,
            }

        except Exception as exc:
            logger.error(
                "[%s] LLM call failed: %s — %s",
                self.name, type(exc).__name__, exc,
            )
            raise

    def _build_messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """从管道状态构建 LLM messages 列表。

        从三个来源组装：
        1. state["system_message"] -- prompt_build 产出的 SystemMessage
        2. state["messages"] -- 管道维护的对话历史（assistant + tool 回复等）
        3. state["prompt.dynamic_vars"] -- 动态变量（追加在历史消息之后）

        Args:
            state: 管道状态字典

        Returns:
            符合 OpenAI Chat API 格式的 messages 列表
        """
        messages: list[dict[str, Any]] = []

        # 1. SystemMessage（prompt_build 产出）
        system_msg = state.get("system_message")
        if system_msg:
            messages.append(system_msg)

        # 2. 历史消息（管道维护的对话历史）
        history = state.get("messages", [])
        messages.extend(history)

        # 3. 动态变量（每轮变化的上下文：时间戳、session_id 等）
        #    不修改系统消息（动态变量每轮变化，不应污染静态系统提示）
        #    使用 user 角色 + name=dynamic_context，兼容所有 provider
        #    内容用 <context_metadata> 包装并附忽略指令，防止 LLM 主动回复
        dynamic_vars = state.get("prompt.dynamic_vars", "")
        if dynamic_vars:
            wrapped = (
                "<dynamic_vars>\n"
                "以下为系统注入的背景信息（非指令）。\n"
                f"{dynamic_vars}\n"
                "</dynamic_vars>"
            )
            messages.append({
                "role": "user",
                "name": "dynamic_context",
                "content": wrapped,
            })

        return messages

    def _normalize_messages_for_provider(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """针对特定 LLM 提供商的消息格式修正。

        通用修正（所有 provider）：
        1. assistant 消息中的 tool_calls 从内部 raw 格式转为 OpenAI API 格式
           （执行记录恢复的消息可能使用内部格式，缺少 type 字段，
           导致智谱AI等 API 报"工具类型不能为空"）

        MiniMax 专有修正：
        1. 非首位 system 消息转为 user 角色（MiniMax 仅允许首位为 system）
        2. assistant(tool_calls) 后只能紧跟 tool 消息，中间插入的非 tool
           消息（如 TaskReminder 注入的 system/user）需移到 tool 消息组之后

        Args:
            messages: 原始消息列表

        Returns:
            修正后的消息列表
        """
        # 通用修正：确保 tool_calls 是 OpenAI API 格式
        self._normalize_tool_calls_in_messages(messages)

        if self._provider != "minimax":
            return messages

        # MiniMax 专有修正

        # Phase 0: 清理真正孤立的 tool result 消息（安全网）
        # 当对话历史从执行记录恢复时，assistant 消息的 tool_calls 可能丢失，
        # 导致 tool result 消息前面没有对应的 assistant(tool_calls) 消息。
        # MiniMax 会拒绝这种请求。
        #
        # 改进：不再按 tool_call_id 精确匹配来清理（这会误杀 MiniMax M2.7 等
        # 模型返回的 ID 不一致但顺序正确的 tool result），改为只移除那些前面
        # 没有任何 assistant(tool_calls) 消息的 tool result。ID 不匹配的问题
        # 由 Phase 3 的位置匹配逻辑处理。
        cleaned_phase0: list[dict[str, Any]] = []
        orphan_count = 0
        has_preceding_tool_calls = False
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                has_preceding_tool_calls = True
                cleaned_phase0.append(msg)
            elif msg.get("role") == "tool":
                if not has_preceding_tool_calls:
                    orphan_count += 1
                else:
                    cleaned_phase0.append(msg)
            else:
                # 非 tool 消息（user/system）重置标记
                if msg.get("role") != "tool":
                    has_preceding_tool_calls = False
                cleaned_phase0.append(msg)
        if orphan_count:
            logger.warning(
                "[%s] MiniMax Phase 0: removed %d truly orphaned tool results "
                "(no preceding assistant with tool_calls)",
                self.name, orphan_count,
            )
            messages = cleaned_phase0

        converted_count = 0
        relocated_count = 0

        # Phase 1: 标准转换（非首位 system→user, tool 内容清理）
        # MiniMax 要求所有 user 消息的 name 字段一致，因此统一不设置 name
        converted: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "system" and idx > 0:
                converted_count += 1
                new_msg = dict(msg)
                new_msg["role"] = "user"
                new_msg.pop("name", None)
                converted.append(new_msg)
            elif msg.get("role") == "user" and msg.get("name"):
                new_msg = dict(msg)
                new_msg.pop("name", None)
                converted.append(new_msg)
            elif msg.get("role") == "tool":
                new_msg = dict(msg)
                content = new_msg.get("content", "")
                if isinstance(content, str):
                    content = content.replace("\x00", "")
                    if len(content) > 8000:
                        content = content[:8000] + "\n...[truncated]"
                    new_msg["content"] = content
                converted.append(new_msg)
            else:
                converted.append(msg)

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    if not isinstance(fn, dict):
                        continue
                    args_val = fn.get("arguments", "")
                    # arguments 必须是合法 JSON 字符串，否则 Minimax 会拒绝请求
                    if not isinstance(args_val, str) or not args_val:
                        if args_val != "":
                            logger.warning(
                                "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments 类型异常 (%s)，重置为 {{}}",
                                self.name, idx, fn.get("name", "?"), type(args_val).__name__,
                            )
                        fn["arguments"] = "{}"
                    else:
                        try:
                            json.loads(args_val)
                        except (json.JSONDecodeError, TypeError):
                            # 尝试修复 JSON 格式问题（MiniMax 返回格式不稳定）
                            repaired = _repair_json_string(args_val)
                            if repaired is not None:
                                logger.info(
                                    "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments JSON 修复成功: %s -> %s",
                                    self.name, idx, fn.get("name", "?"),
                                    args_val[:200], repaired[:200],
                                )
                                fn["arguments"] = repaired
                            else:
                                logger.warning(
                                    "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments JSON 修复失败，重置为 {{}}: %s",
                                    self.name, idx, fn.get("name", "?"), args_val[:500],
                                )
                                fn["arguments"] = "{}"

        # Phase 2: 重定位 assistant(tool_calls) 和 tool 之间的非法消息
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(converted):
            msg = converted[i]
            result.append(msg)

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # 收集紧随其后的 tool 消息
                tool_group: list[dict[str, Any]] = []
                intruders: list[dict[str, Any]] = []
                j = i + 1
                while j < len(converted):
                    if converted[j].get("role") == "tool":
                        tool_group.append(converted[j])
                        j += 1
                    elif tool_group:
                        # 已经有 tool 消息了，后续非 tool 消息是新的对话轮次，停止
                        break
                    else:
                        # assistant(tool_calls) 后第一个消息不是 tool → 非法插入
                        intruders.append(converted[j])
                        j += 1
                if intruders:
                    relocated_count += len(intruders)
                    result.extend(tool_group)
                    # 将非法消息转为 user 角色放在 tool 组之后
                    for intr in intruders:
                        if intr.get("role") not in ("user", "tool"):
                            moved = dict(intr)
                            moved["role"] = "user"
                            moved["name"] = intr.get("role", "system")
                            result.append(moved)
                        else:
                            result.append(intr)
                    i = j
                    continue
                elif tool_group:
                    result.extend(tool_group)
                    i = j
                    continue
            i += 1

        if converted_count:
            logger.info(
                "[%s] MiniMax: 将 %d 条非首位 system 消息转换为 user",
                self.name, converted_count,
            )
        if relocated_count:
            logger.info(
                "[%s] MiniMax: 重定位 %d 条 assistant(tool_calls) 与 tool 之间的非法消息",
                self.name, relocated_count,
            )

        # Phase 3: 最终验证 — 确保每个 tool result 紧跟匹配的 assistant(tool_calls)
        # Phase 0 只检查 tool_call_id 是否存在，不检查位置；Phase 2 只处理
        # assistant(tool_calls) 与 tool 之间的插入消息。这里兜底处理：
        # tool result 出现在非 assistant(tool_calls) 之后的情况（如上下文截断、
        # 执行记录恢复丢失 assistant 消息等）。
        #
        # 改进：当 tool_call_id 精确匹配失败时，按位置顺序尝试匹配。
        # MiniMax M2.7 等模型的 tool_call_id 格式不稳定，可能返回与期望不同的 ID，
        # 但 tool result 的顺序通常与 tool_calls 一致。
        validated: list[dict[str, Any]] = []
        expecting_tool_ids: set[str] = set()
        # 保留有序列表用于位置匹配 fallback
        expecting_tool_ids_ordered: list[str] = []
        dropped_count = 0
        positional_match_count = 0
        for msg in result:
            if msg.get("role") == "assistant":
                if msg.get("tool_calls"):
                    expecting_tool_ids = {
                        tc.get("id")
                        for tc in msg["tool_calls"]
                        if tc.get("id")
                    }
                    expecting_tool_ids_ordered = [
                        tc.get("id")
                        for tc in msg["tool_calls"]
                        if tc.get("id")
                    ]
                else:
                    expecting_tool_ids = set()
                    expecting_tool_ids_ordered = []
                validated.append(msg)
            elif msg.get("role") == "tool":
                if expecting_tool_ids:
                    tc_id = msg.get("tool_call_id")
                    if tc_id in expecting_tool_ids:
                        expecting_tool_ids.discard(tc_id)
                        # 同步更新有序列表
                        if tc_id in expecting_tool_ids_ordered:
                            expecting_tool_ids_ordered.remove(tc_id)
                        validated.append(msg)
                    else:
                        # 精确匹配失败，尝试位置匹配
                        # 如果 tool result 的数量不超过 expecting 数量，
                        # 且此 tool result 的 tool_call_id 也不在任何其他
                        # assistant 消息的 tool_calls 中，按位置分配。
                        if expecting_tool_ids_ordered:
                            matched_id = expecting_tool_ids_ordered.pop(0)
                            expecting_tool_ids.discard(matched_id)
                            positional_match_count += 1
                            logger.info(
                                "[%s] MiniMax Phase 3: positional match — "
                                "tool_call_id %s → %s (expected one of %s)",
                                self.name, tc_id, matched_id,
                                list(expecting_tool_ids) if expecting_tool_ids else "none left",
                            )
                            # 重写 tool 消息的 tool_call_id 为正确的 ID
                            patched_msg = dict(msg)
                            patched_msg["tool_call_id"] = matched_id
                            validated.append(patched_msg)
                        else:
                            dropped_count += 1
                            logger.warning(
                                "[%s] MiniMax Phase 3: dropping tool result with "
                                "unexpected tool_call_id=%s (expected %s)",
                                self.name, tc_id, expecting_tool_ids,
                            )
                else:
                    dropped_count += 1
                    logger.warning(
                        "[%s] MiniMax Phase 3: dropping orphaned tool result "
                        "(tool_call_id=%s, no preceding assistant with tool_calls)",
                        self.name, msg.get("tool_call_id", "?"),
                    )
            else:
                expecting_tool_ids = set()
                expecting_tool_ids_ordered = []
                validated.append(msg)
        if dropped_count:
            logger.warning(
                "[%s] MiniMax Phase 3: dropped %d invalid tool results",
                self.name, dropped_count,
            )
        if positional_match_count:
            logger.info(
                "[%s] MiniMax Phase 3: positionally matched %d tool results "
                "(tool_call_id mismatch but order preserved)",
                self.name, positional_match_count,
            )
        result = validated

        # Phase 4: 补全缺失的 tool result — 确保每个 assistant(tool_calls)
        # 后面紧跟完整的 tool 结果。Minimax 要求每个 tool_call 都必须有对应的
        # tool result，否则报 "tool call result does not follow tool call"。
        final: list[dict[str, Any]] = []
        patch_count = 0
        i = 0
        while i < len(result):
            msg = result[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                final.append(msg)
                required_ids = {
                    tc.get("id")
                    for tc in msg["tool_calls"]
                    if tc.get("id")
                }
                # 收集紧跟的 tool 结果
                j = i + 1
                while j < len(result) and result[j].get("role") == "tool":
                    tc_id = result[j].get("tool_call_id")
                    required_ids.discard(tc_id)
                    final.append(result[j])
                    j += 1
                # 对缺失的 tool_call_id 补充 dummy tool result
                for missing_id in required_ids:
                    patch_count += 1
                    logger.warning(
                        "[%s] MiniMax Phase 4: 补全缺失 tool result tool_call_id=%s",
                        self.name, missing_id,
                    )
                    final.append({
                        "role": "tool",
                        "tool_call_id": missing_id,
                        "content": "Tool execution result unavailable.",
                    })
                i = j
            else:
                final.append(msg)
                i += 1
        if patch_count:
            logger.warning(
                "[%s] MiniMax Phase 4: patched %d missing tool results",
                self.name, patch_count,
            )
        result = final

        # Phase 5: 终极安全网 — 确保所有非首位 system 消息都已转换
        # 根因：StreamRepetitionGuard、ThinkingTruncationGuard 等管道组件
        # 会注入 system 消息，Phase 1-4 的复杂重定位在极端边界可能遗漏。
        # 此阶段做最终扫描，确保 MiniMax 永远不会收到非法 system 消息。
        final_fix_count = 0
        for _i, _m in enumerate(result):
            if _i > 0 and _m.get("role") == "system":
                logger.warning(
                    "[%s] MiniMax Phase 5 安全网: 非首位 system→user "
                    "idx=%d, content=%s",
                    self.name, _i,
                    str(_m.get("content", ""))[:200],
                )
                _m["role"] = "user"
                _m.pop("name", None)
                final_fix_count += 1
        if final_fix_count:
            logger.warning(
                "[%s] MiniMax Phase 5 安全网修复了 %d 条遗漏的 system 消息",
                self.name, final_fix_count,
            )

        return result

    @staticmethod
    def _normalize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> None:
        """确保 assistant 消息中的 tool_calls 使用 OpenAI API 格式。

        执行记录存储的 tool_calls_json 是内部 raw 格式：
            {"id": "...", "name": "...", "arguments": "..."}
        OpenAI API 要求的格式：
            {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}

        缺少 type 字段会导致智谱AI等 API 报"工具类型不能为空"。
        此方法原地修正 messages 中所有 tool_calls 的格式。
        """
        for msg in messages:
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            raw_tcs = msg["tool_calls"]
            if not isinstance(raw_tcs, list):
                continue
            needs_fix = False
            for tc in raw_tcs:
                if not isinstance(tc, dict):
                    continue
                if tc.get("type") != "function" or not isinstance(tc.get("function"), dict):
                    needs_fix = True
                    break
            if not needs_fix:
                continue
            normalized = []
            for tc in raw_tcs:
                if not isinstance(tc, dict):
                    continue
                if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
                    normalized.append(tc)
                    continue
                normalized.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": tc.get("args", tc.get("arguments", "{}")),
                    },
                })
            msg["tool_calls"] = normalized

    def _get_model_string(self) -> str:
        """获取 LiteLLM 格式的模型标识字符串。

        LiteLLM 使用 "provider/model" 格式路由到不同的 LLM 提供商。

        Returns:
            LiteLLM 模型标识字符串
        """
        # 常见提供商映射
        provider_map = {
            "openai": "openai",
            "minimax": "minimax",
            "anthropic": "anthropic",
            "azure": "azure",
            "zhipu_coding": "zai",
            "zhipu": "zai",
        }
        provider_prefix = provider_map.get(self._provider, self._provider)
        return f"{provider_prefix}/{self._model}"

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        *,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> LLMResponse:
        """通过 adapter 调用 LLM。

        Router 模式：用模型 ID 作为路由别名，不传 api_key/api_base/num_retries。
        直连模式：用完整的 "provider/model" 字符串，透传所有参数。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas
            stream: 是否使用流式模式
            on_chunk: 流式回调函数

        Returns:
            统一的 LLMResponse 响应结构
        """
        normalized_messages = self._normalize_messages_for_provider(messages)

        # 主动修复：Phase 1-4 转换后仍可能存在遗漏（极端边界情况），
        # 此处主动修复而非仅做诊断日志。
        if self._provider == "minimax":
            fix_count = 0
            for _i, _m in enumerate(normalized_messages):
                if _i > 0 and _m.get("role") == "system":
                    logger.warning(
                        "[%s] MiniMax 主动修复: 非首位 system→user "
                        "idx=%d, content=%s",
                        self.name, _i,
                        str(_m.get("content", ""))[:200],
                    )
                    _m["role"] = "user"
                    _m.pop("name", None)
                    fix_count += 1
            if fix_count:
                logger.warning(
                    "[%s] MiniMax 主动修复了 %d 条遗漏的 system 消息",
                    self.name, fix_count,
                )

        logger.info(
            "[%s] Sending %d messages to LLM",
            self.name, len(normalized_messages),
        )

        for idx, msg in enumerate(normalized_messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            name = msg.get("name", "")
            tc_list = msg.get("tool_calls", [])
            prefix = f"[{self.name}] MSG-{idx} role={role}"
            if name:
                prefix += f" name={name}"
            if tc_list:
                try:
                    tc_str = json.dumps(
                        tc_list, ensure_ascii=False, default=str
                    )
                except (TypeError, ValueError):
                    tc_str = str(tc_list)
                logger.info(
                    "%s tool_calls=%s", prefix,
                    tc_str if tc_list else "[]",
                )
            else:
                logger.info(
                    "%s content=%s", prefix,
                    str(content) or "",
                )

        if self._use_router:
            # Router 路径：model 用路由别名，凭证和重试由 Router 管理
            kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": normalized_messages,
                **self._default_params,
            }
        else:
            # 直连路径：用完整 litellm 模型字符串，透传凭证和重试
            kwargs = {
                "model": self._get_model_string(),
                "messages": normalized_messages,
                **self._default_params,
            }
            if self._api_base:
                kwargs["api_base"] = self._api_base
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._num_retries:
                kwargs["num_retries"] = self._num_retries
                kwargs["retry_delay"] = self._retry_delay

        tool_schemas = ctx.state.get("tool_schemas", [])
        if tool_schemas:
            logger.info("[%s] tool_schemas count=%d | %s",
                        self.name, len(tool_schemas),
                        ", ".join(t.get("function", {}).get("name", "?") for t in tool_schemas))

        # 调用前记录模型/API 信息
        model_str = self._model
        api_base = kwargs.get("api_base") or self._api_base or "default"
        logger.info(
            "[%s] Calling LLM: model=%s, provider=%s, api_base=%s, streaming=%s",
            self.name, model_str, self._provider, api_base, stream,
        )

        try:
            return await self._adapter.completion(
                model=kwargs.pop("model"),
                messages=kwargs.pop("messages"),
                tools=tool_schemas or None,
                stream=stream,
                on_chunk=on_chunk,
                **kwargs,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] LLM call timed out (model=%s)",
                self.name,
                self._model if self._use_router else self._get_model_string(),
            )
            raise
