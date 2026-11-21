# @feature: FP-0.2.二 browser server 接口 | @ci: python-coverage
"""browser server.py 失败信封契约测试（ADR 2026-09-28 收编漏网回归）。

handler 失败路径必须显式 ``{"success": False, "error": ...}``——裸 error 载荷
会被内核归一层包成 success 伪信封，LLM/前端把失败当成功消费。工具类经模块级
缓存槽位注入桩（handler 的依赖边界即工具类），不触真实 Bridge 网络。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MOD_NAME = "browser_server_contract_test"


def _load_server() -> Any:
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


def _load_tool() -> Any:
    """按文件路径加载 tool.py 的 BrowserTool（与 server.py 的防遮蔽加载同款）。"""
    mod_name = "browser_tool_contract_test_impl"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None, "Cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubTool:
    """确定性桩：execute 返回注入的 ToolExecutionResult。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.config: Any = None

    def __call__(self, config: Any) -> "_StubTool":
        self.config = config
        return self

    async def execute(self, inputs: dict[str, Any]) -> Any:
        return self._result


class TestFailureEnvelopeExplicitSuccess:
    def test_failure_envelope_keeps_status(self, monkeypatch) -> None:
        from agentos_plugin_sdk import create_failure_result

        server = _load_server()
        stub = _StubTool(create_failure_result("bridge 未就绪"))
        monkeypatch.setattr(server, "_browser_tool_cls", stub)
        handler = server.plugin._tools["browser_snapshot"].handler
        out = _run(handler())
        assert out["success"] is False
        assert out["error"] == "bridge 未就绪"
        assert out["status"] == 500

    def test_success_output_passthrough(self, monkeypatch) -> None:
        from agentos_plugin_sdk import create_success_result

        server = _load_server()
        payload = {"status": 200, "snapshot_text": "page"}
        stub = _StubTool(create_success_result(payload))
        monkeypatch.setattr(server, "_browser_tool_cls", stub)
        handler = server.plugin._tools["browser_snapshot"].handler
        out = _run(handler())
        assert out == payload


class TestBridgeConfigDegradation:
    def test_config_read_failure_falls_back_to_default_with_warning(
        self, monkeypatch, caplog
    ) -> None:
        import logging

        server = _load_server()

        def broken_get_config() -> dict[str, Any]:
            raise RuntimeError("config file corrupted")

        monkeypatch.setattr(server.plugin, "get_config", broken_get_config)
        with caplog.at_level(logging.WARNING):
            cfg = server._bridge_config()

        # 配置损坏走默认 bridge 段（空 dict），不抛断主流程
        assert cfg == {}
        # 降级必须可见：warning 级日志携带失败原因
        records = [r for r in caplog.records if "降级默认配置" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "config file corrupted" in records[0].getMessage()

    def test_missing_bridge_section_does_not_warn(self, monkeypatch, caplog) -> None:
        import logging

        server = _load_server()
        monkeypatch.setattr(server.plugin, "get_config", lambda: {"other": 1})
        with caplog.at_level(logging.WARNING):
            cfg = server._bridge_config()

        # bridge 段未配置是合法状态（走 bridge_client 默认值），不产生降级告警
        assert cfg == {}
        assert caplog.records == []


# ── 并发区分度（AR-H1）：容器身份显式传参，不落进程级环境 ──────


class TestConcurrentContainerIdentity:
    """A/B 双会话交错调用，容器身份必须各归各（沙箱隔离边界）。

    sidecar 是多会话共享的单进程（官方 dispatcher 每消息独立 task 分发），
    容器身份若经进程级 os.environ 传递，交错时 A 会话可携 B 容器身份发出。
    交错注入点设在 ``BridgeClient.call``：外层会话身份已定、尚未消费之时，
    同步驱动内层会话的完整 写身份→消费身份 周期——固定"身份各归各"不变量，
    不依赖窗口内恰好无挂起点这类调度细节。docker exec 子进程为外部面，打桩。
    """

    @staticmethod
    def _patch_transport(monkeypatch: Any, executed: list[dict[str, str]]) -> None:
        from agentos_plugin_sdk import bridge_client as bc

        monkeypatch.setattr(bc.BridgeClient, "_resolve_token", staticmethod(lambda cfg: "tok"))
        monkeypatch.setattr(bc, "_docker_gateway_ip", lambda: "")
        monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)

        class Proc:
            returncode = 0
            stderr = b""
            stdout = json.dumps(
                {"ok": True, "status": 200, "body": {"result": {"content": [{"type": "text", "text": "ok"}]}}}
            ).encode("utf-8")

        def fake_run(cmd: list, input=None, capture_output=None, timeout=None):  # noqa: ANN001
            # argv: [docker, exec, -i, -e, PYTHONIOENCODING=utf-8, <container_id>, python, -c, script]
            executed.append(
                {"container": cmd[5], "session": json.loads(input.decode("utf-8"))["session"]}
            )
            return Proc()

        monkeypatch.setattr(bc.subprocess, "run", fake_run)

    @staticmethod
    def _execute(browser_tool_mod: Any, session_id: str, container_id: str) -> Any:
        tool = browser_tool_mod.BrowserTool({"upstream": "browser"})
        return _run(
            tool.execute(
                {
                    "_tool_name": "browser_snapshot",
                    "session_id": session_id,
                    "_container_id": container_id,
                }
            )
        )

    @staticmethod
    def _drive_isolated_loop(coro: Any) -> Any:
        """在独立事件循环里把协程跑到完成（注入点已在线程池 worker 内执行）。

        ``BrowserTool.execute`` 的 Bridge 调用经 ``asyncio.to_thread`` 下放
        （宿主事件循环活性，审计 A-1），交错注入点 ``BridgeClient.call`` 因此
        运行在 worker 线程——该线程无运行中的循环，可安全起一个私有循环驱动
        内层会话，身份窗口（构造客户端→发出调用）仍被完整覆盖。
        """
        return asyncio.run(coro)

    @pytest.mark.parametrize(("outer_cid", "inner_cid"), [("cid-A", "cid-B"), ("cid-B", "cid-A")])
    def test_interleaved_sessions_keep_own_container(
        self, monkeypatch: Any, outer_cid: str, inner_cid: str
    ) -> None:
        from agentos_plugin_sdk import bridge_client as bc

        monkeypatch.delenv("_CONTAINER_ID", raising=False)
        executed: list[dict[str, str]] = []
        self._patch_transport(monkeypatch, executed)
        tool_mod = _load_tool()

        real_call = bc.BridgeClient.call
        interleave_state = {"done": False}

        def call_with_interleave(client: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if interleave_state["done"]:
                return real_call(client, tool_name, arguments)
            interleave_state["done"] = True
            inner_tool = tool_mod.BrowserTool({"upstream": "browser"})
            inner = self._drive_isolated_loop(
                inner_tool.execute(
                    {
                        "_tool_name": "browser_snapshot",
                        "session_id": f"sess-{inner_cid}",
                        "_container_id": inner_cid,
                    }
                )
            )
            assert inner.success is True
            return real_call(client, tool_name, arguments)

        monkeypatch.setattr(bc.BridgeClient, "call", call_with_interleave)

        outer = self._execute(tool_mod, f"sess-{outer_cid}", outer_cid)

        assert outer.success is True
        # 每次容器内调用的 docker exec 目标与 bridge 会话各归各，无串号
        assert executed == [
            {"container": inner_cid, "session": f"sess-{inner_cid}"},
            {"container": outer_cid, "session": f"sess-{outer_cid}"},
        ]
        # 容器身份不落进程级环境（无残留写点）
        assert "_CONTAINER_ID" not in os.environ

    def test_env_untouched_even_when_client_init_fails(self, monkeypatch: Any) -> None:
        """BridgeClient 构造失败（token 缺失）路径同样不得写进程环境。"""
        from agentos_plugin_sdk import bridge_client as bc

        monkeypatch.delenv("_CONTAINER_ID", raising=False)

        def _no_token(cfg: Any) -> str:
            raise bc.BridgeClientError("AGENTOS_BRIDGE_TOKEN 未配置")

        monkeypatch.setattr(bc.BridgeClient, "_resolve_token", staticmethod(_no_token))
        tool_mod = _load_tool()

        result = self._execute(tool_mod, "sess-A", "cid-A")

        assert result.success is False
        assert result.error_code == "BRIDGE_TOKEN_MISSING"
        assert "_CONTAINER_ID" not in os.environ
