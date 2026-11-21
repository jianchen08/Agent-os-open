#!/usr/bin/env python
"""OpenAPI 说明书生成器 + CI 漂移闸（整改方案 §6 企业级补课第 1 项）。

从内核静态路由注册面（server.rs + http_dispatcher.rs 的 `.route(...)` 调用）
生成机器可读的 API 字典 docs/api/openapi.yaml（OpenAPI 3.1），供外部工具
生成客户端代码 / 导入 Postman / 网关对接。

单一真值源（不自建第二套分类）：
  - 枚举键 = 两文件注册调用里的路径字面量（与 P-2 对账测试
    server/tests.rs auth_surface_reconciliation_tests 同型扫描，commit
    a47d44e21 / ADR 2026-10-10 锚定复用）；
  - security 字段复用 P-2 鉴权分类：解析 server.rs 的 AUTH_* 常量组 +
    镜像 classify_auth_surface 判定序（豁免精确 → 任意用户精确 →
    受保护前缀/精确 → 域外）——匿名豁免/域外 = security: []，
    受保护/任意用户 = BearerAuth。

收面判定（/ext 排除）：openapi.yaml 只收内核静态注册面
（/api/v1/** + /uploads/** + /ws/** + /metrics + /health）。
/ext/{*rest} 是插件声明制动态面（manifest http_endpoints），静态字典无法
枚举插件端点——不收录，仅在 info.description 注明经 /api/v1/schema
运行时发现；/api/v1/datasource/{*rest} 代理收内（内核静态前缀，任意
method 同一 handler 透传，真实端点集同样由插件声明面决定）。

响应骨架策略（宁缺毋滥，不臆造字段）：router 注册形态只保证路径/方法/
路径参数可提取；响应统一给策略化骨架——所有操作 200（/ws/chat 为 101
WebSocket 升级）；post/put/patch 给 422（axum Json 提取器拒绝）；
Protected 给 401/403（GET 需 admin/viewer，写需 admin）；AnyUser 给 401；
个别豁免端点按 P-2 行为矩阵补充（login 坏凭据 400；me/refresh/logout/
change-password handler 自校验 token 无 token 即 401）。一律不写响应体
schema——结构以 handler 源码为准，生成物必须可溯源。

漂移闸：--check 重新生成与库内 docs/api/openapi.yaml 逐字节比对，
不一致 exit 1（改路由忘了再生成即红）；fail-closed——源文件缺失 /
AUTH_* 常量抽不到 / 路由面扫描退化（条数低于对账性质断言）/ 路径未落入
鉴权面任何一侧 / 清单条目过期 / 受保护前缀悬空 / 同路径重复注册，
同样 exit 1（防退化成永绿假闸，先例 scripts/check_kernel_port_sync.py）。

用法：
    python scripts/gen_openapi.py            # 生成 docs/api/openapi.yaml
    python scripts/gen_openapi.py --check    # 漂移闸：不一致 exit 1

门禁归属：run_gates.py 的 openapi-drift gate（fast），
CI 经 .github/workflows/ci.yml traceability-gate job 调度。

零第三方依赖（CI 轻量 job 无 pip install）：YAML 由本文件内嵌确定性发射器
产出（标量全双引号 / 字典按构造序 / 路径字典序 / 方法固定序），保证逐字节
可复现。不写 servers 段——端口簿记归 kernel-port-sync 闸（整改K4），
本文件不引入第三处平行簿记。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 源码登记点（路由面/常量组搬移时同刀改这里）──────────────────────
SERVER_RS = ROOT / "kernel" / "crates" / "api" / "src" / "server.rs"
AUTH_SURFACE_RS = ROOT / "kernel" / "crates" / "api" / "src" / "server" / "auth_surface.rs"
DISPATCHER_RS = ROOT / "kernel" / "crates" / "api" / "src" / "http_dispatcher.rs"
OUT_PATH = ROOT / "docs" / "api" / "openapi.yaml"

# 插件动态声明面：注册在 http_dispatcher.rs，端点集由各插件 manifest
# http_endpoints 运行时决定——静态字典不枚举（见模块 docstring 收面判定）。
EXT_DYNAMIC_PATH = "/ext/{*rest}"

# 对账性质断言（镜像 server/tests.rs：注册面条数过低即扫描器退化假绿）
MIN_ROUTE_COUNT = 40

METHOD_ORDER = ("get", "post", "put", "patch", "delete")
ANY_METHOD_EXPANSION = METHOD_ORDER  # any() 透传面展开的常用方法集

# 响应骨架补充表（P-2 行为矩阵，server/tests.rs 行为测试在档）：
# login 坏凭据 400；四端点 AUTH_ANONYMOUS_ALLOW 逐项注释自述
# "handler 自校验 token"——无 token 即 401（区别于豁免面其余成员）。
ANON_EXTRA_400 = frozenset({"/api/v1/auth/login"})
ANON_SELF_VALIDATED_401 = frozenset(
    {
        "/api/v1/auth/me",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/change-password",
    }
)

# P-2 鉴权面五常量（server.rs 单一真值源；缺任一即 fail-closed）
AUTH_CONSTS = (
    "AUTH_PROTECTED_PREFIXES",
    "AUTH_PROTECTED_EXACT",
    "AUTH_ANONYMOUS_ALLOW",
    "AUTH_ANY_USER_ALLOW",
    "AUTH_OUTSIDE_MIDDLEWARE",
)

# 分类 → 操作描述 / security（语义照抄 server.rs AuthSurfaceClass 注释）
AUTH_CLASS_DESC = {
    "AnonymousAllow": "匿名豁免端点（豁免缘由见 server.rs AUTH_ANONYMOUS_ALLOW 逐项注释）",
    "AnyUser": "任意已认证用户可访问（无角色门——票据只是本人 WS 握手凭证）",
    "Protected": "受保护端点（GET 需 admin/viewer，写方法需 admin）",
    "OutsideMiddleware": "鉴权层域外端点（探测面，鉴权另有所属）",
}
AUTH_CLASS_SECURITY: dict[str, list[dict[str, list[str]]]] = {
    "AnonymousAllow": [],
    "AnyUser": [{"BearerAuth": []}],
    "Protected": [{"BearerAuth": []}],
    "OutsideMiddleware": [],
}

# 路径级补充说明（源码注释原文摘录，键为注册面路径字面量）
PATH_NOTES = {
    "/metrics": "Prometheus 文本格式导出（运维契约：URL 稳定优先，内核自持）",
    "/uploads/{filename}": "附件媒体取流（前端统一走鉴权取流加载器，无裸直链消费面）",
    "/api/v1/datasource/{*rest}": (
        "数据源代理透传（改写 /ext/{rest} 复用插件分发，任意 method 同一 handler；"
        "真实端点集由插件 http_endpoints 声明面决定）"
    ),
}


class DriftGateError(Exception):
    """fail-closed 断言失败（解析退化/对账不过/源缺失）——闸显式红。"""


# ── 数据模型 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Operation:
    method: str
    handler: str  # operationId（短名）
    handler_ref: str  # 源码原文 token（可溯源标注）
    any_method: bool  # any() 透传/裸 handler：任意 method 同一 handler


@dataclass(frozen=True)
class Route:
    path: str  # 注册面路径字面量（含 axum {*tail} 形态）
    source: str  # 登记点文件名（溯源标注）
    ops: tuple[Operation, ...]


# ── 解析（纯函数，单测注入伪源）─────────────────────────────────────
def _strip_tests(src: str) -> str:
    """截掉测试面（首个 #[cfg(test)] / mod tests 起）——测试内构造的
    Router 不是注册面（Rust 侧 split("mod tests") 的更保守镜像）。"""
    cut = len(src)
    for marker in ("#[cfg(test)]", "\nmod tests"):
        pos = src.find(marker)
        if pos != -1:
            cut = min(cut, pos)
    return src[:cut]


ROUTE_MARKER = ".route("
# 方法路由器调用：get/post/put/patch/delete/any + handler token。
# (?<!\w) 排除标识符内部命中（get_pipeline_config_with_etag 不误匹配 get）；
# 允许 axum::routing::patch 形态（: 非 \w 不拦截前缀）。
METHOD_CALL_RE = re.compile(r"(?<!\w)(get|post|put|patch|delete|any)\s*\(\s*([A-Za-z0-9_:]+)")
METHOD_CALL_COUNT_RE = re.compile(r"(?<!\w)(?:get|post|put|patch|delete|any)\s*\(")
HANDLER_TOKEN_RE = re.compile(r"[A-Za-z0-9_:]+")


def _read_router_expr(src: str, start: int) -> tuple[str, int]:
    """读取 `.route(` 第二参表达式（到 route 调用自身收尾括号前，括号配平）。

    返回 (表达式, 收尾括号在 src 中的下标)。
    """
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return src[start:i], i
            depth -= 1
    raise DriftGateError("路由表达式括号不配平——源码形态变化，须同刀更新解析器")


def _parse_methods(expr: str) -> list[Operation]:
    """方法路由器表达式 → 操作列表。any()/裸 handler 展开为常用方法集。"""
    calls = METHOD_CALL_RE.findall(expr)
    if len(calls) != len(METHOD_CALL_COUNT_RE.findall(expr)):
        raise DriftGateError(
            f"方法/handler 抽取不完整（闭包等非 token 形态）：{expr.strip()[:80]}——源码形态变化，须同刀更新解析器"
        )
    ops: list[Operation] = []
    for method, handler_ref in calls:
        short = handler_ref.rsplit("::", 1)[-1]
        if method == "any":
            ops.extend(_any_method_ops(short, handler_ref))
        else:
            ops.append(Operation(method=method, handler=short, handler_ref=handler_ref, any_method=False))
    if ops:
        return ops
    # 裸 handler（直接传函数名，内部构造 any() 方法路由器——http_dispatcher
    # 的通配/代理面）：任意 method 同一 handler。
    token = expr.strip()
    if HANDLER_TOKEN_RE.fullmatch(token):
        return _any_method_ops(token.rsplit("::", 1)[-1], token)
    raise DriftGateError(f"路由表达式不可解析：{expr.strip()[:80]}——须同刀更新解析器")


def _any_method_ops(short: str, handler_ref: str) -> list[Operation]:
    # any() 无 OpenAPI 通配方法语法：展开常用方法集并打 x-any-method 标记；
    # operationId 加方法后缀避免五方法同名冲突。
    return [
        Operation(method=m, handler=f"{short}_{m}", handler_ref=handler_ref, any_method=True)
        for m in ANY_METHOD_EXPANSION
    ]


def parse_routes(src: str, source_name: str) -> list[Route]:
    """提取 `.route("路径", 方法路由器)` 注册对——P-2 枚举键扫描的增补版
    （路径字面量同型抽取，另抽方法/handler）。"""
    body = _strip_tests(src)
    routes: list[Route] = []
    rest = body
    while True:
        pos = rest.find(ROUTE_MARKER)
        if pos == -1:
            return routes
        after = rest[pos + len(ROUTE_MARKER) :].lstrip()
        if not after.startswith('"'):
            rest = after  # 首参非字符串字面量（registry.route(&ns) 类）自然落空
            continue
        end = after.find('"', 1)
        if end == -1:
            raise DriftGateError("路径字面量未闭合——源码形态变化，须同刀更新解析器")
        path = after[1:end]
        tail = after[end + 1 :].lstrip()
        if not tail.startswith(","):
            rest = after[end:]
            continue
        expr_start = len(after) - len(tail) + 1
        while expr_start < len(after) and after[expr_start].isspace():
            expr_start += 1
        expr, close = _read_router_expr(after, expr_start)
        routes.append(Route(path=path, source=source_name, ops=tuple(_parse_methods(expr))))
        rest = after[close:]


AUTH_CONST_RE_TMPL = r"pub(?:\(\w+\))? const {name}\s*:[^=]*=\s*&\[(?P<body>.*?)\]"
AUTH_ENTRY_RE = re.compile(r'"([^"]+)"')


def parse_auth_lists(server_src: str, auth_surface_src: str = "") -> dict[str, list[str]]:
    """解析 P-2 鉴权面五常量（server.rs + server/auth_surface.rs 双源拼接抽取）。

    缺任一常量/空清单即 fail-closed——security 分类不允许退化为拍脑袋。
    """
    body = _strip_tests(server_src) + _strip_tests(auth_surface_src)
    lists: dict[str, list[str]] = {}
    for name in AUTH_CONSTS:
        m = re.search(AUTH_CONST_RE_TMPL.format(name=name), body, re.S)
        if not m:
            raise DriftGateError(f"server.rs/auth_surface.rs 抽不到 {name} 声明——常量改名/搬移须同刀更新本脚本")
        entries = AUTH_ENTRY_RE.findall(m.group("body"))
        if not entries:
            raise DriftGateError(f"{name} 清单为空——与 P-2 常量组现状不符，拒绝生成")
        lists[name] = entries
    return lists


def classify_auth(path: str, lists: dict[str, list[str]]) -> str | None:
    """镜像 server.rs classify_auth_surface 判定序（豁免精确 → 任意用户精确
    → 受保护前缀/精确 → 域外）；未落入任何一侧返回 None。"""
    if path in lists["AUTH_ANONYMOUS_ALLOW"]:
        return "AnonymousAllow"
    if path in lists["AUTH_ANY_USER_ALLOW"]:
        return "AnyUser"
    if any(path.startswith(p) for p in lists["AUTH_PROTECTED_PREFIXES"]) or path in lists["AUTH_PROTECTED_EXACT"]:
        return "Protected"
    if path in lists["AUTH_OUTSIDE_MIDDLEWARE"]:
        return "OutsideMiddleware"
    return None


# ── 对账（镜像 server/tests.rs 四断言 + 重复注册防歧义）────────────
def reconcile(routes: list[Route], lists: dict[str, list[str]], min_routes: int) -> list[str]:
    by_path: dict[str, list[str]] = {}
    for r in routes:
        by_path.setdefault(r.path, []).append(r.source)
    violations: list[str] = []
    dupes = sorted(p for p, srcs in by_path.items() if len(srcs) > 1)
    if dupes:
        violations.append(f"同路径重复注册（openapi 面歧义）: {dupes}")
    if len(by_path) < min_routes:
        violations.append(f"注册面扫描仅得 {len(by_path)} 条（< {min_routes}）——扫描器大概率失效")
    for p in sorted(by_path):
        if classify_auth(p, lists) is None:
            violations.append(
                f"路由 {p} 未落入鉴权面任何一侧——新路由须落在受保护前缀内，"
                "或显式登记进对应清单（ADR 2026-10-10 枚举制）"
            )
    registered = set(by_path)
    for list_name in ("AUTH_ANONYMOUS_ALLOW", "AUTH_ANY_USER_ALLOW", "AUTH_OUTSIDE_MIDDLEWARE"):
        for e in lists[list_name]:
            if e not in registered:
                violations.append(f"{list_name} 条目 {e} 不在注册面（过期条目须随路由移除）")
    for pfx in lists["AUTH_PROTECTED_PREFIXES"]:
        if not any(p.startswith(pfx) for p in registered):
            violations.append(f"受保护前缀 {pfx} 未覆盖任何注册路由（悬空前缀）")
    return violations


# ── OpenAPI 文档构造（确定性输出）───────────────────────────────────
def _translate_path(path: str) -> str:
    """axum 路径形态 → OpenAPI 路径形态（{*rest} 尾捕获 → {rest} 单参数）。"""
    return path.replace("{*", "{")


PATH_PARAM_RE = re.compile(r"\{(\*?)([A-Za-z0-9_]+)\}")


def _path_params(path: str) -> list[dict]:
    out: list[dict] = []
    for wild, name in PATH_PARAM_RE.findall(path):
        p: dict = {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
        if wild:
            p["description"] = "尾段通配（axum {*name} 尾捕获；OpenAPI 无通配参数语法，展开为单一参数）"
        out.append(p)
    return out


def _ws_chat_query_params() -> list[dict]:
    # 票据自鉴权握手参数（server.rs 路由注释原文：?ticket= 一次性票据 /
    # ?token= Bearer 兼容保留）
    return [
        {
            "name": "ticket",
            "in": "query",
            "required": False,
            "description": "一次性 WS 握手票据（POST /api/v1/ws-ticket 签发，60s 单次消费）",
            "schema": {"type": "string"},
        },
        {
            "name": "token",
            "in": "query",
            "required": False,
            "description": "Bearer token 直连（兼容保留；推荐路径是 ticket，token 不进 URL query）",
            "schema": {"type": "string"},
        },
    ]


def _responses(path: str, op: Operation, auth_class: str) -> dict:
    r: dict = {}
    if path == "/ws/chat":
        r["101"] = {"description": "WebSocket 升级（票据自鉴权：?ticket= / ?token=，无效即拒绝）"}
    else:
        r["200"] = {"description": "成功（响应体结构以 handler 源码为准，本字典不臆造响应字段）"}
    if path in ANON_EXTRA_400:
        r["400"] = {"description": "凭据错误（业务码——P-2 行为矩阵）"}
    if auth_class in {"Protected", "AnyUser"} or path in ANON_SELF_VALIDATED_401:
        r["401"] = {"description": "未认证或 token 无效"}
    if auth_class == "Protected":
        r["403"] = {"description": "角色不足（GET 需 admin/viewer，写方法需 admin）"}
    if op.method in ("post", "put", "patch") and not op.any_method:
        r["422"] = {"description": "请求体/参数提取失败（axum Json 提取器拒绝）"}
    r["500"] = {"description": "服务器内部错误"}
    return r


def _operation(route: Route, op: Operation, auth_class: str) -> dict:
    o: dict = {"operationId": op.handler}
    desc = AUTH_CLASS_DESC[auth_class]
    if route.path in PATH_NOTES:
        desc = f"{desc}。{PATH_NOTES[route.path]}"
    o["description"] = desc
    o["x-handler"] = op.handler_ref
    o["x-source"] = route.source
    o["x-auth-class"] = auth_class
    if op.any_method:
        o["x-any-method"] = True
    params = _path_params(route.path)
    if route.path == "/ws/chat":
        params.extend(_ws_chat_query_params())
    if params:
        o["parameters"] = params
    o["security"] = AUTH_CLASS_SECURITY[auth_class]
    o["responses"] = _responses(route.path, op, auth_class)
    return o


INFO_DESCRIPTION = """生成物：scripts/gen_openapi.py 从内核静态路由注册面
（kernel/crates/api/src/server.rs + http_dispatcher.rs 的 .route 调用）
自动生成，禁手编——漂移闸 openapi-drift（run_gates.py）逐字节比对，
改路由后须重跑 `python scripts/gen_openapi.py` 再提交。
security 字段复用 P-2 鉴权分类单一真值源（server.rs AUTH_* 常量组 +
classify_auth_surface，ADR 2026-10-10）：security: [] = 匿名豁免/域外；
BearerAuth = 受保护（GET 需 admin/viewer，写需 admin）/任意已认证用户。
/ext/{*rest} 是插件声明制动态面（manifest http_endpoints），静态字典
不枚举——插件端点经 GET /api/v1/schema 运行时发现。
响应体结构不在本字典承诺面（宁缺毋滥，不臆造字段）：结构以 handler
源码为准；本字典保证路径/方法/路径参数/鉴权面与通用错误骨架。"""

BANNER = [
    "# 自动生成文件（scripts/gen_openapi.py）——禁手编。",
    "# 漂移闸 openapi-drift（run_gates.py，CI traceability-gate job）比对再生成的",
    "# 逐字节一致性；改路由后须重跑 `python scripts/gen_openapi.py` 再提交。",
]


def build_document(routes: list[Route], lists: dict[str, list[str]]) -> str:
    paths: dict[str, dict] = {}
    for route in sorted(routes, key=lambda r: r.path):
        if route.path == EXT_DYNAMIC_PATH:
            continue  # 插件动态声明面：不枚举（见模块 docstring 收面判定）
        auth_class = classify_auth(route.path, lists)
        assert auth_class is not None  # reconcile 已拦 None；此为防御性收口
        item: dict = {}
        for op in sorted(route.ops, key=lambda o: METHOD_ORDER.index(o.method)):
            item[op.method] = _operation(route, op, auth_class)
        paths[_translate_path(route.path)] = item

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "AgentOS 内核静态 API",
            "version": "0.2.0",
            "summary": "内核静态面 API 字典（自动生成，漂移闸执法）",
            "description": INFO_DESCRIPTION,
        },
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Authorization: Bearer <access token>（POST /api/v1/auth/login 签发；"
                        "WS 握手另经 POST /api/v1/ws-ticket 一次性票据）"
                    ),
                }
            }
        },
        "paths": paths,
    }
    lines = list(BANNER)
    _emit(doc, lines, 0)
    return "\n".join(lines) + "\n"


# ── YAML 发射器（确定性；标量全双引号，多行字符串走 |- 字面块）────
def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    assert isinstance(v, str)
    return _q(v)


def _emit(value: object, lines: list[str], indent: int) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        assert value
        for k, v in value.items():
            # 数字键（响应状态码）加引号：OpenAPI 要键为字符串，裸数字会被
            # YAML 解析成 int 键，严格校验器/JSON 消费方即红
            key = f'"{k}"' if k.isdigit() else k
            if isinstance(v, str) and "\n" in v:
                lines.append(f"{pad}{key}: |-")
                lines.extend(f"{pad}  {ln}" for ln in v.split("\n"))
            elif isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{key}:")
                _emit(v, lines, indent + 1)
            elif isinstance(v, list):
                lines.append(f"{pad}{key}: []")
            elif isinstance(v, dict):
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}: {_scalar(v)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                sub: list[str] = []
                _emit(item, sub, 0)
                lines.append(f"{pad}- {sub[0]}")
                lines.extend(f"{pad}  {ln}" for ln in sub[1:])
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:  # pragma: no cover —— 文档根永远是 dict
        lines.append(f"{pad}{_scalar(value)}")


# ── 汇总入口 ─────────────────────────────────────────────────────────
def generate(
    server_src: str,
    dispatcher_src: str,
    auth_surface_src: str = "",
    *,
    min_routes: int = MIN_ROUTE_COUNT,
) -> str:
    routes = parse_routes(server_src, "server.rs") + parse_routes(dispatcher_src, "http_dispatcher.rs")
    lists = parse_auth_lists(server_src, auth_surface_src)
    violations = reconcile(routes, lists, min_routes)
    if violations:
        raise DriftGateError("；".join(violations))
    return build_document(routes, lists)


def _display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _report_diff(current: str | None, fresh: str, max_lines: int = 6) -> None:
    """打印库内与再生成的首几处差异行（截断，全貌以重跑生成命令为准）。"""
    if current is None:
        print("[openapi-drift] 库内无 openapi.yaml（首次落地须先跑生成命令）")
        return
    cur = current.splitlines()
    new = fresh.splitlines()
    shown = 0
    for i in range(max(len(cur), len(new))):
        a = cur[i] if i < len(cur) else "<无此行>"
        b = new[i] if i < len(new) else "<无此行>"
        if a != b:
            shown += 1
            print(f"  L{i + 1}: 库内={a!r} 再生成={b!r}")
            if shown >= max_lines:
                print("  ……（更多差异略，重跑生成命令看全文）")
                return


def _stats(fresh: str) -> str:
    n_paths = fresh.count("\n  /")  # 顶层路径键（2 空格缩进）
    n_bearer = fresh.count("- BearerAuth: []")
    n_ops = fresh.count("operationId:")
    return f"{n_paths} paths / {n_ops} operations / BearerAuth 面 {n_bearer} ops"


def run_generate(
    server_rs: Path = SERVER_RS,
    dispatcher_rs: Path = DISPATCHER_RS,
    auth_surface_rs: Path = AUTH_SURFACE_RS,
    out_path: Path = OUT_PATH,
    min_routes: int = MIN_ROUTE_COUNT,
) -> int:
    try:
        server_src = server_rs.read_text(encoding="utf-8")
        dispatcher_src = dispatcher_rs.read_text(encoding="utf-8")
        auth_surface_src = auth_surface_rs.read_text(encoding="utf-8")
        fresh = generate(server_src, dispatcher_src, auth_surface_src, min_routes=min_routes)
    except (OSError, DriftGateError) as e:
        print(f"[openapi-drift] FAIL: {e}")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(fresh, encoding="utf-8", newline="\n")
    print(f"[openapi-drift] 已生成 {_display(out_path)}（{_stats(fresh)}）")
    return 0


def run_check(
    server_rs: Path = SERVER_RS,
    dispatcher_rs: Path = DISPATCHER_RS,
    auth_surface_rs: Path = AUTH_SURFACE_RS,
    out_path: Path = OUT_PATH,
    min_routes: int = MIN_ROUTE_COUNT,
) -> int:
    try:
        server_src = server_rs.read_text(encoding="utf-8")
        dispatcher_src = dispatcher_rs.read_text(encoding="utf-8")
        auth_surface_src = auth_surface_rs.read_text(encoding="utf-8")
        fresh = generate(server_src, dispatcher_src, auth_surface_src, min_routes=min_routes)
    except (OSError, DriftGateError) as e:
        print(f"[openapi-drift] FAIL: {e}")
        return 1
    current = out_path.read_text(encoding="utf-8") if out_path.exists() else None
    if current != fresh:
        print(f"[openapi-drift] FAIL: {_display(out_path)} 与源码注册面不一致（改路由忘了再生成，或生成物被手编）。")
        print("[openapi-drift] 修复: python scripts/gen_openapi.py && git add docs/api/openapi.yaml")
        _report_diff(current, fresh)
        return 1
    print(f"[openapi-drift] OK: {_display(out_path)} 与源码注册面一致（{_stats(fresh)}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAPI 说明书生成器 + 漂移闸")
    parser.add_argument(
        "--check",
        action="store_true",
        help="漂移闸模式：重新生成与库内 openapi.yaml 逐字节比对，不一致 exit 1",
    )
    args = parser.parse_args(argv)
    return run_check() if args.check else run_generate()


if __name__ == "__main__":
    sys.exit(main())
