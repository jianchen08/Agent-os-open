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

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bridge import get_bridge as _get_bridge, shutdown_bridge
from translator import (
    discover_dsh_plugins,
    load_installed_plugins,
    load_plugin_config,
    translate_package,
    _plugin_enabled,
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
        if not _plugin_enabled(pkg.name, config):
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


@plugin.on_unload
async def _on_dsh_adapter_unload(params: dict) -> None:  # noqa: ARG001
    await shutdown_bridge()
    logger.info("dsh_adapter: node runtime bridge shut down")


# ── client_styles 静态 CSS 服务（contributes.client_styles 的拉取目标） ──
# 前端经 /ext/{pluginId}{path} 拉取 CSS（带 Bearer，仅 Enabled 插件可挂路由），
# 本 handler 从适配器 styles/ 目录读取（http_endpoints 声明精确路径，dispatcher
# 契约：body base64 原样回写）。

import base64  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

from translator import (  # noqa: E402
    SKIN_ASSET_EXTS,
    SKIN_CENTER_SKINS_DIR,
    describe_available_skins,
    list_available_skins,
    load_skin_selection,
    resolve_skin_background,
    resolve_skin_css,
    translate_background_css,
)

_STYLES_DIR = Path(__file__).parent / "styles"
# dsh-bg 演示残留已撤（与皮肤通道 body 背景打架，2026-08-21）；文件保留于 styles/
_STYLE_ROUTES: dict[str, tuple[str, str]] = {}
# 皮肤令牌层注入路由：实际文件由 config/dsh_adapter.yaml 的 skin 字段动态
# 决定（skin-center 的 skin.css）；未选择时返回空 CSS（200，注入空 style
# 无副作用，避免前端拉取 404 噪音）。DOM 补丁层（patches.css/hooks.mjs）
# 不接入——选择器只对 DSH Web UI 的 DOM 有效。
_SKIN_CSS_ROUTE = "/ext/dsh_adapter/styles/skin.css"
_SKIN_ASSET_ROUTE_PREFIX = "/ext/dsh_adapter/styles/skin-assets/"
_SKIN_DISABLED_CSS = "/* dsh skin not selected (config/dsh_adapter.yaml: skin: <id>|none) */\n"
_SKIN_LIST_ROUTE = "/ext/dsh_adapter/skins"
_SKIN_SELECT_ROUTE = "/ext/dsh_adapter/skins/current"


def _resolve_skin_route() -> tuple[str, bytes] | None:
    """按配置解析皮肤 CSS（None = 未选/无效 → 空注释 CSS）。

    注入组合（区域语义，2026-08-21 重写）：①皮肤原文 :root 块（--dsw-*
    令牌与字体，先注入）→ ②灵汐适配层（--ds-* 令牌全套接管 + 布局：
    header 收图标条/侧栏半透明/工作区实底，覆盖①冲突项）→ ③聊天区
    立绘背景（区域化，不糊工作区）。皮肤原文其余部分（DOM 补丁层）不注入。
    """
    skin = load_skin_selection()
    if skin is None:
        return None
    css = resolve_skin_css(skin)
    if css is None:
        logger.warning(
            "dsh_adapter: skin %r not found (available: %s); injecting empty css",
            skin, ", ".join(list_available_skins()) or "<none>",
        )
        return None
    try:
        text = css.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("dsh_adapter: skin css read failed (%s): %s", skin, e)
        return None
    from translator import _ROOT_BLOCK_RE, translate_skin_adaptation

    parts: list[str] = []
    root_block = _ROOT_BLOCK_RE.search(text)
    if root_block:
        parts.append("/* skin :root tokens (verbatim) */\n:root {" + root_block.group(1) + "}")
    parts.append(translate_skin_adaptation(skin))
    bg = resolve_skin_background(skin)
    if bg is not None:
        parts.append(translate_background_css(bg, f"{_SKIN_ASSET_ROUTE_PREFIX}{skin}"))
    return skin, ("\n\n".join(parts) + "\n").encode("utf-8")


def _skin_config_path() -> str:
    """config 目录路径（复用 translator 的项目根解析）。"""
    from translator import _project_root

    return str(Path(_project_root()) / "config")


def _skin_list_payload() -> dict[str, Any]:
    """动态皮肤清单（运行时读 skin-center 装载现状——设置页数据源）。"""
    from translator import describe_available_skins

    skins = describe_available_skins()
    return {
        "current": load_skin_selection(),
        "count": len(skins),
        "skins": skins,
    }


def _select_skin(raw_body: str) -> dict[str, Any]:
    """PUT /ext/dsh_adapter/skins/current——写回 config 的 skin 字段。

    body: ``{"skin": "<id>"}``（id 必须 ∈ 当前装载皮肤；"none" = 关闭注入）。
    写回用文本级替换（保住 yaml 注释与 plugins 段）；skin 行缺失则尾追加。
    """
    try:
        body = json.loads(base64.b64decode(raw_body).decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        return {"status": 400, "headers": {}, "body": "bad json body", "body_encoding": "utf-8"}
    skin = body.get("skin") if isinstance(body, dict) else None
    if skin not in (*list_available_skins(), "none"):
        return {"status": 400, "headers": {}, "body": f"unknown skin: {skin!r}", "body_encoding": "utf-8"}
    cfg_path = Path(_skin_config_path()) / "dsh_adapter.yaml"
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("dsh_adapter: skin select read config failed: %s", e)
        return {"status": 500, "headers": {}, "body": "config read failed", "body_encoding": "utf-8"}
    new_line = f"skin: {skin}\n"
    if re.search(r"^skin:.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^skin:.*$", new_line.rstrip("\n"), text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + "\n" + new_line
    try:
        cfg_path.write_text(text, encoding="utf-8")
    except OSError as e:
        logger.warning("dsh_adapter: skin select write config failed: %s", e)
        return {"status": 500, "headers": {}, "body": "config write failed", "body_encoding": "utf-8"}
    logger.info("dsh_adapter: skin selected -> %s", skin)
    return {"status": 200, "headers": {"content-type": "application/json"}, "body": "{}", "body_encoding": "utf-8"}


_CONTENT_TYPES = {
    ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".svg": "image/svg+xml", ".gif": "image/gif", ".avif": "image/avif",
}


def _serve_skin_asset(path: str) -> dict[str, Any]:
    """serve 皮肤背景图资产（/ext/dsh_adapter/styles/skin-assets/<skin>/<file>）。

    白名单约束：皮肤 id 必须在装载的 skin-center 内、文件扩展名限图片、
    resolve 后必须落在 skins 目录内（防穿越）。
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
    return {
        "status": 200,
        "headers": {"content-type": ct},
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
    """serve client_styles 贡献的静态 CSS（dispatcher 契约：body base64 原样回写）。"""
    if path == _SKIN_LIST_ROUTE and method == "GET":
        payload = json.dumps(_skin_list_payload(), ensure_ascii=False)
        return {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(payload.encode("utf-8")).decode(),
            "body_encoding": "base64",
        }
    if path == _SKIN_SELECT_ROUTE and method == "PUT":
        return _select_skin(raw_body)
    if path == _SKIN_CSS_ROUTE:
        resolved = _resolve_skin_route()
        if resolved is None:
            body = _SKIN_DISABLED_CSS.encode()
        else:
            body = resolved[1]
        return {
            "status": 200,
            # 皮肤可经 PUT 热切换，禁缓存避免刷新后仍见旧皮肤（dispatcher 原样回写）
            "headers": {"content-type": "text/css", "cache-control": "no-cache"},
            "body": base64.b64encode(body).decode(),
            "body_encoding": "base64",
        }
    if path.startswith(_SKIN_ASSET_ROUTE_PREFIX):
        return _serve_skin_asset(path)
    route = _STYLE_ROUTES.get(path)
    if route is None or method != "GET":
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    content_type, filename = route
    try:
        body = (_STYLES_DIR / filename).read_bytes()
    except OSError as e:
        logger.warning("dsh_adapter: style css read failed: %s", e)
        return {"status": 500, "headers": {}, "body": "", "body_encoding": "utf-8"}
    return {
        "status": 200,
        "headers": {"content-type": content_type},
        "body": base64.b64encode(body).decode(),
        "body_encoding": "base64",
    }


if __name__ == "__main__":
    plugin.run()
