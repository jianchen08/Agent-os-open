# @feature: FP-0.2.七 插件监控与指标 | @ci: python-coverage
"""payload_diag 钩子条件安装回归测试。

背景（2026-09-04 监控诊断）：adapter.py 的 P0-2 修复注释声明「默认关闭，仅当
AGENTOS_PAYLOAD_DIAG=1 时才安装钩子」，但实现无条件调用 _install_payload_diag_hook()
——生产环境每次 LLM 请求都在 INFO 级打印全量 payload（POST_TRANSFORM_MSG 每条
KB 级，单请求 10+ 条）。本次修复把安装改为条件化。

行为契约（可观测）：
- AGENTOS_PAYLOAD_DIAG 未设或非 "1" → 不安装钩子（无 "已安装" 日志、litellm 未被 patch）
- AGENTOS_PAYLOAD_DIAG=1 → 安装钩子（有 "已安装" 日志）
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _pin_llm_dir_front() -> None:
    """本插件目录置 sys.path[0]——车道共跑方（multimodal 等同名 adapter.py
    目录）在采集期即可能占住前位，裸名 ``import adapter`` 会解析到他人实现。"""
    _s = str(_PLUGIN_DIR)
    while _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)


@pytest.fixture
def fresh_adapter(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> Any:
    """在受控 env 下重新 import adapter（模块级钩子装在 import 时执行）。"""
    for mod_name in list(sys.modules):
        if mod_name == "adapter" or mod_name.startswith("adapter."):
            del sys.modules[mod_name]
    for mod_name in list(sys.modules):
        if mod_name == "provider_adapters" or mod_name.startswith("provider_adapters."):
            del sys.modules[mod_name]

    _pin_llm_dir_front()
    with caplog.at_level(logging.INFO, logger="adapter"):
        import adapter  # noqa: PLC0415

        return importlib.reload(adapter)


def test_hook_not_installed_by_default(fresh_adapter: Any, caplog: Any) -> None:
    """AGENTOS_PAYLOAD_DIAG 未设 → 不安装拦截钩子（默认关闭契约）。"""
    assert "AGENTOS_PAYLOAD_DIAG" not in __import__("os").environ or (
        __import__("os").environ["AGENTOS_PAYLOAD_DIAG"] != "1"
    )
    hook_logs = [r.getMessage() for r in caplog.records]
    assert not any("已安装 litellm transform_request 拦截钩子" in m for m in hook_logs)


def test_hook_installed_when_enabled(
    fresh_adapter: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """AGENTOS_PAYLOAD_DIAG=1 → 安装拦截钩子。"""
    monkeypatch.setenv("AGENTOS_PAYLOAD_DIAG", "1")
    # 重新 import 让模块级条件读取新 env
    for mod_name in list(sys.modules):
        if mod_name == "adapter" or mod_name.startswith("adapter."):
            del sys.modules[mod_name]
    for mod_name in list(sys.modules):
        if mod_name == "provider_adapters" or mod_name.startswith("provider_adapters."):
            del sys.modules[mod_name]
    _pin_llm_dir_front()
    with caplog.at_level(logging.INFO, logger="adapter"):
        import adapter  # noqa: PLC0415

        importlib.reload(adapter)
    hook_logs = [r.getMessage() for r in caplog.records]
    assert any("已安装 litellm transform_request 拦截钩子" in m for m in hook_logs)
