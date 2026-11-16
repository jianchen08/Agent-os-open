#!/usr/bin/env python3
"""DSH 插件适配器 MCP 服务端（task_dsh_plugin_adapter 任务 2 + 4）。

LLM 工具面已退役（工具面审查 2026-10-19 用户裁定：翻译能力与 ecosystem_import
重叠，hooks 翻译经模块导入 translator.translate_hooks_config 产出 checks 建议，
不经工具面）。
保留面：
- 通道 A：Node runtime 桥（bridge.py + runtime/dsh-rpc-bridge.mjs），从用户
  空间 npm 运行时工作区 boot（设计 §4.2 C2；管理端点五件套见本文件下半部，
  工作区/bootstrap 逻辑在 runtime_workspace.py + manager.py）；
  生命周期 on_unload 时 shutdown Node 子进程（防孤儿）。
- http.handle：/ext/dsh_adapter/styles/** 客户端样式 CSS serve +
  /ext/dsh_adapter/api/dsh/plugins* 管理端点 + /ext/dsh_adapter/page/manager
  管理页。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from bridge import shutdown_bridge
from manager import ManagerError
from translator import (
    SKIN_ASSET_EXTS,
    SKIN_CENTER_SKINS_DIR,
    list_available_skins,
)

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("dsh_adapter")


@plugin.on_load
async def _on_dsh_adapter_load(params: dict) -> None:  # noqa: ARG001
    """装载时把皮肤自动声明为 contributes.themes（形态路由终态，零手工）。

    幂等：生成的 themes 与 manifest 现值一致则不写（避免 watcher 指纹
    抖动循环）；不一致才写回——plugin_watcher 检测 manifest 变化自动
    reenable 重注册，前端主题列表随即出现/移除皮肤主题卡。
    添加皮肤 = 放入 ~/.dsh/skins/，本钩子负责翻译成 PluginTheme 声明。
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


# DSH 槽位 → 我方 DOM 锚点（位置侧目标映射；词汇属主在 translator 的
# DSH_KNOWN_SLOT_NAMES，本表只挂目标侧，挂了词汇外槽位即漂移）。
# 键序=规则序：复合名必须先于其前缀（header.actions 先于 header、
# composer.dock 先于 composer），前缀规则先命中会残留错位结构——与主表
# 同一条顺序敏感铁律。
_DSH_SLOT_DOM_ANCHORS: dict[str, str] = {
    "sidebar.footer.action": "sidebar-user-area",
    "settings.trigger": "sidebar-user-area",
    "conversation.session.header.actions": "agent-tab-bar",
    "conversation.session.header": "chat-session-header",
    "conversation.composer.dock": "chat-composer",
    "conversation.composer": "chat-composer",
    "conversation.chat.node": "message-item",
}

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
    # 其余纯 data-slot 锚点规则按 _DSH_SLOT_DOM_ANCHORS 键序原位派生（统一
    # 反向引用保留引号形态，re.escape 保槽位名字面量）
    *(
        (
            re.compile(r'\[data-slot=(["\'])' + re.escape(_slot) + r'\1\]'),
            r"[data-testid=\1" + _anchor + r"\1]",
        )
        for _slot, _anchor in _DSH_SLOT_DOM_ANCHORS.items()
    ),
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
            # 纯 CSS 皮肤（summer-liquid-glass/mint/deep-current 等）无 hooks.mjs
            # 是常态：回 200 空文本（前端契约「无 hooks.mjs 跳过动态层」），
            # 404 会被前端 API 拦截器当错误上报——正常切皮肤不应产生错误。
            return {
                "status": 200,
                "headers": {
                    "content-type": "text/javascript; charset=utf-8",
                    "cache-control": "no-cache",
                },
                "body": "",
                "body_encoding": "utf-8",
            }
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
    description=(
        "HTTP endpoint handler for /ext/dsh_adapter/**: styles CSS serve + "
        "api/dsh/plugins management endpoints + page/manager webview page"
    ),
)
async def _http_handle_style(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",  # noqa: ARG001 — dispatcher 契约固定形参
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：皮肤 CSS/资产、管理端点五件套、管理页 HTML。"""
    merged = _serve_merged_skin_css(path, headers)
    if merged is not None:
        return merged
    if path.startswith(_SKIN_ASSET_ROUTE_PREFIX):
        return _serve_skin_asset(path, headers)
    if path == _PAGE_MANAGER_PATH and method == "GET":
        return _manager_page_response()
    if path == _HOOKS_TRANSLATE_PATH and method == "POST":
        return _hooks_translate_response(raw_body)
    if path.startswith(_WEB_PROXY_PREFIX):
        return await _web_proxy_dispatch(path, method, raw_body, headers)
    if path.startswith(_API_PREFIX):
        resp = await _api_plugins_dispatch(path, method, raw_body, query)
        if resp is not None:
            return resp
    return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}


# ── DSH 生态包管理端点（设计 §4.1 C1 五件套）+ 管理页（webview 自伺服） ──
# 端点前缀：插件端点路由前缀由内核固定（/ext/{pluginId}），设计表的逻辑路径
# /api/dsh/plugins* 在此落地为 /ext/dsh_adapter/api/dsh/plugins*。
# 业务面在 manager.DshPackageManager（审批/engines/依赖保护/状态机在彼处，
# 本层只做路由、body 解析、错误码→HTTP 状态映射）。

_API_PREFIX = "/ext/dsh_adapter/api/dsh/plugins"
_PAGE_MANAGER_PATH = "/ext/dsh_adapter/page/manager"
_HOOKS_TRANSLATE_PATH = "/ext/dsh_adapter/api/dsh/hooks/translate"
_WEB_PROXY_PREFIX = "/ext/dsh_adapter/web/"

_MANAGER: Any | None = None


def _manager() -> Any:
    """管理器单例（惰性；human-interaction capability 经 factory 调用时惰取）。"""
    global _MANAGER  # noqa: PLW0603
    if _MANAGER is None:
        from manager import DshPackageManager
        from translator import load_adapter_config

        def _capability() -> Any:
            try:
                return plugin.get_capability("human-interaction")
            except (KeyError, AttributeError):
                return None

        _MANAGER = DshPackageManager(config=load_adapter_config(), capability_factory=_capability)
    return _MANAGER


def reset_manager() -> None:
    """复位管理器单例（单测隔离用）。"""
    global _MANAGER  # noqa: PLW0603
    _MANAGER = None


# 结构化错误码 → HTTP 状态（管理页按 code 展示可判定失败原因，AC-M2）
_ERROR_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "package_not_found": 404,
    "package_not_installed": 404,
    "has_dependents": 409,
    "version_required_not_in_index": 422,
    "engines_incompatible": 422,
    "approval_denied": 403,
    "approval_unavailable": 503,
    "registry_unreachable": 502,
    "npm_error": 502,
}


def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


def _parse_body(raw_body: str) -> dict[str, Any]:
    """POST 载荷解析（内核 base64 契约 + 裸 JSON 双形态；mode_coding 同款）。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        data = json.loads(decoded)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _api_plugins_dispatch(
    path: str, method: str, raw_body: str, query: dict[str, str] | None
) -> dict[str, Any] | None:
    """管理端点五件套分发；未命中返回 None（交回 404 路径）。"""
    sub = path[len(_API_PREFIX):].strip("/")
    m = _manager()
    try:
        if method == "GET" and sub == "":
            return _json_response(await m.list_installed())
        if method == "GET" and sub == "index":
            return _json_response(await m.refresh_index())
        if method == "POST":
            body = _parse_body(raw_body)
            pkg = str(body.get("pkg") or (query or {}).get("pkg") or "").strip()
            version = str(body.get("version") or "").strip()
            if sub == "install":
                if not pkg:
                    return _json_response({"error": {"code": "invalid_request", "message": "缺 pkg"}}, 400)
                return _json_response(await m.install(pkg, version or None,
                                                      legacy_peers=bool(body.get("legacy_peers"))))
            if sub in ("uninstall", "enable", "disable"):
                if not pkg:
                    return _json_response({"error": {"code": "invalid_request", "message": "缺 pkg"}}, 400)
                if sub == "uninstall":
                    return _json_response(await m.uninstall(pkg))
                return _json_response(await (m.enable(pkg) if sub == "enable" else m.disable(pkg)))
    except ManagerError as e:
        return _json_response({"error": e.to_dict()}, _ERROR_STATUS.get(e.code, 400))
    return None


# ── 管理页 HTML（webview 自伺服；包内自包含，与 mode_coding 先例同构） ──

_MANAGER_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>DSH 插件管理</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 24px; background: var(--ag-bg, #f5f6fa); color: var(--ag-fg, #1d2539); }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #777; font-size: 12px; margin-bottom: 16px; }
  button { font: inherit; padding: 4px 12px; border: 1px solid #bbb; border-radius: 6px;
           background: #fff; cursor: pointer; }
  button:hover { border-color: #4a5fa8; color: #4a5fa8; }
  button.danger:hover { border-color: #c3272b; color: #c3272b; }
  input { font: inherit; padding: 4px 8px; border: 1px solid #bbb; border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 13px; }
  th { background: #eef1f7; font-weight: 600; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
  .badge.enabled { background: #e2f4e8; color: #1a7f37; }
  .badge.disabled { background: #eee; color: #666; }
  .badge.failed, .badge.approval_denied { background: #fde8e8; color: #c3272b; }
  .badge.pending_approval { background: #fff4d6; color: #946c00; }
  .muted { color: #888; }
  .err { color: #c3272b; font-size: 12px; white-space: pre-wrap; }
  #preview, #index-box { background: #fff; border: 1px solid #ddd; border-radius: 8px;
          padding: 12px 16px; margin-top: 16px; white-space: pre-wrap; font-size: 12px;
          font-family: Consolas, monospace; display: none; }
  form { display: flex; gap: 8px; margin: 16px 0; flex-wrap: wrap; }
  .tabs { margin-bottom: 12px; }
  .tabs button.active { border-color: #4a5fa8; color: #4a5fa8; font-weight: 600; }
  code { background: #f0f2f7; padding: 0 4px; border-radius: 4px; }
</style>
</head>
<body>
<h1>DSH 插件</h1>
<div class="sub" id="ws-info">运行时工作区：加载中…</div>

<div class="tabs">
  <button id="tab-installed" class="active" onclick="showTab('installed')">已装</button>
  <button id="tab-index" onclick="showTab('index')">发现源</button>
</div>

<div id="installed-view">
  <form onsubmit="return doInstall(event)">
    <input id="pkg" placeholder="npm 包名（如 @scope/dsh-xxx）" size="34" required>
    <input id="ver" placeholder="精确版本（缺省取索引最新发行版）" size="26">
    <button type="submit">安装（走审批卡）</button>
  </form>
  <table>
    <thead><tr><th>包</th><th>版本</th><th>状态</th><th>贡献面</th><th>engines.dsh</th><th>操作</th></tr></thead>
    <tbody id="rows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
  </table>
  <div id="preview"></div>
  <div id="error" class="err"></div>
</div>

<div id="index-view" style="display:none">
  <div class="sub">发现源 = format 1 静态索引（多源可配置）；安装才进工作区，发现面只读。</div>
  <table>
    <thead><tr><th>包</th><th>类别</th><th>最新精确版本</th><th>说明</th><th>操作</th></tr></thead>
    <tbody id="idx-rows"><tr><td colspan="5" class="muted">加载中…</td></tr></tbody>
  </table>
  <div id="idx-meta" class="sub"></div>
</div>

<script>
const API = '/ext/dsh_adapter/api/dsh/plugins';
let pollTimer = null;

function showTab(name) {
  document.getElementById('installed-view').style.display = name === 'installed' ? '' : 'none';
  document.getElementById('index-view').style.display = name === 'index' ? '' : 'none';
  document.getElementById('tab-installed').classList.toggle('active', name === 'installed');
  document.getElementById('tab-index').classList.toggle('active', name === 'index');
  if (name === 'index') loadIndex();
}

async function api(path, opts) {
  const res = await fetch(API + path, opts);
  let body = null;
  try { body = await res.json(); } catch { body = {}; }
  if (!res.ok || (body && body.error)) {
    const e = (body && body.error) || { code: 'http_' + res.status, message: res.statusText };
    throw e;
  }
  return body;
}

function errText(e) { return e.code ? e.code + '：' + (e.message || '') : String(e); }

function badge(status) {
  return '<span class="badge ' + status + '">' + status + '</span>';
}

function contrib(c) {
  if (!c) return '';
  const parts = [];
  if (c.tools) parts.push('工具' + (c.tool_names && c.tool_names.length ? '(' + c.tool_names.join(', ') + ')' : ''));
  if (c.themes) parts.push('主题×' + c.themes);
  if (c.hooks) parts.push('钩子×' + c.hooks);
  return parts.join(' · ') || '<span class="muted">—</span>';
}

async function refresh() {
  try {
    const out = await api('');
    render(out);
    document.getElementById('error').textContent = '';
  } catch (e) {
    document.getElementById('error').textContent = errText(e);
  }
  const needsPoll = document.querySelectorAll('.badge.pending_approval').length > 0;
  if (needsPoll && !pollTimer) pollTimer = setTimeout(() => { pollTimer = null; refresh(); }, 3000);
}

function render(out) {
  document.getElementById('ws-info').textContent =
    '运行时工作区：' + (out.workspace || '—') + (out.initialized ? '' : '（空：未装任何 DSH 包）');
  const rows = document.getElementById('rows');
  if (!out.packages || out.packages.length === 0) {
    rows.innerHTML = '<tr><td colspan="6" class="muted">空工作区——安装一个包试试（AC-R4：空工作区 boot 不加载任何 DSH 包）</td></tr>';
    return;
  }
  rows.innerHTML = out.packages.map(p => {
    const actions = [
      p.enabled ? '<button onclick="op(\'disable\',\'' + p.name + '\')">禁用</button>'
                : '<button onclick="op(\'enable\',\'' + p.name + '\')">启用</button>',
      '<button class="danger" onclick="op(\'uninstall\',\'' + p.name + '\')">卸载</button>',
    ].join(' ');
    return '<tr><td><code>' + p.name + '</code>' + (p.error ? '<div class="err">' + p.error + '</div>' : '') + '</td>'
      + '<td>' + p.version + (p.requested_version && p.requested_version !== p.version ? ' → ' + p.requested_version : '') + '</td>'
      + '<td>' + badge(p.status) + '</td>'
      + '<td>' + contrib(p.contribution) + '</td>'
      + '<td>' + (p.engines_dsh ? '<code>' + p.engines_dsh + '</code>' : '<span class="muted">未声明</span>')
        + (p.engines_compatible === false ? ' <span class="err">不兼容</span>' : '') + '</td>'
      + '<td>' + actions + '</td></tr>';
  }).join('');
}

async function op(action, pkg) {
  if (action === 'uninstall' && !confirm('卸载 ' + pkg + '？')) return;
  try {
    await api('/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pkg }),
    });
    document.getElementById('error').textContent = '';
  } catch (e) {
    document.getElementById('error').textContent = errText(e);
  }
  refresh();
}

async function doInstall(ev) {
  ev.preventDefault();
  const pkg = document.getElementById('pkg').value.trim();
  const version = document.getElementById('ver').value.trim();
  const pv = document.getElementById('preview');
  pv.style.display = 'block';
  pv.textContent = '提交安装（等待审批卡裁决：批准后后台落盘，状态列轮询可见）…';
  try {
    const out = await api('/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pkg, version: version || undefined }),
    });
    pv.textContent = '审批中 request_id=' + out.request_id + '\n翻译预览（装什么注册什么）：\n'
      + JSON.stringify(out.preview, null, 1);
  } catch (e) {
    pv.textContent = '';
    document.getElementById('error').textContent = errText(e);
  }
  refresh();
  return false;
}

async function loadIndex() {
  const rows = document.getElementById('idx-rows');
  try {
    const out = await api('/index');
    const plugins = out.plugins || [];
    rows.innerHTML = plugins.slice(0, 200).map(p => {
      const latest = (p.versions || []).map(v => v.version).slice(-1)[0] || '';
      const cat = (p.metadata && p.metadata.category) || '';
      const desc = (p.description && (p.description.zh || p.description.en)) || '';
      return '<tr><td><code>' + p.name + '</code></td><td>' + cat + '</td><td>' + latest + '</td>'
        + '<td class="muted">' + String(desc).slice(0, 120) + '</td>'
        + '<td><button onclick="prefill(\'' + p.name + '\',\'' + latest + '\')">装这个</button></td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">索引为空</td></tr>';
    const srcs = (out.sources || []).map(s =>
      s.name.slice(0, 40) + ' → ' + (s.ok ? 'ok' : ('不可达' + (s.stale ? '（缓存已标记过期）' : '')))).join('；');
    document.getElementById('idx-meta').textContent =
      srcs + '｜' + (plugins.length) + ' 条（最多显示 200）';
  } catch (e) {
    rows.innerHTML = '<tr><td colspan="5" class="err">' + errText(e) + '</td></tr>';
  }
}

function prefill(pkg, ver) {
  showTab('installed');
  document.getElementById('pkg').value = pkg;
  document.getElementById('ver').value = ver;
}

refresh();
</script>
</body>
</html>
"""


def _manager_page_response() -> dict[str, Any]:
    """管理页 HTML → HttpHandleResponse（text/html，body base64）。"""
    body = _MANAGER_PAGE_HTML.encode("utf-8")
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


# ── 钩子方言翻译端点（AC-X4：CC/Codex hooks.json 静态翻译入口） ──────────


def _hooks_translate_response(raw_body: str) -> dict[str, Any]:
    """POST body ``{config, dialect?}`` → translate_dialect_hooks 结果。"""
    from translator import translate_dialect_hooks

    body = _parse_body(raw_body)
    config = body.get("config")
    dialect = str(body.get("dialect") or "claude-code")
    return _json_response(translate_dialect_hooks(config, dialect))


# ── webServer 通配反代（AC-X2：/ext/dsh_adapter/web/{path}） ─────────────
#
# 路由表由桥 boot 后的 webServer 注册面决定（包经 ctx.webServer.register
# 注册的 HTTP 路由），本端点只做透传：鉴权由内核端点档（auth=user）执行，
# 转发时剥离调用方 Authorization（DSH 侧路由不共享灵汐 token）。

_HOP_HEADERS = frozenset({
    "host", "authorization", "cookie", "connection", "content-length",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade",
})


def _raw_body_bytes(raw_body: str) -> bytes:
    """端点 body 原样字节：base64 形态优先，裸文本回落 UTF-8。"""
    if not raw_body:
        return b""
    try:
        return base64.b64decode(raw_body, validate=True)
    except (ValueError, binascii.Error):
        return raw_body.encode("utf-8")


async def _web_proxy_dispatch(
    path: str, method: str, raw_body: str, headers: dict[str, str] | None
) -> dict[str, Any]:
    """反代到 DSH webServer；不可用/失败 → 502 + 结构化原因（fail-visible）。"""
    from bridge import get_bridge

    target = "/" + path[len(_WEB_PROXY_PREFIX):]
    fwd = {
        str(k): str(v) for k, v in (headers or {}).items()
        if str(k).lower() not in _HOP_HEADERS
    }
    result = await get_bridge().http_request(method, target, fwd, _raw_body_bytes(raw_body))
    if not result.get("ok"):
        return _json_response({
            "error": {"code": "web_proxy_unavailable", "message": result.get("error") or "反代失败"},
        }, 502)
    out_headers = {
        str(k): str(v) for k, v in (result.get("headers") or {}).items()
        if str(k).lower() not in {"content-encoding", "content-length", "transfer-encoding"}
    }
    return {
        "status": int(result.get("status") or 502),
        "headers": out_headers,
        "body": str(result.get("body_base64") or ""),
        "body_encoding": "base64",
    }


# ══ 能力服务面（capabilities.services 声明即契约，G2 校验声明=实现）══
#
# 设计 §4.3：DSH 侧服务/拦截点的灵汐进入面 = 适配器内部服务方法（不进 LLM
# 工具面——2026-10-19 工具面审查裁定），调用方经显式插件通道（pipeline 步 /
# invoke_entry / capability 路由）消费。


@plugin.tool(
    name="dsh.service.call",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "DSH Service 名（boot context 的 ctx.<name>）"},
            "method": {"type": "string", "description": "Service 实例方法名"},
            "args": {"type": "array", "description": "位置参数数组（方法签名直传）"},
        },
        "required": ["name", "method"],
    },
    description="Call a DSH Service method through the Node runtime bridge (AC-X1)",
)
async def dsh_service_call(name: str = "", method: str = "",
                           args: list[Any] | None = None) -> dict[str, Any]:
    """AC-X1：DSH 服务方法桥调用（同 tool/call 七键信封）。"""
    if not name or not method:
        return {"success": False, "data": None, "error": "缺 name/method", "duration_ms": 0}
    from bridge import get_bridge

    return await get_bridge().call_service(name, method, list(args or []))


@plugin.tool(
    name="dsh.hook.emit",
    schema={
        "type": "object",
        "properties": {
            "event": {"type": "string", "description": "DSH 拦截点事件名（词表见 translator.DSH_NATIVE_HOOK_EVENTS）"},
            "payload": {"type": "object", "description": "事件载荷（waterfall 原样进出）"},
        },
        "required": ["event"],
    },
    description="Emit an event into DSH interceptor listeners via the bridge (AC-X3)",
)
async def dsh_hook_emit(event: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """AC-X3：管道边界事件送回 DSH 监听器（waterfall 组合）。"""
    if not event:
        return {"ok": False, "event": event, "decision": None, "error": "缺 event"}
    from bridge import get_bridge

    return await get_bridge().emit_hook(event, payload or {})


@plugin.tool(
    name="dsh.web.request",
    schema={
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "HTTP 方法（GET/POST/...）"},
            "path": {"type": "string", "description": "目标绝对路径（/api/...）"},
            "headers": {"type": "object", "description": "请求头"},
            "body_base64": {"type": "string", "description": "请求体（base64；空=无体）"},
        },
        "required": ["method", "path"],
    },
    description="Request the booted DSH webServer listener via the bridge (AC-X2 transport)",
)
async def dsh_web_request(method: str = "GET", path: str = "/",
                          headers: dict[str, str] | None = None,
                          body_base64: str = "") -> dict[str, Any]:
    """AC-X2 传输面：经 boot 的 webServer 监听器发请求。"""
    from bridge import get_bridge

    try:
        body = base64.b64decode(body_base64) if body_base64 else None
    except (ValueError, binascii.Error):
        return {"ok": False, "status": 0, "headers": {}, "body_base64": "",
                "error": "body_base64 非法", "duration_ms": 0}
    return await get_bridge().http_request(method, path, headers or {}, body)


if __name__ == "__main__":
    plugin.run()
