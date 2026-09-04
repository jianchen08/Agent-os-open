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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")

import plugin as sc_mod  # noqa: E402
import server as server_mod  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 持久化文件必须隔离到 tmp——模块常量 _PERMISSION_MODES_FILE 就是生产
    # 路径 plugins/shared/data/permission_modes.json，不隔离的话测试切换会
    # 真实写盘，把测试管道键（p1 等）灌进生产模式表（历史事故：生产表被
    # 覆盖成 {"p1": "bypass"}，真实管道键丢失回退 default）。
    monkeypatch.setattr(sc_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
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
        sc_mod._PERMISSION_MODES["p1"] = "auto"
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "GET", "plugin_id": "pipeline_security_check", "raw_body": "", "query": {"pipeline_id": "p1"}}
        )
        result = _decode(resp)
        assert result["mode"] == "auto"
        assert "valid_modes" in result

    @pytest.mark.asyncio
    async def test_未设置时查询默认default(self) -> None:
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "GET", "plugin_id": "pipeline_security_check", "raw_body": "", "query": {"pipeline_id": "p1"}}
        )
        assert _decode(resp)["mode"] == "default"


class TestSwitchThenExecuteE2E:
    """HTTP 切换 → 执行轮的端到端契约（真实 key 链路，防"切了没生效"回归）。

    回归背景：生产观察"切旁路仍审批"——根因是前端不回读 + 表被测试污染
    导致真实管道键缺失回退 default，插件分发本身在键一致时是生效的；
    本契约把"切换键 = 执行键（pipeline_id）"的最短链路钉死。
    """

    # 注入真实安全规则（同 test_security_check_allow_priority 模式）：
    # 安全规则经 manifest config_files 注入生产插件，测试构造无注入时
    # _load_rules 回退内联默认（无 curl 关键词），对标用例会错误放行。
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    _SECURITY_RULES: list[dict[str, Any]] = (
        yaml.safe_load((_REPO_ROOT / "config" / "isolation" / "security_rules.yaml").read_text(encoding="utf-8")) or {}
    ).get("rules", [])

    def _ctx_for(self, command: str) -> Any:
        from pipeline.plugin import PluginContext  # noqa: PLC0415

        return PluginContext(
            state={
                "core_type": "tool_execute",
                "pipeline_id": "p1",
                "raw_tool_calls": [{"name": "bash_execute", "args": {"command": command}}],
                "execution_contexts": [{"provider": "host", "tool_name": "bash_execute", "task_isolated": False}],
            },
            _services={},
        )

    def _plugin(self) -> Any:
        return sc_mod.SecurityCheckPlugin(config={"enabled": True, "rules": self._SECURITY_RULES})

    @pytest.mark.asyncio
    async def test_bypass切换后执行轮不再弹审批(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 切 bypass → 同一进程内 execute：命中 needs_approval 的命令直接放行。"""
        fake = _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("p1", "bypass"))
        assert _decode(resp)["switched"] is True
        fake.call.reset_mock()

        plugin = self._plugin()
        r = await plugin.execute(self._ctx_for("echo ring && curl -s http://example.com"))
        decision = r.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert "soft_block" not in decision.get("reason", ""), (
            f"bypass 模式应放行危险命令，实际 reason={decision.get('reason')!r}"
        )
        fake.call.assert_not_awaited()
        # 放行路径不产生拒绝副作用（不写 tool_results、不清空工具调用）
        assert r.state_updates.get("raw_tool_calls") is None

    @pytest.mark.asyncio
    async def test_default切换后执行轮照常弹审批(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同样命令在 default 下必须走审批链（对标：bypass 与 default 有真实差异）。"""
        fake = _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("p1", "default"))
        assert _decode(resp)["switched"] is True
        fake.call.reset_mock()

        plugin = self._plugin()
        r = await plugin.execute(self._ctx_for("echo ring && curl -s http://example.com"))
        decision = r.state_updates.get("security.decision", {})
        assert "soft_block" in decision.get("reason", ""), (
            f"default 模式命中 curl 应走审批链（无交互服务→软拦截），实际={decision.get('reason')!r}"
        )
