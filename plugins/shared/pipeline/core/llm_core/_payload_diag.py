"""Payload 诊断工具：环境开关 + 敏感字段脱敏 + tempfile 落盘。

历史问题：``adapter._install_payload_diag_hook()`` 在模块加载期无条件 monkey-patch
litellm，每次 LLM 调用把原始 HTTP body（可能含明文 ``api_key`` / ``Authorization``，
且必然含完整 prompt）落盘到仓库内 ``logs/payload_diag/``，并用 ``except Exception: pass``
空吞错——既泄露敏感信息，又污染仓库目录、产生大量未跟踪 JSON。

本模块把诊断相关的纯函数从 adapter 中拆出（不依赖 litellm，便于单测）：

- 默认关闭：仅当环境变量 ``AGENTOS_PAYLOAD_DIAG=1`` 时才可能写诊断文件；
- 脱敏：写入前对 ``api_key`` / ``api-key`` / ``apikey`` / ``authorization`` /
  ``x-api-key`` / ``key`` 等键（递归、大小写无关）的值替换为占位符；
- tempfile：写入系统临时目录，不再污染仓库；

设计约束（见 config/rules）：禁裸 except、禁全局可变状态；脱敏为纯函数。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 大小写无关的敏感键集合。命中即把整个值替换为占位符（含 "Bearer xxx" 令牌）。
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"api_key", "apikey", "api-key", "authorization", "x-api-key", "key", "secret", "token"}
)
_REDACTED_PLACEHOLDER = "***REDACTED***"


def is_payload_diag_enabled() -> bool:
    """是否启用 payload 诊断。

    Returns:
        仅当 ``AGENTOS_PAYLOAD_DIAG`` 恰为 ``"1"`` 时为 True，其余（含未设置）为 False。
    """
    return os.getenv("AGENTOS_PAYLOAD_DIAG", "") == "1"


def _is_sensitive_key(key: Any) -> bool:
    """判断给定键名是否属于敏感键（大小写无关）。"""
    return isinstance(key, str) and key.lower() in _SENSITIVE_KEYS


def _redact(value: Any) -> Any:
    """递归脱敏：对 dict 的敏感键整值替换，list 逐项递归，其余原样返回。"""
    if isinstance(value, dict):
        return {k: (_REDACTED_PLACEHOLDER if _is_sensitive_key(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def redact_payload(body: Any) -> Any:
    """返回 body 的脱敏深拷贝（敏感键的值被替换为占位符）。

    Args:
        body: litellm ``transform_request`` 返回的原始 body（可能含 api_key）。

    Returns:
        结构同 body 但敏感字段已脱敏的新对象；非 dict/list 原样返回。
    """
    return _redact(body)


def _safe_filename_segment(text: str, limit: int = 48) -> str:
    """把任意文本压成文件名安全的字母数字/下划线/连字符段。"""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in text)
    return (cleaned[:limit] or "model")


def dump_payload_diag(model: str, body: dict[str, Any]) -> Path | None:
    """环境开关开启时，把脱敏后的 body 写入系统 tempfile；关闭时返回 None。

    Args:
        model: 模型名（仅用于文件名，会做安全化处理）。
        body: litellm ``transform_request`` 返回的原始 body。

    Returns:
        写入的临时文件路径；未启用（默认）时返回 None。
    """
    if not is_payload_diag_enabled():
        return None
    try:
        redacted = redact_payload(body)
        raw = json.dumps(redacted, ensure_ascii=False)
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
        prefix = f"payload_diag_{_safe_filename_segment(model)}_{digest}_"
        fd, path_str = tempfile.mkstemp(prefix=prefix, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
    except (OSError, ValueError, TypeError) as exc:
        # 收窄：仅容忍写盘/序列化的运行期错误，绝不用裸 except 吞编程错误。
        logger.warning("[payload_diag] 写入 tempfile 失败: %s", exc)
        return None
    return Path(path_str)
