# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P3 mode_coding试点 | @ci: python-coverage
"""context_build 模式命名空间 agent 键装配测试（§3.3 两级解析第二级）。

agent.id = `mode_X/<stem>` 时：系统注册表（config 根 agents/）未命中 →
模式包目录注册表（包内 agents/<stem>.yaml，mode_keys 双根）。断装配产物
（agent_name/tool_ids/level 来自模式包 yaml），不 mock 文件面；用户副本
优先经 AGENTOS_USER_ROOT 钉 tmp 验证，出厂回落走真实 mode_coding 种子。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "context_build")
import plugin as context_build_mod  # noqa: E402

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _ctx(state: dict):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


def _run(state: dict) -> dict:
    cb = context_build_mod.ContextBuildPlugin(config={})
    import asyncio

    return asyncio.run(cb.execute(_ctx(state))).state_updates


@pytest.fixture()
def isolated_roots(tmp_path: Path, monkeypatch):
    """用户根与 factory 根都钉到 tmp。"""
    user_root = tmp_path / "user-root"
    factory = tmp_path / "factory"
    user_root.mkdir()
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    return user_root, factory


def _seed_user_package(user_root: Path, stem: str, display_name: str) -> Path:
    """在用户插件根造一个 mode_coding 包（含包标记与一个 agent 文件）。"""
    pkg = user_root / "plugins" / "modes" / "mode_coding"
    (pkg / "agents").mkdir(parents=True, exist_ok=True)
    (pkg / "plugin.json").write_text("{}", encoding="utf-8")
    yaml_path = pkg / "agents" / f"{stem}.yaml"
    yaml_path.write_text(
        f"display_name: {display_name}\nlevel: L3\ntool_ids: [file_read]\n",
        encoding="utf-8",
    )
    return yaml_path


class TestModeAgentKeyAssembly:
    def test_user_package_agent_assembled(self, isolated_roots) -> None:
        """mode 键经用户模式包解析：agent_name/tool_ids 装配自包内 yaml。"""
        user_root, _factory = isolated_roots
        _seed_user_package(user_root, "code_writer", "用户版编码执行者")

        updates = _run({"agent.id": "mode_coding/code_writer"})
        assert updates["context.agent_name"] == "用户版编码执行者"
        assert updates["tool_ids"] == ["file_read"]
        # level 装配自模式包 yaml：L3 → 非项目级
        assert updates["context.is_project"] is False

    def test_system_registry_takes_precedence_over_mode_package(
        self, isolated_roots
    ) -> None:
        """两级解析序：系统注册表命中优先，模式包同 stem 文件不参与。"""
        user_root, factory = isolated_roots
        _seed_user_package(user_root, "l2coder", "用户模式包版")
        agents = factory / "agents"
        agents.mkdir(parents=True)
        (agents / "l2coder.yaml").write_text(
            "display_name: 系统版执行者\nlevel: L2\n", encoding="utf-8"
        )

        updates = _run({"agent.id": "l2coder"})
        assert updates["context.agent_name"] == "系统版执行者"

    def test_mode_key_miss_runs_defaults(self, isolated_roots) -> None:
        """两级都未命中 → 默认配置运行（L1 主路径，不报错）。"""
        updates = _run({"agent.id": "mode_coding/no_such_stem"})
        assert updates["context.is_project"] is True

    def test_factory_package_agent_resolved_from_repo_seed(
        self, isolated_roots
    ) -> None:
        """出厂种子回落：用户无副本 → 真实 mode_coding 包内 stem 命中并装配。"""
        updates = _run({"agent.id": "mode_coding/code_writer"})
        # 出厂 code_writer.yaml（P3 迁移后驻包内）的身份字段装配
        assert updates["context.agent_name"] == "代码编写专家"
        assert updates["context.is_project"] is False
        assert isinstance(updates.get("tool_ids"), list)
        # 真实文件兜底确认（防显示名漂移掩盖解析失败）
        seed = (
            _REPO_ROOT / "plugins" / "shared" / "modes" / "mode_coding"
            / "agents" / "code_writer.yaml"
        )
        assert os.path.isfile(seed)

    def test_factory_package_agent_resolved_godot_seed(
        self, isolated_roots
    ) -> None:
        """出厂种子回落（mode_godot 包，P3-⑤ 立项）：godot_expert 键经真实包内 yaml 装配。"""
        updates = _run({"agent.id": "mode_godot/godot_expert"})
        # 出厂 godot_expert.yaml（P3 迁移后驻包内）的身份字段装配
        assert updates["context.agent_name"] == "Godot 游戏专家"
        assert updates["context.is_project"] is False
        assert "godot_run" in updates.get("tool_ids", [])
        # 真实文件兜底确认（防显示名漂移掩盖解析失败）
        seed = (
            _REPO_ROOT / "plugins" / "shared" / "modes" / "mode_godot"
            / "agents" / "godot_expert.yaml"
        )
        assert os.path.isfile(seed)
