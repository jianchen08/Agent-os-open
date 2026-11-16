#!/usr/bin/env python3
"""Skill Manager 服务端——技能注册权威（skills/*/SKILL.md 扫描解析单源）。

资源注册收敛方案 T3（与 agent_manager 聚合注册表对仗的 skill 面）：

provides.capabilities[].namespace = ``skill``（契约键 = namespace，human
先例——kernel McpBridge 把 ``skill.<method>`` 经 tool_prefix 转发到本
sidecar 的同名工具，内核零硬编码，manifest 声明即注册）：
- skill.list  全量元数据 ``{items: [{id, name, description, languages,
              source}]}``（source = 实际胜出源：repo_root / mode:<plugin_id>
              / user_root，与工作空间快照侧同一四源并集与优先序，X11-F1）
- skill.get   单技能详情 ``{found, skill: {id, name, description, languages,
              content}}``（content = SKILL.md 正文，detailed 模式取数面；
              解析面与 list 同一四源并集）

消费方（resource_search）经 ``requires_services: ["skill.list", "skill.get"]``
声明 + capability handle 直连调用（approval → human-interaction 生产先例）；
G2 可用性闸 / 静态声明闸 / 运行时对账三层执法在此契约上闭环。将来「工具
技能化」在此注册权威上展开（方案 §四.4，独立需求）。
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from skill_manager import skill_registry  # noqa: E402 —— 包路径导入（共享根即组根，拍平布局）

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("skill_manager")


@plugin.tool(
    name="skill.list",
    schema={"type": "object", "properties": {}},
    description="List registered skills (metadata: id/name/description/languages/source; sorted by id; source = winning origin: repo_root / mode:<plugin_id> / user_root)",
)
async def skill_list() -> dict[str, Any]:
    """全量技能元数据（坏件跳过是注册表职责，消费方不做文件扫描）。"""
    items = skill_registry.list_skills()
    return {"items": items}


@plugin.tool(
    name="skill.get",
    schema={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "Skill id (directory name)"},
        },
    },
    description="Get one skill detail (metadata + SKILL.md body content)",
)
async def skill_get(skill_id: str = "") -> dict[str, Any]:
    """单技能详情；未知 id / 非法形态 → found=false（不算失败）。"""
    return skill_registry.get_skill(skill_id)


if __name__ == "__main__":
    plugin.run()
