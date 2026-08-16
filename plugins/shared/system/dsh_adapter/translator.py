"""DSH 插件包清单翻译器（task_dsh_plugin_adapter 任务 2）。

把一个 DSH 插件包（源码目录）翻译成灵汐可用的注册清单——纯函数、零副作用，
可离线运行也可经 ``dsh_translate_manifest`` 工具在线调用。

翻译边界（诚实声明）：
- **前端部分**：解析 package.json 的 ``dsh.*`` 声明 + 扫描 client 源码中的
  ``slots.register``（toolview 键）→ 产出灵汐 ``contributes.renderers`` 等价物。
  组件本体不在此翻译——视觉组件走 vendor 移植层（任务 3，锁定 commit 复制）。
- **后端部分**：不做 TS 源码静态解析（defineTool 的 schema DSL 在运行时才
  完整）。工具契约来自两条通道：(a) 通道 A——runtime 桥 ``initialize`` 时
  的运行时自省（bridge.list_tools()）；(b) 锁定契约表——翻译器把 (a) 的
  输出固化进 plugin.json（本仓库 dsh_read/dsh_glob 即此产物，DSH commit
  47f9438 锁定）。
- **MCP 工具**：不在适配器范围（external_mcp 天然直连），翻译器明确标记。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

DSH_SOURCE_COMMIT = "47f943859bef60e4160492346772ded9b24f765a"
DSH_SOURCE_VERSION = "0.1.0-rc.5"

# toolview 注册形态：ctx.slots.register({ name: 'tool.call.toolview', key: 'read', ... }, ReadRow)
_SLOT_REGISTER_RE = re.compile(
    r"slots\.register\(\s*\{[^}]*?name:\s*['\"]tool\.call\.toolview['\"][^}]*?key:\s*['\"]([\w.-]+)['\"]",
    re.DOTALL,
)
# MCP 型插件标记：dsh.host 声明 mcp transport / 包名含 mcp
_MCP_HINT_RE = re.compile(r"mcp", re.IGNORECASE)

# ── DSH client 槽位 → 灵汐 UI 槽位映射表（界面语义翻译的单一事实源） ────────
#
# DSH 的 client 插件经 slots 注册 UI 表面（conversation.* / tool.call.* /
# settings.* 等）。灵汐前端按 contributes 声明渲染（viewsContainers/views/
# workspaceTabs/dockItems/floating/modal/statusBarItems/chatMessages/
# chatInteractions/chatActions/settingsPanels/widgets/client_styles/themes）。
# 本表把 DSH 槽位的**语义**翻译成灵汐槽位——侧边栏→侧边栏、输入区→输入区、
# 工具卡→聊天消息卡、详情→浮窗；灵汐无对应槽位的标记 direct（直接渲染，
# 诚实边界，不强行归并）。
DSH_SLOT_LINGXI_MAP: dict[str, dict[str, str]] = {
    "tool.call.toolview": {"lingxi_slot": "chatMessages", "note": "工具结果卡 → 聊天消息卡样式（ActivityCard dsh:* 分支渲染）"},
    "conversation.chat.node": {"lingxi_slot": "chatMessages", "note": "聊天消息节点 → 聊天消息卡"},
    "conversation.details.tool": {"lingxi_slot": "floating", "note": "工具详情面板 → 浮窗/详情浮层"},
    "conversation.composer": {"lingxi_slot": "chatInteractions", "note": "聊天输入区 → 聊天交互模式"},
    "conversation.composer.dock": {"lingxi_slot": "dockItems", "note": "输入区 dock → 底部 dock 栏"},
    "conversation.input.dock": {"lingxi_slot": "dockItems", "note": "输入区 dock → 底部 dock 栏"},
    "settings.general.item": {"lingxi_slot": "settingsPanels", "note": "设置项 → 插件设置面板"},
}
_DSH_SLOT_FALLBACK: dict[str, str] = {
    "lingxi_slot": "direct",
    "note": "灵汐无对应槽位 → 直接渲染（不强行归并）",
}


def map_dsh_slot(slot_name: str) -> dict[str, str]:
    """DSH slot 名 → 灵汐槽位（未收录回退 direct = 直接渲染）。"""
    return DSH_SLOT_LINGXI_MAP.get(slot_name, dict(_DSH_SLOT_FALLBACK))


# slots.register/inject 的 name 声明（任意槽位名，不限于 toolview）
_SLOT_NAME_RE = re.compile(
    r"slots\.(?:register|inject)\([^)]*?name:\s*['\"]([\w.]+)['\"]",
    re.DOTALL,
)

# ── DSH toolview 键 → 灵汐渲染组件映射表（前端组件翻译的单一事实源） ────────
#
# DSH ui-tool 的每个 toolview 键（slots.register key）对应一个 DSH 卡片原语
# （ui-primitives 组件）。灵汐侧的等价物 = vendor 移植组件 + render 意图卡：
# 本表把「DSH 键 → DSH 卡片原语 → 灵汐组件 → render card」三段链一次写清。
# 组件本体在 frontend/src/components/vendor/dsh/（锁定 commit 复制），
# 渲染路由在 frontend/src/utils/dshRenderIntent.ts（payload 构造器）。
# 新增映射 = 改本表 + 对应 payload 构造器，两处同步（有防漂移测试）。
DSH_TOOLVIEW_COMPONENT_MAP: dict[str, dict[str, str]] = {
    # DSH 键: {dsh_component: DSH 卡片原语, lingxi_component: vendor 组件, card: render 意图}
    "read": {"dsh_component": "ReadBlock", "lingxi_component": "ReadBlock", "card": "read"},
    "bash": {"dsh_component": "TerminalBlock", "lingxi_component": "TerminalBlock", "card": "terminal"},
    "web_search": {"dsh_component": "WebBlock(search)", "lingxi_component": "WebBlock", "card": "web"},
    "web_fetch": {"dsh_component": "WebBlock(fetch)", "lingxi_component": "WebBlock", "card": "web"},
    "grep": {"dsh_component": "SearchBlock(matches)", "lingxi_component": "SearchBlock", "card": "search"},
    "glob": {"dsh_component": "SearchBlock(paths)", "lingxi_component": "SearchBlock", "card": "search"},
    "edit": {"dsh_component": "DiffBlock", "lingxi_component": "DiffBlock", "card": "diff"},
    "write": {"dsh_component": "DiffBlock", "lingxi_component": "DiffBlock", "card": "diff"},
    "todo_write": {"dsh_component": "JsonTree", "lingxi_component": "JsonTree", "card": "generic"},
    "ask_user_question": {"dsh_component": "GenericToolCard", "lingxi_component": "ActivityCard(默认)", "card": "generic"},
    # 未列出的键（未来 DSH 新增 toolview）→ generic 兜底，不拒绝翻译
}
_DSH_FALLBACK_MAPPING: dict[str, str] = {
    "dsh_component": "GenericToolCard",
    "lingxi_component": "ActivityCard(默认)",
    "card": "generic",
}


def map_toolview_to_component(toolview_key: str) -> dict[str, str]:
    """DSH toolview 键 → 灵汐组件映射（未收录键回退 generic 兜底）。"""
    return DSH_TOOLVIEW_COMPONENT_MAP.get(toolview_key, dict(_DSH_FALLBACK_MAPPING))


def dsh_params_to_json_schema(params: dict[str, Any] | None) -> dict[str, Any]:
    """DSH parameters DSL → JSON Schema（与 runtime 桥 mjs 侧同构）。

    DSL 形态：``{field: {type, required?, description?}}``。
    """
    if not params:
        return {"type": "object", "properties": {}}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key, spec in params.items():
        if not isinstance(spec, dict):
            continue
        prop: dict[str, Any] = {"type": spec.get("type", "string")}
        if spec.get("description") is not None:
            prop["description"] = spec["description"]
        properties[key] = prop
        if spec.get("required") is True:
            required.append(key)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def to_lingxi_tool_entry(tool: dict[str, Any]) -> dict[str, Any]:
    """DSH 运行时工具契约（bridge.list_tools() 条目）→ 灵汐 capabilities.tools 条目。

    render 意图按 DSH presentResult 词汇表人工判定（运行时自省拿不到
    presentation 回调的输出形态，卡类型是工具作者的语义决定）。
    """
    entry: dict[str, Any] = {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("input_schema") or dsh_params_to_json_schema(tool.get("parameters")),
    }
    if tool.get("output_schema"):
        entry["output_schema"] = tool["output_schema"]
    if tool.get("render"):
        entry["render"] = tool["render"]
    if tool.get("category"):
        entry["category"] = tool["category"]
    return entry


def translate_package(package_dir: str | Path) -> dict[str, Any]:
    """翻译一个 DSH 插件包目录 → 灵汐等价注册清单。

    失败隔离：单个源文件解析异常跳过并记入 ``warnings``，不影响整体翻译。
    """
    root = Path(package_dir)
    warnings: list[str] = []

    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        raise FileNotFoundError(f"not a DSH package (no package.json): {root}")
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"bad package.json in {root}: {e}") from e

    dsh_decl = pkg.get("dsh") if isinstance(pkg.get("dsh"), dict) else {}
    client_decl = dsh_decl.get("client") if isinstance(dsh_decl.get("client"), dict) else {}
    platform = client_decl.get("platform") if isinstance(client_decl, dict) else None
    inject = client_decl.get("inject", []) if isinstance(client_decl, dict) else []

    # 前端表面：client 入口扫描——DSH 槽位（slots.register 的 name 域）与
    # toolview 键（tool.call.toolview 的 key 域）。两种来源：(a) 源码目录
    # src/client/**/*.ts*（仓库检出）；(b) npm 构建产物 lib/*.js（npm pack
    # 下载包无 src——CSS 被 stub、slots.register 调用保留）。
    renderers: list[dict[str, Any]] = []
    slots: dict[str, dict[str, Any]] = {}  # DSH 槽位名 → {lingxi_slot, note, sources}
    scan_targets: list[tuple[Path, str]] = []
    if (root / "src" / "client").is_dir():
        scan_targets = [
            (src, f"src/client/{src.relative_to(root / 'src' / 'client')}")
            for src in sorted((root / "src" / "client").rglob("*.ts*"))
        ]
    elif (root / "lib").is_dir():
        scan_targets = [(f, f"lib/{f.name}") for f in sorted((root / "lib").glob("*.js"))]
    for path, rel in scan_targets:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            warnings.append(f"unreadable source {path.name}: {e}")
            continue
        # 槽位名（任意 DSH slot）→ 灵汐槽位映射（未收录回退 direct = 直接渲染）
        for slot_name in _SLOT_NAME_RE.findall(text):
            entry = slots.setdefault(slot_name, {**map_dsh_slot(slot_name), "sources": []})
            if rel not in entry["sources"]:
                entry["sources"].append(rel)
        # toolview 键（tool.call.toolview 的 key 域）→ 渲染组件映射
        for key in _SLOT_REGISTER_RE.findall(text):
            renderers.append({
                "tool": key,
                "source": rel,
                # 组件映射：DSH 键 → 灵汐渲染组件（单一事实源见模块头部映射表）
                **map_toolview_to_component(key),
            })

    manifest: dict[str, Any] = {
        "source": {
            "package": pkg.get("name", root.name),
            # 包自身版本（npm 下载 = 发布版本；源码检出 = 仓库版本）
            "version": pkg.get("version"),
            "kind": "dsh-plugin",
            "dsh": {
                # vendor 移植层（components/vendor/dsh/）锁定的基线——翻译对象
                # 与基线不同版时，组件等价性需按 vendor README 的升级路径核对
                "vendor_pinned": {"commit": DSH_SOURCE_COMMIT, "package_version": DSH_SOURCE_VERSION},
                "client_platform": platform,
                "client_inject": inject,
            },
        },
        "client": {
            "is_client_plugin": bool(client_decl),
            "slots": slots,
            "renderers": renderers,
            # 功能型包（依赖 DSH 事件投影服务）不适配——诚实边界
            "adapter_scope": "visual-only" if renderers else "none",
        },
        "backend": {
            # 静态翻译不产工具契约：通道 A（runtime 自省）或锁定契约表提供
            "tools_channel": "runtime-introspection",
            "mcp_tools": "out-of-scope (external_mcp direct)",
        },
        "warnings": warnings,
    }
    return manifest


def translate_packages(package_dirs: list[Path] | list[str | Path]) -> dict[str, Any]:
    """批量翻译（失败隔离：单包失败计入 errors，不中断其他包）。"""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for d in package_dirs:
        try:
            results.append(translate_package(d))
        except (FileNotFoundError, ValueError) as e:
            errors.append({"package": str(d), "error": str(e)})
    return {"packages": results, "errors": errors}


def discover_dsh_plugins(base_dir: str | Path | None = None) -> list[Path]:
    """发现适配器 ``dsh_plugins/`` 下已放置的 DSH 插件包目录。

    规则：直接子目录含 package.json 即认作插件包（npm 解压物 / 源码检出
    同形态）；``_`` 前缀目录（如 _README）跳过。DSH 插件全部放在适配器
    下面由适配器自己加载——新增插件 = 放一个子目录，零适配器代码改动。
    """
    base = Path(base_dir) if base_dir is not None else Path(__file__).parent / "dsh_plugins"
    if not base.is_dir():
        return []
    return sorted(
        d for d in base.iterdir()
        if d.is_dir() and not d.name.startswith("_") and (d / "package.json").is_file()
    )


def _project_root() -> str:
    """定位项目根：优先 AGENTOS_PROJECT_ROOT，回退上溯找 config/ 目录。"""
    root = os.environ.get("AGENTOS_PROJECT_ROOT")
    if root and os.path.isdir(root):
        return root
    cur = Path.cwd()
    for _ in range(6):
        if (cur / "config").is_dir():
            return str(cur)
        parent = cur.parent
        if parent == cur:
            break
    return str(Path.cwd())


def load_plugin_config() -> dict[str, Any]:
    """读适配器配置（config/dsh_adapter.yaml）的 ``plugins`` 映射。

    配置是 DSH 插件装载管理：``{包目录名: {enabled: bool}}``。读失败或
    缺文件返回空 dict（语义 = 全部默认启用，不因配置问题阻塞装载）。
    """
    cfg_path = Path(_project_root()) / "config" / "dsh_adapter.yaml"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    return plugins if isinstance(plugins, dict) else {}


def _plugin_enabled(name: str, config: dict[str, Any]) -> bool:
    """按配置判定包是否启用：未列出默认启用；``{enabled: bool}`` 或裸 bool 均认。"""
    entry = config.get(name)
    if isinstance(entry, dict):
        return bool(entry.get("enabled", True))
    if isinstance(entry, bool):
        return entry
    return True


def load_installed_plugins() -> dict[str, Any]:
    """扫描 dsh_plugins/ 并按配置过滤后批量翻译（适配器装载入口）。

    Returns:
        translate_packages 输出 + ``base_dir``（装载位置记录）+ ``count``
        （已启用装载数）+ ``disabled``（被配置禁用的包目录名列表）。
    """
    packages = discover_dsh_plugins()
    config = load_plugin_config()
    enabled = [p for p in packages if _plugin_enabled(p.name, config)]
    out: dict[str, Any] = translate_packages(enabled)
    out["base_dir"] = str(Path(__file__).parent / "dsh_plugins")
    out["count"] = len(enabled)
    out["disabled"] = [p.name for p in packages if not _plugin_enabled(p.name, config)]
    return out
