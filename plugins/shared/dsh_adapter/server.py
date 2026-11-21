#!/usr/bin/env python3
"""DSH 插件适配器 MCP 服务端（task_dsh_plugin_adapter 任务 2 + 4）。

LLM 工具面已退役（工具面审查 2026-10-19 用户裁定：翻译能力与 ecosystem_import
重叠，hooks 翻译经模块导入 translator.translate_hooks_config 产出 checks 建议，
不经工具面）。
保留面：
- 通道 A：Node runtime 桥（bridge.py + runtime/dsh-rpc-bridge.mjs），从用户
  空间 DSH profile boot（设计 §4.2 C2；工作区/bootstrap 逻辑在
  runtime_workspace.py + manager.py）；生命周期 on_unload 时 shutdown Node
  子进程（防孤儿）。
- http.handle：/ext/dsh_adapter/styles/** 客户端样式 CSS serve +
  /ext/dsh_adapter/client-bundle/** client bundle 供给 +
  /ext/dsh_adapter/api/dsh/{client-ui,official-ui,hooks/translate}。

插件管理界面归 DSH 官方 Web 应用（用户裁定 2026-11-17）：自研管理页与自研
管理端点已删除，``api/dsh/official-ui`` 只回答「官方页在哪 / 为什么不可用」，
由 web_app.py 把官方 Web 应用拉起来（同一 profile，故所见列表即官方列表）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from bridge import shutdown_bridge
from translator import (
    SKIN_ASSET_EXTS,
    SKIN_CENTER_SKINS_DIR,
    list_available_skins,
)

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("dsh_adapter")


def _wire_runtime_config() -> None:
    """接入内核注入的 LLM 配置（``agentDefaultModel`` 翻译源，不直读 config/）。

    内核按 manifest ``config_files[id=llm]`` 把 llm.yaml 的生效视图注入
    ``get_config()``；桥启动时 runtime_workspace 经 provider 取用。
    """
    import runtime_workspace as rw

    def _llm_view() -> dict[str, Any]:
        cfg = plugin.get_config()
        llm = cfg.get("llm") if isinstance(cfg, dict) else None
        return llm if isinstance(llm, dict) else {}

    rw.set_llm_config_provider(_llm_view)


_wire_runtime_config()


@plugin.on_load
async def _on_dsh_adapter_load(params: dict) -> None:  # noqa: ARG001
    """装载时同步 contributes 声明（形态路由终态，零手工）。

    - ``themes``：皮肤自动声明（DSH 皮肤 → PluginTheme）；
    - ``renderers``：已装 DSH 包的 toolview 聚合（DSH 工具名 → render 卡）。

    幂等：生成值与 manifest 现值一致则不写（避免 watcher 指纹抖动循环）；
    不一致才写回——plugin_watcher 检测 manifest 变化自动 reenable 重注册，
    前端主题列表/渲染兜底随即更新。
    """
    _sync_manifest_contributions()
    _wire_bridge_kernel_caller()


def _wire_kernel_caller(bridge: Any, plugin: Any) -> None:
    """把插件进程侧内核能力句柄装配成桥的统一内核 caller（垫片四接线）。

    三条内核 wire 契约各走各的能力句柄：loop.status → service-registry、
    loop.stop → pipeline-executor、loop.inject → chat；bind_capability_caller
    剥能力前缀后以短名调句柄。部分能力未注入（旧内核 KeyError）→ 跳过缺席者
    可用者照常装配；全部缺席 → caller=None（handler 调用 fail-visible）。
    """
    from agentos_plugin_sdk.capability import bind_capability_caller

    callers: dict[str, Any] = {}
    for cap in ("service-registry", "pipeline-executor", "chat"):
        try:
            callers[cap] = bind_capability_caller(plugin.get_capability(cap), cap)
        except (KeyError, AttributeError, RuntimeError):
            continue  # 能力未注入（旧内核）→ 跳过缺席者，可用者照常装配

    if not callers:
        bridge.set_kernel_caller(None)
        return

    def unified(method: str, params: dict[str, Any]) -> Any:
        prefix = method.split(".", 1)[0]
        caller = callers.get(prefix)
        if caller is None:
            raise RuntimeError(f"kernel capability not injected: {prefix}")
        return caller(method, params)

    bridge.set_kernel_caller(unified)


def _wire_bridge_kernel_caller() -> None:
    """on_load 装配点：把共享桥接上内核能力（垫片四 loop handler 内核接线）。

    桥实例平时惰性到首个工具调用才创建；接线必须赶在 Node 插件 boot 前
    完成（boot 后插件即可发起 loop 反向调用），故此处主动 get_bridge()
    创建实例（不 boot）。桥不可建（工作区未配置）/装配失败不阻断插件装载
    ——handler 侧 fail-visible 兜底，caller 缺席时插件调用得到可定位错误。
    """
    try:
        from bridge import get_bridge

        _wire_kernel_caller(get_bridge(), plugin)
    except Exception as e:  # noqa: BLE001 — 装配失败不阻断插件装载
        logger.warning("dsh_adapter: loop handler 内核接线跳过: %s", e)


def _installed_package_dirs() -> list[str]:
    """运行时工作区已装 DSH 包目录（renderers 聚合来源；工作区未就绪返回空）。"""
    import runtime_workspace as rw

    ws = Path(_manager().runtime_root)
    if not (ws / "node_modules").is_dir():
        return []
    return [p["dir"] for p in rw.installed_packages(ws)
            if p["name"] not in rw.PROFILE_INFRA_PACKAGES]


def _sync_manifest_contributions() -> None:
    """皮肤主题 + DSH 渲染器声明写回 plugin.json（失败不阻断插件装载）。

    客户端 UI 的槽位页不在此处写清单：页面随「已装/已启用」运行态变化，
    由前端 ``syncClientUiPages`` 按 client-ui 清单动态注册/注销。
    """
    try:
        from client_ui import GENERATED_PAGE_PREFIX
        from translator import collect_installed_renderers, skins_to_plugin_themes

        themes = skins_to_plugin_themes()
        renderers = collect_installed_renderers(_installed_package_dirs())
        manifest_path = Path(__file__).parent / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contributes = manifest.setdefault("contributes", {})
        changed: list[str] = []
        if contributes.get("themes") != themes:
            contributes["themes"] = themes
            changed.append(f"themes={len(themes)}")
        if contributes.get("renderers") != renderers:
            contributes["renderers"] = renderers
            changed.append(f"renderers={len(renderers)}")
        # 自愈：运行期生成的槽位页不得留在清单里（页面随安装态由前端动态注册）
        current_pages = contributes.get("pages") or []
        declared_pages = [p for p in current_pages
                          if not str(p.get("id") or "").startswith(GENERATED_PAGE_PREFIX)]
        if current_pages != declared_pages:
            contributes["pages"] = declared_pages
            changed.append(f"pages-={len(current_pages) - len(declared_pages)}")
        if changed:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            logger.info("dsh_adapter: contributes auto-synced (%s)", ", ".join(changed))
    except Exception as e:  # noqa: BLE001 - 声明同步失败不阻断插件装载
        logger.warning("dsh_adapter: contributes auto-sync failed: %s", e)


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
    "settings.section": "settings-page",
    "sidebar.right.pane.tab": "workspace",
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
    # ── DSH 会话内部 DOM 词汇（ui-conversation 稳定词干）→ 我方消息区锚点 ──
    # 皮肤除结构位外还直接给会话 DOM 内部元素上妆（气泡/思考体/时间戳/用量/
    # 输入区脚注/markdown 正文/模态/弹层）。这些词干是 DSH 客户端 DOM 契约的
    # 一部分，与上方槽位/栏位词干同性质，逐词干映射（2026-11-10 whale-mom
    # 全量对账：18 个内部选择器此前在我方 DOM 零命中 = 皮肤效果对不上 DSH
    # 的根因）。命中面我方锚点：data-bubble / message-markdown / message-think
    # / message-think-body / message-time（前端组件已挂）/ chat-composer、
    # chat-input-actions、context-usage-indicator（既有 testid）/ .ant-modal、
    # .ant-popover/.ant-dropdown（antd 弹层）。
    (re.compile(r'\[class\*=(["\'])bubble\1\]'), r'[data-bubble]'),
    (re.compile(r'\[class\*=(["\'])markdown\1\]'), r'[data-testid=\1message-markdown\1]'),
    (re.compile(r'\[class\*=(["\'])thinkBody\1\]'), r'[data-testid=\1message-think-body\1]'),
    (re.compile(r'\[class\*=(["\'])summary\1\]'), r'[data-testid=\1message-think\1]'),
    (re.compile(r'\[class\*=(["\'])timeEnd\1\]'), r'[data-testid=\1message-time\1]'),
    (re.compile(r'\[class\*=(["\'])timeStart\1\]'), r'[data-testid=\1message-time\1]'),
    # turnStatus（运行读数行）我方无独立渲染位 → 并入元信息行时间锚（同为
    # 次级文字着色意图，色值一致，多命中无害）
    (re.compile(r'\[class\*=(["\'])turnStatus\1\]'), r'[data-testid=\1message-time\1]'),
    (re.compile(r'\[class\*=(["\'])usage\1\]'), r'[data-testid=\1context-usage-indicator\1]'),
    # 输入区脚注行（DSH composer footer）→ 底部工具栏行锚（无条件渲染；
    # chat-input-actions 仅在插件声明动作时存在，不适用作 footer 锚）
    (re.compile(r'\[class\*=(["\'])footerItem\1\]'), r'[data-testid=\1chat-input-footer\1]'),
    (re.compile(r'\[class\*=(["\'])footerHint\1\]'), r'[data-testid=\1chat-input-footer\1]'),
    # 输入区芯片（权限/模型选择器等）→ 声明件芯片锚（含 fallback 槽默认件）
    (re.compile(r'\[class\*=(["\'])chipLabel\1\]'),
     r':is([data-testid^=\1declared-widget-\1], [data-testid^=\1slot-default-\1])'),
    # 附件预览徽章 → 附件预览卡锚
    (re.compile(r'\[class\*=(["\'])previewBadge\1\]'), r'[data-testid=\1attachment-preview\1]'),
    # DSH modal 词干（CSS module 尾缀）→ antd 模态/弹层开放体
    (re.compile(r'\[class\$=(["\'])modal\1\]'), r'.ant-modal'),
    (re.compile(r'\[class\*=(["\'])composerSeat\1\]'), r'[data-testid=\1chat-composer\1]'),
    (re.compile(r'\[class\*=(["\'])root\1\]\[data-open\] \[class\*=(["\'])body\1\]'),
     r'.ant-popover:not(.ant-popover-hidden), .ant-dropdown:not(.ant-dropdown-hidden)'),
    # 引用来源消息（DSH 会话引用块）→ 我方消息内 markdown 块引用
    (re.compile(r'\[data-context-source=(["\'])true\1\]'), r'[data-bubble] blockquote'),
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

# CSS 专属位置映射（不进 hooks 转译）：桌面壳标题栏行 `[class*="titlebar"]`
# 在我方同位锚点=顶带（chat-top-band）。皮肤给标题栏行上深色渐变+金色下边线，
# 不映射则顶带保持透明（透出背景图），标题栏与花边檐口成两条叠在一起的条、
# 侧栏金框与檐口错位（用户 2026-11-10 报「顶部/底部挂载重叠、丑」）。
# 只对样式表成立：hooks 里同一选择器用于「品牌文字挂载」（titlebar-brand
# prepend），挂到顶带会把品牌塞成三区布局首子、挤歪与列对齐的区宽。
_DSH_POSITION_MAP_CSS_ONLY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\[class\*=(["\'])titlebar\1\]'), r'[data-testid=\1chat-top-band\1]'),
]


def _sub_position(text: str, *, css_only: bool = False) -> str:
    """DSH 位置词汇 → 我方锚点（CSS 选择器与 hooks.mjs JS 字符串同源转译；
    css_only=True 时追加只对样式表成立的映射，见 _DSH_POSITION_MAP_CSS_ONLY）。"""
    for pat, repl in _DSH_POSITION_MAP:
        text = pat.sub(repl, text)
    if css_only:
        for pat, repl in _DSH_POSITION_MAP_CSS_ONLY:
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
    new_header = _sub_position(header, css_only=True)
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
        "client bundle serve + api/dsh/{client-ui,official-ui,hooks/translate}"
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
    if path == _HOOKS_TRANSLATE_PATH and method == "POST":
        return _hooks_translate_response(raw_body)
    if path.startswith(_WEB_PROXY_PREFIX):
        return await _web_proxy_dispatch(path, method, raw_body, headers)
    if path.startswith(_CLIENT_BUNDLE_PREFIX):
        return _client_bundle_response(path)
    if path == _CLIENT_UI_PATH and method == "GET":
        return _client_ui_response()
    if path == _OFFICIAL_UI_PATH and method == "GET":
        return await _official_ui_response(query)
    return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}


# ── DSH 生态包管理端点（设计 §4.1 C1 五件套）+ 管理页（webview 自伺服） ──
# 端点前缀：插件端点路由前缀由内核固定（/ext/{pluginId}），设计表的逻辑路径
# /api/dsh/plugins* 在此落地为 /ext/dsh_adapter/api/dsh/plugins*。
# 业务面在 manager.DshPackageManager（审批/engines/依赖保护/状态机在彼处，
# 本层只做路由、body 解析、错误码→HTTP 状态映射）。

_HOOKS_TRANSLATE_PATH = "/ext/dsh_adapter/api/dsh/hooks/translate"
_WEB_PROXY_PREFIX = "/ext/dsh_adapter/web/"
# 客户端 UI 贡献清单（前端组件宿主据此装载插件 client bundle 并挂槽位）
_CLIENT_UI_PATH = "/ext/dsh_adapter/api/dsh/client-ui"
# 组件包文件供给（插件 client bundle 与其静态资源）
_CLIENT_BUNDLE_PREFIX = "/ext/dsh_adapter/client-bundle/"

# 官方插件管理页启动器：自研管理界面已删除，界面归 DSH 官方 Web 应用
# （@deepseek-ai/dsh-client-ui-plugin-manager）。本插件只负责把官方 origin
# 如实交给页面（status/url/reason），不在 AgentOS 侧另造一套列表。
_OFFICIAL_UI_PATH = "/ext/dsh_adapter/api/dsh/official-ui"

_MANAGER: Any | None = None


# ── 官方插件管理页启动器（自研界面已删除，界面归 DSH 官方） ──────────
# DSH 0.2.1 起官方自带 Web 侧插件管理面板（@deepseek-ai/dsh-client-ui-plugin-manager
# 装/启/停/卸/重试 + ui-plugin-market 的 Discover 市场源），它跑在**官方 DSH Web
# 应用**里、注册进 SPA 的 plugins.tab slot，不是独立 HTTP 页。因此"所见即官方"
# 的唯一形态是：把官方 origin 如实交给用户并在新窗口打开它——不在 AgentOS 侧
# 另造一套列表（代理渲染会立刻与官方漂移）。本插件只做「官方页在哪」的报告。

async def _official_ui_response(query: dict[str, str] | None = None) -> dict[str, Any]:
    """官方插件管理页：返回其访问地址（必要时把官方 Web 应用拉起来）。

    AgentOS 所见 = 官方 DSH Web 应用本身（同一 profile、同一 `pluginManager`），
    所以「插件列表一致」由构造保证，不做两份列表对账。地址缺失时如实给可判定
    原因（缺 Node 22 / 全栈未装 / 启动失败），不编造链接、不静默降级。

    ``query.ensure``（"1"/"true"）= 触发启动：启动在后台线程进行、立即返回
    ``starting``，前端轮询本端点直到 ``available``——避免 HTTP 调用被冷启动
    （首次装全栈可达数分钟）长时间占住。
    """
    from web_app import WebAppError, get_supervisor

    sup = get_supervisor()
    status = sup.status()
    if not status.available and not status.starting and _wants_ensure(query):
        try:
            await asyncio.to_thread(_start_in_background, sup)
        except WebAppError as exc:
            return _json_response({"available": False, "url": None, "starting": False,
                                   "reason": exc.message, "code": exc.code})
        status = sup.status()
    payload = {
        "available": status.available,
        "url": status.url,
        "origin": status.origin,
        "starting": status.starting,
        "reason": status.reason,
    }
    return _json_response(payload)


def _wants_ensure(query: dict[str, str] | None) -> bool:
    """``?ensure=1|true`` 判定（缺省不启动：只读状态查询不产生副作用）。"""
    raw = (query or {}).get("ensure")
    return str(raw).strip().lower() in {"1", "true", "yes"}


def _start_in_background(supervisor: Any) -> None:
    """后台启动官方 Web 应用（异常只记日志：状态由 supervisor 记录并按 reason 暴露）。"""
    import threading

    def _run() -> None:
        try:
            supervisor.start()
        except Exception:  # noqa: BLE001 - 失败原因已进 supervisor._error，端点如实回报
            logger.warning("[dsh_adapter] 官方 DSH Web 应用启动失败", exc_info=True)

    threading.Thread(target=_run, name="dsh-web-app-start", daemon=True).start()


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


# ── 客户端 UI 贡献（组件级挂载：DSH 槽位 → 灵汐页面位） ──────────────
# 插件前端是 client bundle（React 组件注册进 DSH 槽位），不是 HTML 页面：
# 清单端点把「哪个包在哪个槽位注册了什么」如实交给前端组件宿主，文件端点
# 供给 bundle 与其静态资源；槽位→页面位的映射在 client_ui.SLOT_PAGE_MAP。


def _client_ui_packages() -> tuple[Path, list[dict[str, Any]]]:
    """运行时工作区里「已装且启用」的 DSH 包（排除 profile 基建包）。"""
    import runtime_workspace as rw

    ws = Path(_manager().runtime_root)
    if not (ws / "node_modules").is_dir():
        return ws, []
    enabled = set(rw.profile_bundles(ws))
    packages = [
        p for p in rw.installed_packages(ws)
        if p["name"] not in rw.PROFILE_INFRA_PACKAGES and (not enabled or p["name"] in enabled)
    ]
    return ws, packages


def _client_ui_response() -> dict[str, Any]:
    """已装包的客户端 UI 贡献清单（槽位/映射页/未映射槽如实报告）。

    随安装/启停变化，必须禁缓存：浏览器缓存会让前端挂到已消失的槽位页。
    """
    from client_ui import client_ui_contributions

    _, packages = _client_ui_packages()
    resp = _json_response({"bundles": client_ui_contributions(packages)})
    resp["headers"]["Cache-Control"] = "no-store"
    return resp


def _client_bundle_response(path: str) -> dict[str, Any]:
    """组件包文件供给：``/client-bundle/<bundle>/<包内路径>``（穿越防护在 client_ui）。"""
    from client_ui import resolve_bundle_file

    rel = path[len(_CLIENT_BUNDLE_PREFIX):]
    segments = rel.split("/")
    if not rel or any(seg in ("", ".", "..") for seg in segments):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    if segments[0].startswith("@"):
        if len(segments) < 3:
            return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
        bundle, inner = "/".join(segments[:2]), segments[2:]
    else:
        if len(segments) < 2:
            return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
        bundle, inner = segments[0], segments[1:]
    found = resolve_bundle_file(Path(_manager().runtime_root), bundle, "/".join(inner))
    if found is None:
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    payload, content_type = found
    return {
        "status": 200,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-cache"},
        "body": base64.b64encode(payload).decode("ascii"),
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


# ── DSH 工具面进入工具（toolface 闭环） ─────────────────────────
# DSH 插件的 tools.register 注册进 DSH 宿主工具注册表；灵汐侧经桥
# tool/call RPC 执行。这里出两个泛化工具：list 供发现（LLM 先查再用）、
# call 供执行（bridge.call_tool 三级信封原样透传）。

_toolface_tools_cache: list[dict[str, Any]] | None = None


# output_schema 口径：内核 mcp_result 归一（②-a）把 {success,data,error} 信封
# 拆包，tool_core 校验的是拆包后的 **data 载荷**（2026-11-10 实测裁定：信封形
# 声明必红 expected object got array）。故此处声明 data 形状，非信封。
_TOOL_LIST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "parameters": {"type": "object"},
        },
        "required": ["name"],
    },
}

# DSH 工具载荷原样透传（形状由各 DSH 工具自决），六型联合 = 不约束。
_TOOL_CALL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": ["object", "array", "string", "number", "boolean", "null"],
}


@plugin.tool(
    name="dsh.tool.list",
    schema={"type": "object", "properties": {}},
    output_schema=_TOOL_LIST_OUTPUT_SCHEMA,
    render={"card": "generic"},
    description=(
        "List tools registered by installed DSH plugins in the DSH host "
        "(name/description/parameters). Call dsh.tool.call to execute one."
    ),
)
async def dsh_tool_list() -> dict[str, Any]:
    """DSH 宿主工具清单（bridge.list_tools 投影，失败 fail-visible）。"""
    global _toolface_tools_cache  # noqa: PLW0603
    from bridge import get_bridge

    tools = await get_bridge().list_tools()
    _toolface_tools_cache = tools if isinstance(tools, list) else None
    return {
        "success": True,
        "data": [
            {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or t.get("input_schema") or {},
            }
            for t in tools if isinstance(t, dict) and t.get("name")
        ],
        "error": None,
        "duration_ms": 0,
    }


@plugin.tool(
    name="dsh.tool.call",
    schema={
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "DSH 工具名（先 dsh.tool.list 查）"},
            "args": {"type": "object", "description": "工具参数（按其 parameters）"},
        },
        "required": ["tool"],
    },
    output_schema=_TOOL_CALL_OUTPUT_SCHEMA,
    render={"card": "generic"},
    description=(
        "Execute a tool registered by an installed DSH plugin (bridge tool/call). "
        "Discover names with dsh.tool.list first."
    ),
)
async def dsh_tool_call(tool: str = "", args: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行一个 DSH 宿主工具（信封原样透传；空名 fail-visible）。"""
    if not tool.strip():
        return {"success": False, "data": None, "error": "缺 tool 名（先 dsh.tool.list 查）", "duration_ms": 0}
    from bridge import get_bridge

    return await get_bridge().call_tool(tool.strip(), dict(args or {}))


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
