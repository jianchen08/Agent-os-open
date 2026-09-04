# @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm/_config_models.py 配置注入桥行为测试。

覆盖：
1. ``${VAR}`` 占位符解析三级顺序：进程环境 → 项目根 .env 兜底 → 未定义
   保持原样；``your-`` 开头示例值视为未配置；递归 dict/list；
2. .env 读取：注释/空行/引号剥离、mtime 缓存命中与失效、无根/无文件降级；
3. ModelConfigLoaderShim：llm/embedding 命名空间读取（缺失/非 dict 降级空表）、
   大小写不敏感模型查找、provider/keys 回退链、tier 解析、defaults 合并
   （超时三参数模型级覆盖 defaults 级）、thinking_strength_params 仅在
   配置时携带。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent


@pytest.fixture()
def mod() -> Any:
    """按显式路径加载 _config_models（裸名防劫持）。"""
    spec = importlib.util.spec_from_file_location(
        "llm_config_models_bridge_test", str(_DIR / "_config_models.py")
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_config_models_bridge_test"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _clean_state(mod: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """隔离模块级单例：配置存储、.env 缓存、环境变量。"""
    monkeypatch.setattr(mod, "_config", {})
    monkeypatch.setattr(mod, "_env_cache", None)


class TestExpandEnvVars:
    def test_env_var_first_then_dotenv_fallback(self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("BRIDGE_T1", "from-env")
        (tmp_path / ".env").write_text("BRIDGE_T2=from-file\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_resolve_project_root", lambda: tmp_path)

        assert mod._expand_env_vars("${BRIDGE_T1}") == "from-env"
        assert mod._expand_env_vars("${BRIDGE_T2}") == "from-file"
        # 都没有 → 原样保留
        assert mod._expand_env_vars("${BRIDGE_NOPE}") == "${BRIDGE_NOPE}"

    def test_example_value_and_prefix_forms(self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("BRIDGE_EX", raising=False)
        (tmp_path / ".env").write_text('BRIDGE_EX="your-key-demo"\n', encoding="utf-8")
        monkeypatch.setattr(mod, "_resolve_project_root", lambda: tmp_path)
        # 示例值视为未配置
        assert mod._expand_env_vars("${BRIDGE_EX}") == "${BRIDGE_EX}"

        monkeypatch.setenv("BRIDGE_P", "v")
        # 非整串占位符走 expandvars 局部展开
        assert mod._expand_env_vars("pre-${BRIDGE_P}-post") == "pre-v-post"
        # 非字符串类型原样
        assert mod._expand_env_vars(42) == 42
        assert mod._expand_env_vars(None) is None

    def test_recursive_containers(self, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K", "k1")
        got = mod._expand_env_vars(
            {"models": {"m": {"api_key": "${BRIDGE_K}"}}, "list": ["${BRIDGE_K}", "x"]}
        )
        assert got["models"]["m"]["api_key"] == "k1"
        assert got["list"] == ["k1", "x"]


class TestEnvFileVars:
    def test_parsing_and_cache(self, mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "# 注释\n\nA=1\nB = \"quoted\"\nINVALIDLINE\nC=2\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_resolve_project_root", lambda: tmp_path)
        got = mod._env_file_vars()
        assert got == {"A": "1", "B": "quoted", "C": "2"}
        # mtime 未变 → 缓存命中（同一 dict 对象）
        assert mod._env_file_vars() is got

        env.write_text("A=changed\n", encoding="utf-8")
        assert mod._env_file_vars() == {"A": "changed"}

    def test_no_root_or_no_file(self, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_resolve_project_root", lambda: None)
        assert mod._env_file_vars() == {}

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            # 根存在但无 .env 文件 → 空表
            monkeypatch.setattr(mod, "_resolve_project_root", lambda: Path(td))
            assert mod._env_file_vars() == {}


class TestSetGetConfig:
    def test_set_config_expands_and_get_roundtrip(self, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_SET", "s1")
        mod.set_config({"llm": {"models": {"m": {"api_key": "${BRIDGE_SET}"}}}})
        assert mod.get_config()["llm"]["models"]["m"]["api_key"] == "s1"

    def test_set_config_non_dict_resets_to_empty(self, mod: Any) -> None:
        mod.set_config("not-a-dict")  # type: ignore[arg-type]
        assert mod.get_config() == {}


class TestLoaderShim:
    def _shim(self, mod: Any, llm: dict[str, Any]) -> Any:
        mod.set_config({"llm": llm, "embedding": {"models": {"e1": {}}}})
        return mod.get_model_config_loader()

    def test_llm_and_embedding_namespaces(self, mod: Any) -> None:
        shim = self._shim(mod, {"models": {"m1": {"provider": "p"}}})
        assert shim._load_llm_data() == {"models": {"m1": {"provider": "p"}}}
        assert shim._load_embedding_data() == {"models": {"e1": {}}}

    def test_missing_or_malformed_namespace_degrades(self, mod: Any) -> None:
        mod.set_config({})
        shim = mod.get_model_config_loader()
        assert shim._load_llm_data() == {}
        assert shim._load_embedding_data() == {}
        mod.set_config({"llm": "corrupt"})
        assert mod.get_model_config_loader()._load_llm_data() == {}

    def test_case_insensitive_model_lookup(self, mod: Any) -> None:
        shim = self._shim(mod, {"models": {"DeepSeek-Chat": {"provider": "ds"}}})
        assert shim.get_model_config("deepseek-chat") == {"provider": "ds"}
        assert shim.get_model_config("unknown") is None
        # 空 key 不命中（None 而非异常）
        assert shim.get_model_config("") is None

    def test_tier_and_default_chat(self, mod: Any) -> None:
        shim = self._shim(
            mod,
            {"defaults": {"tiers": {"large": "m-big"}, "chat": "m-default"}},
        )
        assert shim.resolve_tier("large") == "m-big"
        assert shim.resolve_tier("nope") == ""
        assert shim.resolve_tier("") == ""
        assert shim.get_default_chat_model() == "m-default"

    def test_get_llm_core_config_merge_chain(self, mod: Any) -> None:
        shim = self._shim(
            mod,
            {
                "models": {
                    "m1": {
                        "provider": "prov",
                        "api_key": "model-key",
                        "api_base": "https://m.example",
                        "context_window": 8192,
                        "call_timeout": 120,
                    }
                },
                "providers": {
                    "prov": {"api_base": "https://p.example", "keys": [{"api_key": "pool-key"}]}
                },
                "defaults": {
                    "call_timeout": 300,
                    "first_token_timeout": 45,
                    "stream_idle_timeout": 500,
                },
            },
        )
        got = shim.get_llm_core_config("m1")
        assert got is not None
        # 模型级 api_key/base 优先于 provider 级
        assert got["api_key"] == "model-key"
        assert got["api_base"] == "https://m.example"
        # 模型级超时覆盖 defaults 级；未覆盖的用 defaults
        assert got["call_timeout"] == 120
        assert got["first_token_timeout"] == 45
        assert got["stream_idle_timeout"] == 500
        assert got["model_name"] == "m1"
        assert "thinking_strength_params" not in got

    def test_get_llm_core_config_key_pool_fallback(self, mod: Any) -> None:
        shim = self._shim(
            mod,
            {
                "models": {"m2": {"provider": "prov2"}},
                "providers": {"prov2": {"api_key": "", "keys": [{"api_key": "k0"}, {"api_key": "k1"}]}},
            },
        )
        got = shim.get_llm_core_config("m2")
        assert got is not None
        # 模型无 key、provider.api_key 空 → keys[0] 回退
        assert got["api_key"] == "k0"
        # 模型条目未写 default_params → 空 dict（不发明 0.7/4096 兜底：
        # 参数缺省由 llm.complete_stream 按 llm.yaml 回填，缺即不发）
        assert got["default_params"] == {}

    def test_get_llm_core_config_unknown_model(self, mod: Any) -> None:
        shim = self._shim(mod, {"models": {}})
        assert shim.get_llm_core_config("ghost") is None

    def test_thinking_strength_params_passthrough(self, mod: Any) -> None:
        shim = self._shim(
            mod,
            {
                "models": {
                    "m3": {
                        "provider": "p3",
                        "thinking_strength_params": {"high": {"enabled": True}},
                    }
                }
            },
        )
        got = shim.get_llm_core_config("m3")
        assert got is not None
        assert got["thinking_strength_params"] == {"high": {"enabled": True}}

    def test_provider_thinking_strength_params_passthrough(self, mod: Any) -> None:
        """providers.<name>.thinking_strength_params → provider_thinking_strength_params
        桥接透出（厂商级映射）；未配置的 provider 键省略。"""
        provider_mapping = {
            "high": {"thinking": {"type": "enabled"}},
            "low": {"thinking": {"type": "disabled"}},
        }
        shim = self._shim(
            mod,
            {
                "models": {"glm-x": {"provider": "zhipu"}},
                "providers": {
                    "zhipu": {"type": "zai", "thinking_strength_params": provider_mapping}
                },
            },
        )
        got = shim.get_llm_core_config("glm-x")
        assert got is not None
        assert got["provider_thinking_strength_params"] == provider_mapping

        shim2 = self._shim(mod, {"models": {"m": {"provider": "plain"}}})
        got2 = shim2.get_llm_core_config("m")
        assert got2 is not None
        assert "provider_thinking_strength_params" not in got2
