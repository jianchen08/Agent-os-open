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
import logging
from pathlib import Path
from types import SimpleNamespace
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


def _make_http_post(session_id: str, mode: str) -> dict[str, Any]:
    """构造切换端点 POST 请求（键位 = session_id，会话稳定键——BUG-15 裁定）。"""
    body = base64.b64encode(json.dumps({"session_id": session_id, "mode": mode}).encode("utf-8")).decode("ascii")
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
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "accept_edits"))
        result = _decode(resp)
        assert result == {"switched": True, "mode": "accept_edits"}
        assert sc_mod._PERMISSION_MODES.get("thread-s1") == "accept_edits"
        fake.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_相同模式幂等(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """表内已有同值条目 → 幂等返回 unchanged，不重复写。"""
        sc_mod._PERMISSION_MODES["thread-s1"] = "default"
        _mock_hi(monkeypatch, "cancel")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "default"))
        assert _decode(resp) == {"switched": True, "mode": "default", "unchanged": True}

    @pytest.mark.asyncio
    async def test_default_selection_on_absent_key_writes_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式选择 default（表内无条目）必须落表：显式 default 在隔离会话
        覆盖免审批默认（黑名单照常生效），不落表则无法与「未选择」区分。"""
        _mock_hi(monkeypatch, "cancel")
        assert "thread-s1" not in sc_mod._PERMISSION_MODES
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "default"))
        result = _decode(resp)
        assert result == {"switched": True, "mode": "default"}
        assert sc_mod._PERMISSION_MODES.get("thread-s1") == "default"


class TestHighRiskSwitch:
    @pytest.mark.asyncio
    async def test_auto确认后生效(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "auto"))
        result = _decode(resp)
        assert result == {"switched": True, "mode": "auto"}
        assert sc_mod._PERMISSION_MODES.get("thread-s1") == "auto"
        calls = [c.args[0] for c in fake.call.await_args_list]
        assert "create_choice" in calls
        assert "wait_for_choice" in calls

    @pytest.mark.asyncio
    async def test_auto取消不切换(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "cancel")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "auto"))
        result = _decode(resp)
        assert result["switched"] is False
        assert sc_mod._PERMISSION_MODES.get("thread-s1") is None

    @pytest.mark.asyncio
    async def test_bypass确认后生效(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "bypass"))
        assert _decode(resp) == {"switched": True, "mode": "bypass"}

    @pytest.mark.asyncio
    async def test_交互服务不可用拒绝切换(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: (_ for _ in ()).throw(KeyError(name)))
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "auto"))
        result = _decode(resp)
        assert result["switched"] is False
        assert sc_mod._PERMISSION_MODES.get("s1") is None

    @pytest.mark.asyncio
    async def test_确认异常拒绝切换(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = AsyncMock()
        fake.call.side_effect = RuntimeError("boom")
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: fake)
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "bypass"))
        assert _decode(resp)["switched"] is False


class TestValidationAndQuery:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad_mode", "why"),
        [
            ("hack_mode", "任意非法值"),
            # BUG-6 语义锚定：「高」不是权限档位（档位只有 default/accept_edits/
            # auto/bypass 四档，黑名单制无"逐调用必确认"档）。若未来要引入新档，
            # 必须连同意语义一起改这里，不允许只改前端文案静默放行。
            ("high", "GUI 测试误传的思考强度档名"),
        ],
    )
    async def test_非法mode返回400(
        self, monkeypatch: pytest.MonkeyPatch, bad_mode: str, why: str
    ) -> None:
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", bad_mode))
        result = _decode(resp)
        assert result["switched"] is False, why
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
        sc_mod._PERMISSION_MODES["thread-s1"] = "auto"
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "GET", "plugin_id": "pipeline_security_check", "raw_body": "", "query": {"session_id": "thread-s1"}}
        )
        result = _decode(resp)
        assert result["mode"] == "auto"
        assert result["explicit"] is True, "表内条目 = 用户显式选择"
        assert "valid_modes" in result

    @pytest.mark.asyncio
    async def test_未设置时查询默认default(self) -> None:
        resp = await server_mod.http_handle(
            **{"path": "/ext/pipeline_security_check/permission_mode", "method": "GET", "plugin_id": "pipeline_security_check", "raw_body": "", "query": {"session_id": "thread-s1"}}
        )
        result = _decode(resp)
        assert result["mode"] == "default"
        assert result["explicit"] is False, "缺省态必须显式标记 explicit=false（前端据此显示隔离免审批默认）"


class TestSwitchThenExecuteE2E:
    """HTTP 切换 → 执行轮的端到端契约（真实 key 链路，防"切了没生效"回归）。

    回归背景（BUG-15，2026-09-15）：权限档是会话级设置，键位必须是
    session_id 稳定键。执行上下文的 pipeline_id 在同一会话内随任务/子代理
    轮换（生产实测 2 分钟 3 个），键在 pipeline_id 上时显式选择被隔离豁免
    路径吞掉。本契约把「切换键 = session_id，执行侧同会话异 pipeline_id
    照常命中」的最短链路钉死。
    """

    # 注入真实安全规则（同 test_security_check_allow_priority 模式）：
    # 安全规则经 manifest config_files 注入生产插件，测试构造无注入时
    # _load_rules 回退内联默认（无 curl 关键词），对标用例会错误放行。
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    _SECURITY_RULES: list[dict[str, Any]] = (
        yaml.safe_load((_REPO_ROOT / "config" / "plugins" / "security_check" / "security_rules.yaml").read_text(encoding="utf-8")) or {}
    ).get("rules", [])

    def _ctx_for(self, command: str, task_isolated: bool = False) -> Any:
        from pipeline.plugin import PluginContext  # noqa: PLC0415

        return PluginContext(
            state={
                "core_type": "tool_execute",
                # 执行侧 pipeline_id 刻意与切换键（session_id=thread-s1）不同：
                # 同会话内主/子管道、任务轮换的 pipeline_id 各不相同（BUG-15），
                # 显式档必须仍然命中。
                "pipeline_id": "volatile-pipe-x",
                "session_id": "thread-s1",
                "raw_tool_calls": [{"name": "bash_execute", "args": {"command": command}}],
                "execution_contexts": [
                    {"provider": "host", "tool_name": "bash_execute", "task_isolated": task_isolated}
                ],
            },
            _services={},
        )

    def _plugin(self) -> Any:
        return sc_mod.SecurityCheckPlugin(config={"enabled": True, "rules": self._SECURITY_RULES})

    @pytest.mark.asyncio
    async def test_bypass切换后执行轮不再弹审批(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 切 bypass → 同一进程内 execute：命中 needs_approval 的命令直接放行。"""
        fake = _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "bypass"))
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
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "default"))
        assert _decode(resp)["switched"] is True
        fake.call.reset_mock()

        plugin = self._plugin()
        r = await plugin.execute(self._ctx_for("echo ring && curl -s http://example.com"))
        decision = r.state_updates.get("security.decision", {})
        assert "soft_block" in decision.get("reason", ""), (
            f"default 模式命中 curl 应走审批链（无交互服务→软拦截），实际={decision.get('reason')!r}"
        )

    @pytest.mark.asyncio
    async def test_isolated_task_without_explicit_mode_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """隔离任务 + 表内无条目（未显式选择）→ 黑名单命中也整体放行（免审批默认）。"""
        _mock_hi(monkeypatch, "confirm")
        assert "thread-s1" not in sc_mod._PERMISSION_MODES

        plugin = self._plugin()
        r = await plugin.execute(
            self._ctx_for("echo ring && curl -s http://example.com", task_isolated=True)
        )
        decision = r.state_updates.get("security.decision", {})
        assert decision.get("allowed") is True
        assert decision.get("reason") == "isolated task, base checks passed"
        # 放行路径不产生拒绝副作用
        assert r.state_updates.get("raw_tool_calls") is None

    @pytest.mark.asyncio
    async def test_isolated_task_with_explicit_default_still_approves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 切 default（显式落表）→ 隔离任务黑名单命中照常走审批链。"""
        fake = _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "default"))
        assert _decode(resp)["switched"] is True
        assert sc_mod._PERMISSION_MODES.get("thread-s1") == "default"
        fake.call.reset_mock()

        plugin = self._plugin()
        r = await plugin.execute(
            self._ctx_for("echo ring && curl -s http://example.com", task_isolated=True)
        )
        decision = r.state_updates.get("security.decision", {})
        assert "soft_block" in decision.get("reason", ""), (
            f"显式 default 必须覆盖隔离免审批默认（无交互服务→软拦截），实际={decision.get('reason')!r}"
        )


# ═══════════════════════════════════════════════════════════════
# 2026-09-13 覆盖率补测：server 适配层（on_load/on_unload/execute 工具面、
# 确认链路失败分支、HTTP 体与 GET query 解析容错）
# ═══════════════════════════════════════════════════════════════


class TestOnLoadLifecycle:
    """on_load 装配契约：plugin 引用注入 / 前端通道装配 / 单例生命周期。"""

    @pytest.fixture(autouse=True)
    def _restore_server_globals(self):
        yield
        sc_mod._PLUGIN_REF = None
        sc_mod.set_frontend_emit(None)
        server_mod.get_instance.cache_clear()

    @pytest.mark.asyncio
    async def test_on_load_without_frontend_warns_and_wires_plugin_ref(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """frontend 能力缺失 → 降级提示通道置空并留 warning，plugin 引用照常注入。"""

        def _raise(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(server_mod.plugin, "get_capability", _raise)
        with caplog.at_level(logging.WARNING, logger=server_mod.__name__):
            await server_mod._on_load({})

        assert sc_mod._PLUGIN_REF is server_mod.plugin
        assert sc_mod._frontend_emit is None
        assert any("frontend 能力未注入" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_on_load_with_frontend_wires_emit_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """frontend 能力在位 → _frontend_emit 经 capability.call('emit', ...) 转发。"""
        fake = AsyncMock()
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: fake)

        await server_mod._on_load({})

        assert sc_mod._frontend_emit is not None
        await sc_mod._frontend_emit("security_rules_degraded", {"k": "v"}, "thread-9")
        fake.call.assert_awaited_once_with(
            "emit",
            {"event": "security_rules_degraded", "payload": {"k": "v"}, "thread_id": "thread-9"},
        )

    @pytest.mark.asyncio
    async def test_on_load_builds_cached_plugin_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """on_load 预热：构建 SecurityCheckPlugin 单例并缓存。"""

        def _raise(name: str) -> Any:
            raise KeyError(name)

        monkeypatch.setattr(server_mod.plugin, "get_capability", _raise)
        await server_mod._on_load({})

        inst = server_mod.get_instance()
        assert isinstance(inst, sc_mod.SecurityCheckPlugin)
        assert server_mod.get_instance.cache_info().currsize == 1

    @pytest.mark.asyncio
    async def test_on_unload_clears_singleton_cache(self) -> None:
        """on_unload 清空单例缓存 → 下次 get_instance 重建（配置热更新语义）。"""
        server_mod.get_instance()
        assert server_mod.get_instance.cache_info().currsize == 1

        await server_mod._on_unload({})

        assert server_mod.get_instance.cache_info().currsize == 0


class TestExecuteToolAdapter:
    """security_check.execute 工具面：PluginResult → dict 适配契约。"""

    @pytest.mark.asyncio
    async def test_execute_wraps_plugin_state_updates(self) -> None:
        """真实插件实例：llm_call 轮免检早退，state_updates 被包装返回。"""
        server_mod.get_instance.cache_clear()
        data = await server_mod.execute({"core_type": "llm_call"}, None)
        assert data["state_updates"]["security.decision"]["reason"] == "not a tool execution"

    @pytest.mark.asyncio
    async def test_execute_dict_result_passthrough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """插件返回 dict（Core 插件形态）→ 原样透传不包装。"""
        fake_inst = SimpleNamespace(execute=AsyncMock(return_value={"raw": {"a": 1}}))
        monkeypatch.setattr(server_mod, "get_instance", lambda: fake_inst)
        assert await server_mod.execute({"core_type": "llm_call"}, {}) == {"raw": {"a": 1}}

    @pytest.mark.asyncio
    async def test_execute_skip_remaining_flag_surfaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skip_remaining=True → 结果显式携带该标记。"""
        fake_result = SimpleNamespace(state_updates={"k": "v"}, skip_remaining=True)
        fake_inst = SimpleNamespace(execute=AsyncMock(return_value=fake_result))
        monkeypatch.setattr(server_mod, "get_instance", lambda: fake_inst)
        data = await server_mod.execute({"core_type": "llm_call"}, None)
        assert data == {"state_updates": {"k": "v"}, "skip_remaining": True}

    @pytest.mark.asyncio
    async def test_execute_omits_skip_flag_when_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skip_remaining=False → 不产生该键（响应形状稳定）。"""
        fake_result = SimpleNamespace(state_updates={"k": "v"}, skip_remaining=False)
        fake_inst = SimpleNamespace(execute=AsyncMock(return_value=fake_result))
        monkeypatch.setattr(server_mod, "get_instance", lambda: fake_inst)
        data = await server_mod.execute({"core_type": "llm_call"}, None)
        assert data == {"state_updates": {"k": "v"}}


class TestConfirmSwitchFailureBranches:
    """高风险模式确认链路的失败分支：create 失败 / wait 响应异常 → 拒绝切换。"""

    @pytest.mark.asyncio
    async def test_create_choice_error_rejects_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_choice 返回 error → 确认失败，模式表不变。"""
        fake = AsyncMock()

        async def _call(name: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            assert name == "create_choice", f"create 失败后不得再调用 {name}"
            return {"error": "capacity full"}

        fake.call.side_effect = _call
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: fake)

        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "auto"))
        result = _decode(resp)
        assert result == {"switched": False, "reason": "用户未确认或确认超时", "mode": "default"}
        assert sc_mod._PERMISSION_MODES.get("thread-s1") is None

    @pytest.mark.asyncio
    async def test_wait_non_dict_response_rejects_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wait_for_choice 返回非 dict → 确认失败。"""
        fake = AsyncMock()

        async def _call(name: str, params: dict[str, Any], **kwargs: Any) -> Any:
            if name == "create_choice":
                return {"request_id": "r-9"}
            if name == "wait_for_choice":
                return "not-a-dict"
            raise AssertionError(f"unexpected cap.call: {name}")

        fake.call.side_effect = _call
        monkeypatch.setattr(server_mod.plugin, "get_capability", lambda name: fake)

        resp = await server_mod.http_handle(**_make_http_post("thread-s1", "bypass"))
        assert _decode(resp)["switched"] is False
        assert sc_mod._PERMISSION_MODES.get("thread-s1") is None


class TestHttpBodyAndQueryParsing:
    """HTTP 面解析容错：畸形请求体按空体处理；GET query 支持键值对列表。"""

    @pytest.mark.parametrize(
        "raw_body",
        [
            "not-valid-base64!!!",  # base64 解码失败
            base64.b64encode(b"not-json{{{").decode("ascii"),  # 解码成功但非 JSON
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_body_treated_as_empty(
        self, monkeypatch: pytest.MonkeyPatch, raw_body: str
    ) -> None:
        """畸形请求体 → 按空体处理 → 缺 session_id 返回 400。"""
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(
            path="/ext/pipeline_security_check/permission_mode",
            method="POST",
            plugin_id="pipeline_security_check",
            raw_body=raw_body,
        )
        assert _decode(resp) == {"error": "session_id required", "switched": False}

    @pytest.mark.asyncio
    async def test_get_query_as_pair_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET query 为 [key, value] 对列表（内核异形形态）→ 正常解析读取。"""
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(
            path="/ext/pipeline_security_check/permission_mode",
            method="GET",
            plugin_id="pipeline_security_check",
            raw_body="",
            query=[["session_id", "pq-1"]],
        )
        result = _decode(resp)
        assert result["mode"] == "default"
        assert "valid_modes" in result

    @pytest.mark.asyncio
    async def test_get_malformed_query_still_responds_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """query 完全异形（不可迭代解包）→ 按缺参处理，仍返回默认模式。"""
        _mock_hi(monkeypatch, "confirm")
        resp = await server_mod.http_handle(
            path="/ext/pipeline_security_check/permission_mode",
            method="GET",
            plugin_id="pipeline_security_check",
            raw_body="",
            query="zzzz",
        )
        assert _decode(resp)["mode"] == "default"


class TestSessionKeyContract:
    """BUG-15 键位契约：写入（POST session 键）→ 同会话异管道执行读取必须命中。

    按生产装配序演练：sidecar on_load 先 _load_permission_modes() 加载持久化
    表，之后前端经切换端点写入。要求：
    1. 写入对执行侧 _explicit_permission_mode 立即可见（同会话、pipeline_id
       不同——生产实测同会话 2 分钟换 3 个管道键）；
    2. 写入必须落盘（sidecar 热重载/重启后重读文件不丢显式选择）；
    3. 隔离任务命中黑名单时，端点写入的显式档必须覆盖免审批默认（弹审批）。

    本文件内 plugin/server 相邻导入（同一 plugin 模块实例），与生产
    sidecar 单进程同构——端点驱动的组合场景必须放这里，跨文件复用会踩
    测试基建的裸模块实例分叉。
    """

    # 显式守门规则（判定来源可归因，不依赖降级默认规则）
    _GUARD_RULES: list[dict[str, Any]] = [
        {
            "name": "guard_rm",
            "tools": ["*"],
            "params": ["command", "cmd"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "rm -rf"}],
        },
    ]

    @pytest.fixture(autouse=True)
    def _restore_execute_cap(self):
        """执行侧审批通道走 set_human_interaction_cap 装配缝，测后摘除防残留。"""
        yield
        sc_mod.set_human_interaction_cap(None)

    async def _switch_to(self, mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """生产装配序：预置历史持久化文件 → on_load 加载 → 切换端点写入。"""
        # 预置历史持久化文件（生产 data/permission_modes.json 长期存在旧条目，
        # _load 走成功分支——正是历史版本重绑定共享表的触发条件）
        monkeypatch.setattr(sc_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
        Path(sc_mod._PERMISSION_MODES_FILE).write_text(json.dumps({"legacy-key": "bypass"}), encoding="utf-8")
        _mock_hi(monkeypatch, "cancel")
        sc_mod._load_permission_modes()
        resp = await server_mod.http_handle(**_make_http_post("thread-s1", mode))
        assert _decode(resp) == {"switched": True, "mode": mode}

    @pytest.mark.asyncio
    async def test_切换写入后同会话异管道执行命中显式档(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._switch_to("default", monkeypatch, tmp_path)

        # 写入必须落盘：否则热重载/重启后显式选择丢失、隔离豁免卷土重来
        persisted = json.loads(Path(sc_mod._PERMISSION_MODES_FILE).read_text(encoding="utf-8"))
        assert persisted.get("thread-s1") == "default", (
            f"切换条目必须持久化，实际文件内容={persisted!r}"
        )

        # 执行侧读取：同会话、不同 pipeline_id（主/子管道、任务轮换）必须命中
        from pipeline.plugin import PluginContext  # noqa: PLC0415

        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "session_id": "thread-s1",
                "pipeline_id": "volatile-pipe-y",
                "raw_tool_calls": [],
            },
            _services={},
        )
        assert sc_mod.SecurityCheckPlugin()._explicit_permission_mode(ctx) == "default"

    @pytest.mark.asyncio
    async def test_端点写入的显式档覆盖隔离免审批默认(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GUI 实测场景（BUG-15）：选择器写入（session 键）→ 同会话异管道的
        隔离任务命中黑名单必须弹审批，不被「隔离未显式选择 → 免审批」吞掉。
        """
        from pipeline.plugin import PluginContext  # noqa: PLC0415

        from tests._security_check_harness import wire_approval_cap

        await self._switch_to("default", monkeypatch, tmp_path)

        _cap, create = wire_approval_cap(sc_mod, [{"selected_option": "approved_once"}])
        plugin = sc_mod.SecurityCheckPlugin(config={"enabled": True, "rules": self._GUARD_RULES})
        ctx = PluginContext(
            state={
                "core_type": "tool_execute",
                "session_id": "thread-s1",
                "pipeline_id": "volatile-pipe-z",
                "raw_tool_calls": [
                    {"name": "bash_execute", "args": {"command": "rm -rf /x"}}
                ],
                "execution_contexts": [
                    {"provider": "host", "tool_name": "bash_execute", "task_isolated": True}
                ],
            },
            _services={},
        )

        result = await plugin.execute(ctx)
        decision = result.state_updates.get("security.decision", {})
        assert create.calls == 1, (
            f"切换端点写入的显式 default 必须覆盖隔离免审批默认（弹审批），实际={decision!r}"
        )
        assert decision.get("reason") == "approved", "审批通过后才允许执行"
