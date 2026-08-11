"""Prompt 审计落盘测试（task 日志体系改进）。

验证：
- 默认关闭：开关未开时 _log_prompt_body 零行为（不建文件）
- 开启落盘：开关开时写 prompt_audit.log，含 messages
- 脱敏：api_key / sk- / Bearer 被掩码
- 开关零开销：关闭时不触发文件 handler 初始化
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

pytestmark = pytest.mark.unit


@pytest.fixture
def adapter_mod(monkeypatch, tmp_path):
    """重新 import adapter，确保 env 开关生效（模块级常量在 import 时读取）。"""
    # 清理已 import 的 adapter（强制重读模块级 _PROMPT_AUDIT_ENABLED）
    for mod_name in list(sys.modules):
        if mod_name == "adapter" or mod_name.startswith("adapter."):
            del sys.modules[mod_name]
    # provider_adapters 子包在 adapter import 链里可能已 import，一并清
    for mod_name in list(sys.modules):
        if mod_name == "provider_adapters" or mod_name.startswith("provider_adapters."):
            del sys.modules[mod_name]
    import adapter  # noqa: PLC0415

    return importlib.reload(adapter)


def test_redact_masks_openai_key(adapter_mod) -> None:
    """sk- 前缀密钥应被掩码（保留前缀）。

    注意：出现在 "api_key":"..." 字段内的会被字段规则整体掩码成 ***（更安全），
    这里测试独立出现的 sk- 形态（如 user 消息里粘贴的 key）。
    """
    text = 'user pasted key: sk-abcdef1234567890xyz'
    redacted = adapter_mod._redact_prompt(text)
    assert "sk-abcdef1234567890xyz" not in redacted
    assert "sk-abcd" in redacted  # 保留前 6 字符


def test_redact_masks_api_key_field(adapter_mod) -> None:
    """"api_key": "..." 字段值应替换为 ***。"""
    text = '{"api_key": "raw_secret_value"}'
    redacted = adapter_mod._redact_prompt(text)
    assert "raw_secret_value" not in redacted
    assert "***" in redacted


def test_redact_masks_bearer(adapter_mod) -> None:
    """Bearer token 应被掩码。"""
    text = "Authorization: Bearer abcdefghijklmnop123456"
    redacted = adapter_mod._redact_prompt(text)
    assert "abcdefghijklmnop123456" not in redacted


def test_log_prompt_body_disabled_by_default(adapter_mod, monkeypatch, tmp_path) -> None:
    """默认 AGENTOS_LOG_PROMPT_BODY 未设 → 不落盘、不建 handler。"""
    monkeypatch.delenv("AGENTOS_LOG_PROMPT_BODY", raising=False)
    # 重新读模块级常量（import 时已读 env，需手动覆盖）
    adapter_mod._PROMPT_AUDIT_ENABLED = False
    adapter_mod._prompt_logger.handlers.clear()

    adapter_mod._log_prompt_body(
        "zai/glm-4",
        [{"role": "user", "content": "hi"}],
        None,
        temperature=0.7,
    )
    # 关闭时不应挂任何 handler
    assert adapter_mod._prompt_logger.handlers == []


def test_log_prompt_body_enabled_writes_file(adapter_mod, monkeypatch, tmp_path) -> None:
    """开启时落盘 prompt_audit.log，含 messages，且 api_key 脱敏。"""
    log_file = tmp_path / "prompt_audit.log"
    monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(log_file))
    adapter_mod._PROMPT_AUDIT_ENABLED = True
    adapter_mod._prompt_logger.handlers.clear()
    adapter_mod._prompt_logger.disabled = False

    adapter_mod._log_prompt_body(
        "zai/glm-4",
        [{"role": "user", "content": "hello"}],
        [{"type": "function", "name": "search"}],
        api_key="sk-secret1234567890",
        temperature=0.7,
    )

    # flush handler 确保落盘
    for h in adapter_mod._prompt_logger.handlers:
        h.flush()

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "hello" in content  # messages 内容落盘
    assert "glm-4" in content
    # api_key 脱敏
    assert "sk-secret1234567890" not in content
