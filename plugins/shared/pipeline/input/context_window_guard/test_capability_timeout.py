# @feature: FP-T07 llm api | @ci: python-coverage
"""context_window_guard 压缩链路反向能力调用超时契约测试。

压缩经 capability_caller 调 llm.complete_stream 与 llm_core 同构：必须显式传
等待上限，否则 SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 会先于压缩 LLM 完成
掐断请求（压缩是 LLM 级耗时，几乎必中招）。

契约：
1. timeout = 压缩模型 call_timeout + 60s 余量（下游结构化错误先于 SDK 超时返回）；
2. model_id 为空（llm_service 按默认 chat 兜底）→ 按 defaults.chat 模型配置取值；
3. _config_models 不可达（llm.yaml 未注入，guard 既有降级语义）→ 300s 内部
   默认口径 + 60s；
4. method/params 不受影响（透传回归保护）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# system/llm（_config_models）加入 sys.path
_REPO_ROOT = Path(__file__).resolve().parents[5]
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
if str(_SYSTEM_LLM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_LLM_DIR))

# pipeline 包（plugins/shared）加入 sys.path
_SHARED_DIR = Path(__file__).resolve().parents[3]
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import _config_models  # noqa: E402  平铺模块（system/llm 下）

# SDK 默认等待上限（压缩侧传值必须大于一切下游 LLM 耗时上界）
_CAPABILITY_CALL_TIMEOUT_S = 30.0


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "cwg_cap_timeout_test"
    sys.modules.pop(mod_name, None)
    plugin_path = _PLUGIN_DIR / "plugin.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _await(coro: Any) -> Any:
    """同步等待协程结果（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _TimeoutCapturingCaller:
    """伪 capability caller：记录 (method, params, timeout) 三元组。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params, timeout))
        return {
            "success": True,
            "data": {"text": "摘要", "tool_calls": [], "thinking_text": "", "usage": {}},
        }


def _install_llm_config(monkeypatch: pytest.MonkeyPatch, llm_section: dict[str, Any]) -> None:
    """注入 llm.yaml 命名空间（走真实 ModelConfigLoaderShim 代码路径）。"""
    monkeypatch.setattr(_config_models, "_config", {"llm": llm_section})


def _make_fn_and_call(
    mod: Any, caller: _TimeoutCapturingCaller, model_id: str
) -> None:
    """构建压缩 LLM 调用函数并触发一次调用。"""
    fn = mod._build_compress_llm_call_fn(caller, model_id=model_id)
    _await(fn("compress this text"))


def test_timeout_uses_compress_model_call_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """压缩模型配置 call_timeout=600 → caller 收到 660（+60s 余量）。"""
    _install_llm_config(
        monkeypatch,
        {
            "models": {
                "m-comp": {
                    "provider": "p1",
                    "model_name": "m-comp",
                    "call_timeout": 600,
                }
            },
            "defaults": {"call_timeout": 300, "chat": "m-chat"},
        },
    )
    mod = _load_plugin_module()
    caller = _TimeoutCapturingCaller()

    _make_fn_and_call(mod, caller, "m-comp")

    assert len(caller.calls) == 1
    method, params, timeout = caller.calls[0]
    assert method == "tool-executor.invoke"
    assert params["tool_name"] == "llm.complete_stream"
    assert timeout == 660.0
    assert timeout is not None and timeout > 600
    assert timeout > _CAPABILITY_CALL_TIMEOUT_S


def test_timeout_resolves_via_default_chat_model_when_model_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model_id 为空 → 按 defaults.chat 指向模型的 call_timeout=450 → 510。"""
    _install_llm_config(
        monkeypatch,
        {
            "models": {
                "m-def": {
                    "provider": "p1",
                    "model_name": "m-def",
                    "call_timeout": 450,
                }
            },
            "defaults": {"call_timeout": 300, "chat": "m-def"},
        },
    )
    mod = _load_plugin_module()
    caller = _TimeoutCapturingCaller()

    _make_fn_and_call(mod, caller, "")

    timeout = caller.calls[0][2]
    assert timeout == 510.0
    assert timeout is not None and timeout > 450


def test_timeout_falls_back_when_config_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_config_models 不可达（llm.yaml 未注入）→ 300s 内部默认口径 → 360。"""
    monkeypatch.setitem(sys.modules, "_config_models", None)
    mod = _load_plugin_module()
    caller = _TimeoutCapturingCaller()

    _make_fn_and_call(mod, caller, "m-comp")

    timeout = caller.calls[0][2]
    assert timeout == 360.0
    assert timeout is not None and timeout > 300
    assert timeout > _CAPABILITY_CALL_TIMEOUT_S
