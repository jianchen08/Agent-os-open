# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""六模式包数据面降级/容错补测（对 test_mode_panel_live_* 的补位）。

live 测试覆盖正常读数/落列/派发形状；本文件专攻同构的边角面：
- on_load 惰性 provider 注册 + 能力未注入（KeyError）→ 读面诚实降级空
- provider 调用抛异常 → 降级空 + warn-once（同类告警只发一次）
- 归一化容错：ckpt 非数字钳 0 / 非字典行跳过 / reviews 信封多形态解包
- 写动作：通道抛错如实透传 / 返回缺任务 id / 非法 JSON 载荷按缺参 400
- 未路由 404 与写动作 GET 404

六包 server.py 是种子自包含的同构内联（目录级播种的结构前提），故按模块
参数化跑同一组行为断言；形态差异面（coding 板/派发细分）单独成节。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODES = ("mode_coding", "mode_godot", "mode_planning", "mode_research", "mode_roleplay", "mode_writing")

pytestmark = pytest.mark.unit

_loaded: dict[str, Any] = {}


def _load_server(mode: str):
    """按唯一模块名装载模式包 server.py（进程内重复装载防双实例串扰）。"""
    if mode in _loaded:
        return _loaded[mode]
    path = os.path.join(MODES_DIR, mode, "server.py")
    spec = importlib.util.spec_from_file_location(f"mode_gaps_{mode}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _loaded[mode] = module
    return module


def _call(module, path: str, method: str = "GET", raw_body: str = "", query: dict | None = None) -> dict:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {})
    )


def _body_json(result: dict) -> dict:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status(result: dict) -> int:
    return int(result["data"]["status"])


@pytest.fixture
def fresh(request):
    """干净模块：清 provider + caplog 捕获降级告警。"""
    module = _load_server(request.param)
    module.reset_providers()
    return module


# ── 通用降级面（六包同构，参数化） ────────────────────────────────────────────────


class _FakeHandle:
    """能力句柄替身：记录调用并回放预置结果（模拟内核握手后的真实取数路径）。"""

    def __init__(self, results: dict[tuple[str, ...], Any]):
        self._results = results
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict | None = None):
        self.calls.append((method, params or {}))
        for prefix, value in self._results.items():
            if method == prefix or method.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        return []


def _install_fake_capabilities(monkeypatch, module) -> dict[str, _FakeHandle]:
    """monkeypatch 公开方法 get_capability → 假句柄（内核注入后的同形取数路径）。"""
    handles = {
        "pipeline-state": _FakeHandle({("list", "list"): []}),
        "service-registry": _FakeHandle({("messages",): []}),
        "tool-executor": _FakeHandle({("invoke", "get_pending"): {"data": {"requests": []}}}),
    }
    monkeypatch.setattr(
        module.plugin, "get_capability", lambda name: handles.get(name), raising=True
    )
    return handles


@pytest.mark.parametrize("fresh", MODES, indirect=True)
def test_on_load_with_capabilities_reads_flow_through(fresh, monkeypatch):
    """on_load 注册真实闭包 + 能力句柄在位 → 读端点走完整取数/归一链路。

    与降级面互补：KeyError 面证明能力缺席时诚实降级；本用例证明句柄在位时
    on_load 闭包体（state 取数 / messages 取数 / tool-executor 绑定）真实执行。
    """
    handles = _install_fake_capabilities(monkeypatch, fresh)
    asyncio.run(fresh._on_load({}))

    prefix = next(iter(fresh._DATA_ROUTES)).rsplit("/data/", 1)[0]
    sessions_route = next(
        (r for r in fresh._DATA_ROUTES if r.endswith(("sessions", "projects"))), None
    )
    assert sessions_route is not None
    result = _call(fresh, sessions_route)
    assert _status(result) == 200
    assert any(m == "list" for m, _ in handles["pipeline-state"].calls), "state 取数闭包应执行"

    messages = _call(fresh, f"{prefix}/data/messages", query={"pipeline_id": "p-1"})
    # 六包中 planning 无独立 messages 路由（404 属路由事实非降级），其余须 200
    assert _status(messages) in (200, 404)


@pytest.mark.parametrize("fresh", MODES, indirect=True)
def test_on_load_without_capabilities_degrades_all_reads(fresh, caplog):
    """on_load 真跑（无能力注入）→ 全部 GET 数据端点 200 空载荷，不崩。"""
    with caplog.at_level(logging.WARNING, logger=fresh.logger.name):
        asyncio.run(fresh._on_load({}))

    routes = sorted(getattr(fresh, "_DATA_ROUTES", set()))
    assert routes, "模式包应声明 _DATA_ROUTES"
    for route in routes:
        query = {"pipeline_id": "p-1"} if route.endswith(("/messages", "/report")) else {}
        result = _call(fresh, route, query=query)
        # 读面信封完备：降级空 200 / 带必填 query 端点缺参诚实 400，皆不 500 不崩
        status = _status(result)
        assert status in (200, 400), route
        body = _body_json(result)
        assert isinstance(body, dict)
        if status == 400:
            assert body.get("error"), route


@pytest.mark.parametrize("fresh", MODES, indirect=True)
def test_provider_exception_degrades_and_warns_once(fresh, caplog):
    """provider 抛异常 → 该读面降级空（200），同类告警只发一次。"""
    async def _boom(**kwargs):
        raise RuntimeError("内核读数炸了")

    fresh._set_provider("pipeline-state", _boom)
    sessions_route = next(
        (r for r in fresh._DATA_ROUTES if r.endswith(("sessions", "projects"))), None
    )
    assert sessions_route is not None
    with caplog.at_level(logging.WARNING, logger=fresh.logger.name):
        first = _call(fresh, sessions_route)
        second = _call(fresh, sessions_route)

    assert _status(first) == 200
    assert _status(second) == 200
    degrade_warnings = [
        r for r in caplog.records
        if "调用失败" in r.getMessage() and "pipeline-state" in r.getMessage()
    ]
    assert len(degrade_warnings) == 1, "warn-once：同类降级告警只发一次"


@pytest.mark.parametrize("fresh", MODES, indirect=True)
def test_messages_route_contract(fresh):
    """/data/messages：缺 pipeline_id → 400；GET 之外 → 404 信封。"""
    prefix = next(iter(fresh._DATA_ROUTES)).rsplit("/data/", 1)[0]
    missing = _call(fresh, f"{prefix}/data/messages")
    if _status(missing) == 404:
        pytest.skip(f"{fresh.__name__} 无独立 messages 路由")
    assert _status(missing) == 400
    assert "pipeline_id" in _body_json(missing)["error"]
    not_allowed = _call(fresh, f"{prefix}/data/messages", method="POST", raw_body="{}")
    assert _status(not_allowed) == 404


@pytest.mark.parametrize("fresh", MODES, indirect=True)
def test_unrouted_404_and_action_get_404(fresh):
    """未路由路径 404；GET 打写动作端点 404。"""
    result = _call(fresh, "/ext/_no_such_mode_/data/nothing")
    assert _status(result) == 404
    action = next(iter(getattr(fresh, "_ACTION_ROUTES", set())), None)
    if action:
        assert _status(_call(fresh, action)) == 404


# ── coding 特有形态面 ────────────────────────────────────────────────────────────


def test_coding_sessions_ckpt_garbage_clamped_to_zero():
    """ckpt_max_seq 非数字（str/None）→ checkpoints 钳 0，不抛。"""
    module = _load_server("mode_coding")
    module.reset_providers()

    async def _rows():
        await asyncio.sleep(0)
        return [
            {"pipeline_id": "p1", "mode": "coding", "ckpt_max_seq": "abc"},
            {"pipeline_id": "p2", "mode": "coding", "ckpt_max_seq": None},
            {"pipeline_id": "p3", "mode": "coding", "ckpt_max_seq": -5},
        ]

    module._set_provider("pipeline-state", _rows)
    result = _call(module, "/ext/mode_coding/data/sessions")
    sessions = _body_json(result)["sessions"]
    by_pid = {s["pipeline_id"]: s for s in sessions}
    assert by_pid["p1"]["checkpoints"] == 0
    assert by_pid["p2"]["checkpoints"] == 0
    assert by_pid["p3"]["checkpoints"] == 0  # 负水位钳 0（无消息内核写 -1）


def test_coding_messages_skips_non_dict_rows():
    """messages.list 行含非字典元素 → 跳过不崩。"""
    module = _load_server("mode_coding")
    module.reset_providers()

    async def _msgs(pipeline_id, limit=None):
        await asyncio.sleep(0)
        return ["garbage", 42, {"role": "user", "content": "正常行"}]

    module._set_provider("messages", _msgs)
    result = _call(module, "/ext/mode_coding/data/messages", query={"pipeline_id": "p-1"})
    body = _body_json(result)
    assert len(body["messages"]) == 1


def test_coding_reviews_envelope_shapes():
    """reviews 解包三形态：{data:{requests:[]}} / 裸列表 / 垃圾元素跳过。"""
    module = _load_server("mode_coding")
    module.reset_providers()

    async def _pending_shape_a(payload):
        await asyncio.sleep(0)
        return {"data": {"requests": [
            {"request_id": "r1", "title": "审批一"},
            "garbage",
        ]}}

    module._set_provider("tool-executor", _pending_shape_a)
    items = _body_json(_call(module, "/ext/mode_coding/data/reviews"))["items"]
    assert [i["request_id"] for i in items] == ["r1"]

    async def _pending_shape_b(payload):
        await asyncio.sleep(0)
        return [{"id": "r2", "message_data": {"title": "审批二"}}]

    module._set_provider("tool-executor", _pending_shape_b)
    items = _body_json(_call(module, "/ext/mode_coding/data/reviews"))["items"]
    assert [(i["request_id"], i["title"]) for i in items] == [("r2", "审批二")]

    async def _pending_shape_c(payload):
        await asyncio.sleep(0)
        return {"data": "garbage-string"}

    module._set_provider("tool-executor", _pending_shape_c)
    assert _body_json(_call(module, "/ext/mode_coding/data/reviews"))["items"] == []


def test_coding_dispatch_channel_raise_and_missing_task_id():
    """派发：通道抛错 → error 透传；通道返回缺任务 id → error 如实回报。"""
    module = _load_server("mode_coding")
    module.reset_providers()
    body = json.dumps({"issue_text": "修 x"})

    async def _boom(payload):
        raise RuntimeError("executor 炸了")

    module._set_provider("tool-executor", _boom)
    out = _body_json(
        _call(module, "/ext/mode_coding/data/actions/dispatch_issue", method="POST", raw_body=body)
    )
    assert "task_submit 调用失败" in out["error"]
    assert "executor 炸了" in out["error"]

    async def _no_id(payload):
        await asyncio.sleep(0)
        return {"data": {"ok": True}}

    module._set_provider("tool-executor", _no_id)
    out = _body_json(
        _call(module, "/ext/mode_coding/data/actions/dispatch_issue", method="POST", raw_body=body)
    )
    assert "未返回任务 id" in out["error"]

    async def _nonsense(payload):
        await asyncio.sleep(0)
        return "not-a-dict"

    module._set_provider("tool-executor", _nonsense)
    out = _body_json(
        _call(module, "/ext/mode_coding/data/actions/dispatch_issue", method="POST", raw_body=body)
    )
    assert "未返回任务 id" in out["error"]


def test_coding_actions_bad_json_body_treated_as_missing_params():
    """非法 JSON 载荷 → 按缺参 400（不 500 不静默成功）。"""
    module = _load_server("mode_coding")
    module.reset_providers()
    result = _call(
        module, "/ext/mode_coding/data/actions/dispatch_issue",
        method="POST", raw_body="{not-json",
    )
    assert _status(result) == 400
