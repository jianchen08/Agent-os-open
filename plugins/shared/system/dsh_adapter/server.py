#!/usr/bin/env python3
"""DSH 插件适配器 MCP 服务端（task_dsh_plugin_adapter 任务 2 + 4）。

三个工具面：
- ``dsh_read`` / ``dsh_glob``：通道 A 桥接——DSH 非 MCP 工具跑在自己的
  Node runtime（runtime/dsh-rpc-bridge.mjs 管理的 DSH cordis context），
  本 sidecar 只作宿主 + JSON-RPC 桥。工具契约（input/output schema +
  render 意图）在 plugin.json 锁定（DSH commit 47f9438），tool_core 按
  output_schema 校验返回、前端按 render 意图路由渲染——闭环即任务 1 的
  消费端。
- ``dsh_translate_manifest``：清单翻译器出口（translator.translate_package）。

生命周期：on_unload 时 shutdown Node 子进程（防孤儿）；桥不可用时工具
返回结构化错误（fail-soft，不影响本插件其他工具）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bridge import get_bridge as _get_bridge, shutdown_bridge
from translator import (
    SKIN_ASSET_EXTS,
    SKIN_CENTER_SKINS_DIR,
    discover_dsh_plugins,
    list_available_skins,
    load_installed_plugins,
    load_plugin_config,
    plugin_enabled,
    translate_package,
)

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("dsh_adapter")

# ── 外部工具包装载区（通道 A 扩展：dsh_plugins/ 工具包桥接） ──────────
# 布局：runtime/extra-tools/node_modules/@deepseek-ai/ 同时放 peer 基础包
# （junction → DSH repo 构建产物，零拷贝零漂移）与启用的工具包（junction →
# dsh_plugins/ 下含 lib/index.js 的包）。ESM 解析链：包内 import
# '@deepseek-ai/dsh-tools' 从包目录上溯命中同目录 peer 链接。
_RUNTIME_DIR = Path(__file__).parent / "runtime"
_EXTRA_TOOLS_DIR = _RUNTIME_DIR / "extra-tools" / "node_modules" / "@deepseek-ai"
_DEFAULT_DSH_REPO = "D:/reference_repos/deepseek-harness-rc8"
_PEER_PKGS: dict[str, str] = {
    "dsh-tools": "packages/core/tools",
    "dsh-invariants": "packages/runtime-diagnostics/invariants",
}
_bridge_ready = False


def _make_junction(link: Path, target: Path) -> None:
    """建目录 junction（Windows mklink /J，Linux os.symlink 兜底）；已存在跳过。"""
    if link.exists() or link.is_symlink():
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
        )


def _sync_extra_package(dest: Path, src: Path) -> None:
    """把工具包的 lib/ + package.json 增量同步进装载区（幂等，内容变更时覆盖）。

    用拷贝而非 junction：node ESM 对链接目标做 realpath，junction 会把包
    真实位置打回 dsh_plugins/，包内 ``import '@deepseek-ai/dsh-tools'`` 的
    上溯解析就找不到装载区的 peer 链接；拷贝后包自身位于装载区，上溯恰好
    命中同目录 peer junction。
    """
    dest.mkdir(parents=True, exist_ok=True)
    if (src / "lib").is_dir():
        shutil.copytree(src / "lib", dest / "lib", dirs_exist_ok=True)
    if (src / "package.json").is_file():
        shutil.copy2(src / "package.json", dest / "package.json")


def ensure_extra_tools_layout() -> str | None:
    """把启用且含 lib/index.js 的 DSH 工具包同步进桥装载区（幂等，含清理）。

    Returns:
        装载区目录（供 bridge env 使用）；无工具包时仍返回目录（peer 链接
        就位后桥可加载后续放入的工具包）。
    """
    repo_root = os.environ.get("AGENTOS_DSH_REPO_ROOT") or _DEFAULT_DSH_REPO
    _EXTRA_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    for name, rel in _PEER_PKGS.items():
        _make_junction(_EXTRA_TOOLS_DIR / name, Path(repo_root) / rel)
    config = load_plugin_config()
    keep: set[str] = set(_PEER_PKGS)
    for pkg in discover_dsh_plugins():
        if not (pkg / "lib" / "index.js").is_file():
            continue
        if not plugin_enabled(pkg.name, config):
            continue
        _sync_extra_package(_EXTRA_TOOLS_DIR / pkg.name, pkg)
        keep.add(pkg.name)
    # 清理装载区残留（插件移除/禁用后同步删除，避免旧包继续被桥加载）
    for name in os.listdir(_EXTRA_TOOLS_DIR):
        link = _EXTRA_TOOLS_DIR / name
        if (link.is_dir() or link.is_symlink()) and name not in keep:
            try:
                shutil.rmtree(link)
            except OSError:
                # 清理是尽力而为：文件被占用/权限不足时保留残留，下次装载再试，
                # 不影响插件启用主流程。
                pass
    return str(_EXTRA_TOOLS_DIR)


def get_bridge() -> Any:
    """取共享桥（首次惰性创建时注入外部工具包装载区）。"""
    global _bridge_ready  # noqa: PLW0603
    if not _bridge_ready:
        _get_bridge(extra_plugins_dir=ensure_extra_tools_layout())
        _bridge_ready = True
    return _get_bridge()


@plugin.tool(
    name="dsh_read",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to read, resolved by the filesystem backend."},
            "offset": {"type": "integer", "description": "1-based first line to return. Defaults to 1."},
            "limit": {"type": "integer", "description": "Maximum number of lines to return. Defaults to 2000."},
        },
        "required": ["file_path"],
    },
    description="Read a UTF-8 text file and return line-numbered content (DSH runtime bridge).",
    output_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"number": {"type": "integer"}, "text": {"type": "string"}},
                    "required": ["number", "text"],
                },
            },
            "totalLines": {"type": "integer"},
        },
        "required": ["path", "offset", "lines", "totalLines"],
    },
    render={
        "card": "read",
        "bindings": {"path": "result.path", "lines": "result.lines", "totalLines": "result.totalLines"},
    },
)
async def dsh_read(file_path: str, offset: int | None = None, limit: int | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"file_path": file_path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return await get_bridge().call_tool("read", args)


@plugin.tool(
    name="dsh_glob",
    schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": 'Glob pattern to match file paths against (e.g. "**/*.ts").',
            },
            "path": {"type": "string", "description": "Directory to search in. Defaults to the bridge workspace."},
        },
        "required": ["pattern"],
    },
    description="Discover files whose paths match a glob pattern, sorted by modification time (DSH runtime bridge).",
    output_schema={
        "type": "object",
        "properties": {"root": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["root", "paths"],
    },
    render={"card": "search", "bindings": {"paths": "result.paths"}},
)
async def dsh_glob(pattern: str, path: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"pattern": pattern}
    if path is not None:
        args["path"] = path
    return await get_bridge().call_tool("glob", args)


@plugin.tool(
    name="dsh_translate_manifest",
    schema={
        "type": "object",
        "properties": {
            "package_path": {"type": "string", "description": "DSH 插件包目录（含 package.json 的源码目录；缺省 = 适配器自己的 dsh_plugins/ 全量）"}
        },
        "required": [],
    },
    description="Translate a DSH plugin package (or all installed ones under dsh_plugins/) into AgentOS-equivalent registration manifests.",
)
async def dsh_translate_manifest(package_path: str | None = None) -> dict[str, Any]:
    """单包翻译（指定路径）或全量装载翻译（缺省扫 dsh_plugins/）。"""
    if package_path is None:
        return load_installed_plugins()
    return translate_package(package_path)


@plugin.tool(
    name="dsh_list_plugins",
    schema={"type": "object", "properties": {}, "required": []},
    description="List DSH plugin packages installed under the adapter's dsh_plugins/ directory (name/version/client/renderers).",
)
async def dsh_list_plugins() -> dict[str, Any]:
    """汇报已装载的 DSH 插件包（轻量：不跑 Node runtime，纯清单翻译）。"""
    loaded = load_installed_plugins()
    return {
        "count": loaded["count"],
        "base_dir": loaded["base_dir"],
        "extra_tools_dir": str(_EXTRA_TOOLS_DIR),
        "plugins": [
            {
                "package": p["source"]["package"],
                "version": p["source"]["version"],
                "is_client_plugin": p["client"]["is_client_plugin"],
                "renderers": [r["tool"] for r in p["client"]["renderers"]],
                "adapter_scope": p["client"]["adapter_scope"],
                "extra_tools": bool(p["backend"].get("extra_tools")),
            }
            for p in loaded["packages"]
        ],
        "disabled": loaded["disabled"],
        "errors": loaded["errors"],
    }


@plugin.on_load
async def _on_dsh_adapter_load(params: dict) -> None:  # noqa: ARG001
    """装载时把皮肤自动声明为 contributes.themes（形态路由终态，零手工）。

    幂等：生成的 themes 与 manifest 现值一致则不写（避免 watcher 指纹
    抖动循环）；不一致才写回——plugin_watcher 检测 manifest 变化自动
    reenable 重注册，前端主题列表随即出现/移除皮肤主题卡。
    添加皮肤 = 放包进 dsh_plugins/，本钩子负责翻译成 PluginTheme 声明。
    """
    try:
        from translator import skins_to_plugin_themes

        themes = skins_to_plugin_themes()
        manifest_path = Path(__file__).parent / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contributes = manifest.setdefault("contributes", {})
        if contributes.get("themes") != themes:
            contributes["themes"] = themes
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            logger.info("dsh_adapter: contributes.themes auto-synced (%d skins)", len(themes))
    except Exception as e:  # noqa: BLE001 - 声明同步失败不阻断插件装载
        logger.warning("dsh_adapter: themes auto-sync failed: %s", e)


@plugin.on_unload
async def _on_dsh_adapter_unload(params: dict) -> None:  # noqa: ARG001
    await shutdown_bridge()
    logger.info("dsh_adapter: node runtime bridge shut down")


# ── DSH 皮肤按择注入服务（contributes.themes 主题管线 + CSS 注入通道） ──
# 皮肤主体（配色/背景图/基准）走 contributes.themes 主题管线原生渲染；
# 本 handler 服务皮肤全量 CSS（merged.css = skin.css + patches.css 原样合并，
# 圆角/阴影/动效/鼠标样式等以 CSS 形式生效）、hooks.mjs（DSH 原机制动态
# 效果）与背景图资产。前端经 /ext/{pluginId}{path} 拉取（带 Bearer，仅
# Enabled 插件可挂路由；dispatcher 契约：body base64 原样回写）。

# 皮肤全量 CSS 按择注入路由：插件 CSS 注入通道 + 主题路由——
# 选到哪个皮肤注入哪个；皮肤 CSS 原样搬：圆角/阴影/动效/鼠标
# 样式等全部以 CSS 形式生效，html[data-dsh-skin] 选择器由前端激活时打标
_SKIN_MERGED_PREFIX = "/ext/dsh_adapter/styles/skin/"
_SKIN_MERGED_SUFFIX = "/merged.css"
_SKIN_HOOKS_SUFFIX = "/hooks.mjs"
_SKIN_ASSET_ROUTE_PREFIX = "/ext/dsh_adapter/styles/skin-assets/"


_CONTENT_TYPES = {
    ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".svg": "image/svg+xml", ".gif": "image/gif", ".avif": "image/avif",
}


def _etag_of(payload: bytes) -> str:
    """弱 ETag（W/ 前缀）：内容协商键，命中即 304，无需字节级强校验。"""
    return '"%s"' % hashlib.sha1(payload).hexdigest()[:24]


def _revalidate(headers: dict[str, str] | None, payload: bytes) -> bool:
    """If-None-Match 命中（浏览器带上次的 ETag 回源校验）→ True 应回 304。"""
    if not headers:
        return False
    candidates: list[str] = []
    for key in ("if-none-match", "If-None-Match"):
        val = headers.get(key)
        if val:
            candidates.extend(part.strip() for part in val.split(","))
    if not candidates:
        return False
    return _etag_of(payload) in candidates


# DSH 位置词汇 → AgentOS 位置：按位置映射转译——不给灵汐
# 组件贴 DSH 名字，DSH 选择器在递送层统一翻译到我方锚点；CSS 与 hooks 同源映射表，
# 任何皮肤按同一套映射注入，非逐皮肤对应。
# 顺序敏感：结构全形在前（部分替换会残留错位结构——如只换 data-pane 部分会把
# 页脚装饰挂到整条侧栏）；target 一律我方词汇（data-region / data-testid /
# data-chat-state / role），DSH 词汇不出现在灵汐 DOM。
_DSH_POSITION_MAP: list[tuple[re.Pattern[str], str]] = [
    # ⚠️ 值形规则一律用反向引用保留原引号风格（\1 复用捕获引号）：hooks.mjs 的
    # 选择器活在 JS 字符串字面量里（双引号输出插进单引号串 =
    # SyntaxError → import 抛 → 整个动态层静默死），CSS 双单引号等价无感。
    # scope 属性翻译（平台词汇 data-skin="<plugin>:<skin>"；值形带插件前缀，
    # 裸 token 兜底覆盖 CSS 括号形/JS attributeFilter 字符串/ctx 插值/存在性）
    (re.compile(r'html\[data-dsh-skin=(["\'])([\w-]+)\1\]'), r'html[data-skin=\1dsh_adapter:\2\1]'),
    (re.compile(r'html\[data-dsh-skin=\\"([\w-]+)\\"\]'), r'html[data-skin=\\"dsh_adapter:\1\\"]'),
    (re.compile(r'data-dsh-skin'), 'data-skin'),
    # 暗色变体开关（DSH body[data-ds-dark-theme] → 平台 body[data-skin-dark]；
    # 裸 token 同时救活 hooks 对该属性名的 MutationObserver 监听=昼夜背景切换）
    (re.compile(r'data-ds-dark-theme'), 'data-skin-dark'),
    # hooks 结构全形
    (re.compile(r"\[data-pane=(['\"])sidebar\1\] > div > :last-child"), r'[data-testid=\1sidebar-footer\1]'),
    (re.compile(r"header \[role=(['\"])tablist\1\]"), r'[data-region=\1workspace\1] [role=\1tablist\1]'),
    # 三栏（detail 为个别皮肤的拼写变体）
    (re.compile(r'\[data-pane=(["\'])sidebar\1\]'), r'[data-region=\1sidebar\1]'),
    (re.compile(r'\[data-pane=(["\'])details\1\]'), r'[data-region=\1workspace\1]'),
    (re.compile(r'\[data-pane=(["\'])detail\1\]'), r'[data-region=\1workspace\1]'),
    (re.compile(r'\[data-pane=(["\'])conversation\1\]'), r'[data-region=\1chat\1]'),
    # 聊天容器状态（DSH hero 空态 / active 对话态 → 我方 data-chat-state）
    (re.compile(r'\[data-phase=(["\'])hero\1\]'), r'[data-chat-state=\1empty\1]'),
    (re.compile(r'\[data-phase=(["\'])active\1\]'), r'[data-chat-state=\1active\1]'),
    # 消息流容器
    (re.compile(r'\[data-chat-flow\]'), '[data-testid="message-list"]'),
    # 输入卡片（DSH data-composer-card：hero/active 两态输入卡本体——画框
    # border-image 等 65 处装饰的挂点，漏映射=输入框装饰全灭）
    (re.compile(r'\[data-composer-card\]'), '[data-testid="chat-input"]'),
    # DSH L2 槽位 → 同位组件。sidebar.settings 复合形在前（11 处 CSS 触发器样式
    # + hooks aria-expanded 探测都是 "> button" 形）；裸 token 同指触发器，使
    # hooks 的 footer 走查（settings 槽向上找含 footer.action 的祖先行）把
    # data-maid-sidebar-footer 装饰标记落在我们真正的页脚容器上而非整条侧栏
    (re.compile(r"\[data-slot=(['\"])sidebar\.settings\1\] > :is\(button, \[role=(['\"])button\2\]\)"),
     r'[data-testid=\1sidebar-user-area\1]'),
    (re.compile(r'\[data-slot=(["\'])sidebar\.settings\1\]'), r'[data-testid=\1sidebar-user-area\1]'),
    (re.compile(r'\[data-slot=(["\'])sidebar\.footer\.action\1\]'), r'[data-testid=\1sidebar-user-area\1]'),
    (re.compile(r'\[data-slot=(["\'])settings\.trigger\1\]'), r'[data-testid=\1sidebar-user-area\1]'),
    (re.compile(r'\[data-slot=(["\'])conversation\.session\.header\.actions\1\]'), r'[data-testid=\1agent-tab-bar\1]'),
    (re.compile(r'\[data-slot=(["\'])conversation\.session\.header\1\]'), r'[data-testid=\1chat-session-header\1]'),
    (re.compile(r'\[data-slot=(["\'])conversation\.composer\.dock\1\]'), r'[data-testid=\1chat-composer\1]'),
    (re.compile(r'\[data-slot=(["\'])conversation\.composer\1\]'), r'[data-testid=\1chat-composer\1]'),
    (re.compile(r'\[data-slot=(["\'])conversation\.chat\.node\1\]'), r'[data-testid=\1message-item\1]'),
    # DSH L1 表面（旧代表面词汇）→ 同位并入区域/组件锚点
    (re.compile(r'\[data-dsh-surface=(["\'])sidebar\1\]'), r'[data-region=\1sidebar\1]'),
    (re.compile(r'\[data-dsh-surface=(["\'])conversation\1\]'), r'[data-region=\1chat\1]'),
    (re.compile(r'\[data-dsh-surface=(["\'])settings\1\]'), r'[data-testid=\1settings-page\1]'),
    (re.compile(r'\[data-dsh-surface=(["\'])composer\1\]'), r'[data-testid=\1chat-composer\1]'),
    (re.compile(r'\[data-dsh-surface=(["\'])session-header\1\]'), r'[data-testid=\1chat-session-header\1]'),
    # DSH 组件类名：camelCase 复合词=DSH 组件身份、位置明确方映射；
    # 单词泛型（item/menu/header/panel/seat/trigger/…）无法按位置裁决 → 原样透传惰性
    (re.compile(r"input\[class\*=(['\"])searchInput\1\]"), r'[data-testid=\1sidebar-search-section\1] input'),
    (re.compile(r'\[class\*=(["\'])searchInput\1\]'), r'[data-testid=\1sidebar-search-section\1] input'),
    (re.compile(r'\[class\*=(["\'])searchButton\1\]'), r'[data-testid=\1sidebar-search-section\1]'),
    (re.compile(r'\[class\*=(["\'])searchExpanded\1\]'), r'[data-testid=\1sidebar-search-section\1]'),
    (re.compile(r'\[class\*=(["\'])newSession\1\]'), r'[data-testid=\1new-session-button\1]'),
    (re.compile(r'\[class\*=(["\'])userRow\1\]'), r'[data-testid=\1sidebar-user-area\1]'),
    (re.compile(r'\[class\*=(["\'])navCell\1\]'), r'[data-testid=\1sidebar-nav\1] button'),
    (re.compile(r'\[class\*=(["\'])composerSeat\1\]'), r'[data-testid=\1chat-composer\1]'),
    (re.compile(r'\[class\*=(["\'])composer\1\]'), r'[data-testid=\1chat-composer\1]'),
    (re.compile(r'\[class\*=(["\'])sidebarCol\1\]'), r'[data-region=\1sidebar\1]'),
    (re.compile(r'\[class\*=(["\'])centerCol\1\]'), r'[data-region=\1chat\1]'),
    (re.compile(r'\[class\*=(["\'])detailsCol\1\]'), r'[data-region=\1workspace\1]'),
    (re.compile(r'\[class\*=(["\'])settingsRoot\1\]'), r'[data-testid=\1settings-page\1]'),
    # 空态欢迎页标题（DSH hero headline → 我方空态文案块；:has(fish) 等
    # 子结构在我方 DOM 无命中则该规则自然惰性）
    (re.compile(r'\[class\*=(["\'])headline\1\]'), r'[data-testid=\1message-list-empty\1] > div'),
]
# DSH 的 [id="root"] 是桌面壳窗框：背景位（透出立绘）保留，窗框装饰
# （border/box-shadow/outline）在我们壳里无对应位置 → 剥离（"背景图被
# 边框围一圈" = 窗框装饰错位打在全 app 根上）
_ROOT_FRAME_DECL = re.compile(
    r'^\s*(?:border(?:-[a-z0-9-]+)?|box-shadow|outline(?:-[a-z0-9-]+)?|border-radius)\s*:'
)
# 工作区容器裸选择器（翻译后形态：[data-region="workspace"] 单形或 :is 双支，
# 可带 body 属性前缀如 body[data-skin-dark]）——只匹配容器自身，不含 descendant
_WORKSPACE_CONTAINER_RE = re.compile(
    r'^(?:body\[[^\]]+\] )?(?::is\(\[data-region="workspace"\], \[data-region="workspace"\]\)|\[data-region="workspace"\])$'
)
# 剥离声明：background / background-color（边框/内边距等位置装饰保留）
_WORKSPACE_SURFACE_DECL = re.compile(r'^\s*background(?:-color)?\s*:')


def _sub_position(text: str) -> str:
    """DSH 位置词汇 → 我方锚点（CSS 选择器与 hooks.mjs JS 字符串同源转译）。"""
    for pat, repl in _DSH_POSITION_MAP:
        text = pat.sub(repl, text)
    return text


def _rewrite_dsh_positions(css: str) -> str:
    """DSH 皮肤 CSS 位置路由（通用，逐规则翻译顶层选择器）。

    - 三栏/状态/槽位/表面/组件类名按 _DSH_POSITION_MAP 翻译到我方锚点；
    - [id="root"] 规则 → 剥窗框装饰属性（背景位保留）；块空则整条剔除；
    - [id="root"] > div:has([data-pane])（内容主行）→ #root > div:first-child；
    - 无法位置裁决的泛型选择器原样透传——不匹配即惰性（零副作用）。
    """
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(css):
        if ch == '{':
            depth += 1
            if depth == 1:
                header = css[start:i]
                block_start = i + 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                header_stripped = header.strip()
                block = css[block_start:i]
                out.append(_rewrite_one_rule(header_stripped, block))
                start = i + 1
    if start < len(css):
        out.append(css[start:])
    if depth != 0:
        # 括号不平衡视为异常输入：原样返回（fail-closed 不丢内容）
        return css
    return ''.join(out)


def _rewrite_one_rule(header: str, block: str) -> str:
    if header.startswith('@'):
        # 条件组规则（@media/@supports/@layer/@container）内的规则同样翻译
        # （嵌套规则头也是选择器）；@keyframes/@font-face 等原样透传
        if re.match(r'@(media|supports|layer|container)\b', header):
            return header + '{' + _rewrite_dsh_positions(block) + '}'
        return header + '{' + block + '}'
    new_header = _sub_position(header)
    # 工作区表面让位（用户裁决：工作区=对话区延伸，背景图/立绘透出）：
    # DSH details 容器裸规则画的实色纸面（maid #f2f6fdd1 等）剥离——
    # 边框等位置装饰保留；descendant 规则不动（只剥容器自身的面）
    if _WORKSPACE_CONTAINER_RE.fullmatch(new_header):
        kept = [d for d in block.split(';') if not _WORKSPACE_SURFACE_DECL.match(d)]
        if not [k for k in kept if k.strip()]:
            return ''
        block = ';'.join(kept).rstrip()
        if block and not block.endswith(';'):
            block += ';'
    if '[id="root"]' in new_header:
        if 'div:has' in new_header or ('> div' in new_header and 'data-pane' in header):
            new_header = new_header.replace('[id="root"] > div:has([data-pane])', '#root > div:first-child')
        kept = [d for d in block.split(';') if not _ROOT_FRAME_DECL.match(d)]
        if not [k for k in kept if k.strip()]:
            return ''
        block = ';'.join(kept).rstrip() + ';'
    return new_header + '{' + block + '}'


def _serve_merged_skin_css(path: str, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    """按择注入的皮肤全量 CSS（/ext/dsh_adapter/styles/skin/<skin>/merged.css）。

    skin.css + patches.css 原样合并（皮肤作者的圆角/阴影/动效/鼠标样式等
    全部以 CSS 形式生效，选择器由前端激活时打 html[data-dsh-skin] 标命中）；
    相对 url(assets/...) 重写到皮肤资产路由（浏览器相对解析无法跨路由，
    data:/https:/绝对路径原样保留）。None = 路由不匹配。

    缓存：merged.css / hooks.mjs / 皮肤资产一律 ETag 协商缓存
    （If-None-Match 命中 → 304 零传输）。skin 内容升级后 ETag 变化，浏览器
    自动重拉——比裸 max-age 缓存安全（皮肤文件非内容寻址路径，升级后 URL
    不变）。cache-control: no-cache 表示"必须回源校验"，配 ETag 命中即省 body。
    """
    if not (path.startswith(_SKIN_MERGED_PREFIX) and (path.endswith(_SKIN_MERGED_SUFFIX) or path.endswith(_SKIN_HOOKS_SUFFIX))):
        return None
    skin = path[len(_SKIN_MERGED_PREFIX):-len(_SKIN_MERGED_SUFFIX)] if path.endswith(_SKIN_MERGED_SUFFIX) else path[len(_SKIN_MERGED_PREFIX):-len(_SKIN_HOOKS_SUFFIX)]
    if not skin or any(seg in ("", ".", "..") for seg in skin.split("/")):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    if skin not in list_available_skins():
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    skin_dir = SKIN_CENTER_SKINS_DIR / skin
    # 脚本递送（DSH 原机制 hooks：前端拉取后 blob 导入运行，契约
    # x-org.linxin666.skin-center/v1alpha1；加载不得有顶层副作用）。
    # hooks 里的 DSH 选择器与 CSS 同源转译（我方 DOM 无 DSH 词汇锚点，
    # 不转译则 querySelector 恒空、装饰全落空）
    if path.endswith(_SKIN_HOOKS_SUFFIX):
        hook_file = skin_dir / "hooks.mjs"
        if not hook_file.is_file():
            return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
        try:
            text = hook_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("dsh_adapter: skin hooks read failed (%s): %s", skin, e)
            return {"status": 500, "headers": {}, "body": "", "body_encoding": "utf-8"}
        body = _sub_position(text).encode("utf-8")
        if _revalidate(headers, body):
            return {"status": 304, "headers": {"etag": _etag_of(body)}, "body": "", "body_encoding": "utf-8"}
        return {
            "status": 200,
            "headers": {
                "content-type": "text/javascript; charset=utf-8",
                "cache-control": "no-cache",
                "etag": _etag_of(body),
            },
            "body": base64.b64encode(body).decode(),
            "body_encoding": "base64",
        }
    parts: list[str] = []
    for name in ("skin.css", "patches.css"):
        f = skin_dir / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("dsh_adapter: merged skin css read failed (%s): %s", name, e)
            continue
        parts.append(
            _rewrite_dsh_positions(
                re.sub(
                    r'(url\(\s*[\'"]?)(?!data:|https?:|/)([^\'")]+)([\'"]?\s*\))',
                    lambda m: f"{m.group(1)}{_SKIN_ASSET_ROUTE_PREFIX}{skin}/{m.group(2)}{m.group(3)}",
                    text,
                )
            )
        )
    if not parts:
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    body = ("/* dsh_adapter merged skin css: %s */\n" % skin + "\n\n".join(parts) + "\n").encode("utf-8")
    if _revalidate(headers, body):
        return {"status": 304, "headers": {"etag": _etag_of(body)}, "body": "", "body_encoding": "utf-8"}
    return {
        "status": 200,
        "headers": {
            "content-type": "text/css; charset=utf-8",
            "cache-control": "no-cache",
            "etag": _etag_of(body),
        },
        "body": base64.b64encode(body).decode(),
        "body_encoding": "base64",
    }


def _serve_skin_asset(path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """serve 皮肤背景图资产（/ext/dsh_adapter/styles/skin-assets/<skin>/<file>）。

    白名单约束：皮肤 id 必须在装载的 skin-center 内、文件扩展名限图片、
    resolve 后必须落在 skins 目录内（防穿越）。ETag 协商缓存（同 merged.css）。
    """
    rel = path[len(_SKIN_ASSET_ROUTE_PREFIX):]
    parts = rel.split("/")
    if len(parts) < 2 or not parts[0] or not parts[-1]:
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    skin, file_parts = parts[0], parts[1:]
    if any(seg in ("", ".", "..") for seg in [skin, *file_parts]):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    filename = file_parts[-1]
    if Path(filename).suffix.lower() not in SKIN_ASSET_EXTS:
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    target = (SKIN_CENTER_SKINS_DIR / skin / Path(*file_parts)).resolve()
    if SKIN_CENTER_SKINS_DIR.resolve() not in target.parents or not target.is_file():
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    try:
        data = target.read_bytes()
    except OSError as e:
        logger.warning("dsh_adapter: skin asset read failed (%s): %s", rel, e)
        return {"status": 500, "headers": {}, "body": "", "body_encoding": "utf-8"}
    ct = _CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")
    if _revalidate(headers, data):
        return {"status": 304, "headers": {"etag": _etag_of(data)}, "body": "", "body_encoding": "utf-8"}
    return {
        "status": 200,
        "headers": {
            "content-type": ct,
            "cache-control": "no-cache",
            "etag": _etag_of(data),
        },
        "body": base64.b64encode(data).decode(),
        "body_encoding": "base64",
    }


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/dsh_adapter/styles/** (client_styles CSS serve)",
)
async def _http_handle_style(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """serve 皮肤合并 CSS / hooks.mjs / 背景资产（dispatcher 契约：body base64 原样回写）。"""
    merged = _serve_merged_skin_css(path, headers)
    if merged is not None:
        return merged
    if path.startswith(_SKIN_ASSET_ROUTE_PREFIX):
        return _serve_skin_asset(path, headers)
    return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}


if __name__ == "__main__":
    plugin.run()
