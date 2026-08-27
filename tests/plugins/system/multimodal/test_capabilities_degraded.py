# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""multimodal 能力面静默降级治理测试（scan S1）。

行为契约：
1. 配置源缺失（llm_config / router_factory 不可导入）时按默认能力降级，
   但必须 (a) 发 WARNING 告警 (b) 返回值带 degraded=True 标记；
2. 配置源正常时 degraded 恒 False（防标记泛化）；
3. DefaultAdapter 恒带 degraded=True——附件会被它丢弃，调用方必须可感知；
4. 工具面透出 degraded 字段（multimodal.convert / multimodal.capability /
   files/capabilities payload）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "multimodal"

_MODULE_CACHE: dict[str, Any] = {}


def _load(name: str, unique: str) -> Any:
    """按唯一名加载插件模块（同 conftest 路径策略）。"""
    if unique in _MODULE_CACHE:
        return _MODULE_CACHE[unique]
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location(unique, _PLUGIN_DIR / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[unique] = module
    return module


def _capabilities() -> Any:
    return _load("capabilities", "mm_capabilities_degraded_test")


def _adapter() -> Any:
    return _load("adapter", "mm_adapter_degraded_test")


@pytest.fixture
def registry() -> Any:
    """每次用例重置 ADAPTER_MAPPING（register_adapter 会改类状态）。"""
    cap_mod = _capabilities()
    saved = dict(cap_mod.ModelCapabilityRegistry.ADAPTER_MAPPING)
    yield cap_mod.ModelCapabilityRegistry
    cap_mod.ModelCapabilityRegistry.ADAPTER_MAPPING.clear()
    cap_mod.ModelCapabilityRegistry.ADAPTER_MAPPING.update(saved)


# ═══════════════════════════════════════════════════════════
# 1. 配置源缺失 → warning + degraded 标记
# ═══════════════════════════════════════════════════════════


class TestCapabilityConfigMissingDegraded:
    def test_get_capability_marks_degraded_and_warns(
        self, registry: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setitem(sys.modules, "llm_config", None)
        with caplog.at_level(logging.WARNING, logger="*"):
            cap = registry.get_capability("glm-5.2")

        assert getattr(cap, "degraded", None) is True
        assert cap.supports_image is False  # 默认空能力语义不变
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "配置源缺失必须产生 WARNING 告警"
        assert any("llm_config" in r.getMessage() for r in warnings)
    @pytest.mark.parametrize("model_name", ["gpt-4o", "", "未知模型"])
    def test_get_capability_with_config_not_marked(
        self, registry: Any, monkeypatch: pytest.MonkeyPatch, model_name: str
    ) -> None:
        """配置源正常时（含模型未配置多模态）degraded 必须为 False。"""

        class _MM(types.SimpleNamespace):
            pass

        mm = _MM(
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supports_document=False,
            supported_image_types=["image/png"],
            supported_audio_types=[],
            supported_video_types=[],
            max_image_size=1,
            max_audio_size=1,
            max_video_size=1,
            max_document_size=1,
        )
        fake_config = types.ModuleType("llm_config")
        fake_config.get_llm_config = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
            find_model_by_name_or_alias=lambda _name: types.SimpleNamespace(multimodal=mm)
        )
        monkeypatch.setitem(sys.modules, "llm_config", fake_config)

        cap = registry.get_capability(model_name)
        assert getattr(cap, "degraded", None) is False
        assert cap.supports_image is True


class TestAdapterForModelDegraded:
    def test_router_missing_marks_adapter_degraded_and_warns(
        self, registry: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setitem(sys.modules, "router_factory", None)
        with caplog.at_level(logging.WARNING, logger="*"):
            adapter = registry.get_adapter_for_model("any-model")

        assert isinstance(adapter, _capabilities().DefaultAdapter)
        assert getattr(adapter, "degraded", None) is True
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "provider 解析源缺失必须产生 WARNING 告警"

    def test_known_provider_adapter_not_marked(self, registry: Any) -> None:
        adapter = registry.get_adapter("openai")
        assert isinstance(adapter, registry.ADAPTER_MAPPING["openai"])
        assert getattr(adapter, "degraded", None) is False

    @pytest.mark.parametrize("provider", ["nonexistent-provider", "default"])
    def test_default_adapter_always_flagged(self, registry: Any, provider: str) -> None:
        """DefaultAdapter 会丢弃全部附件：无论命中路径如何，标记必须为 True。"""
        default_cls = _capabilities().DefaultAdapter
        adapter_cls = registry.ADAPTER_MAPPING.get(provider, default_cls)
        assert adapter_cls is default_cls
        assert adapter_cls.degraded is True
        adapter = registry.get_adapter(provider)
        assert isinstance(adapter, default_cls)
        assert adapter.degraded is True

    def test_default_adapter_convert_drops_attachments(self) -> None:
        """degraded 标记与真实丢弃行为一致：附件确实不进输出。"""
        from mm_types import AttachmentInfo, MediaType

        att = AttachmentInfo(
            file_id="f1",
            filename="a.png",
            mime_type="image/png",
            size=10,
            media_type=MediaType.IMAGE,
            base64_data="AAAA",
        )
        messages = _adapter().DefaultAdapter().convert("hello", [att])
        assert messages == [{"type": "text", "text": "hello"}]


# ═══════════════════════════════════════════════════════════
# 4. 工具面透出 degraded
# ═══════════════════════════════════════════════════════════


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestToolFaceSurfacesDegraded:
    def test_capability_payload_carries_degraded_false_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置源可用时工具面如实报 degraded=False。"""
        server = _load("server", "mm_server_degraded_test")
        mm = types.SimpleNamespace(
            supports_image=True, supports_audio=False, supports_video=False,
            supports_document=False, supported_image_types=["image/png"],
            supported_audio_types=[], supported_video_types=[],
            max_image_size=1, max_audio_size=1, max_video_size=1, max_document_size=1,
        )
        fake_config = types.ModuleType("llm_config")
        fake_config.get_llm_config = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
            find_model_by_name_or_alias=lambda _name: types.SimpleNamespace(multimodal=mm)
        )
        monkeypatch.setitem(sys.modules, "llm_config", fake_config)
        result = _run(server.multimodal_capability("glm-5.2"))
        assert result["degraded"] is False

    def test_files_capabilities_payload_surfaces_model_degradation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _load("server", "mm_server_degraded_test")
        monkeypatch.setitem(sys.modules, "llm_config", None)
        payload = server._files_capabilities_payload("glm-5.2")
        assert payload["degraded"] is True

    def test_convert_result_flags_unknown_provider(self) -> None:
        server = _load("server", "mm_server_degraded_test")
        result = _run(server.multimodal_convert(content="hi", provider="no-such-provider"))
        assert result["count"] == 1  # 仅文本块，附件无 → 行为不变
        assert result["degraded"] is True
