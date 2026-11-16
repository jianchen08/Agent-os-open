# @feature: FP-0.2.用户隔离 | @ci: python-coverage
"""project.* 授权名单服务租户取序测试（1018 审计 G1 收口）。

契约（docs/working/用户隔离机制_审计与修复_20261018.md §5 服务端执法点 2）：
租户 = 显式形参 > current_reverse_call_tenant_id()（内核 `_identity` 注入的
执行上下文身份）> default。缺陷回归：授权卡永久 grant 反调 project.add_zone
不传租户，落 config/users/default/（admin 租户）= 跨租户写。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from agentos_plugin_sdk.capability import (  # noqa: E402
    bind_reverse_call_identity,
    reset_reverse_call_identity,
)

pytestmark = pytest.mark.unit


def _load_server_module():
    spec = importlib.util.spec_from_file_location(
        "project_manager_server_zone_tenant", str(Path(__file__).resolve().parent / "server.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(tmp_path / "cfgusers"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
    return _load_server_module()


@pytest.fixture(autouse=True)
def _clean_identity():
    bind_reverse_call_identity(None)
    yield
    reset_reverse_call_identity(bind_reverse_call_identity(None))


def _whitelist(cfgusers: Path, tenant: str) -> Path:
    return cfgusers / tenant / "project_whitelist.yaml"


async def test_grant_without_explicit_tenant_lands_in_identity_tenant(
    server_mod: Any, tmp_path: Path
) -> None:
    """缺陷回归主判例：授权卡反调（无显式租户）→ 名单落执行上下文身份租户。

    修复前：contextvar 无消费，恒落 default（admin 租户）= G1 错位。
    """
    bind_reverse_call_identity({"tenant_id": "u-maintenance", "session_id": "thread-1"})
    result = await server_mod.project_add_zone(path="D:\\work", section="entries")
    assert "error" not in result
    cfgusers = tmp_path / "cfgusers"
    assert "D:\\work" in result["entries"]
    assert _whitelist(cfgusers, "u-maintenance").is_file()
    # 不串租户：default（admin）名单不被写入
    assert not _whitelist(cfgusers, "default").exists()


async def test_identity_tenant_read_scoped(server_mod: Any, tmp_path: Path) -> None:
    """load_zones 按身份租户读：u-maintenance 读不到 default（admin）名单。"""
    cfgusers = tmp_path / "cfgusers"
    _whitelist(cfgusers, "default").parent.mkdir(parents=True, exist_ok=True)
    text = "entries:\n  - 'D:\\admin-only'\n"
    _whitelist(cfgusers, "default").write_text(text, encoding="utf-8")

    bind_reverse_call_identity({"tenant_id": "u-maintenance", "session_id": "thread-1"})
    zones = await server_mod.project_load_zones()
    assert zones["entries"] == [], "maintenance 不得读到 admin 租户名单"

    bind_reverse_call_identity({"tenant_id": "default", "session_id": "thread-1"})
    zones_admin = await server_mod.project_load_zones()
    assert zones_admin["entries"] == ["D:\\admin-only"]


async def test_explicit_tenant_overrides_identity(server_mod: Any, tmp_path: Path) -> None:
    """显式形参优先于执行上下文身份（取序第一级）。"""
    bind_reverse_call_identity({"tenant_id": "u-caller", "session_id": "thread-1"})
    result = await server_mod.project_add_zone(
        path="D:\\x", section="entries", tenant_id="u-explicit"
    )
    assert "error" not in result
    cfgusers = tmp_path / "cfgusers"
    assert _whitelist(cfgusers, "u-explicit").is_file()
    assert not _whitelist(cfgusers, "u-caller").exists()


async def test_no_identity_falls_back_to_default(server_mod: Any, tmp_path: Path) -> None:
    """无身份（旧内核/裸调用）= default 租户（既有语义不回退破坏）。"""
    result = await server_mod.project_add_zone(path="D:\\legacy", section="entries")
    assert "error" not in result
    assert _whitelist(tmp_path / "cfgusers", "default").is_file()


async def test_remove_zone_scoped_by_identity(server_mod: Any, tmp_path: Path) -> None:
    """remove_zone 同取序：删的是身份租户的条目。"""
    bind_reverse_call_identity({"tenant_id": "u-maintenance", "session_id": "thread-1"})
    await server_mod.project_add_zone(path="D:\\work", section="entries")
    removed = await server_mod.project_remove_zone(path="D:\\work", section="entries")
    assert removed["entries"] == []
    assert not _whitelist(tmp_path / "cfgusers", "default").exists()
