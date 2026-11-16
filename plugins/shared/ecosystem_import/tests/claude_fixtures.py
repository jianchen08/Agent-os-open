"""ecosystem_import 测试夹具构造器（Claude Code 三形态）。

夹具一律在 ``tmp_path`` 内构造**真实** Claude Code 实体目录（不依赖本机
``~/.claude``）；目录形态与清单字段按官方最新规范（code.claude.com/docs 的
plugins/manifest-reference、plugins/marketplace-reference、plugins/components）：

- 形态 1 插件目录：``.claude-plugin/plugin.json`` + ``skills/<name>/SKILL.md``
  + ``agents/**/*.md`` + ``commands/**/*.md`` + ``hooks/hooks.json`` + ``.mcp.json``
- 形态 2 市场条目：``.claude-plugin/marketplace.json``（条目内联 ``skills[]``）
- 形态 3 裸技能目录：``<root>/<name>/SKILL.md``

共享构造器（``skill_md`` / ``file_snapshot`` / 模块装载）单源在 ``codex_fixtures``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "add_skill_dir",
    "make_claude_plugin",
    "make_marketplace",
    "write_agents",
    "write_commands",
    "write_hooks",
    "write_mcp",
]


def make_claude_plugin(
    root: Path,
    *,
    name: str = "demo-plugin",
    version: str = "1.0.0",
    license: str | None = None,
    description: str = "演示插件",
    author: str = "Demo Author",
    manifest_extra: dict[str, Any] | None = None,
) -> Path:
    """构造形态 1 插件骨架（``.claude-plugin/plugin.json``），返回插件目录。

    ``license=None`` = 清单不写 license 键（未声明许可证场景）。
    """
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": description,
        "author": {"name": author, "email": "demo@example.com"},
    }
    if license is not None:
        manifest["license"] = license
    manifest.update(manifest_extra or {})
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return plugin_dir


def add_skill_dir(parent_dir: Path, dir_name: str, *, content: str, extra_files: dict[str, str] | None = None) -> Path:
    """在 ``parent_dir`` 下写一个技能目录（SKILL.md + 同目录资源），返回技能目录。"""
    skill_dir = parent_dir / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return skill_dir


def make_marketplace(
    root: Path, *, entries: list[dict[str, Any]], name: str = "demo-marketplace", metadata: dict[str, Any] | None = None
) -> Path:
    """构造形态 2 市场骨架（``.claude-plugin/marketplace.json``），返回市场根。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"name": name, "owner": {"name": "Demo Owner"}, "plugins": entries}
    if metadata is not None:
        doc["metadata"] = metadata
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return root


def write_agents(plugin_dir: Path, files: dict[str, str]) -> None:
    """写 ``agents/`` 下文件（键 = 相对路径，支持子目录）。"""
    _write_files(plugin_dir / "agents", files)


def write_commands(plugin_dir: Path, files: dict[str, str]) -> None:
    """写 ``commands/`` 下文件（键 = 相对路径，支持子目录）。"""
    _write_files(plugin_dir / "commands", files)


def write_hooks(plugin_dir: Path, events: dict[str, Any]) -> None:
    """写 ``hooks/hooks.json``（规范要求顶层 ``hooks`` 包裹层）。"""
    path = plugin_dir / "hooks" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": events}, ensure_ascii=False), encoding="utf-8")


def write_mcp(plugin_dir: Path, servers: dict[str, Any], *, wrapped: bool = True) -> None:
    """写 ``.mcp.json``：``wrapped`` = ``mcpServers`` 包裹形态 / 顶层服务器映射（两者皆最新规范）。"""
    doc = {"mcpServers": servers} if wrapped else servers
    (plugin_dir / ".mcp.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _write_files(base: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
