# @feature: FP-0.2.二 security_check zones 端点 | @ci: python-coverage
"""授权区管理端点测试（ADR 2026-09-24-read-deny-write-zones 批次3）。

security_check server.py http.handle 的 zones* 路由：
1. GET zones → 两节全量（entries/read_deny，真值解析含 legacy 回退）；
2. POST add → add_write_zone / add_read_deny（整盘根 400）；
3. POST remove → remove_zone 幂等移除（未知节 400）。
响应为内核 dispatcher 信封（base64 body），断言解包后的 JSON。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_server_module():
    spec = importlib.util.spec_from_file_location(
        "security_check_server_zone_endpoints", str(Path(_THIS_DIR) / "server.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pytestmark = pytest.mark.unit


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    """解内核 dispatcher 信封（data.body = base64 JSON）。"""
    assert resp["success"] is True
    return json.loads(base64.b64decode(resp["data"]["body"]))


def _install_real_project_service(monkeypatch: Any, no_legacy: Path) -> Any:
    """project.* 服务桩：直连真实登记簿实现（project_manager.project_registry）。

    只桩跨插件 RPC 层——名单落盘/解析链走真实依赖（env 钉 tmp 布局不变，
    legacy 回退钉 no_legacy 与原共享根形态一致）。
    """
    from project_manager import project_registry as pr_local

    monkeypatch.setattr(pr_local, "legacy_config_users_base", lambda: no_legacy)

    class _Handle:
        async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
            if method == "load_zones":
                return pr_local.load_zone_sections()
            if method == "add_zone":
                return {
                    "entries": pr_local.add_zone_entry(
                        str(args.get("path") or ""), section=str(args.get("section") or "entries")
                    )
                }
            if method == "remove_zone":
                return {
                    "entries": pr_local.remove_zone(
                        str(args.get("path") or ""), section=str(args.get("section") or "entries")
                    )
                }
            raise AssertionError(f"unexpected method: {method}")

    return _Handle()


@pytest.fixture
def server_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(tmp_path / "cfgusers"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
    mod = _load_server_module()
    handle = _install_real_project_service(monkeypatch, tmp_path / "no_legacy")
    monkeypatch.setattr(mod.plugin, "get_capability", lambda name: handle)
    return mod


def _b64_body(data: dict[str, Any]) -> str:
    import base64 as _b64

    return _b64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="名单校验按宿主平台路径语义判「绝对+非根」（project_registry._mutate_zone_section）："
    "Windows 风格条目（盘符/反斜杠/normcase 大小写幂等）仅 win32 语义下合法",
)
async def test_get_zone_policy_empty_then_added(server_mod: Any) -> None:
    """GET 空名单 → add 两节 → GET 回读全量（读写闭环）。"""
    handle = server_mod.http_handle

    empty = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones", method="GET"
    ))
    assert empty == {"entries": [], "read_deny": []}

    added = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/add",
        method="POST",
        raw_body=_b64_body({"section": "entries", "path": "D:\\work"}),
    ))
    assert added["added"] is True and added["entries"] == ["D:\\work"]

    added = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/add",
        method="POST",
        raw_body=_b64_body({"section": "read_deny", "path": "C:\\private"}),
    ))
    assert added["added"] is True and added["read_deny"] == ["C:\\private"]

    full = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones", method="GET"
    ))
    assert full == {"entries": ["D:\\work"], "read_deny": ["C:\\private"]}


async def test_add_drive_root_rejected(server_mod: Any) -> None:
    """整盘根不给授权（add_write_zone 底线，400 不落盘）。"""
    resp = _unwrap(await server_mod.http_handle(
        path="/ext/pipeline_security_check/zones/add",
        method="POST",
        raw_body=_b64_body({"section": "entries", "path": "C:\\"}),
    ))
    assert "整盘根" in resp["error"] or "非法" in resp["error"]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="幂等移除依赖 normcase 大小写折叠与盘符路径语义（POSIX normcase 恒等且盘符条目按相对路径拒绝）",
)
async def test_remove_zone_idempotent_and_section_validation(server_mod: Any) -> None:
    """remove 幂等移除；未知节 / 缺 path → 400。"""
    handle = server_mod.http_handle
    await handle(
        path="/ext/pipeline_security_check/zones/add",
        method="POST",
        raw_body=_b64_body({"section": "entries", "path": "D:\\work"}),
    )

    removed = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/remove",
        method="POST",
        raw_body=_b64_body({"section": "entries", "path": "d:\\WORK"}),
    ))
    assert removed["removed"] is True and removed["entries"] == []

    again = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/remove",
        method="POST",
        raw_body=_b64_body({"section": "entries", "path": "D:\\work"}),
    ))
    assert again["removed"] is True and again["entries"] == []

    bad_section = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/remove",
        method="POST",
        raw_body=_b64_body({"section": "other", "path": "D:\\x"}),
    ))
    assert "invalid section" in bad_section["error"]

    missing = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/add",
        method="POST",
        raw_body=_b64_body({"section": "entries"}),
    ))
    assert missing["error"] == "path required"

    unknown = _unwrap(await handle(
        path="/ext/pipeline_security_check/zones/unknown",
        method="POST",
    ))
    assert unknown["error"] == "not found"
