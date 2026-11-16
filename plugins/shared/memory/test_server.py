# @feature: FP-0.2.二 memory server 接口 | @ci: python-coverage
"""memory server.py 失败信封契约测试（ADR 2026-09-28 收编漏网回归）。

2026-10-13 装机事故：memory 工具内部失败（hindsight 后端降级等）时 handler
返回裸 ``{"error": ...}``（无 success 键）——内核归一层删除「error 键猜失败」
分支后，裸 error 载荷被包成 success 伪信封，再撞 output_schema
（required: success）fail-closed，真错误被
「missing required field `success`」掩蔽，LLM 三连败后放弃记忆保存。

契约（ADR 2026-09-28-tool-result-status-explicit）：handler 失败路径必须显式
``{"success": False, "error": ...}``——归一层判失败、跳过 output_schema 校验，
真错误直达 LLM。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MOD_NAME = "memory_server_contract_test"


def _load_server() -> Any:
    """动态加载 server.py（唯一模块名，隔离其他测试的同名装载）。"""
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBackend:
    """确定性假后端：add 返回固定锚点 / raise 可注入。"""

    def __init__(self, memory_id: str = "mem-fake-1", error: Exception | None = None):
        self._memory_id = memory_id
        self._error = error

    async def add(self, **kwargs: Any) -> str:
        if self._error is not None:
            raise self._error
        return self._memory_id


class TestFailureEnvelopeExplicitSuccess:
    """失败路径必须显式带 success=False（收编契约，事故回归锚）。"""

    def test_backend_missing_failure_envelope(self) -> None:
        """真实依赖路径：后端未注入 → 执行失败 → 信封 success=False + 真错误。"""
        server = _load_server()
        out = _run(server.memory(action="retrieve", query="x"))
        assert out["success"] is False
        assert isinstance(out["error"], str) and out["error"]

    def test_store_failure_envelope_carries_real_error(self, monkeypatch) -> None:
        """store 撞后端异常：信封 success=False 且 error 携带真实失败原因。"""
        server = _load_server()
        monkeypatch.setattr(
            server,
            "_memory_backend",
            _FakeBackend(error=RuntimeError("hindsight 后端降级: not initialized")),
        )
        out = _run(server.memory(action="store", content="日程", session_id="thread-t"))
        assert out["success"] is False
        assert "hindsight 后端降级" in out["error"]

    def test_success_envelope_store(self, monkeypatch) -> None:
        """成功路径：信封 success=True 且 memory_id 为后端回传锚点。"""
        server = _load_server()
        monkeypatch.setattr(server, "_memory_backend", _FakeBackend(memory_id="mem-ok-9"))
        out = _run(
            server.memory(action="store", content="日程", session_id="thread-t")
        )
        assert out["success"] is True
        assert out["memory_id"] == "mem-ok-9"


class TestEnvelopeSatisfiesDeclaredSchema:
    """handler 两种信封都满足 plugin.json 声明的 output_schema（声明-实现对账）。"""

    def _output_schema(self) -> dict[str, Any]:
        manifest = json.loads(
            (_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8")
        )
        tools = manifest["capabilities"]["tools"]
        return next(t for t in tools if t["name"] == "memory")["output_schema"]

    def test_both_envelopes_validate_against_declaration(self, monkeypatch) -> None:
        schema = self._output_schema()
        assert "success" in schema["required"]

        server = _load_server()
        monkeypatch.setattr(server, "_memory_backend", _FakeBackend(memory_id="mem-schema-1"))
        ok = _run(server.memory(action="store", content="x", session_id="thread-t"))
        # 字面值断言配性质断言：信封过声明校验（范围：required 全命中）
        assert ok["success"] is True
        jsonschema.validate(ok, schema)

        monkeypatch.setattr(server, "_memory_backend", None)
        # 触发真实失败路径：清空缓存后端 + 无 capability 注入 → 未注入失败
        bad = _run(server.memory(action="retrieve", query="x"))
        jsonschema.validate(bad, schema)
        assert bad["success"] is False
