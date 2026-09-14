# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""community_ops server.py 接口适配层测试。

覆盖 plugins/shared/tools/community_ops/server.py：
1. 工具注册：github_ops / feedback_ledger 均注册到 plugin 对象
2. github_ops handler：成功透传 output / 失败包装 {"success": False, "error"}
3. feedback_ledger handler：成功透传 / 失败包装
4. 合宿平铺防劫持：裸名 ``tool`` 槽位被异成员占据时 handler 仍取本目录实现，
   loader 缓存幂等

tool.py 实现层为真实代码，github 的 httpx 依赖以伪客户端打桩（落在唯一名
impl 模块上）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_IMPL_KEY = "community_ops_tool_impl"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_server() -> Any:
    """动态加载 server.py（逐出裸名 plugin/tool 与 impl 缓存，防跨测试劫持）。"""
    mod_name = "community_ops_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules.pop("plugin", None)
    sys.modules.pop("tool", None)
    sys.modules.pop(_IMPL_KEY, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _impl_tool_mod(server: Any) -> Any:
    """handler 实际使用的 tool.py impl 模块（loader 幂等触发后读唯一名注册位）。"""
    server._load_tool_class("GitHubOpsTool")
    impl = sys.modules.get(_IMPL_KEY)
    assert impl is not None, "server loader 未注册 community_ops_tool_impl"
    return impl


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return self._resp


def test_tools_registered() -> None:
    mod = _load_server()
    assert "github_ops" in mod.plugin._tools
    assert "feedback_ledger" in mod.plugin._tools


def test_declared_input_schemas_match_registry() -> None:
    """G2 契约一致性：plugin.json 声明的每个工具必须注册到 server，且上报
    input_schema 与声明逐字一致——漂移被内核 G2 净化剔除后工具运行时不可见
    （BUG-5：github_ops/feedback_ledger 曾因声明富 schema、上报瘦 schema 的
    双写漂移被全量剔除 2→0）。"""
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = manifest["capabilities"]["tools"]
    assert declared, "manifest 必须声明工具（工具面声明驱动）"
    mod = _load_server()
    for tool in declared:
        name = tool["name"]
        assert name in mod.plugin._tools, (
            f"{name} 在 plugin.json 声明了但 server 未注册（G2 missing 漂移 → 剔除）"
        )
        assert mod.plugin._tools[name].schema == tool["input_schema"], (
            f"{name} 上报 input_schema 与 plugin.json 声明不一致"
            "（G2 schema_mismatch → 剔除）"
        )


def test_github_ops_handler_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_server()
    tool_mod = _impl_tool_mod(mod)
    monkeypatch.setenv("AGENTOS_GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        tool_mod.httpx,
        "AsyncClient",
        lambda *_: _FakeAsyncClient(_FakeResponse({"stargazers_count": 5})),
    )
    out = _run(mod.github_ops(action="repo_stats", repo="o/r"))
    assert out["stats"]["stars"] == 5
    assert out["action"] == "repo_stats"


def test_github_ops_handler_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_server()
    _impl_tool_mod(mod)
    monkeypatch.delenv("AGENTOS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = _run(
        mod.github_ops(action="create_comment", repo="o/r", number=1, body="x")
    )
    assert out["success"] is False
    assert out["error_code"] == "TOKEN_MISSING"
    assert "AGENTOS_GITHUB_TOKEN" in out["error"]


def test_feedback_ledger_handler_roundtrip(tmp_path: Path) -> None:
    mod = _load_server()
    ledger = str(tmp_path / "ledger.json")
    out = _run(
        mod.feedback_ledger(
            action="append",
            source="github_issue",
            summary="巡检发现新 issue",
            ledger_path=ledger,
        )
    )
    assert out["entry"]["id"] == "FB-001"
    out = _run(
        mod.feedback_ledger(action="update_status", id="FB-001", status="已回复",
                            ledger_path=ledger)
    )
    assert out["entry"]["status"] == "已回复"


def test_feedback_ledger_handler_failure(tmp_path: Path) -> None:
    mod = _load_server()
    out = _run(
        mod.feedback_ledger(action="nope", ledger_path=str(tmp_path / "l.json"))
    )
    assert out["success"] is False
    assert out["error_code"] == "INVALID_ACTION"


class TestCohostShadowing:
    """合宿平铺下裸名 ``tool`` 槽位被异成员占据时，handler 仍取本目录实现。"""

    def test_decoy_tool_module_ignored(self) -> None:
        mod = _load_server()
        decoy = types.ModuleType("tool")

        class _ForeignGitHubOpsTool:
            pass

        decoy.GitHubOpsTool = _ForeignGitHubOpsTool
        sys.modules["tool"] = decoy
        try:
            cls = mod._load_tool_class("GitHubOpsTool")
            cls_again = mod._load_tool_class("GitHubOpsTool")
            assert cls is not _ForeignGitHubOpsTool
            assert cls is cls_again
        finally:
            sys.modules.pop("tool", None)
