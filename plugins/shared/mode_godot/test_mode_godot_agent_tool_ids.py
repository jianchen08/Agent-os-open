# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""mode_godot 出厂 agent 的 tool_ids 与插件注册表面一致性契约测试。

契约：``agents/*.yaml`` 的 tool_ids 必须是 ``plugins/shared/*/plugin.json``
``capabilities.tools[].name`` 声明面内的已注册工具 id。引用已退役/未注册的
id → 该 agent 每轮工具面注入都会触发「工具面漂移」告警（tool_schema 插件按
"agent 声明 ⊆ 注册表" 检出并报警），模型侧拿不到该工具。

退役样例：``list_directory`` 已并入 ``file_read`` 目录模式并从 builtin_tools
注册面退役（ADR 2026-10-16）；``project_create`` 已并入 ``project`` 工具
``action='create'``（工具面退役·任务C刀4）。退役 id 清单与扫描实现单源在
``plugins/shared/_libs/testing/tool_ids_contract.py``（与
``tests/plugins/test_tool_ids_contract.py``、mode_coding 包内契约闸共用同一
实现，免多份扫描逻辑漂移）。

用法：随插件目录内 pytest 车道运行（``pytest plugins/shared/mode_godot/``）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent
AGENTS_DIR = PLUGIN_DIR / "agents"
SHARED_ROOT = PLUGIN_DIR.parent
REPO_ROOT = SHARED_ROOT.parent.parent

# _libs 包路径锚（plugins/shared 置于 sys.path，裸名导入 _libs.testing.*）
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from _libs.testing.tool_ids_contract import (  # noqa: E402 —— 需先注入 sys.path
    load_agent_tool_ids,
    load_plugin_ids_with_tools,
    load_tool_registry,
    retired_tool_id_refs,
    unregistered_tool_ids,
)

TOOL_REGISTRY = load_tool_registry(SHARED_ROOT)
PLUGIN_IDS_WITH_TOOLS = load_plugin_ids_with_tools(SHARED_ROOT)


def _agent_yaml_files() -> list[Path]:
    return sorted(AGENTS_DIR.glob("*.yaml"))


@pytest.mark.unit
def test_mode_godot_has_agent_yaml_files():
    """出厂 agent 种子非空（空目录即契约面空转，测试失去意义）。"""
    assert _agent_yaml_files(), "mode_godot/agents/ 下应有出厂 agent yaml"


@pytest.mark.unit
def test_agent_tool_ids_are_registered():
    """每个出厂 agent 的 tool_ids 都在注册表面内（漂移即红）。"""
    ghosts: dict[str, list[str]] = {}
    for path in _agent_yaml_files():
        missing = unregistered_tool_ids(path, TOOL_REGISTRY, PLUGIN_IDS_WITH_TOOLS)
        if missing:
            ghosts[path.name] = missing
    assert not ghosts, (
        "agent tool_ids 引用了注册表不存在的工具（每次工具面注入刷漂移告警）: "
        f"{ghosts}；已退役 id 须从 tool_ids 移除（能力由现役工具承接）"
    )


@pytest.mark.unit
def test_retired_tool_ids_not_referenced():
    """已退役 id（list_directory / project_create）零残留（漂移源专项断言）。"""
    hits = retired_tool_id_refs(_agent_yaml_files(), REPO_ROOT)
    assert not hits, f"仍引用已退役的工具 id: {hits}"


@pytest.mark.unit
def test_project_capability_taken_over_by_project_tool():
    """项目创建能力由 ``project`` 工具（action='create'）承接，出厂 agent 仍持有。"""
    assert "project" in TOOL_REGISTRY, "注册面缺 project 工具（项目创建能力无承接方）"
    holders = [
        path.name
        for path in _agent_yaml_files()
        if "project" in (load_agent_tool_ids(path) or [])
    ]
    assert holders, "mode_godot 出厂 agent 应持有 project 工具（项目接入自愈口）"
