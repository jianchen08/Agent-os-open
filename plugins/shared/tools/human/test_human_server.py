# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""human 插件 server.py 装配测试（双角色：LLM 工具 + 交互服务工具 + event-bus 通知器）。

与 tests/plugins/tools/human/test_file_paths_passthrough.py（file_paths 接线）
互补：本文件覆盖工具入口参数归一化/异常映射、interaction.* 服务工具、
_EventBusNotifier 事件契约、on_load/on_unload 生命周期、冷启动自愈。

外部依赖（event-bus capability 通道）注入替身；服务状态机用真实实现
（interaction.* 工具全真实往返），LLM 工具入口的阻塞等待用 spy 服务
（同既有 handler 接线测试约定，等待语义由 test_service.py 真实覆盖）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _PLUGIN_DIR.parent


def _load_server() -> Any:
    """importlib 显式路径 + 唯一模块名加载 server.py。

    server.py 内部 `from human.service import ...` 需要 tools 目录提供
    human 包命名空间（human 未安装为包）。
    """
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    sys.modules.pop("human", None)
    name = "human_server_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "cannot load human/server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server() -> Any:
    """每个用例加载全新 server 模块（_service 全局互不污染）。"""
    return _load_server()


def test_server_module_self_inserts_tools_dir() -> None:
    """server.py 顶层自插 tools 目录到 sys.path（包路径导入自持，不依赖外部注入）。"""
    if str(_TOOLS_DIR) in sys.path:
        sys.path.remove(str(_TOOLS_DIR))
    sys.modules.pop("human", None)
    name = "human_server_self_insert_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "cannot load human/server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    try:
        assert str(_TOOLS_DIR) in sys.path  # 模块自身完成注入
        assert module._service is None
    finally:
        sys.modules.pop(name, None)
        sys.modules.pop("human", None)
        if str(_TOOLS_DIR) in sys.path:
            sys.path.remove(str(_TOOLS_DIR))


class _FakeBus:
    """记录 notify 调用的假 event-bus capability。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("bus down")
        self.calls.append((method, params))


class _FakePlugin:
    """假插件：get_capability 按注入表返回，缺失抛 KeyError（同 SDK 语义）。"""

    def __init__(self, caps: dict[str, Any] | None = None, attr_error: bool = False) -> None:
        self._caps = caps or {}
        self._attr_error = attr_error

    def get_capability(self, name: str) -> Any:
        if self._attr_error:
            raise AttributeError(name)
        if name not in self._caps:
            raise KeyError(name)
        return self._caps[name]


# ════════════════════════════════════════════════════════════
# _EventBusNotifier 事件契约
# ════════════════════════════════════════════════════════════


async def test_notifier_emit_success_and_failure(server: Any) -> None:
    """_emit：bus 可用时 notify("emit", ...) 返回 True；异常返回 False。"""
    bus = _FakeBus()
    notifier = server._EventBusNotifier(_FakePlugin({"event-bus": bus}))
    assert await notifier._emit("interaction_request", {"a": 1}, "t1") is True
    assert bus.calls == [("emit", {"event": "interaction_request", "payload": {"a": 1}, "thread_id": "t1"})]

    bad_bus = _FakeBus(fail=True)
    bad_notifier = server._EventBusNotifier(_FakePlugin({"event-bus": bad_bus}))
    assert await bad_notifier._emit("interaction_request", {}, "") is False


async def test_notifier_bus_missing_returns_false(server: Any) -> None:
    """event-bus 未注入（KeyError/AttributeError）：_bus 返回 None，emit 返回 False。"""
    notifier = server._EventBusNotifier(_FakePlugin({}))
    assert await notifier._emit("interaction_request", {}, "") is False
    attr_notifier = server._EventBusNotifier(_FakePlugin(attr_error=True))
    assert await attr_notifier._emit("interaction_request", {}, "") is False


async def test_notify_request_payload_from_real_record(server: Any) -> None:
    """notify_request：真实服务记录 → 完整 payload（含可选键）。"""
    await server._on_load({})
    svc = server._service
    assert svc is not None
    rid = await svc.create_choice_request(
        "s1", "t1", "tab1", "审批", description="desc",
        options=[{"id": "1", "label": "批准"}], questions=["确认？"],
        timeout_seconds=30, priority=server.Priority.HIGH, agent_level="L2", pipeline_id="pipe-1",
        file_paths=["a.md"],
    )
    record = await svc.get_request(rid)
    assert record is not None
    bus = _FakeBus()
    notifier = server._EventBusNotifier(_FakePlugin({"event-bus": bus}))
    assert await notifier.notify_request(record) is True
    assert len(bus.calls) == 1
    method, params = bus.calls[0]
    assert method == "emit"
    payload = params["payload"]
    assert payload["request_id"] == rid
    assert payload["session_id"] == "s1"
    assert payload["status"] == "pending"
    assert payload["thread_id"] == "t1"
    assert payload["tab_id"] == "tab1"
    assert payload["interaction_mode"] == "choice"
    assert payload["title"] == "审批"
    assert payload["description"] == "desc"
    assert payload["options"] == [{"id": "1", "label": "批准"}]
    assert payload["questions"] == ["确认？"]
    assert payload["timeout_seconds"] == 30
    assert payload["priority"] == "high"
    assert payload["agent_level"] == "L2"
    assert payload["pipeline_id"] == "pipe-1"
    assert payload["file_paths"] == ["a.md"]
    assert params["thread_id"] == "t1"


async def test_notify_request_non_dict_record_defaults(server: Any) -> None:
    """notify_request：非 dict 记录 / 缺 message_data → 空字段兜底，不崩溃。"""
    bus = _FakeBus()
    notifier = server._EventBusNotifier(_FakePlugin({"event-bus": bus}))
    assert await notifier.notify_request("not-a-record") is True
    payload = bus.calls[0][1]["payload"]
    assert payload["request_id"] == "" and payload["title"] == "" and payload["thread_id"] == ""

    bus.calls.clear()
    assert await notifier.notify_request({"id": "r1", "session_id": "s1", "status": "pending"}) is True
    payload = bus.calls[0][1]["payload"]
    assert payload["request_id"] == "r1"
    assert payload["interaction_mode"] == ""  # 无 message_data → 缺省
    assert "options" not in payload  # 可选键不出现


async def test_notify_cancel_timeout_reminder_conversation(server: Any) -> None:
    """notify_cancel / notify_timeout / notify_timeout_reminder / notify_conversation_start 事件契约。"""
    bus = _FakeBus()
    notifier = server._EventBusNotifier(_FakePlugin({"event-bus": bus}))

    assert await notifier.notify_cancel("r1", "user_abort", "t1") is True
    assert bus.calls[-1] == (
        "emit",
        {"event": "interaction_cancelled", "payload": {"request_id": "r1", "reason": "user_abort"}, "thread_id": "t1"},
    )

    assert await notifier.notify_timeout("r2", "t2") is True
    assert bus.calls[-1] == (
        "emit",
        {"event": "interaction_timeout", "payload": {"request_id": "r2"}, "thread_id": "t2"},
    )

    assert await notifier.notify_timeout_reminder(
        "r3", 60, "t3", title="审批", mode="choice",
        options=[{"id": "1", "label": "批准"}], questions=["确认？"],
    ) is True
    assert bus.calls[-1] == (
        "emit",
        {
            "event": "interaction_timeout_reminder",
            "payload": {
                "request_id": "r3", "remaining_seconds": 60, "title": "审批", "mode": "choice",
                "options": [{"id": "1", "label": "批准"}], "questions": ["确认？"],
            },
            "thread_id": "t3",
        },
    )

    # 区分度输入：无 options/questions → 键不出现
    assert await notifier.notify_timeout_reminder("r4", 30, "t4", title="T", mode="conversation") is True
    payload = bus.calls[-1][1]["payload"]
    assert "options" not in payload and "questions" not in payload

    assert await notifier.notify_conversation_start(
        "t5", "tab5", "讨论", request_id="r5", initial_message="hi", suggestions=["ok"],
    ) is True
    assert bus.calls[-1] == (
        "emit",
        {
            "event": "interaction_conversation_start",
            "payload": {
                "request_id": "r5", "thread_id": "t5", "tab_id": "tab5", "title": "讨论",
                "initial_message": "hi", "suggestions": ["ok"],
            },
            "thread_id": "t5",
        },
    )


# ════════════════════════════════════════════════════════════
# 生命周期
# ════════════════════════════════════════════════════════════


async def test_on_load_initializes_service_with_notifier(server: Any) -> None:
    """on_load：初始化 service 单例并注入 EventBusNotifier；on_unload 置空。"""
    assert server._service is None
    await server._on_load({})
    assert server._service is not None
    assert isinstance(server._service._notifier, server._EventBusNotifier)
    await server._on_unload({})
    assert server._service is None


# ════════════════════════════════════════════════════════════
# 参数归一化辅助
# ════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("not-a-list", None),
        (["批准", "拒绝"], [{"id": "0", "label": "批准"}, {"id": "1", "label": "拒绝"}]),
        (
            [{"id": "1", "label": "批准", "description": "同意"}, {"text": "T"}, {"name": "N"}],
            [
                {"id": "1", "label": "批准", "description": "同意"},
                {"id": "1", "label": "T"},
                {"id": "2", "label": "N"},
            ],
        ),
        (["a", {"label": "b"}], [{"id": "0", "label": "a"}, {"id": "1", "label": "b"}]),
        ([{"id": "x"}, {"label": ""}], None),  # 全部无 label → None
        ([], None),
    ],
)
def test_normalize_options(server: Any, raw: Any, expected: Any) -> None:
    """options 归一化：字符串数组 → [{id,label}]；dict 取 label/text/name；无 label 跳过。"""
    assert server._normalize_options(raw) == expected


def test_resolved_file_paths_skips_invalid_entries(server: Any, tmp_path: Path) -> None:
    """file_paths 归一化：非字符串/空串条目跳过，project_root 锚定相对路径。"""
    out = server._resolved_file_paths(
        {"file_paths": ["", 42, "a.md"], "workspace": str(tmp_path)}
    )
    assert out == [str(tmp_path / "a.md")]
    out2 = server._resolved_file_paths(
        {"file_paths": ["/workspace/b.md"], "project_root": str(tmp_path)}
    )
    assert out2 == [str(tmp_path / "b.md")]


def test_resolved_file_paths_absolute_host_path_passthrough(server: Any, tmp_path: Path) -> None:
    """file_paths 归一化：宿主绝对路径原样透传（root 存在时也不重映射）。"""
    host = str(tmp_path / "c.md")
    out = server._resolved_file_paths({"file_paths": [host], "workspace": str(tmp_path)})
    assert out == [host]


# ════════════════════════════════════════════════════════════
# LLM 工具入口 human_interaction
# ════════════════════════════════════════════════════════════


class _SpyService:
    """记录入参的假服务——LLM 入口的阻塞等待用可控返回值（同既有接线测试约定）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.wait_result: dict[str, Any] = {"response_type": "answered", "selected_option": "ok"}
        self.wait_error: Exception | None = None

    async def send_notification(self, **kwargs: Any) -> str:
        self.calls.append(("notification", kwargs))
        return "rid-n"

    async def create_choice_request(self, **kwargs: Any) -> str:
        self.calls.append(("choice", kwargs))
        return "rid-c"

    async def create_conversation_request(self, **kwargs: Any) -> str:
        self.calls.append(("conversation", kwargs))
        return "rid-v"

    async def wait_for_choice(self, rid: str, timeout: float | None = None) -> dict[str, Any]:
        if self.wait_error is not None:
            raise self.wait_error
        return self.wait_result


@pytest.fixture
def spy(server: Any, monkeypatch: pytest.MonkeyPatch) -> _SpyService:
    s = _SpyService()
    monkeypatch.setattr(server, "_service", s)
    return s


async def test_human_interaction_notification_mode(server: Any, spy: _SpyService) -> None:
    """通知模式：非阻塞返回 sent；message 取 description 或 initial_message。"""
    result = await server.human_interaction(
        mode="notification", title="进度", description="50%", priority="high", pipeline_id="p1",
    )
    assert result == {"status": "sent", "request_id": "rid-n"}
    assert spy.calls[0][0] == "notification"
    kwargs = spy.calls[0][1]
    assert kwargs["session_id"] == "p1"
    assert kwargs["thread_id"] == "p1"
    assert kwargs["message"] == "50%"
    assert kwargs["priority"].value == "high"
    assert kwargs["agent_id"] == "p1"

    # 区分度输入：description 缺失 → initial_message 兜底
    spy.calls.clear()
    result2 = await server.human_interaction(
        mode="notification", title="进度", initial_message="开始", session_id="s9",
    )
    assert result2["status"] == "sent"
    assert spy.calls[0][1]["message"] == "开始"
    assert spy.calls[0][1]["session_id"] == "s9"


async def test_human_interaction_choice_mode_result_fields(server: Any, spy: _SpyService) -> None:
    """选择模式：创建请求（options 归一化）+ 等待 + 完整结果字段。"""
    spy.wait_result = {
        "response_type": "approved", "selected_option": "批准", "answers": ["a"], "feedback": "同意",
    }
    result = await server.human_interaction(
        mode="choice", title="审批", options=["批准", "拒绝"], timeout_seconds=30, pipeline_id="p1",
    )
    assert result == {
        "status": "completed", "response_type": "approved", "selected_option": "批准",
        "answers": ["a"], "feedback": "同意",
    }
    assert spy.calls[0][0] == "choice"
    choice_kwargs = spy.calls[0][1]
    assert choice_kwargs["options"] == [{"id": "0", "label": "批准"}, {"id": "1", "label": "拒绝"}]
    assert choice_kwargs["timeout_seconds"] == 30
    assert choice_kwargs["pipeline_id"] == "p1"
    assert choice_kwargs["tab_id"] == "p1"


async def test_human_interaction_choice_minimal_response(server: Any, spy: _SpyService) -> None:
    """选择模式：最小响应（仅 response_type）→ 结果不含可选字段。"""
    spy.wait_result = {"response_type": "answered"}
    result = await server.human_interaction(mode="choice", title="确认", pipeline_id="p1")
    assert result == {"status": "completed", "response_type": "answered"}


async def test_human_interaction_conversation_approved(server: Any, spy: _SpyService) -> None:
    """对话模式：approved → user_arrived + conversation_mode=True。"""
    spy.wait_result = {"response_type": "approved"}
    result = await server.human_interaction(
        mode="conversation", title="讨论", initial_message="hi", suggestions=["ok"], pipeline_id="p1",
    )
    assert result == {
        "status": "user_arrived", "conversation_mode": True,
        "message": "用户已进入对话标签页，管道自动挂起等待新消息。",
    }
    assert spy.calls[0][0] == "conversation"
    conv_kwargs = spy.calls[0][1]
    assert conv_kwargs["initial_message"] == "hi"
    assert conv_kwargs["suggestions"] == ["ok"]


async def test_human_interaction_conversation_other_response(server: Any, spy: _SpyService) -> None:
    """对话模式：非 approved 响应 → completed + feedback（有则带）。"""
    spy.wait_result = {"response_type": "answered", "feedback": "稍后再说"}
    result = await server.human_interaction(mode="conversation", title="讨论", pipeline_id="p1")
    assert result == {"status": "completed", "response_type": "answered", "feedback": "稍后再说"}

    spy.wait_result = {"response_type": "answered"}
    result2 = await server.human_interaction(mode="conversation", title="讨论", pipeline_id="p1")
    assert result2 == {"status": "completed", "response_type": "answered"}


async def test_human_interaction_mode_alias_and_invalid(server: Any, spy: _SpyService) -> None:
    """参数别名（type→mode、message→title）与非法 mode 拒绝。"""
    result = await server.human_interaction(type="choice", message="审批", pipeline_id="p1")
    assert result["status"] == "completed"
    assert spy.calls[0][1]["title"] == "审批"

    assert await server.human_interaction(mode="blocking", title="T") == {
        "error": "参数 mode 必填，取值 choice/conversation/notification"
    }
    assert await server.human_interaction(title="T") == {
        "error": "参数 mode 必填，取值 choice/conversation/notification"
    }


async def test_human_interaction_exception_mapping(server: Any, spy: _SpyService) -> None:
    """异常映射：超时/取消/拒绝/未知异常 → 结构化错误返回。"""
    spy.wait_error = server.InteractionTimeoutError("rid", 30)
    assert await server.human_interaction(mode="choice", title="T", pipeline_id="p1") == {
        "error": "人类交互超时（30秒）", "error_code": "INTERACTION_TIMEOUT",
    }

    spy.wait_error = server.InteractionCancelledError("rid", "用户取消")
    assert await server.human_interaction(mode="choice", title="T", pipeline_id="p1") == {
        "error": "交互已取消: 用户取消", "error_code": "INTERACTION_CANCELLED",
    }

    spy.wait_error = server.InteractionDeniedError("rid", "理由不充分")
    assert await server.human_interaction(mode="choice", title="T", pipeline_id="p1") == {
        "status": "denied", "selected_option": "用户拒绝", "reason": "理由不充分",
    }

    spy.wait_error = RuntimeError("boom")
    result = await server.human_interaction(mode="choice", title="T", pipeline_id="p1")
    assert result["error"] == "人类交互执行失败: boom"


async def test_human_interaction_invalid_priority_falls_to_generic_error(server: Any, spy: _SpyService) -> None:
    """非法 priority：Priority() 构造抛 ValueError → 通用错误返回（fail-closed）。"""
    result = await server.human_interaction(mode="notification", title="T", priority="urgent")
    assert result["error"].startswith("人类交互执行失败:")


class _StubASyncIO:
    """假 asyncio 模块：sleep 可注入副作用（冷启动等待循环测试）。"""

    def __init__(self, on_sleep: Any = None) -> None:
        self._on_sleep = on_sleep

    async def sleep(self, delay: float) -> None:
        if self._on_sleep is not None:
            self._on_sleep()


async def test_human_interaction_service_never_initialized(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """冷启动自愈：10s 内 on_load 未完成 → 明确 error（不误判工具不可用）。"""
    monkeypatch.setattr(server, "asyncio", _StubASyncIO())
    result = await server.human_interaction(mode="choice", title="T")
    assert result == {"error": "service not initialized (on_load not finished in 10s)"}


async def test_human_interaction_waits_for_service_initialization(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷启动自愈：等待期间 on_load 完成 → 正常执行（等待循环 break 路径）。"""
    spy = _SpyService()

    def _init_on_sleep() -> None:
        server._service = spy

    monkeypatch.setattr(server, "asyncio", _StubASyncIO(_init_on_sleep))
    result = await server.human_interaction(mode="notification", title="T", pipeline_id="p1")
    assert result == {"status": "sent", "request_id": "rid-n"}


# ════════════════════════════════════════════════════════════
# interaction.* 服务工具（真实服务往返）
# ════════════════════════════════════════════════════════════


@pytest.fixture
async def real_service(server: Any) -> Any:
    """on_load 装配真实服务；用例结束取消后台超时任务并卸载。"""
    await server._on_load({})
    yield server._service
    if server._service is not None:
        for task in list(server._service._timeout_tasks.values()):
            task.cancel()
    await server._on_unload({})


async def test_interaction_send_notification_tool(server: Any, real_service: Any) -> None:
    """interaction.send_notification：真实服务创建通知并返回 request_id。"""
    result = await server.interaction_send_notification("s1", "t1", "标题", message="内容", priority="high")
    assert result["status"] == "sent"
    assert result["request_id"]
    record = await real_service.get_request(result["request_id"])
    assert record is not None and record["message_data"]["priority"] == "high"


async def test_interaction_create_choice_and_respond_roundtrip(server: Any, real_service: Any) -> None:
    """interaction.create_choice → interaction.respond → interaction.wait_for_choice 全真实往返。"""
    created = await server.interaction_create_choice(
        "s1", "t1", "tab1", "审批", options=[{"id": "1", "label": "批准"}], timeout_seconds=30,
    )
    assert created["status"] == "pending"
    rid = created["request_id"]

    responded = await server.interaction_respond(rid, "approved", selected_option="批准", feedback="同意")
    assert responded == {"ok": True, "request_id": rid, "status": "submitted"}

    waited = await server.interaction_wait_for_choice(rid, timeout=5)
    assert waited["response_type"] == "approved"
    assert waited["selected_option"] == "批准"
    assert waited["feedback"] == "同意"


async def test_interaction_respond_unknown_request(server: Any, real_service: Any) -> None:
    """interaction.respond：未知请求 → ok=False / not_found。"""
    result = await server.interaction_respond("no-such", "approved")
    assert result == {"ok": False, "request_id": "no-such", "status": "not_found"}


async def test_interaction_wait_for_choice_timeout_error(server: Any, real_service: Any) -> None:
    """interaction.wait_for_choice：超时 → 结构化 error 返回（不抛异常）。"""
    created = await server.interaction_create_choice("s1", "t1", "tab1", "审批", timeout_seconds=1)
    rid = created["request_id"]
    result = await server.interaction_wait_for_choice(rid, timeout=0.1)
    assert result["request_id"] == rid
    assert result["error"].startswith("交互超时:")
    record = await real_service.get_request(rid)
    assert record is not None and record["status"] == "timeout"


async def test_interaction_cancel_tool(server: Any, real_service: Any) -> None:
    """interaction.cancel：取消成功 / 未知请求 not_found。"""
    created = await server.interaction_create_choice("s1", "t1", "tab1", "审批", timeout_seconds=30)
    rid = created["request_id"]
    result = await server.interaction_cancel(rid, reason="user_abort")
    assert result == {"ok": True, "request_id": rid, "status": "cancelled"}
    assert (await real_service.get_request(rid))["status"] == "cancelled"

    result2 = await server.interaction_cancel("no-such")
    assert result2 == {"ok": False, "request_id": "no-such", "status": "not_found"}


async def test_interaction_get_pending_tool(server: Any, real_service: Any) -> None:
    """interaction.get_pending：真实服务待处理列表 + 计数。"""
    await server.interaction_create_choice("s1", "t1", "tab1", "A", timeout_seconds=30)
    await server.interaction_send_notification("s1", "t2", "B")
    await server.interaction_create_choice("s2", "t3", "tab3", "C", timeout_seconds=30)

    all_pending = await server.interaction_get_pending()
    assert all_pending["count"] == 3
    assert len(all_pending["requests"]) == 3

    s1_pending = await server.interaction_get_pending(session_id="s1")
    assert s1_pending["count"] == 2

    limited = await server.interaction_get_pending(limit=1)
    assert limited["count"] == 1


async def test_interaction_tools_service_not_initialized(server: Any) -> None:
    """interaction.* 工具：service 未初始化 → 明确 error。"""
    assert server._service is None
    assert await server.interaction_send_notification("s", "t", "T") == {"error": "service not initialized"}
    assert await server.interaction_create_choice("s", "t", "tab", "T") == {"error": "service not initialized"}
    assert await server.interaction_wait_for_choice("r") == {"error": "service not initialized"}
    assert await server.interaction_respond("r", "approved") == {"error": "service not initialized"}
    assert await server.interaction_cancel("r") == {"error": "service not initialized"}
    assert await server.interaction_get_pending() == {"error": "service not initialized"}
