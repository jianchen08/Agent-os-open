"""DSH 客户端 UI 贡献：client 入口解析 / 槽位扫描 / 槽位映射 / 组件包文件服务。

DSH 插件的前端不是 HTML 页面，而是 client bundle：包清单 ``exports["./client"]``
指向一个 ``window.__ModuleLoader__.load({id, factory})`` 包装的模块，``apply(ctx)``
里用 ``ctx.slots.register({name, ...}, Component)`` 往客户端槽位注册 React 组件。

本模块只做静态事实提取（不执行 DSH 代码）与文件供给：

- ``resolve_client_entry``：按包清单解析 client 入口相对路径；
- ``scan_client_slots`` / ``scan_client_injects``：正则提取注册的槽位与依赖槽位；
- ``page_target``：DSH 槽位 → 灵汐页面位。槽位词汇与语义映射归 translator
  （``DSH_SLOT_LINGXI_MAP`` 单源），本模块只声明「哪些灵汐挂载位的渲染面已
  接通」；未接通的如实报告，不生成占位页面；
- ``client_ui_contributions``：把已装包聚合成前端消费的贡献清单；
- ``resolve_bundle_file``：组件包文件服务（路径穿越防护 + 后缀白名单）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# 包清单里的 client 入口键（DSH 约定：exports["./client"]）
CLIENT_EXPORT_KEY = "./client"
# 清单未声明时的约定回退（多数社区包直接放 lib/client.js）
FALLBACK_CLIENT_ENTRY = "lib/client.js"

# 已接通渲染面的灵汐挂载位 → 页面位声明（space/slot）。
# 词汇与语义映射归 translator（DSH_SLOT_LINGXI_MAP 单源）；本表只表达
# 「前端有渲染器接住这个挂载位」。未在此表的映射 = 有语义位但渲染未接通，
# 如实报告（pending），不生成页面。
ENABLED_LINGXI_SLOTS: dict[str, dict[str, str]] = {
    # 右侧栏标签页 → 工作区标签页（工作区里的一页，非独立 URL 页面）
    "workspaceTabs": {"space": "workspace", "slot": "tab"},
    # 设置分节 → 设置页声明页（settings hub 左导航条目，renderPageContent 分发）
    "settingsPanels": {"space": "settings", "slot": "panel"},
}

# 生成页 id 前缀：用于同步时区分「插件自声明页」与「按贡献生成页」
GENERATED_PAGE_PREFIX = "dsh_ui_"

_REGISTER_RE = re.compile(r"slots\s*\.\s*register\s*\(\s*\{(?P<meta>[^{}]{0,400})\}", re.S)
_INJECT_RE = re.compile(r"slots\s*\.\s*inject\s*\(\s*['\"](?P<name>[^'\"]+)['\"]")
_NAME_RE = re.compile(r"name\s*:\s*['\"](?P<value>[^'\"]+)['\"]")
_ID_RE = re.compile(r"(?:id|key)\s*:\s*['\"](?P<value>[^'\"]+)['\"]")
_ORDER_RE = re.compile(r"order\s*:\s*(?P<value>\d+)")
_LABEL_RE = re.compile(r"label\s*:\s*['\"](?P<value>[^'\"]+)['\"]")
_REQUIRE_RE = re.compile(r"require\(\s*['\"](?P<name>[^'\"]+)['\"]\s*\)")

# 组件宿主能提供的宿主依赖（前端 require 映射）；bundle 依赖超出此集合 =
# 当前渲染面接不住（如 dsh-client-ui-primitives 带 CSS Modules，需打包期处理）
SUPPORTED_REQUIRES: frozenset[str] = frozenset({
    "react", "react-dom", "react-dom/client", "react/jsx-runtime", "react/jsx-dev-runtime",
    "styles",
})

# 可选宿主依赖：缺失不阻断挂载、由宿主以空桩代替的模块。
# 现为空集——实测 dsh-pet 除 primitives 外还需 connection/remote 等宿主服务，
# 仅补空桩也注册不出组件，故仍按硬阻断处理（不生成空页面入口）。
OPTIONAL_REQUIRES: frozenset[str] = frozenset()

# 组件包文件服务：只放行浏览器运行组件必需的静态资源
ALLOWED_SUFFIXES: frozenset[str] = frozenset({
    ".js", ".mjs", ".cjs", ".css", ".json", ".map",
    ".svg", ".png", ".webp", ".jpg", ".jpeg", ".gif",
    ".woff", ".woff2", ".ttf", ".otf",
})

CONTENT_TYPES: dict[str, str] = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".cjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


def _export_target(value: Any) -> str | None:
    """``exports`` 子项归一化：字符串 / ``{default}`` / ``{import:{default}}``。"""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("default", "import", "require", "browser"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
            if isinstance(inner, dict):
                nested = inner.get("default")
                if isinstance(nested, str) and nested:
                    return nested
    return None


def resolve_client_entry(package_json: dict[str, Any], package_dir: str | Path | None = None) -> str | None:
    """解析 client 入口相对路径（``exports["./client"]`` > ``dsh.client.entry``）。

    清单未声明时回退 ``lib/client.js``（存在才算）。返回值统一为包内相对路径。
    """
    exports = package_json.get("exports")
    if isinstance(exports, dict):
        entry = _export_target(exports.get(CLIENT_EXPORT_KEY))
        if entry:
            return entry.lstrip("./")
    dsh = package_json.get("dsh")
    if isinstance(dsh, dict):
        client = dsh.get("client")
        if isinstance(client, dict):
            entry = client.get("entry")
            if isinstance(entry, str) and entry:
                return entry.lstrip("./")
    if package_dir is not None and (Path(package_dir) / FALLBACK_CLIENT_ENTRY).is_file():
        return FALLBACK_CLIENT_ENTRY
    return None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group("value") if match else None


def scan_client_slots(text: str) -> list[dict[str, Any]]:
    """提取 ``slots.register({...}, Component)`` 的槽位元数据（按槽位名去重保序）。

    压缩/动态构造的注册写法扫不到（静态信号能力边界）：返回空列表，由调用方
    按 ``scanned`` 字段如实报告，不臆造。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _REGISTER_RE.finditer(text):
        meta = match.group("meta")
        name = _first(_NAME_RE, meta)
        if not name or name in seen:
            continue
        seen.add(name)
        order = _first(_ORDER_RE, meta)
        out.append({
            "name": name,
            "id": _first(_ID_RE, meta),
            "label": _first(_LABEL_RE, meta),
            "order": int(order) if order else None,
        })
    return out


def scan_client_injects(text: str) -> list[str]:
    """提取 ``slots.inject('<槽位>', cb)`` 的依赖槽位（去重保序）。"""
    out: list[str] = []
    for match in _INJECT_RE.finditer(text):
        name = match.group("name")
        if name not in out:
            out.append(name)
    return out


def scan_client_requires(text: str) -> list[str]:
    """提取 bundle 的宿主依赖名（``require('<name>')``，去重保序）。"""
    out: list[str] = []
    for match in _REQUIRE_RE.finditer(text):
        name = match.group("name")
        if name not in out:
            out.append(name)
    return out


def unsupported_requires(requires: list[str]) -> list[str]:
    """bundle 依赖里宿主未提供的项（含可选依赖）。"""
    return [name for name in requires if name not in SUPPORTED_REQUIRES]


def blocking_requires(requires: list[str]) -> list[str]:
    """硬阻断依赖：宿主未提供且不可空桩（空 = 可挂载）。"""
    return [name for name in unsupported_requires(requires) if name not in OPTIONAL_REQUIRES]


def _slug(name: str) -> str:
    """包名/槽位名 → 页面 id 片段（``@scope/pkg`` → ``scope_pkg``）。"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def page_target(slot_name: str) -> dict[str, str] | None:
    """槽位 → 已接通的灵汐页面位；无映射或渲染未接通返回 None。"""
    from translator import map_dsh_slot

    lingxi_slot = str(map_dsh_slot(slot_name).get("lingxi_slot") or "")
    return ENABLED_LINGXI_SLOTS.get(lingxi_slot)


def page_for_slot(bundle: str, slot: dict[str, Any], title: str | None = None) -> dict[str, Any] | None:
    """槽位 → 灵汐页面声明；无已接通映射返回 None（不生成占位页）。"""
    target = page_target(slot["name"])
    if target is None:
        return None
    # 页面 id 按包+槽位：一个包可注册多个已接通槽位，各自成页（工作区页/设置页并存）
    page_id = f"{GENERATED_PAGE_PREFIX}{_slug(bundle)}_{_slug(str(slot['name']))}"
    order = slot.get("order")
    return {
        "id": page_id,
        "title": title or slot.get("label") or bundle,
        "space": target["space"],
        "slot": target["slot"],
        "path": f"/p/{page_id}",
        "widget": "dsh-slot",
        "props": {
            "bundle": bundle,
            "slot": slot["name"],
            "contributionId": slot.get("id"),
        },
        "order": 60 + (int(order) if isinstance(order, int) else 0),
        "description": f"DSH 插件 {bundle} 注册在 {slot['name']} 的组件（原样挂载）",
    }


def _lingxi_slot_of(slot_name: str) -> str:
    """槽位对应的灵汐挂载位名（无对应为 ``direct``）。"""
    from translator import map_dsh_slot

    return str(map_dsh_slot(slot_name).get("lingxi_slot") or "direct")


def client_ui_contributions(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """已装包 → 前端消费的客户端 UI 贡献清单（只含有 client 入口的包）。"""
    out: list[dict[str, Any]] = []
    for pkg in packages:
        name = str(pkg.get("name") or "")
        pkg_dir = pkg.get("dir")
        if not name or not pkg_dir:
            continue
        manifest = pkg.get("package_json") or {}
        entry = resolve_client_entry(manifest, pkg_dir)
        if not entry:
            continue
        client_path = Path(str(pkg_dir)) / entry
        try:
            text = client_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        slots = scan_client_slots(text)
        requires = scan_client_requires(text)
        blocked = blocking_requires(requires)
        dsh = manifest.get("dsh") if isinstance(manifest.get("dsh"), dict) else {}
        title = dsh.get("title") if isinstance(dsh, dict) else None
        pages = [] if blocked else [p for p in (page_for_slot(name, slot, title) for slot in slots) if p]
        unmapped = [s["name"] for s in slots if page_target(s["name"]) is None
                    and _lingxi_slot_of(s["name"]) == "direct"]
        pending = [s["name"] for s in slots if page_target(s["name"]) is None
                   and _lingxi_slot_of(s["name"]) != "direct"]
        out.append({
            "bundle": name,
            "version": str(pkg.get("version") or ""),
            "entry": entry,
            "entry_url": f"/ext/dsh_adapter/client-bundle/{name}/{entry}",
            "scanned": bool(slots),
            "slots": slots,
            "injects": scan_client_injects(text),
            "requires": requires,
            "blocked": blocked,
            "pages": pages,
            "unmapped": unmapped,
            "pending": pending,
        })
    return out


def _package_base(root: str | Path, bundle: str) -> Path | None:
    """包名 → node_modules 下的包目录（拒绝穿越与空段）。"""
    if not bundle or bundle.startswith(("/", "\\")) or ".." in bundle.split("/"):
        return None
    parts = [p for p in bundle.split("/") if p]
    if not parts or (parts[0].startswith("@") and len(parts) < 2) or (not parts[0].startswith("@") and len(parts) > 1):
        return None
    return Path(root) / "node_modules" / Path(*parts)


def resolve_bundle_file(root: str | Path, bundle: str, rel_path: str) -> tuple[bytes, str] | None:
    """组件包文件读取：返回 ``(bytes, content_type)``；越界/未白名单/不存在 → None。"""
    base = _package_base(root, bundle)
    if base is None:
        return None
    try:
        base_resolved = base.resolve(strict=True)
    except OSError:
        return None
    candidate = (base / rel_path).resolve()
    if base_resolved != candidate and base_resolved not in candidate.parents:
        return None
    if candidate.suffix.lower() not in ALLOWED_SUFFIXES or not candidate.is_file():
        return None
    try:
        payload = candidate.read_bytes()
    except OSError:
        return None
    return payload, CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
