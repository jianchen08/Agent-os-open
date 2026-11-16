"""Claude Code 清单翻译器（B 批前端）——纯函数：只读文件、零写入、零子进程。

输入 = Claude Code 三种实体形态（字段与目录约定按官方最新规范，
code.claude.com/docs 的 plugins/manifest-reference、plugins/marketplace-reference、
plugins/components；凭证见台账 R4）::

    形态 1 插件目录
      <plugin>/.claude-plugin/plugin.json   # name/version/description/author/license
      <plugin>/skills/<name>/SKILL.md       # 默认技能位（一级扫描）
      <plugin>/agents/**/*.md               # 事实面：子代理（本批不导入）
      <plugin>/commands/**/*.md             # 事实面：命令（本批不导入）
      <plugin>/hooks/hooks.json             # 事实面：钩子事件（本批不导入）
      <plugin>/.mcp.json                    # MCP 服务器事实位

    形态 2 市场条目
      <root>/.claude-plugin/marketplace.json  # 条目内联 skills[] 声明技能路径
      ——插件本体 = 条目 ``source`` 指向的目录（``"./"``/``"."`` = 市场根）；
      条目为清单（要求该目录无自有 plugin.json）；声明路径逐项解析，
      每项 = 技能目录（自身含 SKILL.md）或技能容器目录（``*/SKILL.md``）。
      未声明路径（含仓库内其他 SKILL.md）不进入翻译结果。

    形态 3 裸技能目录
      <root>/<name>/SKILL.md                # 无任何清单；翻译单位 = 该根目录，
      身份（name/version/author）按磁盘如实回落（目录名 / 空值）。

输出 = IR（:mod:`import_ir`，纯数据）。agents/commands/hooks 只作为事实上报
（**不得**变成可落地技能）；``.mcp.json`` 只带 transport/requires_oauth 事实位
（``url``/``command``/``env``/``oauth`` 内容不入 IR）。对面代码一行不执行。

坏件与旧形态一律 fail-closed 显式报错（:class:`TranslationError` 带机器可读
code）：旧形态 = 根级清单（无 ``.claude-plugin/`` 前缀），本层只支持最新规范
布局、不写兼容分支；其余坏件见各读取点。

形态 3 的落地审查锚：``skill_landing.audit_skill`` 按技能目录**真实树**上溯最近的
``skills/`` 祖先定位（形态 3 的技能目录落在 ``<skills 根>/<名>`` 即命中）；上溯不到该
段的布局拒落（``AUDIT_ANCHOR_UNSUPPORTED``），不静默放过声明面。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from import_ir import AgentAsset, CommandAsset, McpServer, PluginIR, SkillAsset
from translator_common import (
    MCP_TOP_LEVEL_KINDS,
    TranslationError,
    author_name,
    license_value,
    load_json_object,
    mcp_server_map,
    mcp_servers_from_map,
    read_frontmatter,
    read_frontmatter_best_effort,
)

__all__ = ["TRANSLATOR_VERSION", "translate_claude_code"]

KIND = "claude_code"
TRANSLATOR_VERSION = "1.0.0"

PLUGIN_MANIFEST_REL = Path(".claude-plugin") / "plugin.json"
MARKETPLACE_REL = Path(".claude-plugin") / "marketplace.json"
LEGACY_ROOT_MANIFESTS = ("marketplace.json", "plugin.json")
SKILLS_DIRNAME = "skills"
SKILL_FILENAME = "SKILL.md"
AGENTS_DIRNAME = "agents"
COMMANDS_DIRNAME = "commands"
HOOKS_REL = Path("hooks") / "hooks.json"
MCP_REL = ".mcp.json"

# 源目录/仓库根可识别的许可证文件名（小写比对；只作事实位上报，不参与判定）
_LICENSE_FILENAMES = frozenset({"license", "license.md", "license.txt"})


def translate_claude_code(source_dir: str | Path, *, entry: str | None = None) -> PluginIR:
    """把 Claude Code 实体目录翻译成 IR（只读；形态自动识别）。

    Args:
        source_dir: 插件目录 / 市场根 / 裸技能根（绝对路径）。
        entry: 形态 2 的市场条目名；None = 取第一条。其余形态忽略该参数。

    Raises:
        TranslationError: 形态不可识别（含旧形态）/ 清单与技能路径非法 / 坏件。
    """
    root = Path(source_dir).resolve()
    if not root.is_dir():
        raise TranslationError(f"源目录不存在或不是目录：{root}", "SOURCE_NOT_FOUND")
    if (root / PLUGIN_MANIFEST_REL).is_file():
        return _translate_plugin_dir(root)
    if (root / MARKETPLACE_REL).is_file():
        return _translate_marketplace(root, entry)
    bare_skills = _skills_in_container(root)
    if bare_skills:
        return _build_ir(root, name=root.name, version="", description="", author="", license=None, skills=bare_skills)
    raise _unrecognized_layout(root)


def _unrecognized_layout(root: Path) -> TranslationError:
    """都不可识别：先如实报旧形态（根级清单），否则报无匹配布局。"""
    for legacy in LEGACY_ROOT_MANIFESTS:
        if (root / legacy).is_file():
            return TranslationError(
                f"识别到旧形态（根级 {legacy}，无 .claude-plugin/ 前缀）：本批只支持最新规范布局，旧形态不支持——{root}",
                "LEGACY_LAYOUT",
            )
    return TranslationError(
        f"无法识别的 Claude Code 布局（无 .claude-plugin/ 清单，也无 */SKILL.md 技能目录）：{root}",
        "LAYOUT_UNRECOGNIZED",
    )


# ── 形态 1：插件目录 ─────────────────────────────────────────────────────


def _translate_plugin_dir(root: Path) -> PluginIR:
    manifest_path = root / PLUGIN_MANIFEST_REL
    manifest = load_json_object(manifest_path, error_code="MANIFEST_INVALID", label="插件清单")
    name = _declared_str(manifest.get("name"))
    if not name:
        raise TranslationError(f"插件清单缺 name：{manifest_path}", "MANIFEST_INVALID")
    if "skills" in manifest:
        # 最新规范的 skills 键是「追加扫描目录」语义，本批只支持默认 skills/ 位；
        # 静默漏掉声明的技能目录比拒收更糟，故 fail-closed 明示。
        raise TranslationError(
            f"插件清单声明了 skills 键（追加扫描目录语义）：本批只支持默认 skills/ 布局，"
            f"请改用默认位或直接导入对应技能目录——{manifest_path}",
            "SKILLS_KEY_UNSUPPORTED",
        )
    return _build_ir(
        root,
        name=name,
        version=_declared_str(manifest.get("version")),
        description=_declared_str(manifest.get("description")),
        author=author_name(manifest.get("author")),
        license=license_value(manifest.get("license")),
        skills=_skills_in_container(root / SKILLS_DIRNAME),
        manifest_hooks=manifest.get("hooks"),
    )


# ── 形态 2：市场条目 ─────────────────────────────────────────────────────


def _translate_marketplace(root: Path, entry: str | None) -> PluginIR:
    marketplace_path = root / MARKETPLACE_REL
    doc = load_json_object(marketplace_path, error_code="MARKETPLACE_INVALID", label="市场清单")
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        raise TranslationError(f"市场清单缺 plugins 数组：{marketplace_path}", "MARKETPLACE_INVALID")
    selected = _select_entry(plugins, entry, marketplace_path)
    entry_name = _declared_str(selected.get("name"))
    if not entry_name:
        raise TranslationError(f"市场条目缺 name：{marketplace_path}", "MARKETPLACE_INVALID")
    plugin_root = _resolve_entry_source(root, selected, entry_name, marketplace_path)
    if (plugin_root / PLUGIN_MANIFEST_REL).is_file():
        raise TranslationError(
            f"市场条目 {entry_name} 的 source 指向自带清单的插件目录（条目非清单本体）："
            f"请直接以该目录为 source_dir 导入（形态 1 插件目录）——{plugin_root}",
            "ENTRY_SELF_MANIFESTED",
        )
    return _build_ir(
        plugin_root,
        name=entry_name,
        version=_declared_str(selected.get("version")),
        description=_declared_str(selected.get("description")),
        author=author_name(selected.get("author")),
        license=license_value(selected.get("license")),
        skills=_entry_skills(plugin_root, selected, entry_name),
        manifest_hooks=selected.get("hooks"),
    )


def _select_entry(plugins: list[Any], entry: str | None, marketplace_path: Path) -> dict[str, Any]:
    """按名选条目（None = 第一条）；条目名不存在 → ENTRY_NOT_FOUND 并列出可用名。"""
    entries = [item for item in plugins if isinstance(item, dict)]
    if not entries:
        raise TranslationError(f"市场清单没有任何条目对象：{marketplace_path}", "MARKETPLACE_INVALID")
    if entry is None:
        return entries[0]
    names = [(_declared_str(item.get("name")) or "<无名>") for item in entries]
    for item in entries:
        if _declared_str(item.get("name")) == entry:
            return item
    raise TranslationError(
        f"市场清单无此条目：{entry!r}（可用条目：{'、'.join(names)}）——{marketplace_path}", "ENTRY_NOT_FOUND"
    )


def _resolve_entry_source(root: Path, selected: dict[str, Any], entry_name: str, marketplace_path: Path) -> Path:
    """解析条目 source 为本地插件根：只认相对路径字符串（``./…`` 或 ``.``）。

    非本地形态（对象型 source 如 github/url、裸名需 metadata.pluginRoot）一律
    fail-closed 明示——本层不联网、不猜路径。
    """
    raw = selected.get("source")
    if isinstance(raw, dict):
        raise TranslationError(
            f"市场条目 {entry_name} 的 source 非本地相对路径（对象型 source={raw.get('source')!r}）："
            f"本批只支持本地实体——{marketplace_path}",
            "SOURCE_NOT_LOCAL",
        )
    if not isinstance(raw, str) or not raw.strip():
        raise TranslationError(
            f"市场条目 {entry_name} 缺 source（或非字符串）：{marketplace_path}", "MARKETPLACE_INVALID"
        )
    source = raw.strip()
    if not source.startswith("./") and source != ".":
        raise TranslationError(
            f"市场条目 {entry_name} 的 source 非法（须以 ./ 开头；裸名需 metadata.pluginRoot，本批不支持）："
            f"{source!r}——{marketplace_path}",
            "SOURCE_INVALID",
        )
    if ".." in Path(source).parts:
        raise TranslationError(f"市场条目 {entry_name} 的 source 含路径穿越：{source!r}", "SOURCE_INVALID")
    plugin_root = root if source == "." else root / source[2:]
    plugin_root = plugin_root.resolve()
    if not plugin_root.is_dir():
        raise TranslationError(f"市场条目 {entry_name} 的 source 目录不存在：{plugin_root}", "SOURCE_NOT_FOUND")
    return plugin_root


def _entry_skills(plugin_root: Path, selected: dict[str, Any], entry_name: str) -> list[SkillAsset]:
    """条目技能面：``skills`` 声明为路径或路径数组（缺省 = 默认 ``skills/`` 位）。"""
    declared = selected.get("skills")
    if declared is None:
        return _skills_in_container(plugin_root / SKILLS_DIRNAME)
    items = declared if isinstance(declared, list) else [declared]
    skills: list[SkillAsset] = []
    for item in items:
        skills.extend(_skills_from_declared_path(plugin_root, item, entry_name))
    return skills


def _skills_from_declared_path(plugin_root: Path, item: Any, entry_name: str) -> list[SkillAsset]:
    """解析一条声明技能路径：技能目录（自身含 SKILL.md）或技能容器目录。"""
    if not isinstance(item, str) or not item.strip():
        raise TranslationError(f"市场条目 {entry_name} 的技能声明路径非字符串：{item!r}", "SKILL_PATH_INVALID")
    declared = item.strip()
    if not declared.startswith("./") and declared != ".":
        raise TranslationError(
            f"市场条目 {entry_name} 的技能声明路径须以 ./ 开头（规范形态）：{declared!r}", "SKILL_PATH_INVALID"
        )
    if ".." in Path(declared).parts:
        raise TranslationError(f"市场条目 {entry_name} 的技能声明路径含路径穿越：{declared!r}", "SKILL_PATH_INVALID")
    target = plugin_root if declared == "." else (plugin_root / declared[2:])
    target = target.resolve()
    if not target.exists():
        raise TranslationError(
            f"市场条目 {entry_name} 的技能声明路径不存在：{declared!r}（{plugin_root}）", "SKILL_PATH_NOT_FOUND"
        )
    if not target.is_dir():
        raise TranslationError(f"市场条目 {entry_name} 的技能声明路径不是目录：{declared!r}", "SKILL_PATH_INVALID")
    if (target / SKILL_FILENAME).is_file():
        return [_skill_asset(target)]
    found = _skills_in_container(target)
    if not found:
        raise TranslationError(
            f"市场条目 {entry_name} 的技能声明路径下无技能（无 SKILL.md）：{declared!r}", "SKILL_PATH_EMPTY"
        )
    return found


# ── 形态 3：裸技能目录 ───────────────────────────────────────────────────


def _skills_in_container(container: Path) -> list[SkillAsset]:
    """一级子目录中带 SKILL.md 者逐个成技能项（其余条目跳过，不构成技能）。"""
    if not container.is_dir():
        return []
    skills: list[SkillAsset] = []
    for entry in sorted(container.iterdir(), key=lambda path: path.name):
        if entry.is_dir() and (entry / SKILL_FILENAME).is_file():
            skills.append(_skill_asset(entry))
    return skills


def _skill_asset(skill_dir: Path) -> SkillAsset:
    """技能项：skill_id = frontmatter ``name``，缺失回落磁盘目录名（声明面如实置空）。"""
    front = read_frontmatter(skill_dir / SKILL_FILENAME, error_code="SKILL_INVALID", label="SKILL.md")
    name = _declared_str(front.get("name"))
    return SkillAsset(
        skill_id=name or skill_dir.name,
        name=name,
        description=_declared_str(front.get("description")),
        source_dir=skill_dir.resolve(),
    )


# ── 共用事实面（三形态同扫，只上报不落地）────────────────────────────────


def _build_ir(
    root: Path,
    *,
    name: str,
    version: str,
    description: str,
    author: str,
    license: str | None,
    skills: list[SkillAsset],
    manifest_hooks: Any = None,
) -> PluginIR:
    hooks_declared, hooks_events = _hook_facts(root, manifest_hooks)
    return PluginIR(
        kind=KIND,
        translator_version=TRANSLATOR_VERSION,
        source_dir=root,
        name=name,
        version=version,
        description=description,
        author=author,
        license=license,
        skills=tuple(skills),
        mcp_servers=tuple(_mcp_facts(root)),
        agents=tuple(_agent_facts(root)),
        commands=tuple(_command_facts(root)),
        hooks_declared=hooks_declared,
        hooks_events=hooks_events,
        license_file=_license_file(root),
    )


def _agent_facts(root: Path) -> list[AgentAsset]:
    """``agents/**/*.md`` 的子代理事实（子目录参与，与 Claude 装载语义一致）。"""
    agents_dir = root / AGENTS_DIRNAME
    if not agents_dir.is_dir():
        return []
    facts: list[AgentAsset] = []
    for path in sorted(item for item in agents_dir.rglob("*.md") if item.is_file()):
        front = read_frontmatter_best_effort(path)
        facts.append(
            AgentAsset(
                name=_declared_str(front.get("name")) or path.stem,
                model=_declared_str(front.get("model")),
                tools_count=_tools_count(front.get("tools")),
            )
        )
    return facts


def _command_facts(root: Path) -> list[CommandAsset]:
    """``commands/**/*.md`` 的命令事实（name = 相对路径去 ``.md``）。"""
    commands_dir = root / COMMANDS_DIRNAME
    if not commands_dir.is_dir():
        return []
    return [
        CommandAsset(name=path.relative_to(commands_dir).with_suffix("").as_posix())
        for path in sorted(item for item in commands_dir.rglob("*.md") if item.is_file())
    ]


def _hook_facts(root: Path, manifest_hooks: Any) -> tuple[bool, tuple[str, ...]]:
    """钩子声明事实：是否声明 + 事件名清单。

    ``hooks/hooks.json`` 须带顶层 ``hooks`` 包裹层（规范要求；缺层 = 坏件，
    该文件在对面本就装载失败）。事件名另收清单内联对象形态（事件映射本体）；
    清单内以文件路径/数组转指的形态只记「已声明」，不跟读其内容（钩子面
    本批不导入）。
    """
    declared = False
    events: set[str] = set()
    hooks_path = root / HOOKS_REL
    if hooks_path.is_file():
        doc = load_json_object(hooks_path, error_code="HOOKS_INVALID", label="钩子清单")
        event_map = doc.get("hooks")
        if not isinstance(event_map, dict):
            raise TranslationError(f"钩子清单缺顶层 hooks 映射（规范包裹层）：{hooks_path}", "HOOKS_INVALID")
        declared = True
        events |= set(event_map)
    if isinstance(manifest_hooks, dict):
        declared = True
        events |= set(manifest_hooks)
    elif manifest_hooks is not None:
        declared = True
    return declared, tuple(sorted(events))


def _mcp_facts(root: Path) -> list[McpServer]:
    """``.mcp.json`` 服务器事实位；两种规范形态（``mcpServers`` 包裹 / 顶层映射）同语义。"""
    path = root / MCP_REL
    if not path.is_file():
        return []
    servers = mcp_server_map(path, allow_top_level=KIND in MCP_TOP_LEVEL_KINDS)
    return mcp_servers_from_map(servers, path=path)


def _license_file(root: Path) -> str | None:
    """许可证文件事实位：源目录优先，其次所属仓库根（最近带 .git 的祖先）；无 = None。"""
    found = _license_in(root)
    if found is not None:
        return found
    for parent in root.parents:
        if (parent / ".git").exists():
            return _license_in(parent)
    return None


def _license_in(directory: Path) -> str | None:
    """目录下的许可证文件名（小写比对）；调用点保证传入的是已存在的目录。"""
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        if child.is_file() and child.name.casefold() in _LICENSE_FILENAMES:
            return child.name
    return None


def _tools_count(raw: Any) -> int:
    """``tools`` 字段的条目数（列表 = 元素数；字符串 = 逗号分隔非空段数）。"""
    if isinstance(raw, list):
        return sum(1 for item in raw if str(item).strip())
    if isinstance(raw, str):
        return sum(1 for token in raw.split(",") if token.strip())
    return 0


def _declared_str(raw: Any) -> str:
    """声明字段归一为去空白字符串（非字符串形态按空串处理，如实反映声明面）。"""
    return raw.strip() if isinstance(raw, str) else ""
