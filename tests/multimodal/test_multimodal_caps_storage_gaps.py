# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""多模态能力注册表 / 磁盘存储未覆盖分支补测（簇 J）。

`tests/multimodal/` 已在插桩车道基集（`scripts/coverage_exempt.py`），
本文件随之进覆盖面。

capabilities.py
- ``_load_llm_models``：正常解析、文件缺失 → None、YAML 损坏 → None、
  ``models`` 键缺失 → 归一为空 dict；mtime 缓存两态（同 mtime 命中不重读 /
  mtime 前进重读，配置热更新契约）；
- ``_find_model_conf``：键匹配大小写不敏感、``model_name`` 字段匹配、
  非 dict 条目跳过、无匹配返回 None；
- ``get_capability``：multimodal 节缺失/非 dict → 默认空能力（degraded=False）；
  完整声明 → 字段按 yaml 真值投影（含 str/int 强制）；
  配置断链（文件缺失）→ degraded=True（与"未声明"可区分）；
- ``register_adapter``：注册后 ``get_adapter`` 取回注册实例；未知 provider
  回落 DefaultAdapter；已知 provider 返回声明类。

storage.py
- 第 26 行 ``sys.path`` 自举守卫：``plugins/shared`` 未在路径时插入一次；
  已在路径时不重复插入（幂等，两种路径顺序各测一次）；
- ``DiskFileStorage`` base_dir 三级解析：显式入参 > ``MULTIMODAL_STORAGE_DIR``
  env > 多租户根 ``data/{tenant_id}/multimodal``（tenant_id 缺省回落 default）。

★ 疑似真缺陷（不改生产码，仅报告）：``capabilities.py:_load_llm_models``
第 55 行 ``models = raw.get("models")`` 假定 ``yaml.safe_load`` 结果是 dict。
yaml 顶层为列表/标量（如 `- a\n- b\n` 或 `just-a-scalar\n`）时 ``raw`` 是
list/str → ``AttributeError`` 直接炸穿 get_capability（本应走该行下方的
``not isinstance(models, dict)`` 归一分支降级为空 config）。触发条件：任何
非 dict 顶层结构的 llm.yaml。本测试按"可达行为"钉住 dict 无 models 键的
归一分支，不钉崩溃路径。

外部依赖：yaml 用 tmp_path 真文件驱动（真实解析 + 真实 mtime），仅
``_LLM_YAML_PATH`` 常量按临时路径重定向；不 mock 被测模块内部函数。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "multimodal"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"

_YAML_PAYLOAD = """\
models:
  vision-a:
    model_name: vision-a
    multimodal:
      supports_image: true
      supports_audio: false
      supported_image_types:
        - image/png
      max_image_size: 1048576
  alias-key:
    model_name: RealModelName
    multimodal:
      supports_video: true
"""


def _fresh_capabilities() -> Any:
    """按真身路径重载 capabilities（唯一模块名，缓存独立）。"""
    if str(_SHARED_ROOT) not in sys.path:
        sys.path.insert(0, str(_SHARED_ROOT))
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    for name in ("capabilities", "adapter", "mm_types"):
        sys.modules.pop(name, None)
    name = "multimodal_caps_gap_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "capabilities.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """capabilities 模块 + 临时 yaml（真实解析与 mtime）。"""
    module = _fresh_capabilities()
    yaml_path = tmp_path / "llm.yaml"
    yaml_path.write_text(_YAML_PAYLOAD, encoding="utf-8")
    monkeypatch.setattr(module, "_LLM_YAML_PATH", yaml_path)
    monkeypatch.setattr(module, "_LLM_MODELS_CACHE", None)
    return module


# ══════════════════ _load_llm_models ══════════════════


class TestLoadModels:
    def test_parses_models_section(self, caps: Any) -> None:
        """正常 yaml：返回 models 节（键集合即模型 id）。"""
        models = caps._load_llm_models()

        assert isinstance(models, dict)
        assert set(models) == {"vision-a", "alias-key"}

    def test_missing_file_degrades_to_none(self, caps: Any, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """文件缺失 → None（断链降级信号，与"空 models 节"区分）。"""
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", tmp_path / "absent.yaml")

        assert caps._load_llm_models() is None

    def test_broken_yaml_degrades_to_none(self, caps: Any, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML 损坏 → None（不抛，调用方按断层处理）。"""
        broken = tmp_path / "broken.yaml"
        broken.write_text("models: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", broken)

        assert caps._load_llm_models() is None

    @pytest.mark.parametrize("payload", ["other: 1\n", "asr:\n  enabled: false\n", "{}\n"])
    def test_models_key_absent_normalised_to_empty(
        self, caps: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
    ) -> None:
        """yaml 顶层是 dict 但无 ``models`` 键 → 归一为空 dict（三种输入）。

        钉 ``if not isinstance(models, dict): models = {}`` 归一分支。
        """
        path = tmp_path / "odd.yaml"
        path.write_text(payload, encoding="utf-8")
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", path)

        assert caps._load_llm_models() == {}

    def test_mtime_cache_avoids_reread(self, caps: Any, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        """同 mtime 命中缓存：内容改写但 mtime 保持不变 → 仍返回缓存（不重读）。"""
        path = tmp_path / "cached.yaml"
        path.write_text("models:\n  first:\n    model_name: first\n", encoding="utf-8")
        pinned = 1_700_000_000.0
        os.utime(path, (pinned, pinned))
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", path)

        first = caps._load_llm_models()
        path.write_text("models:\n  second:\n    model_name: second\n", encoding="utf-8")
        os.utime(path, (pinned, pinned))
        cached = caps._load_llm_models()

        assert set(first) == {"first"}
        assert set(cached) == {"first"}, "mtime 未变即走缓存（热更新由 mtime 触发）"

    def test_mtime_advance_triggers_reread(self, caps: Any, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：mtime 前进后重读新内容（配置即时生效）。"""
        path = tmp_path / "hot.yaml"
        path.write_text("models:\n  old:\n    model_name: old\n", encoding="utf-8")
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", path)
        assert set(caps._load_llm_models()) == {"old"}

        path.write_text("models:\n  new:\n    model_name: new\n", encoding="utf-8")
        future = time.time() + 120
        os.utime(path, (future, future))

        assert set(caps._load_llm_models()) == {"new"}


# ══════════════════ _find_model_conf ══════════════════


class TestFindModelConf:
    def test_key_match_is_case_insensitive(self, caps: Any) -> None:
        """键匹配大小写不敏感（两串输入命中同一条）。"""
        models = {"Vision-A": {"model_name": "x"}}

        assert caps._find_model_conf(models, "vision-a") == {"model_name": "x"}
        assert caps._find_model_conf(models, "VISION-A") == {"model_name": "x"}

    def test_model_name_field_match_when_key_differs(self, caps: Any) -> None:
        """键不匹配而 ``model_name`` 字段匹配 → 命中（大小写不敏感）。"""
        models = {"weird-key": {"model_name": "RealModelName"}}

        assert caps._find_model_conf(models, "realmodelname") == {"model_name": "RealModelName"}

    def test_non_dict_entries_skipped(self, caps: Any) -> None:
        """非 dict 条目（列表/字符串）跳过：不炸且不误命中。"""
        models: dict[str, Any] = {"good": {"model_name": "good"}, "bad": ["nope"], "scalar": "x"}

        assert caps._find_model_conf(models, "nope") is None
        assert caps._find_model_conf(models, "good") == {"model_name": "good"}

    def test_no_match_returns_none(self, caps: Any) -> None:
        """性质：无匹配返回 None（区别于命中但空配置的 dict）。"""
        assert caps._find_model_conf({"a": {"model_name": "a"}}, "zzz") is None
        assert caps._find_model_conf({}, "anything") is None


# ══════════════════ get_capability ══════════════════


class TestGetCapability:
    def test_declared_multimodal_projected(self, caps: Any) -> None:
        """完整声明：各字段按 yaml 真值投影（含 str/int 强制）。"""
        cap = caps.ModelCapabilityRegistry.get_capability("vision-a")

        assert cap.model_name == "vision-a"
        assert cap.supports_image is True
        assert cap.supports_audio is False
        assert cap.supports_video is False
        assert cap.supported_image_types == ["image/png"]
        assert cap.max_image_size == 1048576
        assert cap.degraded is False

    def test_model_name_field_lookup(self, caps: Any) -> None:
        """字段匹配路径经 get_capability 生效（键为 alias-key）。"""
        cap = caps.ModelCapabilityRegistry.get_capability("RealModelName")

        assert cap.supports_video is True

    @pytest.mark.parametrize("model_name", ["undeclared-model", "vision-a"[:0] or "x"])
    def test_undeclared_model_yields_empty_capability(self, caps: Any, model_name: str) -> None:
        """未声明多模态的模型 → 默认空能力且 degraded=False（两组输入）。"""
        cap = caps.ModelCapabilityRegistry.get_capability(model_name)

        assert cap.supports_image is False and cap.supports_video is False
        assert cap.degraded is False

    def test_missing_config_marks_degraded(self, caps: Any, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        """配置断链（文件缺失）→ degraded=True，与"未声明"可区分。"""
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", tmp_path / "gone.yaml")
        monkeypatch.setattr(caps, "_LLM_MODELS_CACHE", None)

        cap = caps.ModelCapabilityRegistry.get_capability("vision-a")

        assert cap.degraded is True
        assert cap.supports_image is False

    def test_multimodal_node_not_dict_yields_empty(self, caps: Any, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
        """``multimodal`` 节点非 dict（如字符串）→ 默认空能力（degraded=False）。"""
        path = tmp_path / "odd_node.yaml"
        path.write_text("models:\n  m1:\n    multimodal: not-a-map\n", encoding="utf-8")
        monkeypatch.setattr(caps, "_LLM_YAML_PATH", path)
        monkeypatch.setattr(caps, "_LLM_MODELS_CACHE", None)

        cap = caps.ModelCapabilityRegistry.get_capability("m1")

        assert cap.degraded is False and cap.supports_image is False

    def test_is_multimodal_supported_three_axes(self, caps: Any) -> None:
        """性质：is_multimodal_supported 为三能力轴的或（image/video 真、无声明假）。"""
        assert caps.ModelCapabilityRegistry.is_multimodal_supported("vision-a") is True
        assert caps.ModelCapabilityRegistry.is_multimodal_supported("RealModelName") is True
        assert caps.ModelCapabilityRegistry.is_multimodal_supported("undeclared") is False


# ══════════════════ 适配器注册 ══════════════════


class TestAdapterRegistration:
    def test_registered_adapter_is_returned(self, caps: Any) -> None:
        """register_adapter 后 get_adapter 返回注册类的实例（同 provider 覆盖）。"""

        class _CustomAdapter(caps.MultimodalAdapter):
            async def convert(self, content: Any) -> dict[str, Any]:
                return {"custom": True}

            def get_capability(self) -> Any:
                return None

        try:
            caps.ModelCapabilityRegistry.register_adapter("custom-provider", _CustomAdapter)
            adapter = caps.ModelCapabilityRegistry.get_adapter("custom-provider")
        finally:
            caps.ModelCapabilityRegistry.ADAPTER_MAPPING.pop("custom-provider", None)

        assert isinstance(adapter, _CustomAdapter)

    def test_unknown_provider_falls_back_to_default(self, caps: Any) -> None:
        """未注册 provider → DefaultAdapter（不抛、有兜底实现）。"""
        adapter = caps.ModelCapabilityRegistry.get_adapter("never-registered")

        assert isinstance(adapter, caps.DefaultAdapter)

    def test_known_provider_uses_declared_class(self, caps: Any) -> None:
        """性质：映射表命中的 provider 返回其声明类（与未知回落态区分）。"""
        assert isinstance(caps.ModelCapabilityRegistry.get_adapter("anthropic"), caps.ClaudeVisionAdapter)
        assert isinstance(
            caps.ModelCapabilityRegistry.get_adapter("deepseek"), caps.DefaultAdapter
        )


# ══════════════════ storage.py sys.path 自举 ══════════════════


class TestStoragePathBootstrap:
    def test_inserts_shared_root_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``plugins/shared`` 不在 sys.path 时插入一次（裸名 tenant_data 可解析）。

        先逐出共享根再加载：同车道其他测试常已注入该路径，不逐出则插桩分支
        恒不执行（跨文件共跑时的覆盖率失真）。
        """
        while str(_SHARED_ROOT) in sys.path:
            sys.path.remove(str(_SHARED_ROOT))
        assert str(_SHARED_ROOT) not in sys.path

        module = self._load_storage_fresh()

        assert str(_SHARED_ROOT) in sys.path, "缺失时必须自举插入"
        assert module.DEFAULT_TENANT == "default"

    def test_does_not_duplicate_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已在 sys.path 时不重复插入（幂等：插入前后计数不增）。"""
        while str(_SHARED_ROOT) in sys.path:
            sys.path.remove(str(_SHARED_ROOT))
        sys.path.insert(0, str(_SHARED_ROOT))
        before = sys.path.count(str(_SHARED_ROOT))

        self._load_storage_fresh()

        assert sys.path.count(str(_SHARED_ROOT)) == before, "已在路径不得重复插入"

    @staticmethod
    def _load_storage_fresh() -> Any:
        """重载 storage 模块（唯一名，模块级 sys.path 自举真实执行）。"""
        for name in ("storage", "tenant_data", "mm_types"):
            sys.modules.pop(name, None)
        name = "multimodal_storage_gap_under_test"
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "storage.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


# ══════════════════ DiskFileStorage base_dir 解析 ══════════════════


class TestDiskStorageDirResolution:
    def test_explicit_base_dir_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式入参优先级最高（覆盖 env 与租户根）。"""
        module = TestStoragePathBootstrap._load_storage_fresh()
        monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(tmp_path / "envdir"))

        storage = module.DiskFileStorage(base_dir=str(tmp_path / "explicit"))

        assert storage._base_dir == tmp_path / "explicit"
        assert (tmp_path / "explicit").is_dir()

    def test_env_dir_used_when_no_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无显式入参且 env 有值 → 用 env 目录。"""
        module = TestStoragePathBootstrap._load_storage_fresh()
        monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(tmp_path / "envdir"))

        storage = module.DiskFileStorage()

        assert storage._base_dir == tmp_path / "envdir"

    @pytest.mark.parametrize("tenant_id", [None, "acme"])
    def test_tenant_root_default_when_env_absent(
        self, tenant_id: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无显式与 env → data/{tenant}/multimodal；tenant 缺省为 default。"""
        module = TestStoragePathBootstrap._load_storage_fresh()
        monkeypatch.delenv("MULTIMODAL_STORAGE_DIR", raising=False)
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))

        storage = module.DiskFileStorage(tenant_id=tenant_id)

        assert storage._base_dir == tmp_path / "data" / (tenant_id or "default") / "multimodal"
