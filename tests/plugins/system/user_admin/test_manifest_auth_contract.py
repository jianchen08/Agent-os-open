# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""user_admin manifest 鉴权声明契约锁（A3 对齐修复）。

同一路径的角色写面 PUT/PATCH /users/{user_id}/role 必须同声 ``auth:"admin"``：
dispatcher 层 admin 强制（kernel ext_auth_enforcement_test 锁行为）+ 插件侧
db-admin 凭证透传的内核 admin 写校验 = 双层纵深。历史 PUT 声明漂移为
``user`` 造成同路径双层鉴权分裂，此测试锁死对齐契约。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MANIFEST = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "user_admin" / "plugin.json"


def _endpoints() -> list[dict]:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))["http_endpoints"]


def test_same_path_role_write_endpoints_both_declare_admin() -> None:
    """PUT 与 PATCH /users/{user_id}/role 鉴权声明一致且为 admin。"""
    role_writes = [
        e for e in _endpoints()
        if e["method"] in ("PUT", "PATCH") and e["path"].endswith("/users/{user_id}/role")
    ]
    assert len(role_writes) == 2, f"角色写面端点数异常: {[e['route_id'] for e in role_writes]}"
    for e in role_writes:
        assert e["auth"] == "admin", f"{e['route_id']} 声明 auth={e['auth']!r}，应与 PATCH 对齐为 admin"


def test_admin_only_endpoints_consistency() -> None:
    """user_admin 用户管理写面（角色/租户/删除）全部 admin 声明（防再次漂移）。"""
    admin_paths = {
        "/ext/user_admin/users/{user_id}/role",
        "/ext/user_admin/users/{user_id}/tenant",
        "/ext/user_admin/users/{user_id}",
    }
    declared = {e["path"]: e["auth"] for e in _endpoints() if e["path"] in admin_paths}
    assert len(declared) == len(admin_paths)
    for path, auth in declared.items():
        assert auth == "admin", f"{path} 声明 auth={auth!r}"
