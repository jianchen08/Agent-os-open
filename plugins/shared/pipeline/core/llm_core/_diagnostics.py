"""adapter 诊断与审计基础设施（自 adapter.py 拆出，task_kernel_cleanup_and_split 3b）。

包含：
- payload 诊断（prefix-cache hash + 脱敏落盘）：`_log_final_payload` /
  `_install_payload_diag_hook`（模块级安装，环境开关 `AGENTOS_PAYLOAD_DIAG=1`）
- 流式/诊断 logger：`_diag_logger` / `_stream_logger`（logger 名保持 `adapter.*`，
  与拆分前完全一致——测试与生产日志过滤依赖这些名字）
- prompt 审计（`AGENTOS_LOG_PROMPT_BODY=1` 开启）：`_log_prompt_body` /
  `_sync_prompt_handlers` / `_redact_prompt` 等

纯函数部分（脱敏/落盘原语）在 `_payload_diag.py`（litellm-free），本模块只含
与 litellm 或 logging 基础设施工耦合的部分。
"""

from __future__ import annotations

import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Any

from _payload_diag import dump_payload_diag, is_payload_diag_enabled

logger = logging.getLogger(__name__)


# === 缓存诊断：在 litellm 真正构造 HTTP body 的位置拦截 ===
# transform_request 返回的 dict["messages"] 就是发送给 API 的最终消息体
# （经过 provider transformation、cache_control 处理之后，httpx 发送之前）。
# 这是唯一能看到"真实发出的字节"的位置。adapter 层看到的 messages 还会被
# litellm 内部改写（合并 system、移除 cache_control、字段重排等），不等于
# 真实 payload，故必须在此拦截。两轮对比 prefix_hash 即可定位 cache 断点。
#
# 安全约束（P0-2 修复）：默认**关闭**，仅当 AGENTOS_PAYLOAD_DIAG=1 时才安装钩子；
# 开启后落盘走 _payload_diag.dump_payload_diag（系统 tempfile + 敏感字段脱敏），
# 不再把含 api_key/Authorization 的原始 body 写进仓库 logs/payload_diag。
# logger 名保持 "llm.adapter._payload_diag"（与拆分前一致）。
_DIAG_PAYLOAD_LOGGER = logging.getLogger("llm.adapter._payload_diag")


def _log_final_payload(model: str, body: dict) -> None:
    """记录 prefix-cache 诊断 hash，并把脱敏后的 body 写入 tempfile（受环境开关）。"""
    import hashlib  # noqa: PLC0415

    try:
        msgs = body.get("messages", [])
        # 字段顺序须保留原序：prefix cache 按字节匹配，重排字段 = 前缀变 = 缓存失效，
        # 故绝不能用 sort_keys 重排。此处仅计算 hash，不落盘原始 body。
        running = ""
        body_raw = json.dumps(body, ensure_ascii=False)
        body_hash = hashlib.md5(body_raw.encode("utf-8")).hexdigest()[:12]
        msgs_raw = json.dumps(msgs, ensure_ascii=False)
        msgs_hash = hashlib.md5(msgs_raw.encode("utf-8")).hexdigest()[:12]
        _DIAG_PAYLOAD_LOGGER.info(
            "POST_TRANSFORM model=%s msg_count=%d body_hash=%s msgs_hash=%s",
            model,
            len(msgs),
            body_hash,
            msgs_hash,
        )
        for pi, pm in enumerate(msgs):
            mj = json.dumps(pm, ensure_ascii=False)
            running += mj + "\n"
            full = hashlib.md5(mj.encode("utf-8")).hexdigest()[:8]
            prefix = hashlib.md5(running.encode("utf-8")).hexdigest()[:8]
            _DIAG_PAYLOAD_LOGGER.info(
                "POST_TRANSFORM_MSG[%d] role=%s name=%s full_hash=%s prefix_hash=%s",
                pi,
                pm.get("role", "?"),
                pm.get("name", ""),
                full,
                prefix,
            )
    except (KeyError, TypeError, ValueError) as exc:
        # 收窄：诊断日志失败不得影响主调用；但绝不用裸 except 吞编程错误。
        logger.debug("[payload_diag] hash 日志失败: %s", exc)

    # 落盘走统一入口：默认关闭；开启时写 tempfile + 脱敏 api_key/Authorization。
    dump_payload_diag(model, body)


def _install_payload_diag_hook() -> None:
    """安装 litellm transform_request 拦截钩子（默认关闭）。

    仅当 ``AGENTOS_PAYLOAD_DIAG=1`` 时才 monkey-patch 各 provider 的
    transformation 类；默认不 patch、不落盘，彻底消除敏感信息泄露面。
    """
    if not is_payload_diag_enabled():
        logger.debug("[payload_diag] 未启用（设置 AGENTOS_PAYLOAD_DIAG=1 开启）")
        return
    try:
        import importlib as _importlib  # noqa: PLC0415

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
            except ImportError:
                # 该 provider transformation 模块不存在（litellm 版本/精简安装），跳过。
                continue
            for _cls in vars(_mod).values():
                if isinstance(_cls, type):
                    _patch_class(_cls)

        logger.info(
            "[payload_diag] 已安装 litellm transform_request 拦截钩子，patched=%d 类",
            len(_patched_classes),
        )
    except ImportError as e:
        # 收窄：仅捕获 import 期问题；patch 运行期错误不应在此吞掉。
        logger.warning("[payload_diag] 拦截钩子安装失败: %s", e)


_install_payload_diag_hook()
# === 缓存诊断结束 ===

# 流式/诊断 logger：logger 名保持拆分前的 "adapter.*"（测试 monkeypatch 与生产
# 日志过滤依赖这些名字，不能随 __name__ 变成 "_diagnostics"）。
_diag_logger = logging.getLogger("adapter._diag")
_diag_logger.propagate = False
_stream_logger = logging.getLogger("adapter._stream")
_stream_logger.propagate = False


# ── Prompt 审计日志（完整 messages 请求体落盘，默认关）──────────────────────
# 发给远端 LLM API 的完整 messages/tools 请求体落盘，用于复现/审计/调试。
# 默认关闭（含 api_key/用户隐私，需显式开启并信任本地存储）。开启时经基础脱敏
# 写独立文件 data/logs/prompt_audit.log（独立 RotatingFileHandler，不依赖父 logger，
# 保证 0.2 sidecar 进程里一定有 handler）。
#
# 开关：env AGENTOS_LOG_PROMPT_BODY=1 / true
_PROMPT_AUDIT_ENABLED = os.getenv("AGENTOS_LOG_PROMPT_BODY", "").lower() in ("1", "true")
_prompt_logger = logging.getLogger("adapter._prompt")
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
