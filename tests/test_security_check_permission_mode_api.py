# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""security_check 权限模式切换 HTTP 端点测试（纯插件 http_endpoints 能力）。

覆盖：
- POST /ext/pipeline_security_check/permission_mode：低风险模式直接切换
- 高风险模式（auto/bypass）经 human-interaction 审批确认，确认/取消/异常分支
- 参数校验（非法 mode / 缺 session_id）、相同模式幂等、GET 查询
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")

import plugin as sc_mod  # noqa: E402
import server as server_mod  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_modes():
    sc_mod._PERMISSION_MODES.clear()
    yield
    sc_mod._PERMISSION_MODES.clear()


def _fake_hi_cap(selected: str) -> AsyncMock:
    fake = AsyncMock()

    async def _call(name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == "create_choice":
            return {"request_id": "req-1", "error": None}
        if name == "wait_for_choice":
            return {"selected_option": selected, "error": None}
        return {}

    fake.call.side_effect = _call
    return fake


def _make_http_post(pipeline_id: str, mode: str) -> dict[str, Any]:
    body = base64.b64encode(json.dumps({"pipeline_id": pipeline_id, "mode": mode}).encode("utf-8")).decode("ascii")
    return {"path": "/ext/pipeline_security_check/permission_mode", "method": "POST", "plugin_id": "pipeline_security_check", "raw_body": body}


def _decode(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _mock_hi(monkeypatch: pytest.MonkeyPatch, selected: str) -> AsyncMock:
    fake = _fake_hi_cap(selected)
    monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: fake)
    return fake


class TestLowRiskSwitch:
    @pytest.mark.asyncio
    async def test_default切accept_edits直接生效(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _mock_hi(monkeypatch, "cancel")
        resp = await server_mod.http_handle(**_make_http_post("p1", "accept_edits"))
        result = _decode(resp)
        assert result == {"switched": True, "mode": "accept_edits"}
        assert sc_mod._PERMISSION_MODES.get("p1") == "accept_edits"
        fake.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plan直接生效(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "cancel")
        resp = await server_mod.http_handle(**_make_http_post("p1", "plan"))
        assert _decode(resp) == {"switched": True, "mode": "plan"}

    @pytest.mark.asyncio
    async def test_相同模式幂等(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sc_mod._PERMISSION_MODES["s1"] = "default"
        _mock_hi(monkeypatch, "cancel")
        resp = await server_mod.http_handle(**_make_http_post("p1", "default"))
        assert _decode(resp) == {"switched": True, "mode": "default", "unchanged": True}


class TestHighRiskSwitch:
    @pytest.mark.asyncio
    async def test_auto确认后生效(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("p1", "auto"))
        result = _decode(resp)
        assert result == {"switched": True, "mode": "auto"}
        assert sc_mod._PERMISSION_MODES.get("p1") == "auto"
        calls = [c.args[0] for c in fake.call.await_args_list]
        assert "create_choice" in calls
        assert "wait_for_choice" in calls

    @pytest.mark.asyncio
    async def test_auto取消不切换(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "cancel")
        resp = await server_mod.http_handle(**_make_http_post("p1", "auto"))
        result = _decode(resp)
        assert result["switched"] is False
        assert sc_mod._PERMISSION_MODES.get("p1") is None

    @pytest.mark.asyncio
    async def test_bypass确认后生效(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("p1", "bypass"))
        assert _decode(resp) == {"switched": True, "mode": "bypass"}

    @pytest.mark.asyncio
    async def test_交互服务不可用拒绝切换(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: (_ for _ in ()).throw(KeyError(name)))
        resp = await server_mod.http_handle(**_make_http_post("p1", "auto"))
        result = _decode(resp)
        assert result["switched"] is False
        assert sc_mod._PERMISSION_MODES.get("s1") is None

    @pytest.mark.asyncio
    async def test_确认异常拒绝切换(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = AsyncMock()
        fake.call.side_effect = RuntimeError("boom")
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: fake)
        resp = await server_mod.http_handle(**_make_http_post("p1", "bypass"))
        assert _decode(resp)["switched"] is False


class TestValidationAndQuery:
    @pytest.mark.asyncio
    async def test_非法mode返回400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("p1", "hack_mode"))
        result = _decode(resp)
        assert result["switched"] is False
        assert "invalid mode" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_缺session_id返回400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "confirm")
        body = base64.b64encode(json.dumps({"mode": "auto"}).encode("utf-8")).decode("ascii")
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "POST", "raw_body": body}
        )
        assert _decode(resp)["switched"] is False

    @pytest.mark.asyncio
    async def test_未知路径404(self) -> None:
        resp = await server_mod.http_handle(
            **{"path": "/ext/security_check/other", "method": "POST", "raw_body": ""}
        )
        assert _decode(resp) == {"error": "not found"}

    @pytest.mark.asyncio
    async def test_GET查询当前模式(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "confirm")
        sc_mod._PERMISSION_MODES["p1"] = "plan"
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "GET", "plugin_id": "pipeline_security_check", "raw_body": "", "query": {"pipeline_id": "p1"}}
        )
        result = _decode(resp)
        assert result["mode"] == "plan"
        assert "valid_modes" in result

    @pytest.mark.asyncio
    async def test_未设置时查询默认default(self) -> None:
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "GET", "plugin_id": "pipeline_security_check", "raw_body": "", "query": {"pipeline_id": "p1"}}
        )
        assert _decode(resp)["mode"] == "default"
