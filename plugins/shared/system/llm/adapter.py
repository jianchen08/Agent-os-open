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
import json
import logging
import os
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, Protocol, runtime_checkable

import litellm
from agentos_plugin_sdk.error_classifier import ErrorKind, classify_error
from agentos_plugin_sdk.stream_watchdog import StreamHardTimeout

litellm.suppress_debug_info = True
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# === 缓存诊断：在 litellm 真正构造 HTTP body 的位置拦截 ===
# transform_request 返回的 dict["messages"] 就是发送给 API 的最终消息体
# （经过 provider transformation、cache_control 处理之后，httpx 发送之前）。
# 这是唯一能看到"真实发出的字节"的位置。adapter 层看到的 messages 还会被
# litellm 内部改写（合并 system、移除 cache_control、字段重排等），不等于
# 真实 payload，故必须在此拦截。两轮对比 prefix_hash 即可定位 cache 断点。
def _install_payload_diag_hook() -> None:
    try:
        import hashlib
        import json as _json
        import os as _os

        _diag_logger_local = logging.getLogger("llm.adapter._payload_diag")

        def _log_final_payload(model: str, body: dict) -> None:
            try:
                msgs = body.get("messages", [])
                # 【关键】不 sort、不 default=str 改写，直接 dumps litellm 返回的原始 dict。
                # body 是 transform_request 的返回值，litellm 会原样作为 HTTP body 发出，
                # 所以这里的字段顺序/结构就是真正发给厂商的字节序列。
                # 字段顺序须保留原序：prefix cache 按字节匹配，重排字段 = 前缀变 = 缓存失效，
                # 故绝不能用 sort_keys 重排。
                running = ""
                # 整个 body 的原始字节 hash（含 model/可选参数，服务端可能看完整请求体）
                body_raw = _json.dumps(body, ensure_ascii=False)
                body_hash = hashlib.md5(body_raw.encode("utf-8")).hexdigest()[:12]
                # messages 部分的原始字节 hash
                msgs_raw = _json.dumps(msgs, ensure_ascii=False)
                msgs_hash = hashlib.md5(msgs_raw.encode("utf-8")).hexdigest()[:12]
                _diag_logger_local.info(
                    "POST_TRANSFORM model=%s msg_count=%d body_hash=%s msgs_hash=%s",
                    model,
                    len(msgs),
                    body_hash,
                    msgs_hash,
                )
                for pi, pm in enumerate(msgs):
                    # 每条消息的原始字节（不 sort），累积前缀也是原始字节拼接
                    mj = _json.dumps(pm, ensure_ascii=False)
                    running += mj + "\n"
                    full = hashlib.md5(mj.encode("utf-8")).hexdigest()[:8]
                    prefix = hashlib.md5(running.encode("utf-8")).hexdigest()[:8]
                    _diag_logger_local.info(
                        "POST_TRANSFORM_MSG[%d] role=%s name=%s full_hash=%s prefix_hash=%s | raw=%s",
                        pi,
                        pm.get("role", "?"),
                        pm.get("name", ""),
                        full,
                        prefix,
                        mj[:500],
                    )
                # 写入原始 body（litellm 真实发送的结构），供逐字节 diff
                # 文件名携带元数据：{ts}_{model}_{msgs_hash}_{msg_count}msg.json
                # 这样前端列目录后无需读文件即可展示列表（时间/模型/消息数）。
                # 目录锚定：AGENTOS_LOG_DIR 优先，否则从本文件向上探测项目根
                # （含 config/models 的目录）——sidecar cwd 会漂移到各插件目录，
                # 不能依赖 cwd 落盘（否则与 monitoring 读取端错位）。
                _diag_base = _os.environ.get("AGENTOS_LOG_DIR", "")
                if not _diag_base:
                    _cand = _os.path.dirname(_os.path.abspath(__file__))
                    while _cand and _cand != _os.path.dirname(_cand):
                        if _os.path.isdir(_os.path.join(_cand, "config", "models")):
                            _diag_base = _cand
                            break
                        _cand = _os.path.dirname(_cand)
                _diag_dir = _os.path.join(
                    _diag_base or _os.getcwd(),
                    "logs", "payload_diag",
                )
                _os.makedirs(_diag_dir, exist_ok=True)

                def _sanitize_for_filename(s: str) -> str:
                    """文件名安全化：只保留字母数字下划线连字符，其余转 _，截断 48 字符。"""
                    import re as _re
                    return _re.sub(r"[^A-Za-z0-9_-]", "_", str(s))[:48] or "unknown"

                _model_safe = _sanitize_for_filename(model or "unknown")
                _ts = int(_time.time() * 1000)
                # 文件名格式：{ts}__{model}__{msgs_hash}__{msg_count}msg.json
                # 用双下划线 __ 作字段分隔，model 内部单下划线保留（如 deepseek-v4-flash）。
                # 解析时按 __ split 即可，避免 model 含下划线导致的歧义。
                _diag_file = _os.path.join(
                    _diag_dir,
                    f"{_ts}__{_model_safe}__{msgs_hash}__{len(msgs)}msg.json",
                )
                with open(_diag_file, "w", encoding="utf-8") as fh:
                    fh.write(body_raw)

                # 写入时惰性清理：目录超过 200 个文件删最老的（调试用，避免无限增长）
                try:
                    _files = sorted(
                        (_os.path.join(_diag_dir, f) for f in _os.listdir(_diag_dir)),
                        key=_os.path.getmtime,
                    )
                    while len(_files) > 200:
                        _os.remove(_files.pop(0))
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

        # patch 所有 provider transformation 类的 transform_request / async_transform_request。
        # 扫描 litellm.llms 下各 provider 的 chat transformation 模块（openai/deepseek/...），
        # 确保无论走哪个 provider 都能拦截到真实 HTTP body。
        import importlib as _importlib

        _patched_classes: set[type] = set()

        def _patch_class(_cls: type) -> None:
            if _cls in _patched_classes:
                return
            if not hasattr(_cls, "transform_request"):
                return
            _patched_classes.add(_cls)
            _orig_sync = _cls.transform_request

            def _wrap_sync(_orig):
                def _patched(self, model, messages, optional_params, litellm_params, headers):  # noqa: ANN001
                    body = _orig(self, model, messages, optional_params, litellm_params, headers)
                    _log_final_payload(model, body)
                    return body

                return _patched

            _cls.transform_request = _wrap_sync(_orig_sync)

            if hasattr(_cls, "async_transform_request"):
                _orig_async = _cls.async_transform_request

                def _wrap_async(_orig):
                    async def _patched_async(self, model, messages, optional_params, litellm_params, headers):  # noqa: ANN001
                        body = await _orig(self, model, messages, optional_params, litellm_params, headers)
                        _log_final_payload(model, body)
                        return body

                    return _patched_async

                _cls.async_transform_request = _wrap_async(_orig_async)

        _transformation_modules = [
            "litellm.llms.openai.chat.gpt_transformation",
            "litellm.llms.deepseek.chat.transformation",
            "litellm.llms.anthropic.chat.transformation",
            "litellm.llms.zhipu.chat.transformation",
        ]
        for _mod_path in _transformation_modules:
            try:
                _mod = _importlib.import_module(_mod_path)
            except Exception:  # noqa: BLE001
                continue
            for _cls in vars(_mod).values():
                if isinstance(_cls, type):
                    _patch_class(_cls)

        logger.info(
            "[payload_diag] 已安装 litellm transform_request 拦截钩子，patched=%d 类",
            len(_patched_classes),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[payload_diag] 拦截钩子安装失败: %s", e)


_install_payload_diag_hook()
# === 缓存诊断结束 ===

_diag_logger = logging.getLogger(__name__ + "._diag")
_diag_logger.propagate = False
_stream_logger = logging.getLogger(__name__ + "._stream")
_stream_logger.propagate = False

# aclose 超时上限（流式连接关闭）：正常关闭一个 HTTP 连接仅需毫秒级，超过该阈值
# 说明底层 socket（常为 Windows ProactorEventLoop 上的 SSL 流）已半死——服务端发了
# FIN 但本地 SSL shutdown 握手等不到响应，httpx/httpcore 的 aclose 会永久阻塞。
# 此时放弃优雅关闭，让协程返回，残留 socket（CLOSE_WAIT）交由 GC/OS 回收。
# 选 10s 是远大于健康关闭耗时、又远小于让管道僵死的可忍受时长。
_ACLOSE_TIMEOUT_SECONDS: float = 10.0

# 后台残留任务登记表：_await_with_escape 超时放弃等待后，被取消的协程若吞掉
# CancelledError 继续挂起（litellm 半死连接场景），task 会留在后台运行。
# 持有强引用 + done 回调自动清理，避免 task 被 GC 时触发 "Task was destroyed
# but it is pending" 告警；同时给上层 finally 的 aclose 兜底机会去强制关闭底层连接。
_background_tasks: set[asyncio.Task[Any]] = set()


def _track_background_task(task: asyncio.Task[Any]) -> None:
    """登记后台任务并绑定自动清理（完成/取消/异常均移除）。

    同时消费 task 异常：被放弃的协程最终异常时，若不取 exception() 会触发
    "Task exception was never retrieved" 告警（done 回调里取一次即消费）。
    """
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            with contextlib.suppress(Exception):
                t.exception()  # 消费异常，避免 "never retrieved" 告警

    task.add_done_callback(_on_done)


async def _await_with_escape(
    coro: Any,
    timeout: float,
    *,
    what: str,
) -> Any:
    """带超时等待协程，超时即抛错。

    asyncio.wait 的 timeout 依赖事件循环调度——若协程内部同步阻塞冻住事件循环，
    timeout 回调永远不执行。加独立线程诊断：到点如果 task 还没完成，独立线程
    直接打日志（不依赖事件循环），证明「超时确实该触发但事件循环冻住了」。
    """
    import threading  # noqa: PLC0415
    task = asyncio.ensure_future(coro)
    _track_background_task(task)

    # 独立线程诊断：到点检查 task 是否完成
    def _diag_check() -> None:
        if not task.done():
            logger.error(
                "[_await_with_escape] 独立线程诊断：%.0fs 到点 task 仍未完成 | what=%s "
                "—— asyncio.wait 的 timeout 可能因事件循环冻结而失效",
                timeout, what,
            )

    diag_timer = threading.Timer(timeout, _diag_check)
    diag_timer.daemon = True
    diag_timer.start()

    done, _pending = await asyncio.wait({task}, timeout=timeout)
    diag_timer.cancel()
    if not done:
        logger.error(
            "[_await_with_escape] 超时！cancel task | what=%s timeout=%.0fs",
            what, timeout,
        )
        task.cancel()
        raise asyncio.TimeoutError(f"{what} 超时 {timeout:.0f}s")
    return task.result()


class _ThreadedStreamBridge:
    """跨线程流桥接：worker 线程迭代 litellm 流，主循环从线程安全队列取 chunk。

    背景（生产 2026-08-05）：
    - 17:05:34 litellm.acompletion 卡死 36 分钟：litellm 内部事件循环线程同步
      阻塞冻结主事件循环，asyncio 层超时全部失效 → litellm 移入独立线程。
    - 20:08/20:33:59 首 token 超时修复后出现 "attached to a different loop" /
      "Event loop is closed"：CustomStreamWrapper 绑定 worker 线程的 loop，主循环
      await 它的 __anext__ 会跨 loop 报错 → 流式迭代也留在 worker 线程，
      chunk 经 queue.Queue（线程安全）送回主循环。

    主循环侧接口与 CustomStreamWrapper 对齐：
    - __aiter__/__anext__：从队列取 chunk（StopAsyncIteration 表示流结束）
    - aclose()：通知 worker 关闭底层流（半死连接时不再等 worker，直接返回）
    """

    def __init__(
        self,
        *,
        queue: Any,
        done_evt: Any,
        exc_box: list[BaseException],
        close_evt: Any,
        completion_stream: Any = None,
    ) -> None:
        self._queue = queue
        self._done_evt = done_evt
        self._exc_box = exc_box
        self._close_evt = close_evt
        # 透传底层 completion_stream（心跳诊断读 is_closed 用）；worker 可能
        # 尚未返回流对象时先为 None，worker 完成填充。
        self.completion_stream = completion_stream

    def __aiter__(self) -> _ThreadedStreamBridge:
        return self

    async def __anext__(self) -> Any:
        # 短轮询线程安全队列（不依赖任何事件循环的跨 loop 操作）
        while True:
            if self._exc_box:
                raise self._exc_box[0]
            if self._done_evt.is_set() and self._queue.empty():
                raise StopAsyncIteration
            try:
                return self._queue.get_nowait()
            except Exception:
                await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        """通知 worker 关闭底层流。

        不等待 worker 完成（半死连接会让 aclose 挂起）：设 close_evt 后立即
        返回，worker 线程是 daemon，残留由进程退出回收。上层 finally 已有
        _await_with_escape 兜底，这里保持同步契约（毫秒级返回）。
        """
        self._close_evt.set()


# ── Prompt 审计日志（完整 messages 请求体落盘，默认关）──────────────────────
# 发给远端 LLM API 的完整 messages/tools 请求体落盘，用于复现/审计/调试。
# 默认关闭（含 api_key/用户隐私，需显式开启并信任本地存储）。开启时经基础脱敏
# 写独立文件 data/logs/prompt_audit.log（独立 RotatingFileHandler，不依赖父 logger，
# 保证 0.2 sidecar 进程里一定有 handler）。
#
# 开关：env AGENTOS_LOG_PROMPT_BODY=1 / true
_PROMPT_AUDIT_ENABLED = os.getenv("AGENTOS_LOG_PROMPT_BODY", "").lower() in ("1", "true")
_prompt_logger = logging.getLogger(__name__ + "._prompt")
_prompt_logger.propagate = False


def _sync_prompt_handlers() -> None:
    """惰性初始化 prompt 审计 logger 的独立文件 handler。

    与 _diag_logger 不同，这里挂独立的 RotatingFileHandler（不复用父 logger 的
    handler），确保即便父 logger 未被 setup_logging 配置，prompt 审计仍能落盘。
    幂等：已挂则跳过。
    """
    if _prompt_logger.handlers:
        return
    # 落盘路径：优先 env AGENTOS_LOG_PROMPT_FILE，默认 data/logs/prompt_audit.log
    # 相对 cwd（sidecar 工作目录为插件目录），data/logs 通常在项目根——为兼容，
    # 尝试向上查找 data/logs，找不到就在 cwd 下建。
    log_path = os.getenv("AGENTOS_LOG_PROMPT_FILE") or _resolve_prompt_log_path()
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _prompt_logger.addHandler(handler)
        _prompt_logger.setLevel(logging.DEBUG)
    except OSError:
        # 路径不可写时静默降级（不阻断 LLM 调用主路径）。
        _prompt_logger.disabled = True


def _resolve_prompt_log_path() -> str:
    """查找 data/logs 目录（向上最多 4 层），返回 prompt_audit.log 路径。"""
    cwd = os.getcwd()
    for _ in range(5):
        candidate = os.path.join(cwd, "data", "logs")
        if os.path.isdir(candidate):
            return os.path.join(candidate, "prompt_audit.log")
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    return os.path.join(os.getcwd(), "data", "logs", "prompt_audit.log")


# ── 脱敏 ────────────────────────────────────────────────────
# 请求体含 api_key、用户隐私。基础脱敏：掩码常见密钥/token 形态。
# 非穷举——文档明确警告"开启=信任本地存储"，PII 库可后续接入。
_REDACT_PATTERNS = [
    # OpenAI/Anthropic 风格 key：sk-... / sk-ant-...（保留前 6 位）
    (re.compile(r"(sk-[A-Za-z0-9]{4})[A-Za-z0-9_-]+"), r"\1..."),
    # Bearer token（保留前 8 位）
    (re.compile(r"(Bearer\s+[A-Za-z0-9._-]{6})[A-Za-z0-9._-]+"), r"\1..."),
    # "api_key": "value" / api_key=value 形式（值替换为掩码）
    (re.compile(r'("(?:api_key|api-key|apikey|authorization)"\s*:\s*")[^"]+(")'), r"\1***\2"),
]


def _redact_prompt(text: str) -> str:
    """对序列化后的 prompt 文本做基础脱敏。"""
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _log_prompt_body(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, **kwargs: Any) -> None:
    """记录完整 prompt 请求体到审计日志（开关关闭时零开销直接返回）。

    在 completion() provider 适配后、stream 分发前调用，记录的是真正发往远端
    API 边界的请求体。kwargs 中的 api_key 等敏感值一并记录但经脱敏。
    """
    if not _PROMPT_AUDIT_ENABLED:
        return
    _sync_prompt_handlers()
    if _prompt_logger.disabled:
        return
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        # kwargs 含 temperature/max_tokens/api_base/api_key 等——脱敏后记录
        "params": {k: v for k, v in kwargs.items() if k not in ("on_chunk",)},
    }
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    _prompt_logger.debug("PROMPT %s", _redact_prompt(serialized))


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


@dataclass
class _StreamState:
    """单次流式调用的跨方法共享状态。

    ``_call_streaming`` 按职责拆分为多个私有辅助方法后，原闭包/局部可变状态统一由本
    dataclass 持有并在方法间传递（对象属性原地修改），各方法无需 ``nonlocal`` 即可
    读写同一份状态——行为与拆分前的闭包实现完全等价。

    Attributes:
        result_parts: 正文文本片段（按 chunk 顺序累积）。
        thinking_parts: 思考内容片段（reasoning_content / ``<think/>`` 标签内容）。
        tool_calls_map: 流式 tool_calls 增量按 index 合并的映射。
        stream_usage: 最后一次收到的流式 usage（通常在末尾 chunk）。
        stream_repetition: 是否因 ``on_chunk`` 返回 ``"stop"``（重复检测）而截断。
        thinking_truncated: 思考内容是否因超过 ``max_thinking_chars`` 被截断。
        finish_reason: 结束原因观察值（由接收端点诊断捕获）。
        on_chunk: 流式 chunk 回调（只读配置）。
        max_thinking_chars: 思考内容截断阈值（只读配置）。
        stream_start: 流式消费起始 monotonic 时间（速度统计用）。
        last_chunk_monotonic: 上个 chunk 到达的 monotonic 时间（心跳量化静默用）。
        chunks_received: 累计收到的 chunk 数（心跳/超时日志用）。
        recv_seq: 接收端点诊断序号（tool_calls chunk 到达次数）。
        recv_tc_count: 累计收到含 tool_calls 的 chunk 数。
        in_think_tag: 流式 ``<think/>`` 标签状态机当前是否处于开标签内。
    """

    result_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls_map: dict[int, dict[str, Any]] = field(default_factory=dict)
    stream_usage: dict[str, Any] | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False
    finish_reason: str | None = None
    # 只读配置
    on_chunk: Callable[[dict[str, Any]], Any] | None = None
    max_thinking_chars: int = 180000
    # 计时 / 诊断计数
    stream_start: float = 0.0
    last_chunk_monotonic: float = 0.0
    chunks_received: int = 0
    recv_seq: int = 0
    recv_tc_count: int = 0
    in_think_tag: bool = False


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
        from provider_adapters import get_provider_adapter  # noqa: PLC0415

        adapter = get_provider_adapter(model)
        messages = adapter.adapt_messages_before_send(messages, **kwargs)

        # 弹出 adapter 专属参数（不发给 litellm / API）
        kwargs.pop("reasoning_retention", None)

        # openai/ 前缀的中转端点：litellm openai provider 不认 reasoning_effort
        # 等专有参数，故挪进 extra_body 透传（上游本身能接受）。
        if model.lower().startswith("openai/"):
            _move_to_extra_body(kwargs, ("reasoning_effort", "thinking"))

        # Prompt 审计落盘（默认关，经基础脱敏）：记录真正发往远端 API 的请求体。
        # 放在 provider 适配 + extra_body 处理之后，是 litellm 调用前的最终收口点。
        _log_prompt_body(model, messages, tools, **kwargs)

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
        # ★ 非流式同样包 _await_with_escape：litellm.acompletion 在内部建连
        # 阶段同样可能吞掉取消挂死（与流式首 chunk 同根因），直接 await 会
        # 让引擎永久卡死。到点抛 TimeoutError 透传，由调用方错误链处理。
        response = await _await_with_escape(
            self._do_completion(**call_kwargs, drop_params=True),
            call_timeout,
            what=f"non-streaming completion model={model}",
        )

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

    async def _call_streaming(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """流式调用 LLM。

        阶段序列：
        1. 参数准备：剥出流式专属参数，构造 litellm 调用参数；
        2. 建连+首 chunk（_establish_first_chunk）：首字节超时统一覆盖
           "建连→等响应头→首字节"全过程；
        3. 消费流（_consume_stream）：inter-chunk 静默超时 + 心跳探针 +
           独立线程硬超时兜底，finally 里限时清理全部资源；
        4. 汇总累积状态构造响应。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            on_chunk: 流式 chunk 回调（可选）；回调返回 "stop" 可截断输出
            **kwargs: 其余参数透传 litellm；流式专属键 first_chunk_timeout /
                inter_chunk_timeout / max_thinking_chars 在此剥出，不进入请求参数
        """
        # 流式超时：首个 chunk 检测连接是否建立，后续 chunk 防止连接僵死。
        # 必须在构造 call_kwargs 之前 pop 出来，否则会被 **kwargs 塞进
        # litellm 请求参数（litellm 不识别这两个 key）。
        first_chunk_timeout = float(kwargs.pop("first_chunk_timeout", 180))
        # inter-chunk 静默超时：连续 N 秒收不到任何 chunk 即判定上游/传输静默，
        # 抛 litellm.Timeout 中断死等。每个 chunk 到达即重置计时器（见主循环），
        # 故活跃推理（reasoning 持续吐 chunk）永不触发，只有真正静默（连接挂起/
        # 上游冻结）才在 N 秒后掐断。生产由插件传入 stream_idle_timeout 覆盖；
        # 此处默认 600s 为直连/测试调用兜底。
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

        # timeout 取 first_chunk_timeout：KeyPool 路径在 _direct_call_with_slot 会
        # 覆盖为 first_chunk_timeout 本身——httpx 层超时在线程池线程内生效，事件
        # 循环冻结也能到点抛异常（httpx 层长超时 + asyncio 层超时随事件循环冻结
        # 失效的组合是引擎级挂死的根因，见 KeyPoolAdapter 同类注释）。
        call_kwargs["timeout"] = first_chunk_timeout

        response, first_chunk = await self._establish_first_chunk(
            model, call_kwargs, first_chunk_timeout,
        )

        state = _StreamState(
            on_chunk=on_chunk,
            max_thinking_chars=int(kwargs.pop("max_thinking_chars", 180000)),
            stream_start=_time.monotonic(),
        )
        state.last_chunk_monotonic = state.stream_start
        await self._consume_stream(response, first_chunk, state, model, inter_chunk_timeout)
        return self._build_streaming_response(state)

    async def _establish_first_chunk(
        self,
        model: str,
        call_kwargs: dict[str, Any],
        first_chunk_timeout: float,
    ) -> tuple[Any, Any]:
        """建连并读取首个 chunk，统一受首字节超时保护。

        首字节超时必须同时包住 _do_completion 和首 chunk 读取：上游"半死连接"
        （TCP 建连成功、请求已发出，但上游既不回数据也不断开）会让 _do_completion
        卡在 litellm.acompletion 的建连/等响应头阶段——既不是 429，也不是连接错误，
        若 wait_for 仅包首个 __anext__() 则因 _do_completion 尚未返回而无法启动，
        请求会静默挂死直到 httpx 层超时。

        Raises:
            litellm.Timeout: 首字节超时；或建连成功但零 chunk（过早 EOF，
                按首 token 失败处理——此时底层流已在 _open_and_first_chunk 内关闭）。
        """
        try:
            return await _await_with_escape(
                self._open_and_first_chunk(model, call_kwargs),
                first_chunk_timeout,
                what=f"first chunk (incl. connect) model={model}",
            )
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

    async def _open_and_first_chunk(
        self,
        model: str,
        call_kwargs: dict[str, Any],
    ) -> tuple[Any, Any]:
        """建连并读取首个 chunk，供外层 wait_for 统一限时。

        首个 chunk 读取若抛异常（含 wait_for 超时注入的 CancelledError），
        必须关闭 stream——既为释放 HTTP 连接，也为触发 _bind_release_to_stream
        绑定的 slot.release()，避免并发许可泄漏（建连超时是高频场景）。
        """
        _t0 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: 进入，准备调 _do_completion model=%s t0=%.3f",
            type(self).__name__, model, _t0,
        )
        resp = await self._do_completion(**call_kwargs, drop_params=True)
        _t1 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: _do_completion 返回(%.3fs)，准备读首 chunk model=%s",
            type(self).__name__, _t1 - _t0, model,
        )
        try:
            first = await resp.__aiter__().__anext__()
        except BaseException as _first_exc:
            _t2 = _time.monotonic()
            logger.warning(
                "[%s] _open_and_first_chunk: 首chunk异常(%.3fs后) model=%s exc=%s",
                type(self).__name__, _t2 - _t1, model, type(_first_exc).__name__,
            )
            # 超时/异常/取消：关闭流，触发绑定的 release。aclose 自身的任何
            # 异常（含 CancelledError）都不应掩盖/替换原始异常，故全量抑制。
            # ★ 不能裸 await aclose()：半死 SSL socket 会让 aclose 永久
            # 阻塞，把原始异常（超时/取消）吞在 await 里，外层 wait_for 等不到
            # 协程退出就永远不返回 → 引擎死锁。用 _await_with_escape 限时：
            # 正常 aclose 毫秒级完成（同步契约，测试可立即观测关闭）；半死
            # socket 到点即放弃，原始异常照常 raise 透传，残留协程后台回收。
            aclose = getattr(resp, "aclose", None)
            if aclose is not None:
                try:
                    await _await_with_escape(
                        aclose(),
                        _ACLOSE_TIMEOUT_SECONDS,
                        what="first-chunk aclose",
                    )
                except BaseException:
                    pass
            raise
        _t3 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: 首chunk到达(%.3fs后) model=%s",
            type(self).__name__, _t3 - _t1, model,
        )
        return resp, first

    async def _consume_stream(
        self,
        response: Any,
        first_chunk: Any,
        state: _StreamState,
        model: str,
        inter_chunk_timeout: float,
    ) -> None:
        """消费流：处理首 chunk → 启动心跳/硬超时 → 按 inter-chunk 超时循环消费。

        首 chunk 已在 _establish_first_chunk 内读取（含建连阶段超时保护），此处直接
        处理它。随后启动两个守护：
        - 心跳探针（_stream_heartbeat）：流静默时持续打 idle 时长 + stream_closed，
          证明接收协程存活（排除接收端死锁），量化上游/传输静默时长；
        - 独立线程硬超时（StreamHardTimeout）：asyncio 心跳/wait_for 共享同一
          event loop，一旦底层 socket 阻塞冻住事件循环会全部失效；硬超时用
          threading 线程倒计时，到点强制 aclose，loop 冻住也能打破死锁。语义为
          "chunk 间隔超时"：每收到一个 chunk 即 reset() 重计时，避免误杀总时长
          长但 chunk 间隔始终健康的流。

        finally 收尾（任何退出路径都执行）：取消心跳、disarm 硬超时、限时 aclose
        底层连接。Windows 半死 SSL socket 会让 httpx aclose 永久阻塞导致本 finally
        不返回（引擎级挂死），故 aclose 用 _await_with_escape 限时：到点放弃关闭，
        残留 socket（CLOSE_WAIT）交由 GC/OS 回收。KeyPoolAdapter 路径下 aclose 已被
        _aclose_with_release 包过一层 wait_for，这里再包一层无害（外层到点会 cancel
        内层）；LiteLLMAdapter 路径下 aclose 是原始的，本层是其唯一保护。
        """
        heartbeat_task: asyncio.Task[None] | None = None
        hard_timeout: StreamHardTimeout | None = None
        try:
            await self._process_chunk(first_chunk, state)
            state.last_chunk_monotonic = _time.monotonic()
            state.chunks_received += 1
            # 启动心跳探针：沿用 process_manager._watchdog_loop 的
            # create_task + CancelledError 退出范式。
            heartbeat_task = asyncio.create_task(
                self._stream_heartbeat(
                    model,
                    inter_chunk_timeout,
                    lambda: _time.monotonic() - state.last_chunk_monotonic,
                    lambda: state.chunks_received,
                    getattr(response, "completion_stream", None),
                )
            )
            hard_timeout = StreamHardTimeout(
                response,
                asyncio.get_running_loop(),
                inter_chunk_timeout,
            )
            hard_timeout.arm()
            # 接收端点诊断：首个 chunk 无论是否携带 tool_calls 都观察 finish_reason
            self._diag_recv_chunk(first_chunk, state, first=True)
            # 后续 chunk：逐次超时，每个 chunk 到达即重置计时器。活跃推理 chunk
            # 间隔远小于 timeout 故不误触发；仅真正静默（死连接）累计满 timeout。
            # 用 _await_with_escape：即使底层 __anext__ 吞掉取消挂死（半死连接），
            # 也能到点抛错透传，而不是被 asyncio.wait_for「等协程退出」卡死。
            aiter = response.__aiter__()
            while True:
                try:
                    chunk = await _await_with_escape(
                        aiter.__anext__(),
                        inter_chunk_timeout,
                        what=f"inter-chunk model={model}",
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    _idle = _time.monotonic() - state.last_chunk_monotonic
                    logger.warning(
                        "[%s] STREAM TIMEOUT: inter-chunk 静默超时 (%.0fs) 距上个 chunk #%d 已静默 %.0fs model=%s",
                        type(self).__name__,
                        inter_chunk_timeout,
                        state.chunks_received,
                        _idle,
                        model,
                    )
                    raise litellm.Timeout(  # noqa: B904
                        message=(
                            "Stream inter-chunk timeout:"
                            f" no data for {_idle:.0f}s"
                            f" (last chunk #{state.chunks_received}, timeout={inter_chunk_timeout:.0f}s)"
                        ),
                        model=model,
                        llm_provider="zai",
                    )
                state.last_chunk_monotonic = _time.monotonic()
                state.chunks_received += 1
                # chunk 健康到达：重置硬超时倒计时（chunk 间隔语义，避免误杀长流）
                if hard_timeout is not None:
                    hard_timeout.reset()
                if await self._process_chunk(chunk, state):
                    break
                # ── 接收端点诊断：本 chunk 的 delta.tool_calls / finish_reason 到达情况 ──
                self._diag_recv_chunk(chunk, state, first=False)
        finally:
            # 取消心跳探针任务（避免任务泄漏：超时/异常/正常结束都要清理）
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat_task
            # 取消独立线程硬超时（正常结束时不触发强制关闭，幂等）
            if hard_timeout is not None:
                hard_timeout.disarm()
            # 确保超时或异常时关闭 async iterator，释放 HTTP 连接（限时防挂死）
            if hasattr(response, "aclose"):
                try:
                    await _await_with_escape(
                        response.aclose(),
                        _ACLOSE_TIMEOUT_SECONDS,
                        what="response.aclose",
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] response.aclose finally 超时 %.0fs（半死 socket 放弃关闭），"
                        "残留连接交 GC 回收",
                        type(self).__name__,
                        _ACLOSE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.debug("[%s] response.aclose finally 异常（已忽略）", type(self).__name__)

    async def _process_chunk(self, chunk: Any, state: _StreamState) -> bool:
        """处理单个 chunk（按载荷类型路由），返回是否应中断消费循环。

        先做限频诊断与 usage 核算（与载荷类型无关的公共步骤），再依次分发：
        reasoning_content → 正文/<think/> 状态机 → 工具调用增量。任一载荷要求
        截断（thinking 超限、消费方 stop 信号）即返回 True，跳过本 chunk 的剩余
        载荷与后续所有 chunk。
        """
        self._log_chunk_flow_diag(chunk, state)
        self._collect_stream_usage(chunk, state)

        if not chunk.choices:
            return False

        delta = chunk.choices[0].delta

        # LiteLLM 统一推理内容映射到 delta.reasoning_content
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning and self._append_thinking(reasoning, state, verbose=True, enforce_limit=True):
            return True

        # 文本内容：流式 <think/> 状态机处理（MiniMax 等模型）
        if delta.content and self._handle_delta_content(delta.content, state):
            return True

        if delta.tool_calls:
            self._accumulate_tool_call_deltas(delta.tool_calls, state)
        return False

    def _log_chunk_flow_diag(self, chunk: Any, state: _StreamState) -> None:
        """限频落盘诊断：前 2 个及每第 200 个 chunk 记录关键字段摘要（debug）。"""
        chunk_idx = len(state.result_parts) + len(state.thinking_parts)
        if chunk_idx <= 1 or chunk_idx % 200 == 0:
            _sync_diag_handlers()
            if _diag_logger.handlers:
                delta = getattr(
                    getattr(chunk, "choices", [None])[0],
                    "delta",
                    None,
                )
                has_tc = getattr(delta, "tool_calls", None)
                has_usage = getattr(chunk, "usage", None)
                if chunk_idx <= 1 or has_tc or has_usage:
                    content = getattr(delta, "content", None)
                    reasoning = getattr(delta, "reasoning_content", None)
                    _diag_logger.debug(
                        "[%s] chunk #%d: content=%s reasoning=%s tc=%s usage=%s",
                        type(self).__name__,
                        chunk_idx,
                        repr((content or "")[:40]),
                        repr((reasoning or "")[:40]) if reasoning else "-",
                        "Y" if has_tc else "-",
                        "Y" if has_usage else "-",
                    )

    def _collect_stream_usage(self, chunk: Any, state: _StreamState) -> None:
        """收集流式 usage（通常出现在最后一个 chunk），记入 state.stream_usage。"""
        if hasattr(chunk, "usage") and chunk.usage:
            _prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
            state.stream_usage = {
                "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
            }

    def _append_thinking(
        self,
        text: str,
        state: _StreamState,
        *,
        verbose: bool,
        enforce_limit: bool,
    ) -> bool:
        """追加思考片段并发 thinking 事件；enforce_limit 时检查截断阈值。

        返回是否因思考内容超过 max_thinking_chars 而应中断流（仅 enforce_limit
        时可能为 True）。verbose 控制是否打 THINKING 计数 debug 日志（完整段打，
        标签切片片段不打，与拆分前各调用点行为一致）。
        """
        state.thinking_parts.append(text)
        if verbose:
            _stream_logger.debug(
                "[STREAM][THINKING] #%d +%d chars",
                len(state.thinking_parts),
                len(text),
            )
        if state.on_chunk:
            state.on_chunk({"type": "thinking", "content": text})
        if not enforce_limit:
            return False
        thinking_len = sum(len(p) for p in state.thinking_parts)
        if state.max_thinking_chars > 0 and thinking_len > state.max_thinking_chars:
            logger.warning(
                "[%s] 思考内容过长(%d>%d chars)，截断",
                type(self).__name__,
                thinking_len,
                state.max_thinking_chars,
            )
            state.thinking_truncated = True
            return True
        return False

    def _emit_text(self, text: str, state: _StreamState) -> bool:
        """追加正文片段并发 text 事件；返回消费方是否要求停止（"stop" 信号）。"""
        state.result_parts.append(text)
        if not state.on_chunk:
            return False
        return state.on_chunk({"type": "text", "content": text}) == "stop"

    def _strip_think_close(self, seg: str, state: _StreamState) -> str:
        """处理确定含 ``</think`` 的文本段：闭合标记前的部分计入 thinking 通道。

        返回闭标签 ``>`` 之后剩余的正文片段（可能为空串/空白，由调用方决定是否
        发正文事件）。``in_think_tag`` 标志由调用方维护。开标签与闭标签同 chunk
        到达、跨 chunk 切分两种路径共用本方法。
        """
        close_idx = seg.index("</think")
        head = seg[:close_idx]
        if head:
            self._append_thinking(head, state, verbose=False, enforce_limit=False)
        tail = seg[close_idx:]
        gt = tail.find(">")
        return tail[gt + 1 :] if gt >= 0 else ""

    def _handle_delta_content(self, content: str, state: _StreamState) -> bool:
        """按流式 ``<think/>`` 标签状态机路由 delta.content，返回是否应中断流。

        MiniMax 等模型的思考内容以 ``<think/>`` 标签包裹在 delta.content 中返回，
        且标签会跨多个 chunk 切分。状态机通过 "<think" / "</think" 字符串查找跟踪
        开/闭状态，确保 thinking 内容路由 thinking 通道、正文路由 text 通道。

        中断条件（现状契约，仅两处）：thinking 内容超过 max_thinking_chars；
        正文路径消费方返回 "stop"。开标签前的前缀片段只发事件不检查停止信号；
        标签切片出的 thinking 片段不做截断检查——均沿用拆分前实现。
        """
        if state.in_think_tag:
            # 标签内：检查闭合标签
            if "</think" in content:
                rest = self._strip_think_close(content, state)
                state.in_think_tag = False
                if rest.strip() and self._emit_text(rest, state):
                    state.stream_repetition = True
                    return True
            else:
                return self._append_thinking(content, state, verbose=True, enforce_limit=True)
        elif "<think" in content:
            # 标签外：检查开标签
            open_idx = content.index("<think")
            before = content[:open_idx]
            if before:
                # 现状契约：开标签前的前缀发 text 事件但不检查停止信号
                self._emit_text(before, state)
            after_open = content[open_idx:]
            gt = after_open.find(">")
            inner = after_open[gt + 1 :] if gt >= 0 else ""
            state.in_think_tag = True
            if "</think" in inner:
                rest = self._strip_think_close(inner, state)
                state.in_think_tag = False
                if rest.strip() and self._emit_text(rest, state):
                    state.stream_repetition = True
                    return True
            elif inner:
                self._append_thinking(inner, state, verbose=True, enforce_limit=False)
        else:
            # 纯正文：先收口已开的思考段再发正文事件
            if state.on_chunk and state.thinking_parts:
                state.on_chunk({"type": "thinking_end", "content": ""})
            state.result_parts.append(content)
            _stream_logger.debug(
                "[STREAM][TEXT] #%d +%d chars: %s",
                len(state.result_parts),
                len(content),
                repr(content[:80]),
            )
            if state.on_chunk and state.on_chunk({"type": "text", "content": content}) == "stop":
                state.stream_repetition = True
                logger.warning(
                    "[%s] 收到 stop 信号，截断流式输出",
                    type(self).__name__,
                )
                return True
        return False

    def _accumulate_tool_call_deltas(self, tool_calls: Any, state: _StreamState) -> None:
        """合并流式工具调用增量（同 index 的 id/name/arguments 逐段拼接）并发事件。"""
        # thinking→tool_calls 过渡：发送 thinking_end 确保思考完整关闭后再输出工具卡片
        if state.on_chunk and state.thinking_parts:
            state.on_chunk({"type": "thinking_end", "content": ""})
        for tc in tool_calls:
            idx = tc.index if hasattr(tc, "index") else 0
            if idx not in state.tool_calls_map:
                state.tool_calls_map[idx] = {
                    "id": (getattr(tc, "id", None) or f"tc_{idx}_{id(state.tool_calls_map)}"),
                    "name": "",
                    "arguments": "",
                }
                _stream_logger.debug(
                    "[STREAM][TOOL_CALL] #%d new: id=%s",
                    idx,
                    state.tool_calls_map[idx]["id"],
                )
            if tc.function:
                if tc.function.name:
                    state.tool_calls_map[idx]["name"] += tc.function.name
                    _stream_logger.debug(
                        "[STREAM][TOOL_CALL] #%d name=%s",
                        idx,
                        state.tool_calls_map[idx]["name"],
                    )
                if tc.function.arguments:
                    state.tool_calls_map[idx]["arguments"] += tc.function.arguments
                    _arg_len = len(state.tool_calls_map[idx]["arguments"])
                    _stream_logger.debug(
                        "[STREAM][TOOL_CALL] #%d args +%d → %d chars: %s",
                        idx,
                        len(tc.function.arguments),
                        _arg_len,
                        repr(tc.function.arguments[:100]),
                    )

        if state.on_chunk:
            state.on_chunk(
                {
                    "type": "tool_call",
                    "tool_calls": tool_calls,
                }
            )

    def _diag_recv_chunk(self, chunk: Any, state: _StreamState, *, first: bool) -> None:
        """接收端点诊断：观察 tool_calls 与 finish_reason 是否到达接收侧。

        只做 debug 日志与计数，尽力而为（结构异常整体吞掉）。不对称是现状契约
        （勿顺手"修复"）：首 chunk 无论是否携带 tool_calls 都更新 finish_reason
        观察值；后续 chunk 仅在同 chunk 携带 tool_calls 时才更新。
        """
        state.recv_seq += 1
        try:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is not None:
                delta = getattr(choice, "delta", None)
                finish = getattr(choice, "finish_reason", None)
                tcs = getattr(delta, "tool_calls", None) if delta else None
                if tcs:
                    state.recv_tc_count += 1
                    summary = []
                    for tc in tcs:
                        fn = getattr(tc, "function", None)
                        name = getattr(fn, "name", "?") if fn else "?"
                        args = getattr(fn, "arguments", "") if fn else ""
                        summary.append(f"{name}(args={len(args)}c)")
                    if first:
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d tool_calls 到达(首chunk, %d个): %s",
                            state.recv_seq,
                            len(tcs),
                            ", ".join(summary),
                        )
                    else:
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d tool_calls 到达(%d个): %s",
                            state.recv_seq,
                            len(tcs),
                            ", ".join(summary),
                        )
                if first:
                    if finish:
                        state.finish_reason = finish
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d finish=%s (首chunk, 累计tc=%d)",
                            state.recv_seq,
                            finish,
                            state.recv_tc_count,
                        )
                elif tcs:
                    state.finish_reason = finish
                    _stream_logger.debug(
                        "[STREAM][RECV] #%d finish=%s (累计tc=%d)",
                        state.recv_seq,
                        finish,
                        state.recv_tc_count,
                    )
        except Exception:
            pass

    def _build_streaming_response(self, state: _StreamState) -> LLMResponse:
        """拼接累积片段、核算速度统计，构造最终 LLMResponse。"""
        result_text = "".join(state.result_parts) if state.result_parts else None
        thinking_text = "".join(state.thinking_parts) if state.thinking_parts else None
        tool_calls = self._normalize_tool_calls(state.tool_calls_map)

        # 流式接收完成：记录速度统计
        elapsed = _time.monotonic() - state.stream_start
        comp_tokens = (state.stream_usage or {}).get("completion_tokens", 0)
        speed = (comp_tokens / elapsed) if elapsed > 0 and comp_tokens else 0
        _stream_logger.debug(
            "[STREAM][DONE] finish=%s text=%d chars thinking=%d chars "
            "chunks=%d tool_calls=%d "
            "tokens=%d elapsed=%.2fs speed=%.1f tok/s",
            state.finish_reason,
            len(result_text or ""),
            len(thinking_text or ""),
            len(state.result_parts) + len(state.thinking_parts),
            len(tool_calls),
            comp_tokens,
            elapsed,
            speed,
        )
        # 接收端点汇总：API 端实际送达的 tool_calls chunk 数 vs 最终解析数
        _stream_logger.debug(
            "[STREAM][STATS] recv_chunks=%d recv_tc=%d parsed_tc=%d",
            state.recv_seq,
            state.recv_tc_count,
            len(tool_calls),
        )

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=state.stream_usage,
            stream_repetition=state.stream_repetition,
            thinking_truncated=state.thinking_truncated,
            finish_reason=state.finish_reason,
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
        from router_factory import (  # noqa: PLC0415
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
        from key_pool import KeySlot  # noqa: PLC0415
        from router_factory import get_key_pool  # noqa: PLC0415

        model_str = kwargs.get("model", "")
        provider_name = self._resolve_provider(model_str)
        pool = get_key_pool(provider_name) if provider_name else None

        if pool is None:
            # 无 KeyPool，直接走 Router
            return await self._route_call(**kwargs)

        # 尝试每个可用 key，失败后自动换下一个重试
        max_retries = len(pool.slots)
        last_exc: Exception | None = None

        from exceptions import KeyPoolExhaustedError, LLMKeyUnresolvedError  # noqa: PLC0415

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
                if "${" in slot.api_key:
                    # fail-closed：占位符未解析（进程环境与 .env 均无值）时，
                    # 字面量 ${VAR} 作为 key 发往上游只会得到无法排查的 401，
                    # 发起 HTTP 前直接报配置错误。
                    slot.release()
                    raise LLMKeyUnresolvedError(model_str, provider_name, slot.api_key)
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
        from _config_models import get_model_config_loader  # noqa: PLC0415
        from exceptions import LLMKeyUnresolvedError  # noqa: PLC0415
        from router_factory import (  # noqa: PLC0415
            get_key_pool,
            get_or_create_router,
            get_provider_for_model,
        )

        model_loader = get_model_config_loader()
        router = get_or_create_router(model_loader)

        # fail-closed：Router 部署烘入的 api_key（模型级优先，回退 provider 槽，
        # 显式 kwargs 最高）若是未解析占位符，调用前直接报配置错误——同池路径
        # 契约，占位符发往上游只会得到字面量 key 的 401。
        model_str = str(kwargs.get("model", ""))
        model_id = model_str.split("/", 1)[1] if "/" in model_str else model_str
        provider = get_provider_for_model(model_id)
        pool = get_key_pool(provider) if provider else None
        slot_key = (pool.slots[0].api_key or "") if (pool and pool.slots) else ""
        model_conf = model_loader.get_model_config(model_id)
        model_key = str((model_conf or {}).get("api_key", "") or "")
        effective_key = str(kwargs.get("api_key", "") or "") or model_key or slot_key
        if "${" in effective_key:
            raise LLMKeyUnresolvedError(model_str, provider, effective_key)

        return await router.acompletion(**kwargs)

    # aclose 超时上限（引用模块级常量，便于统一调整）。
    _ACLOSE_TIMEOUT_SECONDS: float = _ACLOSE_TIMEOUT_SECONDS

    @staticmethod
    def _bind_release_to_stream(stream: Any, slot: Any) -> None:
        """把 slot.release() 绑定到 stream.aclose()，流关闭时释放并发许可。

        流式调用返回的 stream wrapper（litellm CustomStreamWrapper）是惰性对象，
        真正的流式传输发生在调用方消费它期间。信号量许可必须覆盖整段传输，
        故 release 推迟到 stream 被关闭（_call_streaming 的 finally 调用 aclose）。

        用一次性标志保证 release 只执行一次（litellm 可能多次调用 aclose）。

        original_aclose 用超时包裹：Windows 上半死 SSL socket 会让
        httpx/httpcore 的 aclose 永久阻塞，导致 _call_streaming 的 finally 不
        返回 → _run_loop 永久卡死（其他管道因 slot.release 已执行而不受影响，
        但本管道僵死需重启才能恢复）。超时后放弃关闭，协程得以返回。
        ★ 用 _await_with_escape：即使 aclose 吞掉取消挂死，也能到点返回，
        异常不掩盖 finally 主路径。
        """
        original_aclose = getattr(stream, "aclose", None)
        released = False

        async def _aclose_with_release() -> None:
            nonlocal released
            if not released:
                released = True
                slot.release()
            if original_aclose is not None:
                try:
                    await _await_with_escape(
                        original_aclose(),
                        timeout=_ACLOSE_TIMEOUT_SECONDS,
                        what="stream.aclose",
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] stream.aclose 超时 %.0fs（半死 socket 放弃优雅关闭），"
                        "残留连接交由 GC 回收",
                        KeyPoolAdapter.__name__,
                        _ACLOSE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    # aclose 自身异常（非超时）不阻断 finally 返回
                    logger.debug("[%s] stream.aclose 异常（已忽略）", KeyPoolAdapter.__name__)

        stream.aclose = _aclose_with_release  # type: ignore[method-assign]

    async def _direct_call_with_slot(self, slot: Any, **kwargs: Any) -> Any:
        """用指定 slot 的 key 直接调用 litellm.acompletion。

        不经过 Router，直接构建 litellm 参数，确保使用 slot 的 key。
        关键：kwargs["model"] 此时是 model_id（yaml key），不是 model_name。
        需要反查 model_name 来拼 litellm 模型字符串（如 apigo/MiniMax-M3 → openai/MiniMax-M3），
        因为上游 API 只认 model_name，不认内部 model_id。
        """
        from router_factory import (  # noqa: PLC0415
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

        # ★ 阶段日志：定位卡死在 litellm.acompletion 内部哪一步
        # _direct_call_with_slot 是「拿到 slot 后 → 调 litellm.acompletion」的唯一出口，
        # litellm.acompletion 返回 stream wrapper 后才真正建连/发请求。
        # 若卡死在 litellm 内部（建连/发请求/等首字节），下面「进入/返回」两条日志
        # 能精确定位耗时区间，配合 _open_and_first_chunk 的首chunk日志锁定卡死层。
        _dc_t0 = _time.monotonic()
        logger.info(
            "[%s] _direct_call_with_slot: 进入 litellm.acompletion model=%s api_base=%s kw_keys=%s t0=%.3f",
            type(self).__name__, litellm_model, input_kwargs.get("api_base", "?"),
            sorted(input_kwargs.keys()), _dc_t0,
        )
        # ★ 把首 token 超时"包括在" litellm.acompletion 调用本身（HTTP 层）。
        # litellm 内部存在事件循环线程内同步阻塞路径（如 get_llm_provider 的
        # 同步网络调用），冻结主事件循环时 asyncio 层超时（wait_for/
        # asyncio.wait/shield）全部失效。httpx 层 timeout 在线程池线程内由
        # socket 定时生效，不依赖事件循环调度——传 180s 后卡死的 HTTP
        # 请求必然到点抛异常透传。"将首 token 超时包括在调用本身"。
        _acompletion_timeout = float(kwargs.pop("first_chunk_timeout", 0)) or 180.0
        # 调用方已显式传 HTTP timeout（流式 _call_streaming 会把 first_chunk_timeout
        # pop 掉并转成 timeout 传入）→ 以调用方为准，避免默认 180 覆盖自定义值。
        if kwargs.get("timeout"):
            try:
                _acompletion_timeout = float(kwargs["timeout"])
            except (TypeError, ValueError):
                pass  # httpx.Timeout 对象等非数值：保持 first_chunk_timeout
        input_kwargs["timeout"] = _acompletion_timeout

        # ★ litellm.acompletion 在独立线程 + 独立事件循环中运行，流式迭代也
        # 在该线程完成，chunk 经线程安全队列送回主循环：
        # - litellm 内部同步阻塞（冻结自己的线程）不再影响主事件循环 → 其他管道
        #   永不陪葬（生产 17:05:34 全进程 0 日志 36 分钟的根因）
        # - CustomStreamWrapper 绑定 worker loop，主循环直接 await 会报
        #   "attached to a different loop"（生产 20:33:59 MidStreamFallbackError）
        #   → 主循环只从 queue.Queue 取 chunk，彻底避免跨 loop
        # - 主协程轮询 threading.Event（OS 层事件，到点必然置位/超时，不依赖
        #   任何事件循环调度），超时抛 TimeoutError 透传
        import queue  # noqa: PLC0415
        import threading  # noqa: PLC0415

        _done_evt = threading.Event()
        _result_box: list[Any] = []
        _exc_box: list[BaseException] = []
        _chunk_queue: queue.Queue[Any] = queue.Queue()
        _close_evt = threading.Event()
        _stream_obj: list[Any] = []

        async def _run_litellm() -> Any:
            return await litellm.acompletion(**input_kwargs)

        async def _worker_main() -> Any:
            """worker 线程主体：跑 litellm 并把流式 chunk 塞进队列。"""
            resp = await _run_litellm()
            # 非流式：直接返回结果对象
            if not hasattr(resp, "__aiter__"):
                return resp
            # 流式：在 worker 自己的 loop 里迭代，chunk 进线程安全队列
            _stream_obj.append(resp)
            try:
                async for chunk in resp:
                    _chunk_queue.put(chunk)
                    if _close_evt.is_set():
                        break
            finally:
                # 主循环可能已超时放弃（残留迭代），此处尽量关闭底层流
                aclose = getattr(resp, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
            return None

        def _worker() -> None:
            # ★ 不用 asyncio.run：它会在结束时 close() 线程事件循环，而
            # CustomStreamWrapper 的 logging 回调 / fallback 重试的 async client
            # 绑定该 loop → 消费时报 "Event loop is closed"（生产 20:08:02）。
            # 用 new_event_loop + run_until_complete，loop 保持存活
            # （daemon 线程持有，进程退出时由 OS 回收），流对象可被主循环消费。
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                _result_box.append(loop.run_until_complete(_worker_main()))
            except BaseException as exc:  # noqa: BLE001 - 异常装箱透传
                _exc_box.append(exc)
            finally:
                _done_evt.set()
                # 不 close loop：流 wrapper 绑定的 logging/fallback client 需要它存活。

        _worker_thread = threading.Thread(
            target=_worker,
            name=f"litellm-acompletion-{litellm_model[:24]}",
            daemon=True,
        )
        _worker_thread.start()

        # 主协程等待 worker 首次返回（建连 + 非流式结果 / 流式首个 chunk 入队前）
        _deadline = _time.monotonic() + _acompletion_timeout
        while not _done_evt.is_set() and _chunk_queue.empty():
            if _time.monotonic() >= _deadline:
                _close_evt.set()
                raise asyncio.TimeoutError(
                    f"litellm.acompletion 超时 {_acompletion_timeout:.0f}s"
                    f"（HTTP 层 timeout 已设 {_acompletion_timeout:.0f}s；残留线程由 daemon 回收）"
                    f"model={litellm_model}"
                )
            if _exc_box:
                raise _exc_box[0]
            await asyncio.sleep(0.1)

        if _exc_box:
            raise _exc_box[0]
        _result = _result_box[0] if _result_box else None
        _dc_t1 = _time.monotonic()
        logger.info(
            "[%s] _direct_call_with_slot: litellm.acompletion 返回(%.3fs) model=%s type=%s",
            type(self).__name__, _dc_t1 - _dc_t0, litellm_model, type(_result).__name__,
        )
        if _result is not None:
            return _result

        # 流式：返回桥接 iterator，主循环从线程安全队列消费 chunk
        return _ThreadedStreamBridge(
            queue=_chunk_queue,
            done_evt=_done_evt,
            exc_box=_exc_box,
            close_evt=_close_evt,
            completion_stream=_stream_obj[0] if _stream_obj else None,
        )
