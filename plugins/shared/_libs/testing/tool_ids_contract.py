# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""agent tool_ids 契约扫描单点实现（工具面漂移守卫的单一真源）。

契约（工具面过滤 ADR 2026-09-02-agent-config-decouple-tool-surface 的配置面
投影）：agent yaml 声明的 ``tool_ids`` 必须落在插件注册表面内——

    注册表面 = plugins/shared/**/plugin.json 的 capabilities.tools[].name
             ∪ 声明了 ≥1 工具的插件 id（内核 tool-surface 的整包注入配法）
             ∪ 已知合法非工具 id 白名单 ∪ 用户空间插件提供的 id 白名单

引用未注册/已退役 id ⇒ 该 agent 每轮工具面注入都触发「工具面漂移」告警
（tool_schema 插件按"agent 声明 ⊆ 注册表"检出并报警），模型侧拿不到该工具。

**本模块是扫描逻辑的唯一实现**，三处消费方共用（免多份扫描逻辑各自漂移）：

- ``tests/plugins/test_tool_ids_contract.py``——仓内出厂 agent 配置面
  （``config/agents/**``）＋ 模式插件出厂 agent 面（``plugins/shared/*/agents/``）
- ``plugins/shared/mode_coding/test_agent_tool_ids.py``——mode_coding 包内契约闸
- ``plugins/shared/mode_godot/test_mode_godot_agent_tool_ids.py``——mode_godot 包内契约闸

用法（消费方需把 ``plugins/shared`` 置于 sys.path——_libs 包路径）::

    from _libs.testing.tool_ids_contract import (
        config_agent_yaml_files, plugin_agent_yaml_files,
        load_tool_registry, load_plugin_ids_with_tools,
        unregistered_tool_ids, retired_tool_id_refs,
    )
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

# 遍历裁剪目录：dsh_adapter 的 node_modules 连接点会让 rglob 卡死
# （与 tests/plugins/test_plugin_smoke_matrix.py 同款处置）。
SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules",
    "__pycache__",
    "target",
    "runtime",
    "dsh_plugins",
    ".venv",
    ".venv-hindsight",
    ".ai_workspaces",
})

# 已知合法非工具 id 白名单（当前为空，理由见模块 docstring）。
KNOWN_NON_TOOL_IDS: frozenset[str] = frozenset()

# 用户空间插件提供的工具 id（ADR 2026-09-14 单一存在：external_mcp 四插件迁
# %APPDATA%/agentos/plugins，经用户插件根热发现注册，仓内 manifest 不可见）。
# agent yaml 保留引用是合法的——生产面（本机用户根）可解析；仓内独立部署不含
# 这些 id，此处显式登记豁免，新增用户空间 id 须同名追加。
USER_SPACE_TOOL_IDS: frozenset[str] = frozenset({
    "godot_run",          # external_mcp/godot_mcp（mode_godot 两份 agent 消费）
    "universal_search",   # external_mcp/omnisearch（general/research 系消费）
    "web_reader",         # external_mcp/omnisearch（URL→markdown，general/research 系消费）
    # 外部桌面通道（windows-mcp 外部 MCP，config/mcp.json 声明）工具真名：
    # 上游实际导出 desktop_*（desktop_capture / desktop_launch / … 共 21 件），
    # main/agentos.yaml 与 executor/general_agent.yaml 消费。
    "desktop_capture", "desktop_look", "desktop_windows", "desktop_tree",
    "desktop_click", "desktop_type", "desktop_key", "desktop_scroll",
    "desktop_drag", "desktop_window", "desktop_invoke", "desktop_read",
    "desktop_text", "desktop_wait", "desktop_sequence", "desktop_web",
    "desktop_course", "desktop_doctor", "desktop_log", "desktop_steer",
    "desktop_launch",     # 启动应用 / 打开文件 / 打开 URL（软件类别）
    # 历史抽象名（旧版上游原名）。仍留在白名单：agent yaml 若残留旧名不该因本闸
    # 静默通过——旧名调用必败，改由「桌面通道实际工具名」承接，见上方 desktop_*。
    "App", "Snapshot", "Click", "Type", "Shortcut", "Wait",
    "Move", "Screenshot", "Scroll",
})

# 已退役工具 id（残留引用即漂移源；能力由现役工具承接，不得再出现在 tool_ids）
RETIRED_TOOL_IDS: frozenset[str] = frozenset({
    # 并入 file_read 目录模式，从 builtin_tools 注册面退役（ADR 2026-10-16）
    "list_directory",
    # 并入 project 工具 action='create'，从注册面退役（工具面退役·任务C刀4）
    "project_create",
    # 拆为 bash/log/input/kill 四工具，旧名直退不设别名（ADR 2026-11-07
    # bash 工具契约重构；残留引用 = 该 agent 每次调用必败）
    "bash_execute",
})


def pruned_files(base: Path) -> list[Path]:
    """带剪枝的文件遍历（os.walk；剪掉连接点迷宫与构建目录）。"""
    found: list[Path] = []
    for cur, subdirs, files in os.walk(base):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
        found.extend(Path(cur) / f for f in files)
    return sorted(found)


def load_tool_registry(shared_dir: Path) -> dict[str, list[str]]:
    """plugins/shared 全部 manifest 的工具声明面：工具名 → 声明来源清单。"""
    registry: dict[str, list[str]] = {}
    for path in pruned_files(shared_dir):
        if path.name != "plugin.json":
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        rel = path.relative_to(shared_dir.parent.parent).as_posix()
        for tool in manifest.get("capabilities", {}).get("tools", []) or []:
            name = tool.get("name")
            if name:
                registry.setdefault(name, []).append(rel)
    return registry


def load_plugin_ids_with_tools(shared_dir: Path) -> set[str]:
    """声明了 ≥1 工具的插件 id 集合。

    内核 tool-surface.schemas 契约（capability_router.rs）：tool_ids 条目等于
    插件 id → 该插件全部工具入面（一行接入配法，与精确工具名并存）。
    """
    ids: set[str] = set()
    for path in pruned_files(shared_dir):
        if path.name != "plugin.json":
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("capabilities", {}).get("tools"):
            plugin_id = manifest.get("id")
            if plugin_id:
                ids.add(plugin_id)
    return ids


def config_agent_yaml_files(repo_root: Path) -> list[Path]:
    """仓内出厂 agent 配置面：``config/agents/**`` 下的 yaml（递归）。"""
    base = repo_root / "config" / "agents"
    if not base.is_dir():
        return []
    return [p for p in pruned_files(base) if p.suffix in (".yaml", ".yml")]


def plugin_agent_yaml_files(repo_root: Path) -> list[Path]:
    """插件出厂 agent 面：``plugins/shared/<插件id>/agents/*.yaml``（平铺布局单层）。"""
    shared = repo_root / "plugins" / "shared"
    if not shared.is_dir():
        return []
    found: list[Path] = []
    for plugin_dir in sorted(p for p in shared.iterdir() if p.is_dir()):
        agents_dir = plugin_dir / "agents"
        if not agents_dir.is_dir():
            continue
        found.extend(sorted(agents_dir.glob("*.yaml")))
    return found


def load_agent_tool_ids(yaml_path: Path) -> list[str] | None:
    """读 agent yaml 的 tool_ids 声明。

    未声明 tool_ids → None（该 agent 不受工具面过滤约束）；声明为非列表 →
    ValueError（配置面畸形，必须报错而非静默放行）。
    """
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    tool_ids = data.get("tool_ids")
    if tool_ids is None:
        return None
    if not isinstance(tool_ids, list):
        raise ValueError(f"{yaml_path} 的 tool_ids 应为列表，实为 {type(tool_ids).__name__}")
    return [str(t) for t in tool_ids]


def unregistered_tool_ids(
    yaml_path: Path,
    registry: dict[str, list[str]],
    plugin_ids: set[str],
    allowed_ids: frozenset[str] = KNOWN_NON_TOOL_IDS | USER_SPACE_TOOL_IDS,
) -> list[str]:
    """该 agent yaml 引用的注册表面外 id（空列表 = 无漂移）。"""
    tool_ids = load_agent_tool_ids(yaml_path)
    if not tool_ids:
        return []
    return sorted(
        set(tool_ids) - set(registry) - set(plugin_ids) - set(allowed_ids)
    )


def retired_tool_id_refs(yaml_paths: list[Path], repo_root: Path | None = None) -> dict[str, list[str]]:
    """退役 id 残留清单：yaml 标识 → 命中的退役 id 列表。

    ``repo_root`` 给出时标识为仓内相对路径（错误信息可读），否则为绝对路径。
    """
    hits: dict[str, list[str]] = {}
    for path in yaml_paths:
        tool_ids = load_agent_tool_ids(path) or []
        found = sorted(set(tool_ids) & RETIRED_TOOL_IDS)
        if found:
            key = path.relative_to(repo_root).as_posix() if repo_root else path.as_posix()
            hits[key] = found
    return hits
