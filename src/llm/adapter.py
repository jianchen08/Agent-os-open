"""LLM Adapter 中间层 — 统一 LLM 调用抽象与多模型 fallback。

在 LLMCore 和 litellm 之间加一层抽象，支持：
- 统一的 LLMResponse 响应结构
- 非流式和流式两种调用模式
- 多 key 自动切换（KeyPool + litellm Router 内置）
- reasoning_content（thinking）解析
- tool_calls 解析（非流式和流式增量合并）
- 自适应并发控制：根据限流信号动态调整并发 1-3
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import litellm

from llm.error_classifier import ErrorKind, classify_error
from llm.stream_watchdog import StreamHardTimeout

litellm.suppress_debug_info = True
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_diag_logger = logging.getLogger(__name__ + "._diag")
_diag_logger.propagate = False
_stream_logger = logging.getLogger(__name__ + "._stream")
_stream_logger.propagate = False


def _sync_diag_handlers() -> None:
    """将父 logger 的 FileHandler 同步到 _diag_logger。"""
    if _diag_logger.handlers:
        return
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            _diag_logger.addHandler(h)
            _diag_logger.setLevel(logging.DEBUG)


_THINK_PATTERN = re.compile(
    r"<think[^>]*>(.*?)</think[^>]*>",
    re.DOTALL,
)
_THINK_PATTERN_NO_GT = re.compile(
    r"<think\s(.*?)</think[^>]*>",
    re.DOTALL,
)


def _extract_thinking_from_content(content: str | None) -> tuple[str | None, str | None]:
    """从 content 中提取 <think/> 标签内容，返回 (thinking_text, cleaned_content)。

    MiniMax-M2.7 等推理模型把思考内容包裹在 <think/> 标签中混在 content 字段返回，
    litellm 不会自动映射到 reasoning_content，因此这里手动解析 <think/> 标签，
    将思考内容与正文分离。

    支持两种标签格式：
    1. 标准 XML: <think\\n...\\n</think/> 或 <think type="x">...</think...>
    2. MiniMax: <think\\n...\\n</think/> (开始标签无 >)

    Args:
        content: LLM 返回的原始 content 文本

    Returns:
        (thinking_text, cleaned_content) 元组
    """
    if not content:
        return None, content

    pattern, matches = _THINK_PATTERN, _THINK_PATTERN.findall(content)
    if not matches:
        pattern, matches = _THINK_PATTERN_NO_GT, _THINK_PATTERN_NO_GT.findall(content)
    if not matches:
        return None, content

    thinking = "\n".join(m.strip() for m in matches if m.strip())
    cleaned = pattern.sub("", content).strip()
    return thinking if thinking else None, cleaned if cleaned else None


def _move_to_extra_body(kwargs: dict[str, Any], keys: tuple[str, ...]) -> None:
    """把指定的 kwargs 挪进 extra_body，让 litellm/OpenAI SDK 原样透传给上游。

    litellm 的 openai provider 对部分参数（reasoning_effort、thinking 等）会
    主动拦截或丢弃，但这些参数经 OpenAI 兼容中转端（如 apigo）时上游能接受。
    extra_body 是 OpenAI SDK 的官方透传通道，litellm 把它原样合并进请求 body。

    仅移动 kwargs 中已存在的 key；不存在的跳过。原地修改 kwargs。

    Args:
        kwargs: litellm 调用参数字典（原地修改）
        keys: 需要挪进 extra_body 的参数名
    """
    extra = dict(kwargs.get("extra_body") or {})
    for k in keys:
        if k in kwargs:
            extra[k] = kwargs.pop(k)
    if extra:
        kwargs["extra_body"] = extra


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """统一 LLM 响应结构。

    Attributes:
        text: LLM 响应文本内容
        tool_calls: 解析后的工具调用列表
        thinking_text: 思考过程文本（如 DeepSeek reasoning_content）
        usage: token 用量信息
        stream_repetition: 流式输出是否被检测为重复而截断
        thinking_truncated: 思考内容是否因过长被截断
        stream_truncated: 流式响应是否被 API 侧超时异常截断
            （如推理模型 thinking 正常但正文极少 token 后 SSE 超时）
        finish_reason: LLM 返回的结束原因（stop/length/tool_calls…）。
            ``length`` 表示因命中 max_tokens 被截断，此时 tool_call 的
            arguments JSON 可能不完整，下游需据此识别并处理截断。
    """

    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str | None = None
    usage: dict[str, Any] | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMAdapter(Protocol):
    """LLM 调用适配器抽象接口。

    所有 LLM 调用实现都应遵循此协议，
    包括直接调用 litellm 的适配器和带 fallback 的适配器。
    """

    async def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行 LLM 调用。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            stream: 是否使用流式模式
            on_chunk: 流式回调函数（仅流式模式下使用）
            **kwargs: 其他传递给 litellm 的参数（如 api_base、api_key、temperature 等）

        Returns:
            统一的 LLMResponse 响应结构
        """
        ...

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。

        Args:
            model: LiteLLM 格式的模型标识字符串

        Returns:
            模型是否健康可用
        """
        ...


# ---------------------------------------------------------------------------
# 基类 — 共享响应解析逻辑
# ---------------------------------------------------------------------------


class _BaseLiteLLMAdapter:
    """共享的 LLM 响应解析逻辑。

    子类只需实现 _do_completion() 提供实际的 API 调用入口，
    基类负责非流式/流式调用编排和响应解析。
    """

    async def _do_completion(self, **kwargs: Any) -> Any:
        """执行实际的 LLM API 调用，子类必须覆写。"""
        raise NotImplementedError

    @staticmethod
    def _ensure_minimax_role_safety(
        model: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """防御性兜底：确保 minimax 模型不会收到非首位 system 消息。

        根因：MiniMax API 仅允许首位消息为 system role。管道中的
        StreamRepetitionGuard、ThinkingTruncationGuard 等会注入 system 消息，
        _normalize_messages_for_provider 的 Phase 1-4 已做转换，但极端边界
        情况可能遗漏。此方法作为最后一道防线，在 adapter 层拦截。

        Args:
            model: LiteLLM 模型标识字符串（如 "minimax/MiniMax-M2.7"）
            messages: 对话消息列表

        Returns:
            修正后的消息列表（原地修改 + 返回引用）
        """
        # 检测是否为 minimax 模型
        if "minimax" not in model.lower():
            return messages

        needs_fix = False
        for i, msg in enumerate(messages):
            if i > 0 and msg.get("role") == "system":
                needs_fix = True
                break

        if not needs_fix:
            return messages

        for i, msg in enumerate(messages):
            if i > 0 and msg.get("role") == "system":
                msg["role"] = "user"
                msg.pop("name", None)
                logger.warning(
                    "[adapter] Minimax 兜底: 非首位 system→user idx=%d content=%s",
                    i,
                    str(msg.get("content", ""))[:100],
                )
        return messages

    async def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行 LLM 调用，支持非流式和流式两种模式。"""
        # 防御性兜底：确保 minimax 不会收到非法 system 消息
        self._ensure_minimax_role_safety(model, messages)

        # provider 适配：按 provider 规则裁剪/转换消息（如 DeepSeek 采样保留 rc）
        # 透传 **kwargs（即 default_params），adapter 按需读取自身配置
        from llm.provider_adapters import get_provider_adapter  # noqa: PLC0415

        adapter = get_provider_adapter(model)
        messages = adapter.adapt_messages_before_send(messages, **kwargs)

        # 弹出 adapter 专属参数（不发给 litellm / API）
        kwargs.pop("reasoning_retention", None)

        # openai/ 前缀的中转端点：litellm openai provider 不认 reasoning_effort
        # 等专有参数，故挪进 extra_body 透传（上游本身能接受）。
        if model.lower().startswith("openai/"):
            _move_to_extra_body(kwargs, ("reasoning_effort", "thinking"))

        if stream:
            return await self._call_streaming(model, messages, tools=tools, on_chunk=on_chunk, **kwargs)
        return await self._call_non_streaming(model, messages, tools=tools, **kwargs)

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。"""
        try:
            response = await self._do_completion(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return bool(response.choices)
        except Exception as exc:
            logger.warning(
                "[%s] health_check 失败 model=%s: %s — %s",
                type(self).__name__,
                model,
                type(exc).__name__,
                exc,
            )
            return False

    async def _call_non_streaming(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """非流式调用 LLM。"""
        # 流式专属参数对非流式无意义，pop 出来不传给 litellm（与流式路径对齐）。
        # inter_chunk_timeout 是 plugin 传入的 call_timeout，复用为非流式整体超时。
        call_timeout = float(kwargs.pop("inter_chunk_timeout", 300))
        kwargs.pop("first_chunk_timeout", None)
        kwargs.pop("max_thinking_chars", None)

        # 非流式路径必须显式传 float 类型 timeout：litellm 的 Router 默认（yaml
        # call_timeout，可能是 int）或自身默认 int，传给 zai 会触发
        # "Timeout needs to be a float"。显式设 float，与流式路径（3600.0）对齐。
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": call_timeout,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        # drop_params 与流式路径对齐：openai provider 不接受 thinking /
        # reasoning_effort 等 deepseek/anthropic 专有参数（自定义中转端点经
        # type=openai 接入时常见），不丢会抛 UnsupportedParamsError。
        response = await self._do_completion(**call_kwargs, drop_params=True)

        choice = response.choices[0]
        result_text = choice.message.content
        tool_calls = self._parse_tool_calls(choice.message.tool_calls)

        # 优先从 reasoning_content 提取思考内容
        thinking_text: str | None = None
        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
            thinking_text = choice.message.reasoning_content
            if not result_text:
                result_text = thinking_text
                logger.info(
                    "[%s] 使用 reasoning_content 作为 result_text (len=%d)",
                    type(self).__name__,
                    len(result_text),
                )

        # 兜底：当 reasoning_content 为空时，手动从 content 中提取 <think/> 标签
        if not thinking_text and result_text:
            extracted_thinking, cleaned_content = _extract_thinking_from_content(result_text)
            if extracted_thinking:
                thinking_text = extracted_thinking
                result_text = cleaned_content
                logger.info(
                    "[%s] 从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    type(self).__name__,
                    len(thinking_text),
                    len(result_text or ""),
                )

        # 解析 usage 信息
        usage: dict[str, Any] | None = None
        if hasattr(response, "usage") and response.usage:
            _prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
                "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
            }

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def _call_streaming(  # noqa: PLR0915
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """流式调用 LLM。"""
        # 流式超时：首个 chunk 检测连接是否建立，后续 chunk 防止连接僵死。
        # 必须在构造 call_kwargs 之前 pop 出来，否则会被 **kwargs 塞进
        # litellm 请求参数（litellm 不识别这两个 key）。
        first_chunk_timeout = float(kwargs.pop("first_chunk_timeout", 120))
        # inter-chunk 静默超时：连续 N 秒收不到任何 chunk 即判定上游/传输静默，
        # 抛 litellm.Timeout 中断死等。每个 chunk 到达即重置计时器（见下方主循环
        # asyncio.wait_for），故活跃推理（reasoning 持续吐 chunk）永不触发，只有
        # 真正静默（连接挂起/上游冻结）才在 N 秒后掐断。生产由插件传入 stream_idle_timeout
        # 覆盖；此处默认 600s 为直连/测试调用兜底。
        inter_chunk_timeout = float(kwargs.pop("inter_chunk_timeout", 600))

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        # timeout 必须设大值：httpx 的 read timeout 作用于流式连接的每一次 socket
        # 读取，不能用 first_chunk_timeout，否则长 reasoning 间隙会被掐断。
        call_kwargs["timeout"] = 3600.0

        # 首字节超时统一覆盖"建连→等响应头→首字节"全过程：把 first_chunk_timeout 的
        # wait_for 同时包住 _do_completion 和首 chunk 读取。
        # 上游"半死连接"（TCP 建连成功、请求已发出，但上游既不回数据也不断开）会让
        # _do_completion 卡在 litellm.acompletion 的建连/等响应头阶段——既不是 429，
        # 也不是连接错误，若 wait_for 仅包首个 __anext__() 则因 _do_completion 尚未
        # 返回而无法启动，请求会静默挂死直到 1 小时的 httpx timeout。

        async def _open_and_first_chunk() -> tuple[Any, Any]:
            """建连并读取首个 chunk，供外层 wait_for 统一限时。

            首个 chunk 读取若抛异常（含 wait_for 超时注入的 CancelledError），
            必须关闭 stream——既为释放 HTTP 连接，也为触发 _bind_release_to_stream
            绑定的 slot.release()，避免并发许可泄漏（建连超时是高频场景）。
            """
            resp = await self._do_completion(**call_kwargs, drop_params=True)
            try:
                first = await resp.__aiter__().__anext__()
            except BaseException:
                # 超时/异常/取消：关闭流，触发绑定的 release。aclose 自身的任何
                # 异常（含 CancelledError）都不应掩盖/替换原始异常，故全量抑制。
                aclose = getattr(resp, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except BaseException:
                        pass
                raise
            return resp, first

        first_chunk: Any = None
        try:
            response, first_chunk = await asyncio.wait_for(_open_and_first_chunk(), timeout=first_chunk_timeout)
        except StopAsyncIteration:
            # 空流：建连成功但首字节即 EOF（零 chunk），按首 token 失败处理。
            # resp 已在 _open_and_first_chunk 内部 aclose，此处无需再关。
            logger.warning(
                "[%s] STREAM EMPTY: 首字节即空流 (建连成功但零 chunk) model=%s，按首 token 失败处理",
                type(self).__name__,
                model,
            )
            raise litellm.Timeout(  # noqa: B904
                message=("Stream first chunk empty: server returned 200 but zero chunks (premature EOF)"),
                model=model,
                llm_provider="zai",
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] STREAM TIMEOUT: first chunk 超时 (%.0fs) 含建连阶段 model=%s",
                type(self).__name__,
                first_chunk_timeout,
                model,
            )
            raise litellm.Timeout(  # noqa: B904
                message=(f"Stream first chunk timeout (incl. connect): no response for {first_chunk_timeout:.0f}s"),
                model=model,
                llm_provider="zai",
            )

        result_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}
        stream_usage: dict[str, Any] | None = None
        _stream_start: float = _time.monotonic()
        # inter-chunk 静默追踪：每个 chunk 到达即更新 _last_chunk_monotonic，
        # 心跳据此量化"距上个 chunk 多久"，是区分"上游不发"与"接收端卡死"的关键。
        _last_chunk_monotonic: float = _stream_start
        _chunks_received: int = 0
        # litellm CustomStreamWrapper 的底层流对象（openai/zai 路径即 httpx.Response）。
        # 心跳日志读其 is_closed 作为半死 TCP 的廉价（不可靠但便宜）附加信号。
        _completion_stream: Any = getattr(response, "completion_stream", None)
        # 心跳探针任务句柄（首 chunk 后启动，finally 中取消）。
        _heartbeat_task: asyncio.Task[None] | None = None
        # 独立线程硬超时句柄（首 chunk 后 arm，finally 中 disarm）。
        # asyncio 心跳/inter_chunk wait_for 在 loop 被 socket 阻塞冻住时全部
        # 失效（实测：僵死管道零 HEARTBEAT 日志）。watchdog 用 threading 线程
        # 倒计时，到点强制 stream.aclose() 打破死锁，是 loop 冻住也能生效的兜底。
        _hard_timeout: StreamHardTimeout | None = None

        stream_repetition = False
        thinking_truncated = False
        _max_thinking_chars = int(kwargs.pop("max_thinking_chars", 180000))
        # 接收端点诊断：统计 tool_calls 字段从 API 到达次数，定位丢失环节
        _recv_seq = 0
        _recv_tc_count = 0
        _finish_reason: str | None = None

        # 流式 <think/> 标签状态机。MiniMax 等模型的思考内容通过 <think/> 标签
        # 包裹在 delta.content 中返回（而非 delta.reasoning_content），且标签会
        # 跨多个 chunk 切分。状态机通过 "<think" / "</think" 字符串查找跟踪开/闭状态，
        # 确保 thinking 内容正确路由到 thinking 通道、正文路由到 text 通道。
        _in_think_tag: bool = False

        aiter = response.__aiter__()
        try:
            # 首个 chunk 已在 _open_and_first_chunk 内读取（含建连阶段的超时保护，
            # 见上方首字节超时统一覆盖的说明）。此处直接处理它。
            chunk = first_chunk

            # 边收边处理，保持真正的流式
            # _process_chunk 内联处理每个 chunk
            async def _process_chunk(chunk: Any) -> bool:  # noqa: PLR0911,PLR0912,PLR0915
                """处理单个 chunk，返回是否应该 break。"""
                nonlocal stream_repetition, _in_think_tag, stream_usage, thinking_truncated
                # 流式诊断：只写文件，不显示在 CLI
                _chunk_idx = len(result_parts) + len(thinking_parts)
                if _chunk_idx <= 1 or _chunk_idx % 200 == 0:
                    _sync_diag_handlers()
                    if _diag_logger.handlers:
                        _delta = getattr(
                            getattr(chunk, "choices", [None])[0],
                            "delta",
                            None,
                        )
                        _tc = getattr(_delta, "tool_calls", None)
                        _usage = getattr(chunk, "usage", None)
                        if _chunk_idx <= 1 or _tc or _usage:
                            _rc = getattr(_delta, "reasoning_content", None)
                            _ct = getattr(_delta, "content", None)
                            _diag_logger.debug(
                                "[%s] chunk #%d: content=%s reasoning=%s tc=%s usage=%s",
                                type(self).__name__,
                                _chunk_idx,
                                repr((_ct or "")[:40]),
                                repr((_rc or "")[:40]) if _rc else "-",
                                "Y" if _tc else "-",
                                "Y" if _usage else "-",
                            )
                # 收集流式 usage（通常在最后一个 chunk）
                if hasattr(chunk, "usage") and chunk.usage:
                    _prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
                    stream_usage = {
                        "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                        "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
                    }

                if not chunk.choices:
                    return False

                delta = chunk.choices[0].delta

                # LiteLLM 统一推理内容映射到 delta.reasoning_content
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking_parts.append(reasoning)
                    _stream_logger.debug(
                        "[STREAM][THINKING] #%d +%d chars",
                        len(thinking_parts),
                        len(reasoning),
                    )
                    if on_chunk:
                        on_chunk({"type": "thinking", "content": reasoning})
                    # 思考内容过长 → 截断
                    thinking_len = sum(len(p) for p in thinking_parts)
                    if _max_thinking_chars > 0 and thinking_len > _max_thinking_chars:
                        logger.warning(
                            "[%s] 思考内容过长(%d>%d chars)，截断",
                            type(self).__name__,
                            thinking_len,
                            _max_thinking_chars,
                        )
                        thinking_truncated = True
                        return True

                # 文本内容：流式 <think/> 状态机处理（MiniMax 等模型）
                if delta.content:
                    content = delta.content

                    if _in_think_tag:
                        # 标签内：检查闭合标签
                        if "</think" in content:
                            close_idx = content.index("</think")
                            _think_part = content[:close_idx]
                            if _think_part:
                                thinking_parts.append(_think_part)
                                if on_chunk:
                                    on_chunk({"type": "thinking", "content": _think_part})
                            _after_close = content[close_idx:]
                            _gt = _after_close.find(">")
                            _rest = _after_close[_gt + 1 :] if _gt >= 0 else ""
                            _in_think_tag = False
                            if _rest.strip():
                                result_parts.append(_rest)
                                if on_chunk:
                                    signal = on_chunk({"type": "text", "content": _rest})
                                    if signal == "stop":
                                        stream_repetition = True
                                        return True
                        else:
                            thinking_parts.append(content)
                            _stream_logger.debug(
                                "[STREAM][THINKING] #%d +%d chars",
                                len(thinking_parts),
                                len(content),
                            )
                            if on_chunk:
                                on_chunk({"type": "thinking", "content": content})
                            thinking_len = sum(len(p) for p in thinking_parts)
                            if _max_thinking_chars > 0 and thinking_len > _max_thinking_chars:
                                logger.warning(
                                    "[%s] 思考内容过长 (%d>%d chars)，截断",
                                    type(self).__name__,
                                    thinking_len,
                                    _max_thinking_chars,
                                )
                                thinking_truncated = True
                                return True
                    # 标签外：检查开标签
                    elif "<think" in content:
                        _open_idx = content.index("<think")
                        _before = content[:_open_idx]
                        if _before:
                            result_parts.append(_before)
                            if on_chunk:
                                on_chunk({"type": "text", "content": _before})
                        _after_open = content[_open_idx:]
                        _gt = _after_open.find(">")
                        _inner = _after_open[_gt + 1 :] if _gt >= 0 else ""
                        _in_think_tag = True
                        if "</think" in _inner:
                            _ci = _inner.index("</think")
                            _tp = _inner[:_ci]
                            if _tp:
                                thinking_parts.append(_tp)
                                if on_chunk:
                                    on_chunk({"type": "thinking", "content": _tp})
                            _ac = _inner[_ci:]
                            _g2 = _ac.find(">")
                            _rs = _ac[_g2 + 1 :] if _g2 >= 0 else ""
                            _in_think_tag = False
                            if _rs.strip():
                                result_parts.append(_rs)
                                if on_chunk:
                                    signal = on_chunk({"type": "text", "content": _rs})
                                    if signal == "stop":
                                        stream_repetition = True
                                        return True
                        elif _inner:
                            thinking_parts.append(_inner)
                            _stream_logger.debug(
                                "[STREAM][THINKING] #%d +%d chars",
                                len(thinking_parts),
                                len(_inner),
                            )
                            if on_chunk:
                                on_chunk({"type": "thinking", "content": _inner})
                    else:
                        if on_chunk and thinking_parts:
                            on_chunk({"type": "thinking_end", "content": ""})
                        result_parts.append(content)
                        _stream_logger.debug(
                            "[STREAM][TEXT] #%d +%d chars: %s",
                            len(result_parts),
                            len(content),
                            repr(content[:80]),
                        )
                        if on_chunk:
                            signal = on_chunk({"type": "text", "content": content})
                            if signal == "stop":
                                stream_repetition = True
                                logger.warning(
                                    "[%s] 收到 stop 信号，截断流式输出",
                                    type(self).__name__,
                                )
                                return True

                # 工具调用（流式增量）
                if delta.tool_calls:
                    # thinking→tool_calls 过渡：发送 thinking_end 确保思考完整关闭后再输出工具卡片
                    if on_chunk and thinking_parts:
                        on_chunk({"type": "thinking_end", "content": ""})
                    for tc in delta.tool_calls:
                        idx = tc.index if hasattr(tc, "index") else 0
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": (getattr(tc, "id", None) or f"tc_{idx}_{id(tool_calls_map)}"),
                                "name": "",
                                "arguments": "",
                            }
                            _stream_logger.debug(
                                "[STREAM][TOOL_CALL] #%d new: id=%s",
                                idx,
                                tool_calls_map[idx]["id"],
                            )
                        if tc.function:
                            if tc.function.name:
                                tool_calls_map[idx]["name"] += tc.function.name
                                _stream_logger.debug(
                                    "[STREAM][TOOL_CALL] #%d name=%s",
                                    idx,
                                    tool_calls_map[idx]["name"],
                                )
                            if tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += tc.function.arguments
                                _arg_len = len(tool_calls_map[idx]["arguments"])
                                _stream_logger.debug(
                                    "[STREAM][TOOL_CALL] #%d args +%d → %d chars: %s",
                                    idx,
                                    len(tc.function.arguments),
                                    _arg_len,
                                    repr(tc.function.arguments[:100]),
                                )

                    if on_chunk:
                        on_chunk(
                            {
                                "type": "tool_call",
                                "tool_calls": delta.tool_calls,
                            }
                        )
                return False

            # 处理首个 chunk
            await _process_chunk(chunk)
            _last_chunk_monotonic = _time.monotonic()
            _chunks_received += 1
            # 启动心跳探针：流静默时持续打 idle 时长 + stream_closed，
            # 证明接收协程存活（排除接收端死锁），并量化上游/传输静默时长。
            # 沿用 process_manager._watchdog_loop 的 create_task + CancelledError 退出范式。
            _heartbeat_task = asyncio.create_task(
                self._stream_heartbeat(
                    model,
                    inter_chunk_timeout,
                    lambda: _time.monotonic() - _last_chunk_monotonic,
                    lambda: _chunks_received,
                    _completion_stream,
                )
            )
            # 独立线程硬超时兜底：上述 asyncio 心跳/inter_chunk wait_for 共享同一
            # event loop，一旦底层 socket 阻塞冻住事件循环，全部失效（僵死管道零
            # HEARTBEAT 即铁证）。硬超时到点强制 aclose，loop 冻住也能打破死锁。
            # 语义为"chunk 间隔超时"：每收到一个 chunk 调 reset() 重新计时，
            # 避免误杀总时长长但 chunk 间隔始终健康的流（issue: 长流式响应总时长
            # 超过 inter_chunk_timeout 时被误关）。
            _hard_timeout = StreamHardTimeout(
                response,
                asyncio.get_running_loop(),
                inter_chunk_timeout,
            )
            _hard_timeout.arm()
            # 接收端点诊断（首个 chunk）
            _recv_seq += 1
            try:
                _rc0 = chunk.choices[0] if chunk.choices else None
                if _rc0 is not None:
                    _d0 = getattr(_rc0, "delta", None)
                    _fr0 = getattr(_rc0, "finish_reason", None)
                    _tc0 = getattr(_d0, "tool_calls", None) if _d0 else None
                    if _tc0:
                        _recv_tc_count += 1
                        _tc_summary0 = []
                        for _tci in _tc0:
                            _fn0 = getattr(_tci, "function", None)
                            _tc_name0 = getattr(_fn0, "name", "?") if _fn0 else "?"
                            _tc_args0 = getattr(_fn0, "arguments", "") if _fn0 else ""
                            _tc_summary0.append(f"{_tc_name0}(args={len(_tc_args0)}c)")
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d tool_calls 到达(首chunk, %d个): %s",
                            _recv_seq,
                            len(_tc0),
                            ", ".join(_tc_summary0),
                        )
                    if _fr0:
                        _finish_reason = _fr0
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d finish=%s (首chunk, 累计tc=%d)",
                            _recv_seq,
                            _fr0,
                            _recv_tc_count,
                        )
            except Exception:
                pass

            # 后续 chunk：逐次 wait_for 超时，每个 chunk 到达即重置计时器。
            # 活跃推理 chunk 间隔远小于 timeout 故不误触发；仅真正静默（死连接）累计满 timeout。
            while True:
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=inter_chunk_timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    _idle = _time.monotonic() - _last_chunk_monotonic
                    logger.warning(
                        "[%s] STREAM TIMEOUT: inter-chunk 静默超时 (%.0fs) 距上个 chunk #%d 已静默 %.0fs model=%s",
                        type(self).__name__,
                        inter_chunk_timeout,
                        _chunks_received,
                        _idle,
                        model,
                    )
                    raise litellm.Timeout(  # noqa: B904
                        message=(
                            "Stream inter-chunk timeout:"
                            f" no data for {_idle:.0f}s"
                            f" (last chunk #{_chunks_received}, timeout={inter_chunk_timeout:.0f}s)"
                        ),
                        model=model,
                        llm_provider="zai",
                    )
                _last_chunk_monotonic = _time.monotonic()
                _chunks_received += 1
                # chunk 健康到达：重置硬超时倒计时（chunk 间隔语义，避免误杀长流）
                if _hard_timeout is not None:
                    _hard_timeout.reset()
                if await _process_chunk(chunk):
                    break
                # ── 接收端点诊断：每个 chunk 检查 delta.tool_calls 是否到达 ──
                _recv_seq += 1
                try:
                    _rc = chunk.choices[0] if chunk.choices else None
                    if _rc is not None:
                        _d = getattr(_rc, "delta", None)
                        _fr = getattr(_rc, "finish_reason", None)
                        _tc = getattr(_d, "tool_calls", None) if _d else None
                        if _tc:
                            _recv_tc_count += 1
                            # 打印完整 tool_call 内容（name + arguments 长度 + 预览）
                            _tc_summary = []
                            for _tci in _tc:
                                _fn = getattr(_tci, "function", None)
                                _tc_name = getattr(_fn, "name", "?") if _fn else "?"
                                _tc_args = getattr(_fn, "arguments", "") if _fn else ""
                                _tc_summary.append(f"{_tc_name}(args={len(_tc_args)}c)")
                            _stream_logger.debug(
                                "[STREAM][RECV] #%d tool_calls 到达(%d个): %s",
                                _recv_seq,
                                len(_tc),
                                ", ".join(_tc_summary),
                            )
                            _finish_reason = _fr
                            _stream_logger.debug(
                                "[STREAM][RECV] #%d finish=%s (累计tc=%d)",
                                _recv_seq,
                                _fr,
                                _recv_tc_count,
                            )
                except Exception:
                    pass
        finally:
            # 取消心跳探针任务（避免任务泄漏：超时/异常/正常结束都要清理）
            if _heartbeat_task is not None and not _heartbeat_task.done():
                _heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await _heartbeat_task
            # 取消独立线程硬超时（正常结束时不触发强制关闭，幂等）
            if _hard_timeout is not None:
                _hard_timeout.disarm()
            # 确保超时或异常时关闭 async iterator，释放 HTTP 连接
            if hasattr(response, "aclose"):
                await response.aclose()

        result_text = "".join(result_parts) if result_parts else None
        thinking_text = "".join(thinking_parts) if thinking_parts else None
        tool_calls = self._normalize_tool_calls(tool_calls_map)

        # 流式接收完成：记录速度统计
        _stream_elapsed = _time.monotonic() - _stream_start
        _comp_tokens = (stream_usage or {}).get("completion_tokens", 0)
        _speed = (_comp_tokens / _stream_elapsed) if _stream_elapsed > 0 and _comp_tokens else 0
        _stream_logger.debug(
            "[STREAM][DONE] finish=%s text=%d chars thinking=%d chars "
            "chunks=%d tool_calls=%d "
            "tokens=%d elapsed=%.2fs speed=%.1f tok/s",
            _finish_reason,
            len(result_text or ""),
            len(thinking_text or ""),
            len(result_parts) + len(thinking_parts),
            len(tool_calls),
            _comp_tokens,
            _stream_elapsed,
            _speed,
        )
        # 接收端点汇总：API 端实际送达的 tool_calls chunk 数 vs 最终解析数
        _stream_logger.debug(
            "[STREAM][STATS] recv_chunks=%d recv_tc=%d parsed_tc=%d",
            _recv_seq,
            _recv_tc_count,
            len(tool_calls),
        )

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=stream_usage,
            stream_repetition=stream_repetition,
            thinking_truncated=thinking_truncated,
            finish_reason=_finish_reason,
        )

    async def _stream_heartbeat(
        self,
        model: str,
        inter_chunk_timeout: float,
        idle_getter: Callable[[], float],
        chunks_getter: Callable[[], int],
        completion_stream: Any,
    ) -> None:
        """流式心跳探针：周期性打 idle 时长 + stream_closed 信号。

        诊断目标（区分"上游/API 端不发"vs"我们接收端卡死"）：
          - 心跳持续输出 → 接收协程存活，非接收端死锁；
          - idle 时长持续增长 → 上游/传输静默（接收端在等，没人发）；
          - idle 在心跳间隔(30s)附近震荡 → 正常活跃流。

        idle 接近 timeout/2 时升级为 WARNING，使静默即将触发超时时醒目可见。
        stream_closed 取底层 httpx Response.is_closed（对静默半死 TCP 仍可能为
        False，仅作廉价附加信号，不可靠不独断）。

        沿用 process_manager._watchdog_loop 的范式：CancelledError 单独捕获并退出，
        其他异常吞掉保持循环存活。由 _call_streaming 的 finally 负责取消。

        Args:
            model: 模型标识（日志用）
            inter_chunk_timeout: inter-chunk 静默超时阈值（用于决定日志级别）
            idle_getter: 返回距上个 chunk 的秒数（闭包读 _last_chunk_monotonic）
            chunks_getter: 返回累计收到的 chunk 数（闭包读 _chunks_received）
            completion_stream: litellm 底层流对象（读 is_closed）
        """
        half = inter_chunk_timeout / 2
        try:
            while True:
                await asyncio.sleep(30.0)
                idle = idle_getter()
                received = chunks_getter()
                closed = getattr(completion_stream, "is_closed", None) if completion_stream is not None else None
                _stream_logger.log(
                    logging.WARNING if idle >= half else logging.DEBUG,
                    "[STREAM][HEARTBEAT] idle=%.0fs since chunk #%d total=%d stream_closed=%s model=%s",
                    idle,
                    received,
                    received,
                    closed,
                    model,
                )
        except asyncio.CancelledError:
            pass

    def _parse_tool_calls(self, raw_tool_calls: Any) -> list[dict[str, Any]]:
        """解析非流式响应中的 tool_calls。"""
        if not raw_tool_calls:
            return []

        parsed: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            parsed.append(
                {
                    "id": getattr(tc, "id", None) or f"call_{len(parsed)}",
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            )
        return parsed

    def _normalize_tool_calls(self, tool_calls_map: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        """将流式收集的 tool_calls 映射归一化。"""
        if not tool_calls_map:
            return []

        result: list[dict[str, Any]] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            result.append(
                {
                    "id": tc.get("id") or f"call_{idx}",
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                }
            )
        return result


# ---------------------------------------------------------------------------
# LiteLLM 适配器 — 直接调用 litellm.acompletion()
# ---------------------------------------------------------------------------


class LiteLLMAdapter(_BaseLiteLLMAdapter):
    """基于 litellm.acompletion() 的 LLM 调用适配器。

    直接调用 litellm 的 acompletion 函数，不经过 Router。
    适用于不需要并发控制的场景或测试环境。
    """

    async def _do_completion(self, **kwargs: Any) -> Any:
        """调用 litellm.acompletion()。"""
        return await litellm.acompletion(**kwargs)


# ---------------------------------------------------------------------------
# KeyPool 适配器 — 基于 KeyPool 的多 key 聚合 + RPM 限流
# ---------------------------------------------------------------------------


class KeyPoolAdapter(_BaseLiteLLMAdapter):
    """基于 KeyPool 的 LLM 调用适配器。

    按 API key 做并发控制（一个 key 一个信号量 + RPM + 配额）。

    多 key 场景下：
    - 请求前从 KeyPool 选一个最优 key（余量最多）
    - 通过该 key 的信号量控制并发
    - 成功后记录 usage，429 后冷却该 key
    - 所有 key 共享同一个 litellm.Router 的 fallback 能力

    无 KeyPool 的 provider 回退到 Router 默认行为（不限流）。
    """

    def __init__(
        self,
        router: Any,
        *,
        default_max_concurrent: int = 2,
    ) -> None:
        self._router = router
        self._default_max_concurrent = default_max_concurrent

    def _resolve_provider(self, model: str) -> str:
        """从 model_id 查找 provider 名称。

        优先用 router_factory 的映射表（model_id → provider），
        兜底用 litellm 前缀反查。
        """
        from llm.router_factory import (  # noqa: PLC0415
            get_key_pool,
            get_provider_for_model,
        )

        # 去掉 litellm 前缀（"zai/glm-5.1" → "glm-5.1"）
        model_id = model.split("/", 1)[1] if "/" in model else model

        # 直接查映射表
        provider = get_provider_for_model(model_id)
        if provider and get_key_pool(provider):
            return provider
        return ""

    def _extract_model_name(self, kwargs: dict[str, Any]) -> str:
        """从 kwargs 中提取 model_name（去掉 provider 前缀）。"""
        model = kwargs.get("model", "")
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    async def _do_completion(self, **kwargs: Any) -> Any:  # noqa: PLR0912,PLR0915
        from llm.key_pool import KeySlot  # noqa: PLC0415
        from llm.router_factory import get_key_pool  # noqa: PLC0415

        model_str = kwargs.get("model", "")
        provider_name = self._resolve_provider(model_str)
        pool = get_key_pool(provider_name) if provider_name else None

        if pool is None:
            # 无 KeyPool，直接走 Router
            return await self._route_call(**kwargs)

        # 尝试每个可用 key，失败后自动换下一个重试
        max_retries = len(pool.slots)
        last_exc: Exception | None = None

        from llm.exceptions import KeyPoolExhaustedError  # noqa: PLC0415

        try:
            for attempt in range(max_retries):
                slot: KeySlot = await pool.acquire_slot()
                logger.info(
                    "[KeyPoolAdapter] provider=%s 选用 key=%s (api_key=%s...) attempt=%d/%d",
                    provider_name,
                    slot.key_id,
                    slot.api_key[:6],
                    attempt + 1,
                    max_retries,
                )
                # 信号量释放：流式路径的真正传输在调用方消费 stream wrapper 期间，
                # 故 release 推迟到 stream.aclose；非流式 finally 立即 release。
                # 用 _defer_release 标志区分两条路径。
                _defer_release = False
                try:
                    key_kwargs = dict(kwargs)
                    key_kwargs["api_key"] = slot.api_key
                    if slot.api_base:
                        key_kwargs.setdefault("api_base", slot.api_base)

                    result = await self._direct_call_with_slot(slot=slot, **key_kwargs)

                    slot.on_success()
                    # 流式返回值是 async iterator（CustomStreamWrapper），其流式
                    # 传输尚未发生——把 release 绑定到 aclose，由消费方在流结束后触发。
                    if hasattr(result, "__aiter__"):
                        _defer_release = True
                        self._bind_release_to_stream(result, slot)
                    return result
                except asyncio.CancelledError:
                    # 用户取消：不冷却，直接抛
                    raise
                except Exception as exc:
                    # 统一异常处理：先翻译成 ErrorInfo，再按 kind 决策
                    info = classify_error(exc)

                    # BAD_REQUEST 是不可恢复的参数错误，直接抛（不换 key）
                    if info.kind == ErrorKind.BAD_REQUEST:
                        logger.warning(
                            "[KeyPoolAdapter] BAD_REQUEST 不可恢复 → key=%s: %s",
                            slot.key_id,
                            str(exc)[:200],
                        )
                        raise

                    # SERVICE_DOWN：上游临时挂，退避后重试。
                    # handle_error 会从第 2 次起给 key 置短冷却，所以 finally 的
                    # release + 下一轮 acquire_slot 中，select() 会暂时绕开这个
                    # key（单 key 场景则等到冷却到期再重试），避免无限选回坏 key。
                    if info.kind == ErrorKind.SERVICE_DOWN:
                        backoff = min(2.0 * (2**slot._consecutive_down), 16.0)
                        logger.warning(
                            "[KeyPoolAdapter] SERVICE_DOWN → key=%s 退避 %.1fs 重试 (attempt %d/%d): %s",
                            slot.key_id,
                            backoff,
                            attempt + 1,
                            max_retries,
                            str(exc)[:150],
                        )
                        slot.handle_error(info)
                        await asyncio.sleep(backoff)
                        last_exc = exc
                    else:
                        # 其他可恢复错误：交给 KeySlot 统一策略处理（冷却/降级/不冷却）
                        slot.handle_error(info)
                        logger.info(
                            "[KeyPoolAdapter] %s → key=%s 处理 (attempt %d/%d)",
                            info.kind.value,
                            slot.key_id,
                            attempt + 1,
                            max_retries,
                        )
                        last_exc = exc
                finally:
                    # 流式成功路径已把 release 延迟到 stream.aclose，这里跳过；
                    # 其余路径（异常/非流式成功）立即释放，保证换 key 重试时槽位归还。
                    if not _defer_release:
                        slot.release()
        except KeyPoolExhaustedError as exc:
            # 所有 key 不可用且等待超时：不可恢复的资源耗尽，
            # 转成业务可读的 RateLimitError，保留原始异常链（backend_rules §3.1）。
            logger.error(
                "[KeyPoolAdapter] key 池耗尽 provider=%s model=%s: %s",
                provider_name,
                model_str,
                exc,
            )
            last_exc = litellm.RateLimitError(
                message=f"所有 API key 不可用且等待超时（{exc.timeout:.0f}s）；不可用 key 诊断: {exc.unavailable}",
                model=model_str,
                llm_provider=provider_name or "unknown",
            )
            last_exc.__cause__ = exc

        # 所有 key 都试过了或 pool 已耗尽 → 尝试 Router fallback
        # 走 router.acompletion() 利用 llm.yaml 的 fallback_chain 配置
        # 切换到备用模型（如 deepseek-v4-pro → minimax-m3）
        logger.warning(
            "[KeyPoolAdapter] 所有 key 均失败 provider=%s model=%s，尝试 Router fallback...",
            provider_name,
            model_str,
        )
        try:
            return await self._route_call(**kwargs)
        except Exception as fb_exc:
            logger.error(
                "[KeyPoolAdapter] Router fallback 也失败: %s",
                fb_exc,
            )
            if last_exc is not None:
                raise last_exc  # noqa: B904
            raise fb_exc

    async def _route_call(self, **kwargs: Any) -> Any:
        """无 KeyPool 时的回退路径，动态获取最新 Router。

        不缓存 self._router，而是每次调用时通过 get_or_create_router() 动态获取最新
        Router，若模块级单例已被重置（前端修改模型配置后 invalidate_all_llm_caches()
        会清除）则自动从 YAML 重建，确保模型配置变更对 KeyPoolAdapter 立即生效。
        """
        from config.models import get_model_config_loader  # noqa: PLC0415
        from llm.router_factory import get_or_create_router  # noqa: PLC0415

        model_loader = get_model_config_loader()
        router = get_or_create_router(model_loader)
        return await router.acompletion(**kwargs)

    @staticmethod
    def _bind_release_to_stream(stream: Any, slot: Any) -> None:
        """把 slot.release() 绑定到 stream.aclose()，流关闭时释放并发许可。

        流式调用返回的 stream wrapper（litellm CustomStreamWrapper）是惰性对象，
        真正的流式传输发生在调用方消费它期间。信号量许可必须覆盖整段传输，
        故 release 推迟到 stream 被关闭（_call_streaming 的 finally 调用 aclose）。

        用一次性标志保证 release 只执行一次（litellm 可能多次调用 aclose）。
        """
        original_aclose = getattr(stream, "aclose", None)
        released = False

        async def _aclose_with_release() -> None:
            nonlocal released
            if not released:
                released = True
                slot.release()
            if original_aclose is not None:
                await original_aclose()

        stream.aclose = _aclose_with_release  # type: ignore[method-assign]

    async def _direct_call_with_slot(self, slot: Any, **kwargs: Any) -> Any:
        """用指定 slot 的 key 直接调用 litellm.acompletion。

        不经过 Router，直接构建 litellm 参数，确保使用 slot 的 key。
        关键：kwargs["model"] 此时是 model_id（yaml key），不是 model_name。
        需要反查 model_name 来拼 litellm 模型字符串（如 apigo/MiniMax-M3 → openai/MiniMax-M3），
        因为上游 API 只认 model_name，不认内部 model_id。
        """
        from llm.router_factory import (  # noqa: PLC0415
            get_litellm_prefix,
            get_model_name_for_id,
            get_provider_for_model,
        )

        model_id = kwargs.get("model", "")
        # 去掉 litellm 前缀（"zai/glm-5.1" → "glm-5.1"）
        bare = model_id.split("/", 1)[1] if "/" in model_id else model_id

        # 查 provider → 构建 litellm 模型字符串
        provider = get_provider_for_model(bare)
        prefix = get_litellm_prefix(provider) if provider else ""
        # 反查 model_name（yaml 的 model_name 字段），而非直接用 model_id
        # 例: bare="deepseek-v4-pro-apigo" → model_name="deepseek-v4-pro"
        model_name = get_model_name_for_id(bare)
        litellm_model = f"{prefix}/{model_name}" if prefix else model_name

        # 构建 kwargs：用 slot 的凭证，去掉 model 让 litellm_params 里的生效
        input_kwargs = {k: v for k, v in kwargs.items() if k not in ("model",)}
        input_kwargs["model"] = litellm_model
        input_kwargs["api_key"] = slot.api_key
        if slot.api_base:
            input_kwargs["api_base"] = slot.api_base

        # 禁用 litellm 内部重试：由 KeyPoolAdapter 自己用不同 key 重试
        input_kwargs["num_retries"] = 0

        return await litellm.acompletion(**input_kwargs)
