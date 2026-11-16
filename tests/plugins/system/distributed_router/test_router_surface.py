# @feature: FP-0.2.多端同步 2c 透明路由 | @ci: python-coverage
"""工具面零新增断言（R24 用户指令：四工具撤出 LLM 工具面）。

断声明面行为：distributed_router 不声明任何 LLM 工具（路径统一承载 = OS
文件系统挂载，ADR 2026-10-18 D14；地盘观测归前端呈现层）；config 主控
白名单不含四件旧工具名（面清单与 pack12 前一致，零新增）。声明面漂移在
此立闸——任何把路由做成显式工具的回潮当场红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ROUTER_PLUGIN_JSON = REPO_ROOT / "plugins" / "shared" / "distributed_router" / "plugin.json"
AGENTOS_YAML = REPO_ROOT / "config" / "agents" / "main" / "agentos.yaml"

RETIRED_TOOLS = (
    "resolve_path",
    "list_territories",
    "mount_device_channel",
    "unmount_device_channel",
)

pytestmark = pytest.mark.unit


def _declared_router_tools() -> list[str]:
    manifest = json.loads(ROUTER_PLUGIN_JSON.read_text(encoding="utf-8"))
    tools = manifest.get("capabilities", {}).get("tools") or []
    return [str(t.get("name", "")) for t in tools if isinstance(t, dict)]


def _config_tool_ids() -> set[str]:
    """agentos.yaml 的 tool_ids 白名单（缩进行以 '- ' 起始的条目收集）。"""
    ids: set[str] = set()
    in_section = False
    for raw in AGENTOS_YAML.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("tool_ids:"):
            in_section = True
            continue
        if in_section and line and not line.startswith(("-", "#", " ")):
            break  # 下一顶层键
        if in_section and line.lstrip().startswith("- "):
            ids.add(line.lstrip()[2:].strip())
    return ids


class TestRouterSurfaceZeroNewTools:
    def test_distributed_router_declares_no_llm_tools(self) -> None:
        """撤四工具后声明面必须为空（路径统一承载=OS 挂载，非 LLM 工具，D14/D19）。"""
        assert _declared_router_tools() == []

    @pytest.mark.parametrize("tool", RETIRED_TOOLS)
    def test_retired_tool_absent_from_plugin_manifest(self, tool: str) -> None:
        assert tool not in _declared_router_tools()

    @pytest.mark.parametrize("tool", RETIRED_TOOLS)
    def test_retired_tool_absent_from_config_tool_ids(self, tool: str) -> None:
        assert tool not in _config_tool_ids()

    def test_config_tool_ids_nonempty(self) -> None:
        """白名单解析自检：收集器必须收集到实质条目（防解析静默空集假绿）。"""
        assert len(_config_tool_ids()) >= 20
