# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""context_build 双根解析测试（ADR 2026-09-14 §2.4）。

agent 配置（config/agents/**）并入用户空间双根：用户配置层的 agents/ 优先——
用户层存在同路径（同 agent_id）文件即接管生效，factory 同名文件完全不参与
（文件级整体替换，不合并）；用户层无此文件回落 factory。

env 隔离：AGENTOS_USER_ROOT 钉到 tmp（经 plugins/shared/user_space.py，
与 Rust 侧 dirs::data_dir() 同一解析契约）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "context_build_plugin_dualroot_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """用户根与 factory 根都钉到 tmp，返回 (user_root, factory_root)。"""
    user_root = tmp_path / "user-root"
    factory = tmp_path / "factory"
    user_root.mkdir()
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    return user_root, factory


def _write_agent(base: Path, agent_id: str, text: str) -> None:
    agents = base / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{agent_id}.yaml").write_text(text, encoding="utf-8")


class TestDualRootAgentConfig:
    def test_user_layer_takeover_wins(self, isolated_roots: tuple[Path, Path]) -> None:
        """用户层接管：同 agent_id 用户层文件生效，factory 不参与（整体替换）。"""
        user_root, factory = isolated_roots
        _write_agent(factory, "l2coder", "display_name: factory执行者\nlevel: L2\n")
        _write_agent(user_root / "config", "l2coder", "display_name: 用户版执行者\nlevel: L2\n")

        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert res.state_updates["context.agent_name"] == "用户版执行者", (
            f"用户层接管后应读用户层文件，实际: {res.state_updates}"
        )

    def test_factory_fallback_when_user_absent(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """用户层无此文件 → 回落 factory（未接管路径随出厂更新自动生效）。"""
        _user_root, factory = isolated_roots
        _write_agent(factory, "l2coder", "level: L2\n")

        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert res.state_updates["context.is_project"] is False

    def test_user_dir_without_file_still_reads_factory(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """用户层 agents/ 目录存在但无此文件 ≠ 接管（按文件判定，非目录）。"""
        user_root, factory = isolated_roots
        (user_root / "config" / "agents").mkdir(parents=True)
        (user_root / "config" / "agents" / "other.yaml").write_text(
            "level: L3\n", encoding="utf-8"
        )
        _write_agent(factory, "l2coder", "level: L2\n")

        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert res.state_updates["context.is_project"] is False

    def test_both_absent_runs_defaults(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """两侧都没有该 agent → 默认配置运行（L1，不报错）。"""
        _user_root, _factory = isolated_roots
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "no-such-agent"})))
        assert res.state_updates["context.is_project"] is True
