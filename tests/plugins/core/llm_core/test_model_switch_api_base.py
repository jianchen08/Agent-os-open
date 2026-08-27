# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core 换模型解析契约测试（api_base 整体替换）。

同 sidecar 内 LLMCore 实例按 state.model_id 动态切换模型时：

1. 新模型配置缺 api_base → 显式报错（绝不拿上一模型的 base 兜底发请求——
   跨 provider 串味会把请求打到错误端点，属静默数据外泄面）；
2. 新模型配置带 api_base → 实例整体替换为该值（execute 结果
   ``llm_api_base`` 可观测），不与旧值合并；
3. 解析失败不得留下半套改动：失败后回到原模型执行，
   ``llm_api_base`` / ``llm_model`` 仍是原模型的值。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
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


class _FakeCaller:
    """伪 capability caller：tool-executor.invoke 返回成功信封并记录入参。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append(params)
        return {
            "success": True,
            "data": {"text": "ok", "tool_calls": [], "thinking_text": "", "usage": None},
        }


class _FakeLoader:
    """预设 model_id → llm_core 配置的桩加载器（不在表中 = 模型未配置）。"""

    def __init__(self, models: dict[str, dict[str, Any]]) -> None:
        self._models = models

    def get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        return self._models.get(model_id)

    def resolve_tier(self, tier: str) -> str:
        return ""

    def get_default_chat_model(self) -> str:
        return ""


def _ctx(state: dict[str, Any]) -> Any:
    from pipeline.plugin import PluginContext

    return PluginContext(state=dict(state), config={})


def _make_plugin() -> tuple[LLMCore, _FakeCaller]:
    caller = _FakeCaller()
    set_capability_caller(caller)
    core = LLMCore(
        config={
            "model_id": "a-model",
            "provider": "openai",
            "model_name": "a-real",
            "api_base": "https://a-host.example",
            "api_key": "k-a",
            "default_params": {},
        }
    )
    return core, caller


_B_CONF_NO_BASE = {"provider": "openai", "model_name": "b-real"}  # 缺 api_base


def test_missing_api_base_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """切到缺 api_base 的模型 → RuntimeError 点名 model_id；不得用旧 base 兜底。"""
    monkeypatch.setattr(
        _config_models,
        "get_model_config_loader",
        lambda: _FakeLoader({"b-model": _B_CONF_NO_BASE}),
    )
    core, _caller = _make_plugin()

    with pytest.raises(RuntimeError, match="b-model"):
        asyncio.run(core.execute(_ctx({"model_id": "b-model"})))


def test_switch_replaces_api_base_wholesale(monkeypatch: pytest.MonkeyPatch) -> None:
    """新模型带 base → execute 结果 llm_api_base 即新值，与旧值无关。"""
    monkeypatch.setattr(
        _config_models,
        "get_model_config_loader",
        lambda: _FakeLoader(
            {
                "b-model": {
                    "provider": "openai",
                    "model_name": "b-real",
                    "api_base": "https://b-host.example",
                    "api_key": "k-b",
                }
            }
        ),
    )
    core, caller = _make_plugin()

    result = asyncio.run(core.execute(_ctx({"model_id": "b-model"})))

    assert result["llm_api_base"] == "https://b-host.example"
    assert result["llm_model"] == "b-real"
    assert caller.calls[0]["args"]["model"] == "b-model"


def test_failed_resolution_leaves_original_model_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败的解析不允许半套生效：回原模型继续执行，llm_api_base 保持原值。"""
    monkeypatch.setattr(
        _config_models,
        "get_model_config_loader",
        lambda: _FakeLoader({"b-model": _B_CONF_NO_BASE}),
    )
    core, _caller = _make_plugin()

    with pytest.raises(RuntimeError):
        asyncio.run(core.execute(_ctx({"model_id": "b-model"})))

    # 原模型再执行（loader 无 a-model 配置 → 走未配置合法降级，保持构造默认）
    result = asyncio.run(core.execute(_ctx({"model_id": "a-model"})))
    assert result["llm_api_base"] == "https://a-host.example"
    assert result["llm_model"] == "a-real"
