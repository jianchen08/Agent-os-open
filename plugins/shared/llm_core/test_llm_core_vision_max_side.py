# @feature: FP-0.2.〇 TB参赛适配 R20 | @ci: none-local
"""llm_core 视觉输入上限解析测试（R20/R21：state.llm_vision_max_pixels 写面（R21 总像素口径））。

规格链：llm.yaml models.<id>.multimodal.max_side → ModelIntent.vision_max_pixels
→ execute state_updates（消费链 tool_args_inject → file_read →
multimodal.split_image）。断行为：声明值优先 / 未声明保守缺省 1048576（1024²） / 非法值
归缺省 / 配置查询失败 None / 不支持视觉的模型不写值（None）。
模块装载与配置桥注入同 test_llm_core_model_binding.py 既有机制。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

if str(_LLM_DIR) not in sys.path:
    sys.path.append(str(_LLM_DIR))

_MOD_NAME = "llm_core_vision_max_pixels_under_test"


def _load_plugin() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin()
LLMCore = _mod.LLMCore
set_capability_caller = _mod.set_capability_caller
DEFAULT_VISION_MAX_PIXELS = _mod._DEFAULT_VISION_MAX_PIXELS


class _FakeCaller:
    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        # tool-executor.invoke 信封契约：{success, data}（fail-closed 缺 success 抛错）。
        return {
            "success": True,
            "data": {
                "status": "streamed",
                "stream_id": "stream_test",
                "partial": None,
                "text": "ok",
                "tool_calls": [],
                "thinking_text": None,
                "usage": {},
                "finish_reason": "stop",
            },
        }


def _model_entry(model_id: str, multimodal: dict[str, Any] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "provider": "prov",
        "model_name": model_id,
        "api_base": f"https://{model_id}.example.com",
        "api_key": "secret-key",
        "context_window": 64000,
        "default_params": {"temperature": 0.3},
    }
    if multimodal is not None:
        entry["multimodal"] = multimodal
    return entry


def _set_llm_config(monkeypatch: Any, llm: dict[str, Any]) -> None:
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {"llm": llm})


def _state() -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "test-vision",
        "run_id": "run-1",
    }


def _vision_declared(max_pixels: Any) -> dict[str, Any]:
    return {
        "supports_image": True,
        "supported_image_types": ["image/png"],
        "max_image_size": 20971520,
        "max_pixels": max_pixels,
    }


async def _run_execute(monkeypatch: Any, models: dict[str, Any]) -> dict[str, Any]:
    """执行一轮 execute，返回 state_updates（含 llm_vision_max_pixels 键）。"""
    _set_llm_config(monkeypatch, {"models": models, "defaults": {"chat": "model-a"}})
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    set_capability_caller(_FakeCaller())
    result = await plugin.execute(SimpleNamespace(state=_state()))
    assert result["llm_model_id"] == "model-a"
    return result


async def test_declared_max_pixels_flows_to_state(monkeypatch: Any) -> None:
    """声明值原样写 state（deepseek 295936=544² 取数形态）。"""
    updates = await _run_execute(
        monkeypatch,
        {"model-a": _model_entry("model-a", _vision_declared(295936))},
    )
    assert updates.get("llm_supports_vision") is True
    assert updates.get("llm_vision_max_pixels") == 295936


async def test_undeclared_max_pixels_falls_back_to_conservative_default(monkeypatch: Any) -> None:
    """支持视觉但未声明 max_side → 保守缺省 1048576（1024²）。"""
    updates = await _run_execute(
        monkeypatch,
        {
            "model-a": _model_entry(
                "model-a",
                {"supports_image": True, "supported_image_types": ["image/png"]},
            )
        },
    )
    assert updates.get("llm_vision_max_pixels") == DEFAULT_VISION_MAX_PIXELS == 1048576


async def test_invalid_max_pixels_falls_back_to_default(monkeypatch: Any) -> None:
    """非法声明（0/负数/字符串/bool）→ 归保守缺省，不透传脏值。"""
    for bad in (0, -5, "2048", True):
        updates = await _run_execute(
            monkeypatch, {"model-a": _model_entry("model-a", _vision_declared(bad))}
        )
        assert updates.get("llm_vision_max_pixels") == 1048576, f"bad={bad!r}"


async def test_non_vision_model_writes_none(monkeypatch: Any) -> None:
    """不支持视觉 → None（消费链不注入规格，file_read 行为与既往一致）。"""
    updates = await _run_execute(
        monkeypatch,
        {"model-a": _model_entry("model-a", {"supports_image": False})},
    )
    assert updates.get("llm_supports_vision") is False
    assert updates.get("llm_vision_max_pixels") is None


async def test_missing_multimodal_section_writes_none(monkeypatch: Any) -> None:
    updates = await _run_execute(monkeypatch, {"model-a": _model_entry("model-a", None)})
    assert updates.get("llm_vision_max_pixels") is None


async def test_config_query_failure_writes_none(monkeypatch: Any) -> None:
    """配置桥故障 → None（与 supports_vision 同款降级，不阻断模型解析）。"""
    import _config_models

    def _raise_bridge_down(self: Any, model_id: str) -> Any:
        raise RuntimeError("bridge down")

    monkeypatch.setattr(
        _config_models.ModelConfigLoaderShim,
        "get_model_config",
        _raise_bridge_down,
        raising=False,
    )
    plugin = LLMCore({"provider": "openai", "model_name": "gpt-4", "default_params": {}})
    assert plugin._read_vision_max_pixels("model-a") is None
