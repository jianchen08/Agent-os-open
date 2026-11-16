# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""agent tool_ids 与插件注册表面一致性契约测试（仓内出厂 agent 全域）。

契约：agent yaml（``config/agents/**`` 与 ``plugins/shared/*/agents/**``）的
tool_ids，必须 ⊆ 插件注册表面（plugins/shared/ 全部 manifest 的
capabilities.tools[].name ∪ 声明了工具的插件 id）∪ 已知合法非工具 id 白名单。
tool_ids 引用注册表不存在的 id → 该 agent 每轮 LLM 调用必然以"未注册工具"
失败（零报错痕迹的配置面漂移，见 ADR 2026-08-28-g2-sanitization-evidence-retention
第 4/5 条）。

白名单当前为空。全仓核实结论：tool_ids 中的点分条目（review.get_report）
为对应 manifest capabilities.tools[].name 显式声明的 LLM 工具
（plugins/shared/review/），不是服务方法形式，直接按注册表校验，无需归类豁免。

**扫描实现单源**在 ``plugins/shared/_libs/testing/tool_ids_contract.py``——
本文件与两个模式包内契约闸（mode_coding / mode_godot）共用同一实现，免多份
扫描逻辑各自漂移（问题 8 工具面漂移余波收口）。

发现方式必须用带剪枝的 os.walk：rglob 会钻进 dsh_adapter runtime 的
node_modules 连接点迷宫卡死（与 tests/plugins/test_plugin_smoke_matrix.py
_discover_plugin_dirs 同款处置）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = ROOT / "plugins" / "shared"

# _libs 包路径锚（plugins/shared 置于 sys.path，裸名导入 _libs.testing.*）
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from _libs.testing.tool_ids_contract import (  # noqa: E402 —— 需先注入 sys.path
    RETIRED_TOOL_IDS,
    config_agent_yaml_files,
    load_agent_tool_ids,
    load_plugin_ids_with_tools,
    load_tool_registry,
    plugin_agent_yaml_files,
    retired_tool_id_refs,
    unregistered_tool_ids,
)

pytestmark = pytest.mark.unit

TOOL_REGISTRY = load_tool_registry(SHARED_DIR)
PLUGIN_IDS_WITH_TOOLS = load_plugin_ids_with_tools(SHARED_DIR)
CONFIG_AGENT_YAMLS = config_agent_yaml_files(ROOT)
PLUGIN_AGENT_YAMLS = plugin_agent_yaml_files(ROOT)
AGENT_YAMLS = CONFIG_AGENT_YAMLS + PLUGIN_AGENT_YAMLS


def test_discovery_surface_non_vacuous() -> None:
    """剪枝误伤防护：agent 清单与注册表面非空，且跨域抽查工具仍在表面。"""
    assert CONFIG_AGENT_YAMLS, "config/agents 下未发现任何 yaml（walk 剪枝过宽或目录漂移）"
    assert PLUGIN_AGENT_YAMLS, "plugins/shared/*/agents 下未发现任何 yaml（目录漂移）"
    assert TOOL_REGISTRY, "plugins/shared 下未发现任何工具声明（walk 剪枝过宽或目录漂移）"
    # 抽查覆盖 tools 域（bash/task/task_evaluate 插件）与 system 域（review 插件）；
    # pipeline 域现状零 LLM 工具声明，不在抽查之列；
    # review_ops：工具面退役任务 C 合一件（trigger_review+review_get_report 二合一）
    for probe in ("bash_execute", "task_manage", "task_evaluate", "review_ops"):
        assert probe in TOOL_REGISTRY, f"注册表缺抽查工具 {probe}（发现逻辑误伤或 manifest 漂移）"


@pytest.mark.parametrize(
    "yaml_path",
    AGENT_YAMLS,
    ids=[p.relative_to(ROOT).as_posix() for p in AGENT_YAMLS],
)
def test_agent_tool_ids_subset_of_registry(yaml_path: Path) -> None:
    """每个 agent 的 tool_ids ⊆ 注册表面 ∪ 白名单；缺失条目显式列出。"""
    missing = unregistered_tool_ids(yaml_path, TOOL_REGISTRY, PLUGIN_IDS_WITH_TOOLS)
    assert not missing, (
        f"{yaml_path.relative_to(ROOT).as_posix()} 的 tool_ids 引用了注册表不存在的 id: "
        f"{missing}（生产面 LLM 调用这些 id 必败；修复对应插件 manifest 或修订 tool_ids。"
        f"注意：等于插件 id 的条目为整包注入配法，须对应插件声明 ≥1 工具）"
    )


def test_retired_tool_ids_not_referenced() -> None:
    """已退役 id 全域零残留（退役样例见 _libs.testing.tool_ids_contract.RETIRED_TOOL_IDS）。"""
    hits = retired_tool_id_refs(AGENT_YAMLS, ROOT)
    assert not hits, (
        f"仍引用已退役的工具 id（每次工具面注入刷漂移告警，模型侧拿不到该工具）: {hits}；"
        f"已退役 id 须从 tool_ids 移除（能力由现役工具承接）"
    )


def test_retired_tool_ids_are_not_in_registry() -> None:
    """退役 id 不得重新出现在注册表面（否则专项断言形同虚设）。"""
    for retired in sorted(RETIRED_TOOL_IDS):
        assert retired not in TOOL_REGISTRY, f"退役 id {retired} 仍在注册表面（退役事实与 manifest 漂移）"


def test_contract_gate_lanes_are_wired() -> None:
    """契约闸所在目录必须已进插桩车道基集（否则本地跑绿、CI 车道不收集）。"""
    scripts_dir = ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from coverage_exempt import BASE_TEST_PATHS  # noqa: PLC0415 —— 需先注入 sys.path

    wired = {p.rstrip("/") for p in BASE_TEST_PATHS}
    for lane in ("plugins/shared/mode_coding", "plugins/shared/mode_godot"):
        assert lane in wired, f"{lane} 未进插桩车道基集（契约闸在 CI 失效：本地绿、CI 不跑）"


# ── 扫描器自身行为（新增模式插件漏检须能被拦） ────────────────────────────


def _write_plugin_agent(root: Path, plugin_id: str, tool_ids: list[str]) -> Path:
    """构造假仓布局：plugins/shared/<plugin_id>/{plugin.json,agents/a.yaml}。"""
    plugin_dir = root / "plugins" / "shared" / plugin_id
    agents_dir = plugin_dir / "agents"
    agents_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "id": plugin_id,
            "capabilities": {"tools": [{"name": "demo_tool"}]},
        }),
        encoding="utf-8",
    )
    agent_yaml = agents_dir / "demo_agent.yaml"
    agent_yaml.write_text(
        "config_id: demo_agent\ntool_ids:\n" + "".join(f"- {t}\n" for t in tool_ids),
        encoding="utf-8",
    )
    return agent_yaml


def test_new_plugin_agent_ghost_tool_id_is_caught(tmp_path: Path) -> None:
    """新增模式插件的 agent 引用幽灵 id ⇒ 扫描器必须报出（漏检即被拦）。"""
    agent_yaml = _write_plugin_agent(tmp_path, "demo_mode", ["demo_tool", "ghost_tool"])

    assert plugin_agent_yaml_files(tmp_path) == [agent_yaml]
    registry = load_tool_registry(tmp_path / "plugins" / "shared")
    plugin_ids = load_plugin_ids_with_tools(tmp_path / "plugins" / "shared")
    assert unregistered_tool_ids(agent_yaml, registry, plugin_ids) == ["ghost_tool"]


def test_new_plugin_agent_retired_tool_id_is_caught(tmp_path: Path) -> None:
    """新增模式插件的 agent 引用退役 id ⇒ 退役专项断言必须报出。"""
    agent_yaml = _write_plugin_agent(tmp_path, "demo_mode", ["demo_tool", "list_directory"])

    registry = load_tool_registry(tmp_path / "plugins" / "shared")
    plugin_ids = load_plugin_ids_with_tools(tmp_path / "plugins" / "shared")
    assert unregistered_tool_ids(agent_yaml, registry, plugin_ids) == ["list_directory"]
    assert retired_tool_id_refs([agent_yaml]) == {agent_yaml.as_posix(): ["list_directory"]}


def test_new_plugin_agent_known_ids_pass(tmp_path: Path) -> None:
    """注册面内 id 与整包注入配法（插件 id 本身）均不得误报。"""
    agent_yaml = _write_plugin_agent(tmp_path, "demo_mode", ["demo_tool", "demo_mode"])

    registry = load_tool_registry(tmp_path / "plugins" / "shared")
    plugin_ids = load_plugin_ids_with_tools(tmp_path / "plugins" / "shared")
    assert unregistered_tool_ids(agent_yaml, registry, plugin_ids) == []
    assert retired_tool_id_refs([agent_yaml]) == {}


def test_load_agent_tool_ids_absent_and_malformed(tmp_path: Path) -> None:
    """未声明 tool_ids → None；声明为非列表 → ValueError（畸形配置不静默放行）。"""
    absent = tmp_path / "absent.yaml"
    absent.write_text("config_id: x\n", encoding="utf-8")
    assert load_agent_tool_ids(absent) is None

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("config_id: x\ntool_ids: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tool_ids 应为列表"):
        load_agent_tool_ids(malformed)


def test_missing_dirs_and_non_mapping_yaml(tmp_path: Path) -> None:
    """目录缺失 ⇒ 契约面空集（空转不报错）；顶层非映射 yaml ⇒ 无 tool_ids。"""
    assert config_agent_yaml_files(tmp_path) == []
    assert plugin_agent_yaml_files(tmp_path) == []

    non_mapping = tmp_path / "non_mapping.yaml"
    non_mapping.write_text("- a\n- b\n", encoding="utf-8")
    assert load_agent_tool_ids(non_mapping) is None
