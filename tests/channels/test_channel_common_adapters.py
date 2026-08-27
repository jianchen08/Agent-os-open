# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_common 渠道共享包测试（A5.2 补）。

覆盖 base_combo_adapter / input_adapter / output_adapter 三文件的
默认实现与抽象契约。模块经 importlib 显式加载（唯一模块名），
不依赖 use_channel 的 sys.path 副作用。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_CC_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "channel_common"
_FILES = {
    "base_combo": "base_combo_adapter.py",
    "input": "input_adapter.py",
    "output": "output_adapter.py",
}
_EVICT = {"base_combo_adapter", "input_adapter", "output_adapter"}


@pytest.fixture
def load_common() -> Iterator[dict[str, Any]]:
    """加载 channel_common 三模块，teardown 恢复被逐出的同名模块。"""
    saved_modules = {m: sys.modules[m] for m in _EVICT if m in sys.modules}
    loaded: dict[str, Any] = {}
    try:
        for m in _EVICT:
            sys.modules.pop(m, None)
        for name, filename in _FILES.items():
            mod_name = f"channel_common_{name}_test"
            sys.modules.pop(mod_name, None)
            spec = importlib.util.spec_from_file_location(mod_name, _CC_DIR / filename)
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        yield loaded
    finally:
        for name in _FILES:
            sys.modules.pop(f"channel_common_{name}_test", None)
        for name, mod in saved_modules.items():
            sys.modules[name] = mod


class TestBaseComboAdapter:
    """BaseComboAdapter 基于 stream_client 的状态查询默认实现。"""

    @staticmethod
    def _make(load_common: dict[str, Any], connected: bool) -> Any:
        base = load_common["base_combo"]

        def _init(self: Any) -> None:
            self.stream_client = SimpleNamespace(is_connected=connected)

        dummy_cls = type(
            "Dummy",
            (base.BaseComboAdapter,),
            {"channel_type": "dummy", "__init__": _init},
        )
        return dummy_cls()

    def test_is_connected_delegates_to_stream_client(
        self, load_common: dict[str, Any]
    ) -> None:
        assert self._make(load_common, True).is_connected is True
        assert self._make(load_common, False).is_connected is False

    @pytest.mark.asyncio
    async def test_health_check_reflects_stream_client(
        self, load_common: dict[str, Any]
    ) -> None:
        assert await self._make(load_common, True).health_check() is True
        assert await self._make(load_common, False).health_check() is False

    def test_get_status_shape(self, load_common: dict[str, Any]) -> None:
        connected = self._make(load_common, True).get_status()
        assert connected == {"type": "dummy", "connected": True, "healthy": True}
        disconnected = self._make(load_common, False).get_status()
        assert disconnected["type"] == "dummy"
        assert disconnected["connected"] is False
        # 性质断言：healthy 与 connected 同源
        assert disconnected["healthy"] == disconnected["connected"]


class TestIInputAdapter:
    """IInputAdapter 抽象契约与默认实现。"""

    @staticmethod
    def _make(load_common: dict[str, Any], name: str) -> Any:
        mod = load_common["input"]

        async def _receive(self: Any) -> dict[str, Any]:
            return {"user_input": "x"}

        return type(name, (mod.IInputAdapter,), {"receive": _receive})()

    def test_abstract_cannot_instantiate(self, load_common: dict[str, Any]) -> None:
        with pytest.raises(TypeError):
            load_common["input"].IInputAdapter()

    @pytest.mark.asyncio
    async def test_default_health_check_and_connected(
        self, load_common: dict[str, Any]
    ) -> None:
        adapter = self._make(load_common, "AlphaInput")
        assert await adapter.health_check() is True
        assert adapter.is_connected is True

    def test_get_status_uses_class_name(self, load_common: dict[str, Any]) -> None:
        alpha = self._make(load_common, "AlphaOutput").get_status()
        assert alpha == {"type": "AlphaOutput", "connected": True, "healthy": True}
        beta = self._make(load_common, "BetaOutput").get_status()
        assert beta["type"] == "BetaOutput"
        assert beta["connected"] is True


# ═══════════════════════════════════════════════════════════
# BufferedChannelOutputAdapter：四渠道共用的发送骨架
# ═══════════════════════════════════════════════════════════

# 参数化渠道差异对照：
# - plain  镜像 dingtalk/feishu/wecom（目标恒等，纯文本投递）；
# - numeric 镜像 qq（整数校验目标 + 实例级消息类型回退）。
# 两组输入走同一骨架断言，防公共实现向单一渠道形态漂移。


class TestBufferedChannelOutputAdapter:
    """send 错误/结果直发 + send_stream 累积缓冲的共用契约。"""

    @staticmethod
    def _make(load_common: dict[str, Any], variant: str) -> tuple[Any, list[dict[str, Any]]]:
        mod = load_common["output"]
        calls: list[dict[str, Any]] = []

        class PlainAdapter(mod.BufferedChannelOutputAdapter):
            """镜像 dingtalk/feishu/wecom：恒等目标，纯文本投递。"""

            channel_name = "plain"

            async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
                calls.append({"target": target, "text": text, "msg_type": "<none>"})

        class NumericAdapter(mod.BufferedChannelOutputAdapter):
            """镜像 qq：整数目标校验 + 消息类型 state 优先/实例回退。"""

            channel_name = "numeric"

            def __init__(self) -> None:
                super().__init__()
                self.message_type = "private"

            def set_message_type(self, message_type: str) -> None:
                self.message_type = message_type

            def _resolve_target(self, raw_user_id: str) -> int | None:
                try:
                    return int(raw_user_id)
                except (ValueError, TypeError):
                    return None

            async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
                calls.append(
                    {
                        "target": target,
                        "text": text,
                        "msg_type": state.get("_message_type", self.message_type),
                    }
                )

        cls = PlainAdapter if variant == "plain" else NumericAdapter
        return cls(), calls

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_send_error_takes_precedence_over_result(
        self, load_common: dict[str, Any], variant: str
    ) -> None:
        adapter, calls = self._make(load_common, variant)
        await adapter.send({"raw_result": "ok", "raw_error": "boom", "_channel_user_id": "7"})
        assert len(calls) == 1
        # 性质断言：错误文案 = 前缀 + 原始错误全文，正常结果不再投递
        assert calls[0]["text"].startswith("❌ 错误: ")
        assert calls[0]["text"].endswith("boom")

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_send_result_str_coerced_and_falsy_skipped(
        self, load_common: dict[str, Any], variant: str
    ) -> None:
        adapter, calls = self._make(load_common, variant)
        await adapter.send({"raw_result": 12345, "_channel_user_id": "7"})
        assert len(calls) == 1
        # 非字符串结果经 str 化后原样投递（性质：无内容增删）
        assert str(12345) == calls[0]["text"]

        empty_cases = [("",), (None,)]
        for (empty,) in empty_cases:
            await adapter.send({"raw_result": empty, "_channel_user_id": "7"})
        assert len(calls) == 1  # 空结果不触发投递

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_send_state_target_wins_over_instance_fallback(
        self, load_common: dict[str, Any], variant: str
    ) -> None:
        adapter, calls = self._make(load_common, variant)
        await adapter.send({"raw_result": "x", "_channel_user_id": "111"})
        assert len(calls) == 1
        assert calls[0]["target"] != ""

        fallback, calls2 = self._make(load_common, variant)
        fallback.set_channel_user_id("222")
        expected_target: Any = 222 if variant == "numeric" else "222"
        await fallback.send({"raw_result": "x"})
        assert len(calls2) == 1
        assert calls2[0]["target"] == expected_target

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_send_without_any_target_warns_and_skips(
        self,
        load_common: dict[str, Any],
        variant: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter, calls = self._make(load_common, variant)
        with caplog.at_level(logging.WARNING):
            await adapter.send({"raw_result": "x"})
        assert calls == []
        assert any("No user_id for" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_numeric_variant_rejects_non_integer_target(
        self, load_common: dict[str, Any]
    ) -> None:
        """渠道特有差异点（QQ 数字校验）：非数字目标跳过且不投递。"""
        adapter, calls = self._make(load_common, "numeric")
        await adapter.send({"raw_result": "x", "_channel_user_id": "not-a-number"})
        assert calls == []
        await adapter.send({"raw_result": "x", "_channel_user_id": "55"})
        assert calls == [{"target": 55, "text": "x", "msg_type": "private"}]

    @pytest.mark.asyncio
    async def test_numeric_stream_message_type_falls_back_to_instance(
        self, load_common: dict[str, Any]
    ) -> None:
        """流路径无管道 state，消息类型回退实例属性。"""
        adapter, calls = self._make(load_common, "numeric")
        adapter.set_channel_user_id("321")
        if hasattr(adapter, "set_message_type"):
            adapter.set_message_type("group")
        await adapter.send_stream({"text": "第一"})
        await adapter.send_stream({"text": "段", "type": "end"})
        assert len(calls) == 1
        assert calls[0]["target"] == 321
        assert calls[0]["text"] == "第一段"
        assert calls[0]["msg_type"] == "group"

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_send_stream_accumulates_until_end_then_clears(
        self, load_common: dict[str, Any], variant: str
    ) -> None:
        adapter, calls = self._make(load_common, variant)
        target = "9" if variant == "numeric" else "user-9"
        adapter.set_channel_user_id(target)

        await adapter.send_stream({"text": "Hello "})
        await adapter.send_stream({"text": "World"})
        assert calls == []  # 未标记 flush/end 前不投递

        await adapter.send_stream({"text": "", "flush": True})
        delivered = [c for c in calls if c["text"]]
        assert len(delivered) == 1
        # 性质断言：投递内容 = 各 chunk 文本的顺序拼接
        assert delivered[0]["text"] == "Hello World"

        # 缓冲已清空：再次 flush 不重复投递（幂等）
        await adapter.send_stream({"text": "", "flush": True})
        assert len([c for c in calls if c["text"]]) == 1

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_send_stream_retains_buffer_when_no_target_or_empty(
        self, load_common: dict[str, Any], variant: str
    ) -> None:
        no_target, calls_a = self._make(load_common, variant)
        await no_target.send_stream({"text": "orphan", "flush": True})
        assert calls_a == []
        # 无目标也不丢缓冲语义由渠道各自路由保证；此处断言公共层不误投递即可

        has_target, calls_b = self._make(load_common, variant)
        await has_target.send_stream({"text": ""})
        has_target.set_channel_user_id("1" if variant == "numeric" else "u1")
        await has_target.send_stream({"flush": True})  # 无累积文本 → 不投递
        assert calls_b == []

    @pytest.mark.parametrize("variant", ["plain", "numeric"])
    @pytest.mark.asyncio
    async def test_deliver_failure_propagates_and_buffer_survives(
        self, load_common: dict[str, Any], variant: str
    ) -> None:
        """D-1 失败上抛契约在公共骨架中保持：异常穿透且累积文本可重试。"""
        mod = load_common["output"]
        attempts: list[str] = []
        failed_once = False

        class FlakyAdapter(mod.BufferedChannelOutputAdapter):
            channel_name = "flaky"

            async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise RuntimeError("channel api down")
                attempts.append(text)

        adapter = FlakyAdapter()
        adapter.set_channel_user_id("u1")
        await adapter.send_stream({"text": "完整消息"})  # 仅累积，未投递

        # 首次投递失败：异常穿透给调用方（D-1 契约，公共骨架不吞）
        with pytest.raises(RuntimeError, match="channel api down"):
            await adapter.send({"raw_result": "hello", "_channel_user_id": "u1"})

        # 重试同一 flush：完整累积文本仍在，可再次投递
        await adapter.send_stream({"flush": True})
        assert attempts == ["完整消息"]


class TestQueuedChannelInputAdapter:
    """队列缓冲机制 + build_channel_state 信封契约。"""

    @staticmethod
    def _make(load_common: dict[str, Any], parser: str) -> Any:
        import_adapter = load_common["input"]

        if parser == "flat":
            # 镜像 dingtalk：平铺字段命名
            class FlatInput(import_adapter.QueuedChannelInputAdapter):
                @staticmethod
                def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
                    return import_adapter.build_channel_state(
                        channel_type="flat",
                        user_input=raw.get("content", ""),
                        session_id=raw.get("mid", ""),
                        channel_user_id=raw.get("uid", ""),
                        raw_message=raw,
                        _sender_id=raw.get("uid", ""),
                    )

            return FlatInput()

        # mirror nested：嵌套字段命名
        class NestedInput(import_adapter.QueuedChannelInputAdapter):
            @staticmethod
            def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
                payload = raw.get("payload", {})
                return import_adapter.build_channel_state(
                    channel_type="nested",
                    user_input=payload.get("text", ""),
                    session_id=raw.get("event_id", ""),
                    channel_user_id=payload.get("open_id", ""),
                    raw_message=raw,
                )

        return NestedInput()

    def test_abstract_cannot_instantiate(self, load_common: dict[str, Any]) -> None:
        with pytest.raises(TypeError):
            load_common["input"].QueuedChannelInputAdapter()

    @pytest.mark.parametrize("parser", ["flat", "nested"])
    @pytest.mark.asyncio
    async def test_enqueue_receive_roundtrip_builds_envelope(
        self, load_common: dict[str, Any], parser: str
    ) -> None:
        adapter = self._make(load_common, parser)
        raw = (
            {"content": "hi", "mid": "m-1", "uid": "u-1"}
            if parser == "flat"
            else {"event_id": "e-1", "payload": {"text": "hi", "open_id": "ou-1"}}
        )
        await adapter.enqueue_message(raw)
        state = await adapter.receive()

        expected_user = "u-1" if parser == "flat" else "ou-1"
        assert state["user_input"] == "hi"
        assert state["_channel_type"] == parser
        assert state["_channel_user_id"] == expected_user
        # 公共信封不变量（渠道无关）
        assert state["core_type"] == "llm_call"
        assert state["iteration"] == 1
        assert state["should_stop"] is False
        assert state["_raw_message"] is raw  # 原始报文透传不拷贝

    @pytest.mark.asyncio
    async def test_receive_preserves_fifo_order(self, load_common: dict[str, Any]) -> None:
        adapter = self._make(load_common, "flat")
        for i in range(3):
            await adapter.enqueue_message({"content": f"msg-{i}", "mid": f"m-{i}", "uid": "u"})
        received = [await adapter.receive() for _ in range(3)]
        # 时序不变量：先入先出
        assert [s["session_id"] for s in received] == ["m-0", "m-1", "m-2"]
        assert [s["user_input"] for s in received] == ["msg-0", "msg-1", "msg-2"]


class TestBuildChannelState:
    """build_channel_state 公共信封构造器。"""

    def test_minimal_envelope_contains_all_common_keys(self, load_common: dict[str, Any]) -> None:
        raw = {"k": "v"}
        state = load_common["input"].build_channel_state(
            channel_type="ct",
            user_input="hello",
            session_id="s-1",
            channel_user_id="cu-1",
            raw_message=raw,
        )
        assert state["user_input"] == "hello"
        assert state["core_type"] == "llm_call"
        assert state["session_id"] == "s-1"
        assert state["should_stop"] is False
        assert state["iteration"] == 1
        assert state["_channel_type"] == "ct"
        assert state["_channel_user_id"] == "cu-1"
        assert state["_raw_message"] == {"k": "v"}

    def test_extra_keys_merge_without_dropping_common_keys(
        self, load_common: dict[str, Any]
    ) -> None:
        builder = load_common["input"].build_channel_state
        a = builder(
            channel_type="x",
            user_input="i",
            session_id="s",
            channel_user_id="c",
            raw_message={},
            _agent_id="a-1",
            _to_user="t-1",
        )
        b = builder(
            channel_type="y",
            user_input="j",
            session_id="s2",
            channel_user_id="c2",
            raw_message={},
            _message_type="group",
            _group_id=42,
        )
        # 性质断言：任意渠道附加键都挂在公共信封之上且互不影响
        for extra_key in ("_agent_id", "_to_user"):
            assert extra_key in a
        for extra_key in ("_message_type", "_group_id"):
            assert extra_key in b
        for st in (a, b):
            assert st["core_type"] == "llm_call"
            channel_keys = {k for k in st if k.startswith("_channel")}
            assert channel_keys >= {"_channel_type", "_channel_user_id"}


class TestUnsupportedMessageText:
    """非文本/未识别报文的统一拒收标记契约（scan 辖区四 S2）。

    渠道解析器对未支持消息类型不得把原始报文转储伪装成 user_input，
    必须返回带渠道与消息类型的显式标记。
    """

    @pytest.mark.parametrize(
        ("channel", "msg_type"),
        [("dingtalk", "picture"), ("feishu", "image"), ("wecom", "file"), ("qq", "record")],
    )
    def test_marker_contains_channel_and_type(
        self, load_common: dict[str, Any], channel: str, msg_type: str
    ) -> None:
        fn = load_common["input"].unsupported_message_text
        assert fn(channel, msg_type) == f"[不支持的消息类型: {channel}/{msg_type}]"

    def test_marker_never_equals_payload_repr(self, load_common: dict[str, Any]) -> None:
        """性质断言：任何原始报文 repr 都不得作为返回值形态。"""
        fn = load_common["input"].unsupported_message_text
        raw_repr = str({"downloadCode": "xyz"})
        out = fn("dingtalk", "picture")
        assert out != raw_repr
        assert "{" not in out
        assert "不支持" in out


class TestIOutputAdapter:
    """IOutputAdapter 抽象契约与默认实现。"""

    @staticmethod
    def _make(load_common: dict[str, Any], name: str) -> Any:
        mod = load_common["output"]

        async def _send(self: Any, state: dict[str, Any]) -> None:
            return None

        async def _send_stream(self: Any, chunk: dict[str, Any]) -> None:
            return None

        return type(
            name,
            (mod.IOutputAdapter,),
            {"send": _send, "send_stream": _send_stream},
        )()

    def test_abstract_cannot_instantiate(self, load_common: dict[str, Any]) -> None:
        with pytest.raises(TypeError):
            load_common["output"].IOutputAdapter()

    @pytest.mark.asyncio
    async def test_default_health_check_and_connected(
        self, load_common: dict[str, Any]
    ) -> None:
        adapter = self._make(load_common, "AlphaOutput")
        assert await adapter.health_check() is True
        assert adapter.is_connected is True

    def test_get_status_uses_class_name(self, load_common: dict[str, Any]) -> None:
        alpha = self._make(load_common, "AlphaOutput").get_status()
        assert alpha == {"type": "AlphaOutput", "connected": True, "healthy": True}
        beta = self._make(load_common, "BetaOutput").get_status()
        assert beta["type"] == "BetaOutput"
