# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""agent tool_ids 与插件注册表面一致性契约测试。

契约：config/agents/ 下每个 agent yaml 的 tool_ids，必须 ⊆
plugins/shared/ 全部 manifest 的 capabilities.tools[].name 注册表面
（tools / system / pipeline 各域）∪ 已知合法非工具 id 白名单。
tool_ids 引用注册表不存在的 id → 该 agent 每轮 LLM 调用必然以
"未注册工具"失败（零报错痕迹的配置面漂移，见 ADR
2026-08-28-g2-sanitization-evidence-retention 第 4/5 条）。

白名单当前为空。全仓核实结论：tool_ids 中的点分条目（lsp.*、
review.get_report）均为对应 manifest capabilities.tools[].name 显式声明的
LLM 工具（plugins/shared/tools/lsp/、plugins/shared/system/review/），
不是服务方法形式，直接按注册表校验，无需归类豁免。

发现方式必须用带剪枝的 os.walk：rglob 会钻进 dsh_adapter runtime 的
node_modules 连接点迷宫卡死（与 tests/plugins/test_plugin_smoke_matrix.py
_discover_plugin_dirs 同款处置）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = ROOT / "config" / "agents"
SHARED_DIR = ROOT / "plugins" / "shared"

_SKIP_DIRS = {
    "node_modules", "__pycache__", "target", "runtime", "dsh_plugins",
    ".venv", ".venv-hindsight", ".ai_workspaces",
}

# 已知合法非工具 id 白名单（当前为空，理由见模块 docstring）。
_KNOWN_NON_TOOL_IDS: frozenset[str] = frozenset()


def _pruned_files(base: Path) -> list[Path]:
    found: list[Path] = []
    for cur, subdirs, files in os.walk(base):
        subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
        found.extend(Path(cur) / f for f in files)
    return sorted(found)


def _load_tool_registry() -> dict[str, list[str]]:
    """plugins/shared 全部 manifest 的工具声明面：工具名 → 声明来源清单。"""
    registry: dict[str, list[str]] = {}
    for path in _pruned_files(SHARED_DIR):
        if path.name != "plugin.json":
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        tools = manifest.get("capabilities", {}).get("tools", []) or []
        for tool in tools:
            name = tool.get("name")
            if name:
                registry.setdefault(name, []).append(rel)
    return registry


TOOL_REGISTRY = _load_tool_registry()
AGENT_YAMLS = [p for p in _pruned_files(AGENTS_DIR) if p.suffix in (".yaml", ".yml")]


def test_discovery_surface_non_vacuous() -> None:
    """剪枝误伤防护：agent 清单与注册表面非空，且跨域抽查工具仍在表面。"""
    assert AGENT_YAMLS, "config/agents 下未发现任何 yaml（walk 剪枝过宽或目录漂移）"
    assert TOOL_REGISTRY, "plugins/shared 下未发现任何工具声明（walk 剪枝过宽或目录漂移）"
    # 抽查覆盖 tools 域（bash/task/task_evaluate 插件）与 system 域（review 插件）；
    # pipeline 域现状零 LLM 工具声明，不在抽查之列；
    # review_get_report：点号名违反 OpenAI 工具名契约已改下划线（3ee3e89e7）
    for probe in ("bash_execute", "task_manage", "task_evaluate", "review_get_report"):
        assert probe in TOOL_REGISTRY, f"注册表缺抽查工具 {probe}（发现逻辑误伤或 manifest 漂移）"


@pytest.mark.parametrize(
    "yaml_path",
    AGENT_YAMLS,
    ids=[p.relative_to(ROOT).as_posix() for p in AGENT_YAMLS],
)
def test_agent_tool_ids_subset_of_registry(yaml_path: Path) -> None:
    """每个 agent 的 tool_ids ⊆ 注册表面 ∪ 白名单；缺失条目显式列出。"""
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    tool_ids = data.get("tool_ids") if isinstance(data, dict) else None
    if tool_ids is None:
        pytest.skip("该 agent 未声明 tool_ids")
    assert isinstance(tool_ids, list), (
        f"tool_ids 应为列表，实为 {type(tool_ids).__name__}"
    )
    missing = sorted(set(tool_ids) - set(TOOL_REGISTRY) - set(_KNOWN_NON_TOOL_IDS))
    assert not missing, (
        f"{yaml_path.relative_to(ROOT).as_posix()} 的 tool_ids 引用了注册表不存在的 id: "
        f"{missing}（生产面 LLM 调用这些 id 必败；修复对应插件 manifest 或修订 tool_ids）"
    )
