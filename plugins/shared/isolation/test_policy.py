# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation 插件（隔离策略加载器）单元测试。

覆盖（对齐 SDK agentos_plugin_sdk.isolation_policy，三插件共享单一真值源）：
1. default_policy_path：定位仓库根 config/plugins/isolation/isolation_policy.yaml
   （AGENTOS_CONFIG_ROOT 优先，回退祖先目录查找，不硬编码父目录层数）
2. IsolationPolicyLoader：config_center 不可用时文件回退加载真实策略
   （bash 应命中 tools 精确匹配 → isolated/command_in_container）
3. 路径找不到时降级默认策略（不 panic）
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentos_plugin_sdk.isolation_policy import (
    DEFAULT_POLICY_PATH,
    IsolationPolicyLoader,
    ToolIsolationPolicy,
    default_policy_path,
)

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/

# 仓库根 = 插件目录向上 4 层（plugins/shared/isolation → 仓库根）
_REPO_ROOT = _PLUGIN_DIR.parents[2]
_POLICY_FILE = _REPO_ROOT / "config" / "plugins" / "isolation" / "isolation_policy.yaml"


class TestDefaultPolicyPath:
    def test_resolves_to_repo_root_policy_file(self) -> None:
        """定位函数返回的路径存在且指向仓库根 config/plugins/isolation/isolation_policy.yaml。"""
        path = default_policy_path()
        assert path.exists(), f"策略文件不存在: {path}"
        assert path == _POLICY_FILE, f"期望 {_POLICY_FILE}，实际 {path}"

    def test_module_default_constant_matches_resolver(self) -> None:
        """DEFAULT_POLICY_PATH 与 default_policy_path() 锚点解析一致（loader 默认路径来源）。"""
        assert default_policy_path() == DEFAULT_POLICY_PATH

    def test_ancestor_walk_not_hardcoded(self) -> None:
        """路径通过祖先目录查找得到（不依赖固定父目录层数）。"""
        path = default_policy_path()
        # 仓库根是包含 config/plugins/isolation/isolation_policy.yaml 的祖先目录
        assert _REPO_ROOT in path.parents
        assert path.parent == _REPO_ROOT / "config" / "plugins" / "isolation"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 指向的配置根优先于祖先目录推导。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "config"))
        path = default_policy_path()
        assert path == _POLICY_FILE
        assert path.exists()

    def test_env_override_missing_file_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 指向的路径不存在时回退祖先目录查找。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "nonexistent-config-root"))
        path = default_policy_path()
        assert path == _POLICY_FILE
        assert path.exists()

    def test_env_override_alt_root_beats_ancestor_probe(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """锚点解析：env 根含自有 isolation/isolation_policy.yaml 时，即使祖先探测
        也能命中仓库根策略，仍以 env 根为准（部署布局覆盖仓库布局）。"""
        alt_root = tmp_path / "alt-config"
        (alt_root / "plugins" / "isolation").mkdir(parents=True)
        alt_policy = alt_root / "plugins" / "isolation" / "isolation_policy.yaml"
        alt_policy.write_text("default: {isolation: non_isolated}\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(alt_root))
        assert default_policy_path() == alt_policy

    def test_no_ancestor_found_returns_fallback_without_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """任何祖先目录都找不到时返回推导路径（加载失败走默认策略降级，不 panic）。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "nonexistent-config-root"))
        # 模拟模块位于无 config/plugins/isolation 祖先的目录：把 SDK 策略模块文件复制到
        # 临时目录重新加载（isolation_types 经包名导入，不依赖同目录文件）
        with tempfile.TemporaryDirectory() as tmp:
            fake_dir = Path(tmp) / "somewhere" / "else"
            fake_dir.mkdir(parents=True)
            policy_module = sys.modules["agentos_plugin_sdk.isolation_policy"]
            assert policy_module.__file__ is not None, "SDK isolation_policy 必须来自真实文件"
            sdk_policy_file = Path(policy_module.__file__)
            (fake_dir / "isolation_policy.py").write_text(
                sdk_policy_file.read_text(encoding="utf-8"), encoding="utf-8"
            )

            mod_name = "isolation_policy_fallback_test"
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            spec = importlib.util.spec_from_file_location(mod_name, fake_dir / "isolation_policy.py")
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            path = module.default_policy_path()
            # 不 panic，返回推导路径（不存在也允许——调用方降级默认策略）
            assert isinstance(path, Path)
            assert not path.exists()


class TestPolicyLoaderFileFallback:
    def test_loads_real_policy_from_repo_root(self) -> None:
        """config_center 不可用时文件回退加载真实策略，bash 命中 tools 精确匹配。

        工具名用现行命令面单工具 ``bash``（ADR 2026-11-07 T2 四单词重构，
        bash_execute 已退役出 isolation_policy.yaml 精确匹配键）。"""
        loader = IsolationPolicyLoader()
        policy = loader.resolve("bash")
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

    @pytest.fixture
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

    def test_policy_dataclass_defaults(self) -> None:
        """ToolIsolationPolicy 默认值 = 容器隔离兜底。"""
        p = ToolIsolationPolicy()
        assert p.isolation.value == "isolated"
        assert p.execution == "command_in_container"
        assert p.checkpoint is False
        assert p.approval is False

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
        assert p.checkpoint is False
        assert p.approval is False

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


class TestWatcherAvailabilityGuard:
    """watcher 注册的可用性守卫（件2：config_center 0.2 起结构性缺失）。

    dev/装机两布局的 sidecar 进程均无 config 包（0.1 源码已归档
    reference/0.1_src）：守卫必须显式降级（warning 留痕、策略照常文件加载），
    不得每实例必败 import；模块在位时注册路径照常工作，watch 注册失败同样
    告警不阻断构造。
    """

    _LOGGER = "agentos_plugin_sdk.isolation_policy"

    def test_config_center_absent_degrades_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """模块缺失（dev/装机共同形态）：构造不炸、文件回退策略可用、降级带 warning。"""
        monkeypatch.setitem(sys.modules, "config.config_center", None)  # import 即失败
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            loader = IsolationPolicyLoader(config_path=str(_POLICY_FILE))
        assert (
            loader.resolve("bash").isolation.value == "isolated"
        ), "config_center 缺席时文件回退加载必须照常工作"
        assert any(
            "config_center 不可用" in rec.getMessage() for rec in caplog.records
        ), "降级路径必须显式 warning 留痕"

    def test_config_center_present_registers_watcher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模块在位：watch 注册被执行恰一次，前缀锚定 isolation/ 命名空间。"""
        watched: list[tuple[str, Any]] = []

        class FakeCenter:
            def watch(self, prefix: str, callback: Any) -> None:
                watched.append((prefix, callback))

        fake = types.ModuleType("config.config_center")
        fake.get_config_center = FakeCenter  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "config", types.ModuleType("config"))
        monkeypatch.setitem(sys.modules, "config.config_center", fake)
        IsolationPolicyLoader(config_path=str(_POLICY_FILE))
        assert len(watched) == 1, "config_center 在位时必须注册 watcher（恰一次）"
        assert watched[0][0] == "isolation/", "watch 前缀必须锚定 isolation/ 命名空间"

    def test_watch_registration_failure_degrades_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """模块在位但 watch 注册抛错：告警留痕、构造不炸、策略照常可用。"""

        class BrokenCenter:
            def watch(self, prefix: str, callback: Any) -> None:
                raise RuntimeError("registry down")

        fake = types.ModuleType("config.config_center")
        fake.get_config_center = BrokenCenter  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "config", types.ModuleType("config"))
        monkeypatch.setitem(sys.modules, "config.config_center", fake)
        with caplog.at_level(logging.WARNING, logger=self._LOGGER):
            loader = IsolationPolicyLoader(config_path=str(_POLICY_FILE))
        assert loader.resolve("bash").isolation.value == "isolated"
        assert any(
            "注册 watcher 失败" in rec.getMessage() for rec in caplog.records
        ), "watch 注册失败必须告警留痕"


class TestPolicyToolKeysDeclared:
    """机械闸：isolation_policy.yaml 工具策略键 ⊆ 全插件 capabilities.tools 声明名并集。

    工具级规则靠工具名精确匹配生效（tools > categories > default），键指向
    不存在的工具 = 策略静默失效落 default。扫描真实插件目录
    （plugins/**/plugin.json，含 mode_* 模式包）对账，禁 mock 声明面。
    """

    @staticmethod
    def _declared_tool_names() -> set[str]:
        names: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(_REPO_ROOT / "plugins"):
            dirnames[:] = [
                d for d in dirnames if d not in ("node_modules", ".venv", "__pycache__")
            ]
            if "plugin.json" not in filenames:
                continue
            try:
                data = json.loads((Path(dirpath) / "plugin.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # 非法清单不参与对账（装载面由 G2 闸执法）
            for tool in (data.get("capabilities") or {}).get("tools") or []:
                name = tool.get("name")
                if isinstance(name, str):
                    names.add(name)
        return names

    def test_tools_keys_all_declared(self) -> None:
        """tools 节每个键都能在真实插件声明面找到同名工具（含 mode_* 包）。"""
        policy = yaml.safe_load(_POLICY_FILE.read_text(encoding="utf-8"))
        tool_keys = set((policy.get("tools") or {}).keys())
        declared = self._declared_tool_names()
        assert declared, "插件目录扫描不得为空（真实目录扫描，禁 mock）"
        assert "eval_query" in declared, "扫描须覆盖 mode_* 模式包声明（本闸的含包要求）"
        undeclared = tool_keys - declared
        assert not undeclared, (
            f"isolation_policy.yaml tools 键指向未声明工具（策略静默失效）: {sorted(undeclared)}"
        )
