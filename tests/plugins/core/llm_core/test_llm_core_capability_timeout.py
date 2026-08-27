# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 反向能力调用超时契约测试。

llm_core 经 capability caller（tool-executor.invoke → llm.complete_stream）调
LLM 时必须显式传等待上限：SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 面向短调用，
LLM 耗时可到 llm.yaml call_timeout（默认 600s），不传大值会先于 LLM 完成被
SDK 掐断（事故形态：[-32001] tool-executor.invoke timed out，llm_usage 全 0）。

契约：
1. timeout = 模型配置 call_timeout + 60s 余量（余量让 llm_service 的结构化
   错误信封先于 SDK 超时返回——错误是值，不丢语义）；
2. 模型段无 call_timeout → 回退 llm.yaml defaults.call_timeout；
3. 模型不在 llm.yaml → 用 loader 内部默认 300s 兜底（口径与
   get_llm_core_config 的 defaults 兜底一致）；
4. method/params 不受影响（透传回归保护）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
_CORE_DIR = _LLM_CORE_DIR.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import _config_models  # noqa: E402  平铺模块（system/llm 下）
from llm_core.plugin import LLMCore, set_capability_caller  # noqa: E402

# SDK 默认等待上限（llm_core 侧传值必须大于一切下游 LLM 耗时上界）
_CAPABILITY_CALL_TIMEOUT_S = 30.0


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
            "data": {"text": "ok", "tool_calls": [], "thinking_text": "", "usage": {}},
        }


def _install_llm_config(monkeypatch: pytest.MonkeyPatch, llm_section: dict[str, Any]) -> None:
    """注入 llm.yaml 命名空间（走真实 ModelConfigLoaderShim 代码路径）。"""
    monkeypatch.setattr(_config_models, "_config", {"llm": llm_section})


def _make_and_execute(model_name: str) -> _TimeoutCapturingCaller:
    """构造挂伪 caller 的 LLMCore 并执行一轮，返回 caller 供断言。"""
    caller = _TimeoutCapturingCaller()
    set_capability_caller(caller)
    plugin = LLMCore(
        {"provider": "openai", "model_name": model_name, "default_params": {}}
    )
    ctx = SimpleNamespace(
        state={
            "messages": [{"role": "user", "content": "hi"}],
            "streaming": True,
            "pipeline_id": "test-cap-timeout",
        }
    )
    asyncio.run(plugin.execute(ctx))  # type: ignore[arg-type]
    return caller


def test_timeout_uses_model_call_timeout_plus_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型配置 call_timeout=600 → caller 收到 660（+60s 余量）。"""
    _install_llm_config(
        monkeypatch,
        {
            "models": {
                "m-large": {
                    "provider": "p1",
                    "model_name": "m-large",
                    "call_timeout": 600,
                }
            },
            "defaults": {"call_timeout": 300},
        },
    )
    caller = _make_and_execute("m-large")

    assert len(caller.calls) == 1
    method, params, timeout = caller.calls[0]
    # 透传回归保护：method/params 形态不变
    assert method == "invoke"
    assert params["tool_name"] == "llm.complete_stream"
    # 字面值断言 + 性质断言：必须 > 下游 call_timeout（下游错误先于 SDK 掐断返回）
    assert timeout == 660.0
    assert timeout is not None and timeout > 600
    assert timeout > _CAPABILITY_CALL_TIMEOUT_S


def test_timeout_falls_back_to_defaults_call_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型段无 call_timeout → 回退 defaults.call_timeout=450 → 510。"""
    _install_llm_config(
        monkeypatch,
        {
            "models": {"m-large": {"provider": "p1", "model_name": "m-large"}},
            "defaults": {"call_timeout": 450},
        },
    )
    caller = _make_and_execute("m-large")

    timeout = caller.calls[0][2]
    assert timeout == 510.0
    assert timeout is not None and timeout > 450


def test_timeout_falls_back_to_internal_default_when_model_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型不在 llm.yaml → loader 内部默认 300s 口径 → 360。"""
    _install_llm_config(monkeypatch, {"models": {}, "defaults": {}})
    caller = _make_and_execute("m-unknown")

    timeout = caller.calls[0][2]
    assert timeout == 360.0
    assert timeout is not None and timeout > 300
    assert timeout > _CAPABILITY_CALL_TIMEOUT_S
