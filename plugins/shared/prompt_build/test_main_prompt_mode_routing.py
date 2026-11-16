# @feature: FP-0.2.四 模式体系收口批F | @ci: python-coverage
"""主 agent 提示词模式路由真机断言（批 F1/F3/F4，ADR 2026-10-09）。

契约（匹配决策模式行退役后，主 agent 域路由知识单源 {{mode_catalog}}）：
1. 真机目录：agent_manager.list_modes()（mode.list 取数单源，直读六包
   mode.yaml）六包在册且 description 全声明（批 F3 接管口径）；
2. 退役清零：真实种子（config/agents/main/agentos.yaml）不含已退役的
   匹配决策 mode_* 行与速查表——种子漂移即红；
3. 真机渲染：种子 system_prompt 全文经 prompt_build 渲染，{{mode_catalog}}
   展开六条目（mode 键 + name + description 路由键），速查表/箭头式退役行
   不复活；系统行留守（调研/资源管理/验证/通用仍在匹配决策）。

agent_manager server 以 importlib 裸名加载直调（真机 registry 聚合面）；
文件 IO 一律真实，零 mock。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

_REPO_ROOT = _PLUGIN_DIR.parents[2]
_SEED_YAML = _REPO_ROOT / "config" / "agents" / "main" / "agentos.yaml"
_AGENT_MANAGER_SERVER = (
    _REPO_ROOT / "plugins" / "shared" / "agent_manager" / "server.py"
)

# 批 F1 退役的匹配决策模式键（域路由知识归 {{mode_catalog}} 单源）
_RETIRED_MODE_KEYS = (
    "mode_coding", "mode_godot", "mode_writing",
    "mode_research", "mode_roleplay", "mode_evolution",
)

# 特许服务执行者键（非域路由行）：资源编排者随进化模式包承载（用户裁定 2026-10-16），
# 其派发键合法含 mode_evolution 前缀；守卫检查前掩蔽，其余任何 mode_* 出现仍判漂移
_SANCTIONED_SERVICE_KEY = "mode_evolution/resource_manager_agent"


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_main_routing_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_agent_manager_module() -> Any:
    mod_name = "agent_manager_server_main_routing_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _AGENT_MANAGER_SERVER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _seed_yaml() -> dict[str, Any]:
    assert _SEED_YAML.is_file(), f"主 agent 种子不存在: {_SEED_YAML}"
    seed = yaml.safe_load(_SEED_YAML.read_text(encoding="utf-8"))
    assert isinstance(seed, dict)
    return seed


def _seed_prompt() -> str:
    prompt = _seed_yaml()["system_prompt"]
    assert isinstance(prompt, str)
    assert prompt.strip()
    return prompt


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 1. 真机目录（mode.list 单源直读七包声明面） ──────────────────────────


def test_factory_catalog_has_seven_routing_entries() -> None:
    """七包在册且 description 全声明（批 F3 + mode_personal M0）：目录条目是 L1 唯一域路由知识。"""
    entries = _load_agent_manager_module().list_modes()
    assert [e["mode"] for e in entries] == [
        "coding", "evolution", "godot", "personal", "research", "roleplay", "writing",
    ]
    for entry in entries:
        assert isinstance(entry["description"], str) and entry["description"].strip(), (
            f"{entry['mode']} description 缺失——模式行退役后目录描述不可为空"
        )


# ── 2. 退役清零（种子不含匹配决策 mode_* 行与速查表） ────────────────────


def test_seed_prompt_has_no_retired_mode_rows() -> None:
    """批 F1/F2：匹配决策表 mode_* 行与速查表退役后不得在种子复活——
    任一模式键/速查表字样再现即种子漂移（域路由知识唯一出口 = {{mode_catalog}}）。"""
    prompt = _seed_prompt()
    # 特许服务键（资源编排者，随进化模式包）先掩蔽再查——服务执行者键非域路由行
    prompt_masked = prompt.replace(_SANCTIONED_SERVICE_KEY, "")
    for key in _RETIRED_MODE_KEYS:
        assert key not in prompt_masked, f"退役模式键 {key} 仍在主 agent 种子中"
    assert "速查表" not in prompt, "速查表已整体退役（命名规则归 deliverables.output_path 声明面）"
    assert "novel_orchestrator_agent" not in prompt, "小说域路由行已退役（writing 目录条目接管）"
    # 路由单源锚点仍在场：任务模式归类段引用 {{mode_catalog}}
    assert "{{mode_catalog}}" in prompt


def test_seed_system_rows_remain_in_matching_table() -> None:
    """批 F2 留守面：系统执行者行留在匹配决策 static_vars（治理留守面只认
    系统资产）；速查表条目不再作为独立 static_vars 段落复活。"""
    items = _seed_yaml()["static_vars"]["items"]
    matching = next(i for i in items if i["name"] == "Agent 匹配决策")
    content = matching["content"]
    assert "## Agent 匹配决策" in content
    for fragment in (
        "- 调研：research_agent / research_orchestrator_agent",
        "- 资源管理：mode_evolution/resource_manager_agent（进化模式包）",
        "- 验证：container_verification_agent",
        "- 通用：general_agent",
    ):
        assert fragment in content, f"系统行留守缺失: {fragment}"
    assert "## Agent 速查表" not in content


# ── 3. 真机渲染（种子全文 × 真机目录 → {{mode_catalog}} 展开） ────────────


def test_seed_prompt_renders_expanded_mode_catalog() -> None:
    """种子 system_prompt 全文真机渲染：{{mode_catalog}} 展开六条目（含
    description 路由键），占位符无残留，退役行不复活。"""
    mod = _load_plugin_module()
    entries = _load_agent_manager_module().list_modes()

    async def _fetch() -> dict[str, Any]:
        return {"modes": entries, "total": len(entries)}

    plugin = mod.PromptBuildPlugin(catalog_fetcher=_fetch)
    rendered = _run(plugin._resolve_placeholders(PluginContext(state={}), _seed_prompt()))

    assert "{{mode_catalog}}" not in rendered, "目录占位符未展开"
    # 六条目按 mode 键序展开，条目 = mode 键 | name | description 路由
    for mode_key, name in (
        ("coding", "编码模式"), ("evolution", "自进化模式"), ("godot", "Godot 游戏开发模式"),
        ("research", "研究模式"), ("roleplay", "酒馆"), ("writing", "写作模式"),
    ):
        assert f"- {mode_key} | {name} | " in rendered, f"目录条目缺失: {mode_key}"
    # 路由键随 description 在场（直派快道 / 编排链执行者，批 F3 接管口径；
    # coding 自 2026-10-23 用户裁定只剩编排链执行者，L3 直派键退役）
    for routing_key in (
        "mode_coding/programming_orchestrator_agent_v2",
        "mode_godot/godot_expert", "mode_godot/godot_orchestrator_agent",
        "mode_evolution/evolution",
    ):
        assert routing_key in rendered, f"目录路由键缺失: {routing_key}"
    # 退役行不复活：速查表与箭头式匹配行（目录 description 为「直派 X」句式，
    # 退役行为「→X 直派」箭头式，可辨）
    assert "速查表" not in rendered
    assert "→mode_" not in rendered
