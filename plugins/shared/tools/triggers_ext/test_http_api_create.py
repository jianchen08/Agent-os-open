# @feature: FP-0.2.二 内部模块 manifest（trigger 创建 REST 契约） | @ci: python-coverage
"""回归：POST /ext/trigger_setup_tool/triggers 的错误信封契约（BUG-22）。

契约：create 的校验/业务拒绝必须走协议错误信封（success:true + data 携带真实
HTTP 状态 401/400），不得走 ``_error`` 的 success:false 信封——内核
SidecarHttpHandler 对 success:false 一律映射 502，前端按可重试 5xx 处理
（同一请求重放三次）且丢失结构化错误体。

覆盖三条路径：
- 缺 Bearer 凭据 → 401（协议信封）；
- 缺 pipeline_id（表单目标管道留空且无激活管道）→ 400，错误文案点名参数；
- 带目标管道创建成功 → 200 + trigger 载荷（目标管道字段契约锚）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/triggers_ext/
_SHARED_ROOT = os.path.abspath(os.path.join(str(_PLUGIN_DIR), "..", ".."))


def _load_module(mod_name: str, filename: str, evict_bare: tuple[str, ...] = ()) -> Any:
    """按唯一模块名装载插件内模块（逐出同名裸名缓存，防跨插件串扰）。

    本插件的 http_api.py 内用裸名 ``from tool import TriggerSetupTool`` 取同目录
    tool.py。车道共跑时 sys.path 里更靠前的其它插件目录（如 tools/task_evaluate）
    也有同名 tool.py，会先被解析命中 → 装载 ImportError（单跑绿、共跑红）。

    tests/plugins/conftest.py 的 pytest_runtest_setup 只治理 ``/tests/plugins/``
    下的测试文件，插件目录内的测试（本文件）不在其庇护范围；故此处自防御：
    装载前把本插件目录【晋升到 sys.path 最前】并逐出同名裸名缓存，使裸名 import
    确定性地解析到本目录。
    """
    import importlib.util

    here = str(_PLUGIN_DIR)
    while here in sys.path:
        sys.path.remove(here)
    sys.path.insert(0, here)

    if mod_name in sys.modules:
        del sys.modules[mod_name]
    for bare in evict_bare:
        sys.modules.pop(bare, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _b64_token(user_id: str = "u-trigger", exp: int | None = None) -> str:
    """构造内核 0.2 开发期 token（base64_nopad("access:{user_id}:{username}:{exp}")）。"""
    payload = f"access:{user_id}:tester:{exp or int(time.time()) + 3600}"
    return base64.b64encode(payload.encode()).decode().rstrip("=")


def _decode_http(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """解包 http.handle ToolExecutionResult 信封 → (status, json_body)。"""
    assert result["success"], f"协议错误信封必须 success:true：{result}"
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def http_api() -> Any:
    return _load_module("triggers_ext_http_api_create", "http_api.py", ("tool", "http_api", "server"))


class TestCreateTriggerErrorEnvelope:
    """create_trigger 拒绝路径：协议错误信封（真实 HTTP 状态到达前端）。"""

    def test_missing_bearer_maps_to_http_401(self, http_api: Any) -> None:
        """无 Authorization 头 → 401 协议信封（非 success:false 的伪 502）。"""
        result = _run(http_api.create_trigger({"trigger_type": "delay", "message": "hi"}, {}))
        status, body = _decode_http(result)
        assert status == 401
        assert "user_id" in body["error"]["message"]

    @pytest.mark.parametrize("headers", [None, {"authorization": "Bearer not-a-token"}])
    def test_invalid_bearer_maps_to_http_401(self, http_api: Any, headers: Any) -> None:
        """无效/过期 token 同样 401（身份不可恢复即拒绝，区分度输入）。"""
        result = _run(
            http_api.create_trigger({"trigger_type": "delay", "message": "hi"}, headers)
        )
        status, _body = _decode_http(result)
        assert status == 401

    def test_missing_pipeline_id_maps_to_http_400_with_reason(
        self, http_api: Any
    ) -> None:
        """目标管道缺失（表单留空且无激活管道）→ 400 + 文案点名 pipeline_id。"""
        headers = {"authorization": f"Bearer {_b64_token()}"}
        body = {"trigger_type": "delay", "message": "到点了", "delay_seconds": 90}
        result = _run(http_api.create_trigger(body, headers))
        status, payload = _decode_http(result)
        assert status == 400
        assert "pipeline_id" in payload["error"]["message"]

    def test_missing_delay_seconds_maps_to_http_400(self, http_api: Any) -> None:
        """类型=delay 缺 delay_seconds 的业务校验拒绝同样走 400 协议信封。"""
        headers = {"authorization": f"Bearer {_b64_token()}"}
        body = {"trigger_type": "delay", "message": "到点了", "pipeline_id": "pipe-x"}
        result = _run(http_api.create_trigger(body, headers))
        status, payload = _decode_http(result)
        assert status == 400
        assert "delay_seconds" in payload["error"]["message"]


class TestCreateTriggerSuccess:
    """带目标管道创建成功：200 + trigger 载荷（BUG-22 验收②的后端锚）。"""

    def test_create_with_pipeline_id_registers_and_returns_trigger(
        self, http_api: Any
    ) -> None:
        headers = {"authorization": f"Bearer {_b64_token()}"}
        body = {
            "trigger_type": "delay",
            "message": "到点了",
            "delay_seconds": 90,
            "name": "回归锚",
            "pipeline_id": "pipe-reg",
        }
        try:
            result = _run(http_api.create_trigger(body, headers))
            status, payload = _decode_http(result)
            assert status == 200
            trigger = payload["trigger"]
            assert trigger["trigger_id"].startswith("trigger_delay_")
            assert trigger["pipeline_id"] == "pipe-reg"
            # 注册表可查（落 TriggerManager 单例，列表数据源同源）
            assert http_api.get_trigger_manager().get(trigger["trigger_id"]) is not None
        finally:
            for cfg in http_api.get_trigger_manager().list_all():
                if cfg.pipeline_id == "pipe-reg":
                    http_api.get_trigger_manager().unregister(cfg.trigger_id)
