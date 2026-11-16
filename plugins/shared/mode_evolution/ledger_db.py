"""mode_evolution 三运维脚本共享的 db-read 契约 HTTP 客户端。

DB 判定不进程内读库：统一经内核 HTTP 管理端点 ``POST /api/v1/db/read``
（db-read 契约服务，执法在内核 Rust read_faces）。本模块承载三脚本共享的
三件事：

- **admin 登录链**：``POST /api/v1/auth/login``，凭据来源与既有链一致
  （``AGENTOS_ADMIN_PASSWORD`` 环境变量，缺省本地开发口令）；内核在线是
  三脚本的既有前置（任务端点轮询同源）。
- **读面调用**：``pipeline_state_admin`` 面（global 档，ADR 2026-09-16
  跨租户收割语义，仅 admin 可调）；``tenant_id`` 非空才携带 ``tenant``
  参数（global 档 admin 收窄到该单租户；空串会被按空租户收窄成空集，
  缺省键 = 全局面语义）。
- **出参解包**：``{columns, rows}`` → 行列表（行 = 值数组，与调用方
  SELECT 投影列按位对齐）。

错误语义：非 2xx 由调用方 ``http()`` 原样上抛（403 = 档位/角色拒绝，
401 = 未认证）——运维判定面 fail-closed，不静默降级出假 scorecard。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

# mode_evolution 运维收割面（global 档；config/kernel/read_faces.yaml 注册）
FACE = "pipeline_state_admin"

# http(method, path, body=None, token=None) -> 解析后的 JSON 值（各脚本既有形态）
HttpFn = Callable[..., Any]


def admin_login(http: HttpFn) -> str:
    """admin 登录取 Bearer token（AGENTOS_ADMIN_PASSWORD，与既有登录链同源）。"""
    body = {
        "username": "admin",
        "password": os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345"),
    }
    d = http("POST", "/api/v1/auth/login", body) or {}
    d = d.get("data") or d
    return d.get("token") or d.get("access_token") or ""


def db_read_rows(
    http: HttpFn,
    token: str,
    sql: str,
    params: dict[str, Any],
    tenant_id: str = "",
) -> list[list[Any]]:
    """在 pipeline_state_admin 面执行自由只读 SELECT，返回行列表。

    身份由 Bearer token 承载（内核认证会话权威解析）；收窄/档位/面限制
    全部由内核契约执法，客户端只做请求装配与行解包。
    """
    body: dict[str, Any] = {"face": FACE, "sql": sql, "params": params}
    if tenant_id:
        body["tenant"] = tenant_id
    d = http("POST", "/api/v1/db/read", body, token=token) or {}
    return list(d.get("rows") or [])
