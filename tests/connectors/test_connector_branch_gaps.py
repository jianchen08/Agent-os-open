# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""连接器簇未覆盖分支补测（VSCode 连接器 / 基类 / 降级 / 配置混入）。

按 HEAD 实测缺行钉行为契约：

channel.py（is_available 真实探测）
- 健康端点 200 → True；非 200 → False；连接失败（URLError）→ False 不抛。
  出网面经 urllib 替身注入（本地回环不建真实扩展）。

connector.py
- execute_action 的非 ConnectionError 异常（如通道协议错）→ 失败结果且
  错误文案带原因（区别于 ConnectionError 分支的状态置 ERROR）；
- _on_config_changed 回调经 ConfigCenter 订阅链触发（VSCode 自己的实现）。

base.py
- on_state_update 默认实现为纯日志钩子（不抛、不改状态）；
- get_info 默认实现从 connector_type 派生展示名与零能力（子类未重写时）。

degradation.py
- _fallback_open_file 读取异常兜底：目录（IsADirectoryError）与非法
  UTF-8 二进制文件（UnicodeDecodeError）均返回失败结果，不向上抛。

config_mixin.py
- 基类 _on_config_changed 默认实现（子类未重写）：经 _create_callback
  注册的订阅回调原样转交事件三参；
- 适配器名解析的 connector_type / channel_type / 类名三级回退。

外部依赖（网络、文件系统权限）按车道约定替身/真实文件驱动；同行为均给
≥2 组有区分度输入。
"""

from __future__ import annotations

import contextlib
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from base import BaseConnector
from config_mixin import ConfigSubscriberMixin
from connector_types import ActionResult, ConnectorAction, ConnectorState
from degradation import DegradationManager
from vscode.channel import VSCodeChannel
from vscode.connector import VSCodeConnector

pytestmark = pytest.mark.unit


class _FakeResponse(contextlib.AbstractContextManager):
    """urlopen 替身响应：仅暴露 is_available 读取的 status。"""

    def __init__(self, status: int) -> None:
        self.status = status

    def __exit__(self, *exc: Any) -> None:
        return None


class _MinimalConnector(BaseConnector):
    """最小具体连接器：不重写 get_info / on_state_update（钉基类默认实现）。"""

    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0

    @property
    def connector_type(self) -> str:
        return "minimal"

    async def connect(self) -> None:
        self.connect_calls += 1
        self._set_state(ConnectorState.CONNECTED)

    async def disconnect(self) -> None:
        self._set_state(ConnectorState.DISCONNECTED)

    async def get_context(self) -> Any:
        raise NotImplementedError

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        raise NotImplementedError


# ══════════════════ channel.is_available ══════════════════


class TestChannelAvailability:
    def test_healthy_status_200_is_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /health 返回 200 → True（真实探测而非假定）。"""
        channel = VSCodeChannel(host="127.0.0.1", port=9741, timeout=1.0)
        seen: list[Any] = []

        def _urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
            seen.append((req.full_url, req.get_method(), timeout))
            return _FakeResponse(200)

        monkeypatch.setattr("vscode.channel.urllib.request.urlopen", _urlopen)

        assert channel.is_available() is True
        assert seen == [("http://127.0.0.1:9741/health", "GET", 1.0)]

    @pytest.mark.parametrize("status", [204, 404, 500])
    def test_non_200_status_unavailable(
        self, status: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 200 状态一律不可用（三组有区分度状态码）。"""
        channel = VSCodeChannel(host="localhost", port=9999, timeout=2.0)
        monkeypatch.setattr(
            "vscode.channel.urllib.request.urlopen",
            lambda *_a, **_k: _FakeResponse(status),
        )

        assert channel.is_available() is False

    def test_connection_failure_is_false_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """连接级失败（URLError）→ False，异常不外抛（健康探测语义）。"""

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise urllib.error.URLError("connection refused")

        channel = VSCodeChannel(host="localhost", port=1, timeout=1.0)
        monkeypatch.setattr("vscode.channel.urllib.request.urlopen", _boom)

        assert channel.is_available() is False


# ══════════════════ connector.execute_action ══════════════════


class TestExecuteActionFailures:
    async def test_non_connection_error_wrapped_without_state_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """通道抛非连接类异常（协议错）→ 失败结果带原因，状态保持 CONNECTED。"""
        connector = VSCodeConnector(host="localhost", port=9999, timeout=1.0)
        connector._set_state(ConnectorState.CONNECTED)

        async def _boom(_endpoint: str, _data: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("malformed channel payload")

        monkeypatch.setattr(connector.channel, "send_request", _boom)

        result = await connector.execute_action(
            ConnectorAction(action_type="open_file", parameters={"file_path": "a.py"})
        )

        assert result.success is False
        assert "malformed channel payload" in (result.error or "")
        assert connector.state == ConnectorState.CONNECTED

    async def test_connection_error_sets_error_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：ConnectionError 分支置 ERROR 状态（与上例的保持态区分）。"""
        connector = VSCodeConnector(host="localhost", port=9999, timeout=1.0)
        connector._set_state(ConnectorState.CONNECTED)

        async def _boom(_endpoint: str, _data: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError("extension offline")

        monkeypatch.setattr(connector.channel, "send_request", _boom)

        result = await connector.execute_action(
            ConnectorAction(action_type="open_file", parameters={})
        )

        assert result.success is False
        assert "extension offline" in (result.error or "")
        assert connector.state == ConnectorState.ERROR


class TestVSCodeConfigCallback:
    def test_on_config_changed_logs_without_mutation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """VSCode 的 _on_config_changed 为留痕回调：不抛、不改连接器状态。"""
        connector = VSCodeConnector(host="localhost", port=1, timeout=1.0)
        connector._set_state(ConnectorState.CONNECTED)

        with caplog.at_level("INFO"):
            connector._on_config_changed("modified", "/config/x.yaml", {"config_type": "t"})

        assert connector.state == ConnectorState.CONNECTED, "配置回调不得改动连接状态"
        assert any(
            "VSCode 配置变更" in rec.getMessage() and "/config/x.yaml" in rec.getMessage()
            for rec in caplog.records
        )


# ══════════════════ base 默认实现 ══════════════════


class TestBaseDefaults:
    async def test_on_state_update_is_logging_hook(self) -> None:
        """on_state_update 默认实现：可 await、不抛、状态归 _set_state 管。"""
        connector = _MinimalConnector()
        before = connector.state

        await connector.on_state_update(ConnectorState.ACTIVE)

        assert connector.state == before, "状态通知钩子自身不改状态"

    def test_default_get_info_derives_from_type(self) -> None:
        """get_info 默认实现：展示名=连接器类型、零能力、优先级 0。"""
        info = _MinimalConnector().get_info()

        assert info.connector_type == "minimal"
        assert info.display_name == "minimal"
        assert info.capabilities == []
        assert info.priority == 0

    def test_get_status_shape_from_defaults(self) -> None:
        """get_status 透传默认 info 字段（性质：结构键与取值同源）。"""
        status = _MinimalConnector().get_status()

        assert status["type"] == "minimal"
        assert status["state"] == ConnectorState.DISCONNECTED.value
        assert status["connected"] is False
        assert status["info"] == {
            "display_name": "minimal",
            "capabilities": [],
            "priority": 0,
        }


# ══════════════════ degradation._fallback_open_file 异常面 ══════════════════


class TestFallbackOpenFileErrors:
    def test_missing_file_failure(self, tmp_path: Path) -> None:
        """参数校验面：file_path 缺失 / 文件不存在（两组输入）。"""
        manager = DegradationManager()

        empty = manager.execute_with_fallback("open_file", {})
        absent = manager.execute_with_fallback("open_file", {"file_path": str(tmp_path / "no")})

        assert empty.success is False
        assert "缺少 file_path" in (empty.error or "")
        assert absent.success is False
        assert "文件不存在" in (absent.error or "")

    def test_directory_read_failure_wrapped(self, tmp_path: Path) -> None:
        """目录路径存在但不可作文本读（IsADirectoryError）→ 失败结果带原因。"""
        target = tmp_path / "adir"
        target.mkdir()

        result = DegradationManager().execute_with_fallback(
            "open_file", {"file_path": str(target)}
        )

        assert result.success is False
        assert "读取文件失败" in (result.error or "")

    def test_invalid_utf8_content_failure_wrapped(self, tmp_path: Path) -> None:
        """非法 UTF-8 内容（UnicodeDecodeError）→ 同款失败结果，不向上抛。"""
        blob = tmp_path / "bad.bin"
        blob.write_bytes(b"\xff\xfe\x00invalid utf-8 \x80")

        result = DegradationManager().execute_with_fallback(
            "open_file", {"file_path": str(blob)}
        )

        assert result.success is False
        assert "读取文件失败" in (result.error or "")

    def test_valid_text_file_succeeds(self, tmp_path: Path) -> None:
        """性质对照：合法 UTF-8 文件走成功分支（degraded 标记 + 原文回传）。"""
        doc = tmp_path / "ok.txt"
        doc.write_text("hello 世界\n", encoding="utf-8")

        result = DegradationManager().execute_with_fallback("open_file", {"file_path": str(doc)})

        assert result.success is True
        assert result.data["degraded"] is True
        assert result.data["content"] == "hello 世界\n"


# ══════════════════ config_mixin 基类回调 ══════════════════


class _RecordingCenter:
    """ConfigCenter 替身（外部依赖：配置中心），记录 watch/unwatch 与回调。"""

    def __init__(self) -> None:
        self.watches: dict[str, list[Any]] = {}

    def watch(self, path_prefix: str, callback: Any) -> None:
        self.watches.setdefault(path_prefix, []).append(callback)

    def unwatch(self, path_prefix: str, callback: Any) -> bool:
        callbacks = self.watches.get(path_prefix, [])
        if callback in callbacks:
            callbacks.remove(callback)
            return True
        return False

    def fire(self, path_prefix: str, event: str, file_path: str) -> None:
        for cb in list(self.watches.get(path_prefix, [])):
            cb(event, file_path, {"config_type": "test"})


class _PlainSubscriber(ConfigSubscriberMixin):
    """未重写 _on_config_changed 的订阅者（钉基类默认实现）。"""

    def __init__(self, kind: str | None = None) -> None:
        if kind == "connector":
            self.connector_type = "plain_connector"
        elif kind == "channel":
            self.channel_type = "plain_channel"
        self.connector_type = getattr(self, "connector_type", "") or None  # type: ignore[assignment]
        self.channel_type = getattr(self, "channel_type", None)


class TestConfigMixinBaseCallback:
    def test_base_callback_receives_triple(self, caplog: pytest.LogCaptureFixture) -> None:
        """基类 _on_config_changed 经 _create_callback 收到 (event, path, context) 三参。"""
        center = _RecordingCenter()
        sub = _PlainSubscriber()
        sub.subscribe_config(center, "config/a.yaml")

        with caplog.at_level("INFO", logger="config_mixin"):
            center.fire("config/a.yaml", "created", "C:/cfg/a.yaml")

        assert any(
            "配置变更通知" in rec.message and "created" in str(rec.args)
            for rec in caplog.records
        ), "基类默认实现必须留痕事件与路径"

    @pytest.mark.parametrize("kind", ["connector", "channel", None])
    def test_adapter_name_fallbacks(self, kind: str | None) -> None:
        """适配器名三级回退：connector_type > channel_type > 类名（三种输入）。"""
        sub = _PlainSubscriber(kind)
        name = (
            getattr(sub, "connector_type", None)
            or getattr(sub, "channel_type", None)
            or sub.__class__.__name__
        )

        assert name == {
            "connector": "plain_connector",
            "channel": "plain_channel",
            None: "_PlainSubscriber",
        }[kind]

    def test_resubscribe_then_unsubscribe_roundtrip(self) -> None:
        """重复订阅替换旧回调；unsubscribe 后旧 prefix 不再收到事件。"""
        center = _RecordingCenter()
        sub = _PlainSubscriber("channel")
        sub.subscribe_config(center, "first.yaml")
        sub.subscribe_config(center, "second.yaml")

        assert center.watches.get("first.yaml") == []
        assert len(center.watches["second.yaml"]) == 1

        sub.unsubscribe_config()
        center.fire("second.yaml", "modified", "C:/cfg/second.yaml")

        assert center.watches["second.yaml"] == [], "取消订阅后回调必须摘除"
