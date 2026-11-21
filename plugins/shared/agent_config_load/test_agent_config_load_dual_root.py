# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""agent_config_load 双根解析测试（ADR 2026-09-14 §2.4）。

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
import tempfile
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_dualroot_test"
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
        plugin = mod.AgentConfigLoadPlugin(config={})
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
        plugin = mod.AgentConfigLoadPlugin(config={})
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
        plugin = mod.AgentConfigLoadPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert res.state_updates["context.is_project"] is False

    def test_both_absent_runs_defaults(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """两侧都没有该 agent → 默认配置运行（L1，不报错）。"""
        _user_root, _factory = isolated_roots
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "no-such-agent"})))
        assert res.state_updates["context.is_project"] is True


class TestRelativeEnvResolution:
    """相对 AGENTOS_CONFIG_ROOT 的内核解析基还原（2026-11-16 R6 返修）。

    真内核实测根因：内核对显式相对 config root 不规范化，sidecar（cwd=插件
    目录）原样继承 → Path("config")/agents 落到插件目录下幻影路径 → agent
    yaml 双根全空 → 工具面/模型档静默回退（tb_eval_agent 绑定会话跑成无工具
    纯文本）。修复 = 按 instance.yaml 启动契约标记从 cwd/插件目录祖先还原。
    """

    def test_relative_config_root_resolves_via_cwd_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """相对 env + cwd 锚（tmp 工作区含 instance.yaml）→ 正确装载 tmp agent。"""
        ws = tmp_path / "ws"
        cfg = ws / "config"
        (cfg / "kernel").mkdir(parents=True)
        (cfg / "kernel" / "instance.yaml").write_text(
            "schema: 1\ntier1_root: ../../plugins/shared\n"
            "tier2_root: ../../plugins/shared\nshared_root: ../../plugins/shared\n",
            encoding="utf-8",
        )
        agents = cfg / "agents" / "system"
        agents.mkdir(parents=True)
        (agents / "tb_probe_agent.yaml").write_text(
            "config_id: tb_probe_agent\nmodel_tier: small\ntool_ids:\n- bash\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", "config")  # 相对（宿主 repro 形态）
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "no-such-user-root"))
        monkeypatch.chdir(ws)  # sidecar 视角：cwd 锚含 instance.yaml 标记

        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        cfg_loaded = plugin._load_agent_config("tb_probe_agent")
        assert cfg_loaded, "相对 config root 必须经内核解析基还原装载（静默回退=bug）"
        assert cfg_loaded.get("model_tier") == "small"
        assert cfg_loaded.get("tool_ids") == ["bash"]

    def test_absolute_config_root_unchanged(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """绝对 env（既有形态）行为不回归：tmp factory 直接命中。"""
        _user_root, factory = isolated_roots
        _write_agent(factory, "abs_probe", "model_tier: large\n")
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        cfg_loaded = plugin._load_agent_config("abs_probe")
        assert cfg_loaded.get("model_tier") == "large"

    def test_unresolvable_relative_falls_back_to_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """还原失败（无 instance.yaml 标记）→ 保留原值按 cwd 解析（不劣于修复前）。"""
        mod = _load_plugin_module()
        helper = mod._resolve_kernel_relative_path
        monkeypatch.chdir(Path(tempfile.mkdtemp()))  # 孤立 tmp：祖先链无标记
        raw = Path("nonexistent-cfg")
        resolved = helper(raw)
        assert resolved == raw  # 原样返回，行为与修复前一致

    def test_absolute_passthrough(self, tmp_path: Path) -> None:
        """绝对路径直通（不做标记探测）。"""
        mod = _load_plugin_module()
        absolute = tmp_path / "anywhere" / "config"
        assert mod._resolve_kernel_relative_path(absolute) == absolute


def test_resolve_project_roots_service_stub(monkeypatch):
    """project.list_paths 服务消费（T6a）：命中返回根列表，未命中/服务不可用降级空表。"""
    mod = _load_plugin_module()

    async def _caller(method, args, timeout=None):
        assert method == "list_paths"
        return {"paths": {"proj-1": "/tmp/proj-one"}}

    plugin = mod.AgentConfigLoadPlugin(config={})
    plugin.set_project_service_caller(_caller)
    assert _run(plugin._resolve_project_roots("proj-1")) == ["/tmp/proj-one"]
    assert _run(plugin._resolve_project_roots("no-such")) == []

    plugin.set_project_service_caller(None)
    assert _run(plugin._resolve_project_roots("proj-1")) == [], "未注入 = 降级不注入"


class TestModePackRootKey:
    """模式包注册表命中的 agent 携带 context.agent_pack_root（包内相对
    形态 {{path:./...}} 的包根锚点）；用户/factory config 命中不写该键
    （缺席语义）。"""

    def _write_mode_package(self, user_root: Path, mode: str, stem: str, text: str) -> Path:
        pkg = user_root / "plugins" / "modes" / f"mode_{mode}"
        (pkg / "agents").mkdir(parents=True, exist_ok=True)
        (pkg / "plugin.json").write_text("{}", encoding="utf-8")
        (pkg / "agents" / f"{stem}.yaml").write_text(text, encoding="utf-8")
        return pkg

    def test_mode_registry_hit_writes_pack_root(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """模式包注册表命中 → state_updates 含包根键且值指向该包目录。"""
        user_root, _factory = isolated_roots
        pkg = self._write_mode_package(
            user_root, "ztest", "packagent", "display_name: 包内执行者\nlevel: L3\n"
        )

        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "mode_ztest/packagent"})))
        assert Path(res.state_updates["context.agent_pack_root"]) == pkg
        assert res.state_updates["context.agent_name"] == "包内执行者", (
            "包根键与配置内容同源于包内 yaml"
        )

    def test_factory_config_hit_no_pack_root(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """factory config 命中 → 包根键缺席（保持缺席语义）。"""
        _user_root, factory = isolated_roots
        _write_agent(factory, "l2coder", "display_name: factory执行者\n")

        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert "context.agent_pack_root" not in res.state_updates

    def test_user_config_shadowing_mode_key_no_pack_root(
        self, isolated_roots: tuple[Path, Path]
    ) -> None:
        """agent 键为模式键形态但被用户 config 层接管 → 不写包根键
        （键只随模式包注册表命中出现，双根优先级不被包根键污染）。"""
        user_root, _factory = isolated_roots
        self._write_mode_package(user_root, "ztest", "packagent", "display_name: 包内执行者\n")
        user_agents = user_root / "config" / "agents" / "mode_ztest"
        user_agents.mkdir(parents=True)
        (user_agents / "packagent.yaml").write_text(
            "display_name: 用户接管\n", encoding="utf-8"
        )

        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        res = _run(plugin.execute(_ctx({"agent.id": "mode_ztest/packagent"})))
        assert res.state_updates["context.agent_name"] == "用户接管"
        assert "context.agent_pack_root" not in res.state_updates
