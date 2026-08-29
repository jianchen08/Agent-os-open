# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""multimodal 能力面行为测试。

能力真值源 = ``config/models/llm.yaml`` 的 models.<id>.multimodal 节。
行为契约：
1. llm.yaml 缺失 / 解析失败 → WARNING + degraded=True（字段值不可作路由依据）；
2. llm.yaml 正常且模型配了 multimodal 节 → 按配置如实返回（degraded=False）；
3. llm.yaml 正常但模型无 multimodal 节 → 默认空能力（degraded=False，语义为
   "模型未声明多模态"，与"配置断链"的 degraded=True 可区分）；
4. 查找兼容 yaml models 键与 model_name 字段（大小写不敏感）；
5. 配置文件变更后能力随 mtime 缓存失效而更新；
6. DefaultAdapter 恒带 degraded=True——附件会被它丢弃，调用方必须可感知；
7. 工具面透出 degraded 字段（multimodal.capability / files/capabilities payload）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
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


_LLM_YAML = """\
models:
  glm-5.2:
    model_name: glm-5.2
    provider: zhipu_coding
    multimodal:
      supports_image: true
      supported_image_types: [image/png, image/jpeg]
      max_image_size: 20971520
  deepseek-v4-flash:
    model_name: deepseek-v4-flash
    provider: deepseek
  MiniMax-M3:
    model_name: minimax-m3
    provider: minimax
    multimodal:
      supports_image: true
      supported_image_types: [image/png]
"""


@pytest.fixture
def llm_yaml_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """能力模块指向临时 llm.yaml，并隔离模块级 mtime 缓存。"""
    cap_mod = _capabilities()
    yaml_path = tmp_path / "llm.yaml"
    yaml_path.write_text(_LLM_YAML, encoding="utf-8")
    monkeypatch.setattr(cap_mod, "_LLM_YAML_PATH", yaml_path)
    monkeypatch.setattr(cap_mod, "_LLM_MODELS_CACHE", None)
    return yaml_path


@pytest.fixture
def registry() -> Any:
    """每次用例重置 ADAPTER_MAPPING（register_adapter 会改类状态）。"""
    cap_mod = _capabilities()
    saved = dict(cap_mod.ModelCapabilityRegistry.ADAPTER_MAPPING)
    yield cap_mod.ModelCapabilityRegistry
    cap_mod.ModelCapabilityRegistry.ADAPTER_MAPPING.clear()
    cap_mod.ModelCapabilityRegistry.ADAPTER_MAPPING.update(saved)


# ═══════════════════════════════════════════════════════════
# 1. 配置源缺失/损坏 → warning + degraded 标记
# ═══════════════════════════════════════════════════════════


class TestCapabilityConfigBrokenDegraded:
    def test_missing_yaml_degrades_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        cap_mod = _capabilities()
        monkeypatch.setattr(cap_mod, "_LLM_YAML_PATH", tmp_path / "no-such.yaml")
        monkeypatch.setattr(cap_mod, "_LLM_MODELS_CACHE", None)
        with caplog.at_level(logging.WARNING):
            cap = cap_mod.ModelCapabilityRegistry.get_capability("glm-5.2")

        assert cap.degraded is True
        assert cap.supports_image is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "配置源缺失必须产生 WARNING 告警"
        assert any("llm.yaml" in r.getMessage() for r in warnings)

    def test_malformed_yaml_degrades_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        cap_mod = _capabilities()
        bad = tmp_path / "llm.yaml"
        bad.write_text("models: [unclosed", encoding="utf-8")
        monkeypatch.setattr(cap_mod, "_LLM_YAML_PATH", bad)
        monkeypatch.setattr(cap_mod, "_LLM_MODELS_CACHE", None)
        with caplog.at_level(logging.WARNING):
            cap = cap_mod.ModelCapabilityRegistry.get_capability("glm-5.2")

        assert cap.degraded is True
        assert cap.supports_image is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "配置损坏必须产生 WARNING 告警"


# ═══════════════════════════════════════════════════════════
# 2/3/4/5. 真配置读取：配了→如实返回；没配→默认空；键/字段双兼容；缓存随 mtime 失效
# ═══════════════════════════════════════════════════════════


class TestCapabilityFromConfig:
    def test_configured_model_reports_support(self, registry: Any, llm_yaml_path: Path) -> None:
        cap = registry.get_capability("glm-5.2")
        assert cap.degraded is False
        assert cap.supports_image is True
        assert cap.supported_image_types == ["image/png", "image/jpeg"]
        assert cap.max_image_size == 20971520

    @pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "unknown-model", ""])
    def test_unconfigured_model_reports_default_empty(
        self, registry: Any, llm_yaml_path: Path, model_name: str
    ) -> None:
        """模型存在但无 multimodal 节 / 模型不存在：默认空能力且 degraded=False
        （"未声明多模态"不是"配置断链"）。"""
        cap = registry.get_capability(model_name)
        assert cap.degraded is False
        assert cap.supports_image is False

    @pytest.mark.parametrize("query", ["minimax-m3", "MiniMax-M3", "MINIMAX-M3"])
    def test_lookup_matches_yaml_key_case_insensitive(self, registry: Any, llm_yaml_path: Path, query: str) -> None:
        cap = registry.get_capability(query)
        assert cap.supports_image is True

    def test_capability_tracks_config_change(self, registry: Any, llm_yaml_path: Path) -> None:
        """删值实验：配置改为不支持后，能力查询必须跟着变（缓存随 mtime 失效）。"""
        assert registry.get_capability("glm-5.2").supports_image is True
        llm_yaml_path.write_text(
            "models:\n  glm-5.2:\n    model_name: glm-5.2\n",
            encoding="utf-8",
        )
        cap = registry.get_capability("glm-5.2")
        assert cap.supports_image is False
        assert cap.degraded is False


# ═══════════════════════════════════════════════════════════
# 6. DefaultAdapter 丢弃语义
# ═══════════════════════════════════════════════════════════


class TestDefaultAdapterDegraded:
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
# 7. 工具面透出 degraded 与真实能力
# ═══════════════════════════════════════════════════════════


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestToolFaceSurfacesCapability:
    def test_capability_tool_reports_configured_model(
        self, monkeypatch: pytest.MonkeyPatch, llm_yaml_path: Path
    ) -> None:
        server = _load("server", "mm_server_degraded_test")
        result = _run(server.multimodal_capability("glm-5.2"))
        assert result["degraded"] is False
        assert result["supports_image"] is True

    def test_files_capabilities_payload_reports_configured_model(
        self, monkeypatch: pytest.MonkeyPatch, llm_yaml_path: Path
    ) -> None:
        server = _load("server", "mm_server_degraded_test")
        payload = server._files_capabilities_payload("glm-5.2")
        assert payload["degraded"] is False
        assert payload["supports_image"] is True
        assert payload["is_multimodal"] is True

    def test_convert_result_flags_unknown_provider(self) -> None:
        server = _load("server", "mm_server_degraded_test")
        result = _run(server.multimodal_convert(content="hi", provider="no-such-provider"))
        assert result["count"] == 1  # 仅文本块，附件无 → 行为不变
        assert result["degraded"] is True
