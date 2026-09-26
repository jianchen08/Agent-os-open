# @feature: FP-0.2.二 user_admin 插件服务 | @vision: V1 可进化 | @ci: none-local
"""user_admin 服务端测试——用户管理页 widget 化配套（/admin 声明组台数据面）。

覆盖：
1. stats 两形态契约：平键真值（total_users/active_users/admin_count）与
   metrics 卡片形状（status_card scalar 消费）同源同值
2. 空表（db-admin 不可用回退）→ 零值两形态仍对齐
3. is_active 1/True 双形态都计入活跃（db 行布尔两形态）

唯一外部依赖是 _call_db 打桩，不接真实内核。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("ua_server_test", _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    mod = _load_module()
    monkeypatch.setattr(mod, "_call_db", _fake_call_db)
    return mod


async def _fake_call_db(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    raise AssertionError("per-test override required")


def _body(envelope: dict[str, Any]) -> dict[str, Any]:
    """解 _ok 信封：data.body 是 base64 JSON（_json_response 形状）。"""
    inner = envelope["data"]
    raw = inner["body"]
    if inner.get("body_encoding") == "base64":
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


class TestUsersStats:
    async def test_metrics_and_flat_keys_same_source(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {"is_active": 1, "role": "admin"},
            {"is_active": True, "role": "user"},
            {"is_active": 0, "role": "user"},
        ]

        async def fake(method: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"status": 200, "body": {"rows": rows}}

        monkeypatch.setattr(server, "_call_db", fake)
        payload = _body(await server._users_stats("auth"))
        assert payload["total_users"] == 3
        assert payload["active_users"] == 2
        assert payload["admin_count"] == 1
        metrics = payload["metrics"]
        assert [m["title"] for m in metrics] == ["总用户数", "活跃用户", "管理员"]
        assert [m["value"] for m in metrics] == [3, 2, 1]

    async def test_db_unavailable_falls_back_to_zero_pair(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
            return None

        monkeypatch.setattr(server, "_call_db", fake)
        payload = _body(await server._users_stats("auth"))
        assert payload["total_users"] == 0
        assert all(m["value"] == 0 for m in payload["metrics"])

    async def test_boolean_both_forms_counted_active(self, server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"is_active": True}, {"is_active": 1}, {"is_active": False}, {"is_active": 0}]

        async def fake(method: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"status": 200, "body": {"rows": rows}}

        monkeypatch.setattr(server, "_call_db", fake)
        payload = _body(await server._users_stats("auth"))
        assert payload["active_users"] == 2
