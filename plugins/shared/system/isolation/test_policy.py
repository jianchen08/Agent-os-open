# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""isolation 插件（隔离策略加载器）单元测试。

覆盖（对齐 plugins/shared/system/isolation/policy.py）：
1. _default_policy_path：定位仓库根 config/isolation/isolation_policy.yaml
   （AGENTOS_CONFIG_ROOT 优先，回退祖先目录查找，不硬编码父目录层数）
2. IsolationPolicyLoader：config_center 不可用时文件回退加载真实策略
   （bash_execute 应命中 tools 精确匹配 → isolated/command_in_container）
3. 路径找不到时降级默认策略（不 panic）

测试不依赖真实内核——直接加载 policy.py 与同目录 isolation_types.py。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 仓库根 = 插件目录向上 4 层（plugins/shared/system/isolation → 仓库根）
_REPO_ROOT = _PLUGIN_DIR.parents[3]
_POLICY_FILE = _REPO_ROOT / "config" / "isolation" / "isolation_policy.yaml"


def _load_policy() -> Any:
    mod_name = "isolation_policy_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "policy.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_policy()
_default_policy_path = _MOD._default_policy_path
IsolationPolicyLoader = _MOD.IsolationPolicyLoader


class TestDefaultPolicyPath:
    def test_resolves_to_repo_root_policy_file(self) -> None:
        """定位函数返回的路径存在且指向仓库根 config/isolation/isolation_policy.yaml。"""
        path = _default_policy_path()
        assert path.exists(), f"策略文件不存在: {path}"
        assert path == _POLICY_FILE, f"期望 {_POLICY_FILE}，实际 {path}"

    def test_ancestor_walk_not_hardcoded(self) -> None:
        """路径通过祖先目录查找得到（不依赖固定父目录层数）。"""
        path = _default_policy_path()
        # 仓库根是包含 config/isolation/isolation_policy.yaml 的祖先目录
        assert _REPO_ROOT in path.parents
        assert path.parent == _REPO_ROOT / "config" / "isolation"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 指向的配置根优先于祖先目录推导。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "config"))
        path = _default_policy_path()
        assert path == _POLICY_FILE
        assert path.exists()

    def test_env_override_missing_file_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 指向的路径不存在时回退祖先目录查找。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "nonexistent-config-root"))
        path = _default_policy_path()
        assert path == _POLICY_FILE
        assert path.exists()

    def test_no_ancestor_found_returns_fallback_without_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """任何祖先目录都找不到时返回推导路径（加载失败走默认策略降级，不 panic）。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "nonexistent-config-root"))
        # 模拟 policy.py 位于无 config/isolation 祖先的目录：用临时目录重新加载模块
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_plugin_dir = Path(tmp) / "plugins" / "shared" / "system" / "isolation"
            fake_plugin_dir.mkdir(parents=True)
            # 复制 policy.py 与 isolation_types.py 到临时目录（保持相对导入可用）
            for src in (_PLUGIN_DIR / "policy.py", _PLUGIN_DIR / "isolation_types.py"):
                (fake_plugin_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

            mod_name = "isolation_policy_fallback_test"
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            spec = importlib.util.spec_from_file_location(mod_name, fake_plugin_dir / "policy.py")
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            path = module._default_policy_path()
            # 不 panic，返回推导路径（不存在也允许——调用方降级默认策略）
            assert isinstance(path, Path)
            assert not path.exists()


class TestPolicyLoaderFileFallback:
    def test_loads_real_policy_from_repo_root(self) -> None:
        """config_center 不可用时文件回退加载真实策略，bash_execute 命中 tools 精确匹配。"""
        loader = IsolationPolicyLoader()
        policy = loader.resolve("bash_execute")
        assert policy.isolation.value == "isolated"
        assert policy.execution == "command_in_container"

    def test_default_policy_for_unlisted_tool(self) -> None:
        """未配置工具走 default（non_isolated/host_direct）。"""
        loader = IsolationPolicyLoader()
        policy = loader.resolve("some_unlisted_tool")
        assert policy.isolation.value == "non_isolated"
        assert policy.execution == "host_direct"

    def test_missing_config_path_degrades_to_default(self) -> None:
        """显式传入不存在的路径时降级默认策略（容器隔离兜底），不抛异常。"""
        loader = IsolationPolicyLoader(config_path=str(_PLUGIN_DIR / "no_such_policy.yaml"))
        policy = loader.resolve("bash_execute")
        assert policy.isolation.value == "isolated"


class TestParseAndPriority:
    """_parse_policy 字段解析与 tools > categories > default 决策优先级。"""

    @pytest.fixture()
    def tmp_yaml(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "policy.yaml"
        cfg.write_text(
            """
default:
  isolation: non_isolated
  execution: host_direct
tools:
  exact_tool:
    isolation: isolated
    network: disabled
    checkpoint: true
    approval: true
    disk_quota: 512m
categories:
  risky_cat:
    isolation: isolated
    execution: command_in_container
""",
            encoding="utf-8",
        )
        return cfg

    def test_full_field_parse(self, tmp_yaml: Path) -> None:
        loader = IsolationPolicyLoader(config_path=str(tmp_yaml))
        p = loader.resolve("exact_tool")
        assert p.isolation.value == "isolated"
        assert p.network == "disabled"
        assert p.checkpoint is True
        assert p.approval is True
        assert p.disk_quota == "512m"

    def test_priority_tool_over_category_and_default(self, tmp_yaml: Path) -> None:
        loader = IsolationPolicyLoader(config_path=str(tmp_yaml))
        # 工具名优先于分类
        assert loader.resolve("exact_tool").isolation.value == "isolated"
        # 分类命中优先于默认
        cat = loader.resolve("other_tool", category="risky_cat")
        assert cat.isolation.value == "isolated"
        assert cat.execution == "command_in_container"
        # 都不命中走默认；未知分类不误伤
        assert loader.resolve("other_tool").isolation.value == "non_isolated"
        assert loader.resolve("other_tool", category="unknown_cat").execution == "host_direct"

    def test_empty_entry_gets_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / "policy.yaml"
        cfg.write_text("tools:\n  blank_tool:\ndefault:\n", encoding="utf-8")
        loader = IsolationPolicyLoader(config_path=str(cfg))
        p = loader.resolve("blank_tool")
        assert p.isolation.value == "isolated"
        assert p.execution == "command_in_container"
        assert p.checkpoint is False and p.approval is False

    def test_name_lists(self, tmp_yaml: Path) -> None:
        loader = IsolationPolicyLoader(config_path=str(tmp_yaml))
        assert loader.get_tool_names() == ["exact_tool"]
        assert loader.get_category_names() == ["risky_cat"]


class TestHotReload:
    def test_reload_with_new_path(self, tmp_path: Path) -> None:
        v1 = tmp_path / "v1.yaml"
        v2 = tmp_path / "v2.yaml"
        v1.write_text("default: {isolation: non_isolated}\n", encoding="utf-8")
        v2.write_text("default: {isolation: isolated}\n", encoding="utf-8")
        loader = IsolationPolicyLoader(config_path=str(v1))
        assert loader.resolve("x").isolation.value == "non_isolated"
        loader.reload(config_path=str(v2))
        assert loader.resolve("x").isolation.value == "isolated"

    def test_on_config_changed_reloads_matching_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "isolation_policy.yaml"
        cfg.write_text("default: {isolation: non_isolated}\n", encoding="utf-8")
        loader = IsolationPolicyLoader(config_path=str(cfg))
        assert loader.resolve("x").isolation.value == "non_isolated"
        cfg.write_text("default: {isolation: isolated}\n", encoding="utf-8")
        # 与 isolation_policy 无关的文件不触发
        loader._on_config_changed("modified", "other_file.yaml")
        assert loader.resolve("x").isolation.value == "non_isolated"
        # 命中 isolation_policy 的变更触发 reload
        loader._on_config_changed("modified", "isolation/isolation_policy.yaml")
        assert loader.resolve("x").isolation.value == "isolated"
