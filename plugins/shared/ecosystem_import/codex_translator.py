"""Codex 插件清单翻译器（A 批前端）——纯函数：只读文件、零写入、零子进程。

输入 = Codex 插件目录布局（实测格式，见
``docs/working/Codex能力与插件移植评估_20261017.md`` §2.2）::

    <plugin>/.codex-plugin/plugin.json   # name/version/description/author/license
    <plugin>/skills/<name>/SKILL.md      # YAML frontmatter: name + description
    <plugin>/.mcp.json                   # {"mcpServers": {<name>: {...}}}（可选）
    <plugin>/.app.json                   # {"apps": {<name>: {"id": ...}}}（可选）

输出 = IR（:mod:`import_ir`，纯数据）。对面代码一行不执行——外部包在本层是数据，
不是可执行物；骨架/interface/hooks 一类宿主专属面不翻译（本仓无对应物）。

坏件一律 fail-closed 显式报错（:class:`TranslationError` 带机器可读 code）：
源目录不存在 / 缺插件清单 / JSON 不可解析 / 结构非法 / frontmatter 不可解析。
清单与正文读取的共用实现单源在 :mod:`translator_common`（与 Claude 前端同面）。
"""

from __future__ import annotations

from pathlib import Path

from import_ir import AppEntry, McpServer, PluginIR, SkillAsset
from translator_common import (
    MCP_TOP_LEVEL_KINDS,
    TranslationError,
    author_name,
    license_value,
    load_json_object,
    mcp_server_map,
    mcp_servers_from_map,
    read_frontmatter,
)

__all__ = ["TRANSLATOR_VERSION", "TranslationError", "translate_codex"]

KIND = "codex"
TRANSLATOR_VERSION = "1.0.0"

MANIFEST_REL = Path(".codex-plugin") / "plugin.json"
SKILLS_DIRNAME = "skills"
SKILL_FILENAME = "SKILL.md"
MCP_REL = ".mcp.json"
APPS_REL = ".app.json"


def translate_codex(source_dir: str | Path) -> PluginIR:
    """把 Codex 插件目录翻译成 IR（只读）。

    Args:
        source_dir: Codex 插件目录（绝对路径）。

    Raises:
        TranslationError: 源目录/清单/技能/MCP/apps 任一不可解析或非法。
    """
    root = Path(source_dir).resolve()
    if not root.is_dir():
        raise TranslationError(f"源目录不存在或不是目录：{root}", "SOURCE_NOT_FOUND")
    manifest_path = root / MANIFEST_REL
    if not manifest_path.is_file():
        raise TranslationError(f"缺 Codex 插件清单（非 Codex 插件目录）：{manifest_path}", "MANIFEST_MISSING")
    manifest = load_json_object(manifest_path, error_code="MANIFEST_INVALID", label="插件清单")
    name = str(manifest.get("name") or "").strip()
    if not name:
        raise TranslationError(f"插件清单缺 name：{manifest_path}", "MANIFEST_INVALID")
    return PluginIR(
        kind=KIND,
        translator_version=TRANSLATOR_VERSION,
        source_dir=root,
        name=name,
        version=str(manifest.get("version") or "").strip(),
        description=str(manifest.get("description") or "").strip(),
        author=author_name(manifest.get("author")),
        license=license_value(manifest.get("license")),
        skills=tuple(_translate_skills(root)),
        mcp_servers=tuple(_translate_mcp(root)),
        apps=tuple(_translate_apps(root)),
    )


def _translate_skills(root: Path) -> list[SkillAsset]:
    """``skills/<name>/SKILL.md`` 逐个成技能项（无 SKILL.md 的条目不是技能）。"""
    skills_root = root / SKILLS_DIRNAME
    if not skills_root.is_dir():
        return []
    skills: list[SkillAsset] = []
    for entry in sorted(skills_root.iterdir(), key=lambda path: path.name):
        if not entry.is_dir() or not (entry / SKILL_FILENAME).is_file():
            continue
        front = read_frontmatter(entry / SKILL_FILENAME, error_code="SKILL_INVALID", label="SKILL.md")
        declared_name = front.get("name")
        name = declared_name.strip() if isinstance(declared_name, str) else ""
        declared_desc = front.get("description")
        skills.append(
            SkillAsset(
                skill_id=name or entry.name,
                name=name,
                description=declared_desc.strip() if isinstance(declared_desc, str) else "",
                source_dir=entry.resolve(),
            )
        )
    return skills


def _translate_mcp(root: Path) -> list[McpServer]:
    """``.mcp.json`` 的服务器事实位（transport + 是否带 OAuth 块）；文件缺失 = 无。"""
    path = root / MCP_REL
    if not path.is_file():
        return []
    servers = mcp_server_map(path, allow_top_level=KIND in MCP_TOP_LEVEL_KINDS)
    return mcp_servers_from_map(servers, path=path)


def _translate_apps(root: Path) -> list[AppEntry]:
    """``.app.json`` 的应用指针（宿主托管，本仓不可移植）；文件缺失 = 无。"""
    path = root / APPS_REL
    if not path.is_file():
        return []
    doc = load_json_object(path, error_code="APPS_INVALID", label=".app.json")
    apps = doc.get("apps")
    if not isinstance(apps, dict):
        raise TranslationError(f".app.json 缺 apps 映射：{path}", "APPS_INVALID")
    entries: list[AppEntry] = []
    for name in sorted(apps):
        entry = apps[name]
        app_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(app_id, str) or not app_id.strip():
            raise TranslationError(f".app.json 应用 {name} 缺 id：{path}", "APPS_INVALID")
        entries.append(AppEntry(name=str(name), app_id=app_id.strip()))
    return entries
