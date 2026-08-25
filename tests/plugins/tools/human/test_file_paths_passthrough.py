# @ci: python-coverage
"""human_interaction file_paths 接线测试（交互面板文件展示链路）。

意图（WHY）：工具 schema 声明 file_paths 且描述承诺"系统会自动读取文件内容
并在交互面板中展示"——链路为 handler → service.create_*_request(file_paths=)
→ 事件 payload file_paths → 前端 useInteractionHandler 拉内容建工作区 Tab。
handler 必须把 kwargs["file_paths"] 传给 service，且利用 param_inject 注入的
workspace/project_root（宿主绝对路径）把 agent 视角路径（容器挂载路径
/workspace/*、工作区相对路径）翻译成宿主绝对路径——前端按绝对路径直读宿主
文件系统，原样回传容器路径将读不到文件（工作区不显示）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "human"


def _load_server():
    mod_name = "human_server_file_paths_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load human/server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _SpyService:
    """记录 create_*_request 入参的假服务——不依赖真实事件总线。"""

    def __init__(self) -> None:
        self.choice_calls: list[dict[str, Any]] = []
        self.conversation_calls: list[dict[str, Any]] = []
        self.notification_calls: list[dict[str, Any]] = []

    async def create_choice_request(self, **kwargs: Any) -> str:
        self.choice_calls.append(kwargs)
        return "rid-1"

    async def wait_for_choice(self, rid: str, timeout: int | None = None) -> dict[str, Any]:
        return {"response_type": "answered", "selected_option": "ok"}

    async def create_conversation_request(self, **kwargs: Any) -> str:
        self.conversation_calls.append(kwargs)
        return "rid-2"

    async def send_notification(self, **kwargs: Any) -> str:
        self.notification_calls.append(kwargs)
        return "rid-3"


@pytest.fixture()
def server_with_spy(monkeypatch: pytest.MonkeyPatch):
    server = _load_server()
    spy = _SpyService()
    monkeypatch.setattr(server, "_service", spy)
    return server, spy


class TestResolvedFilePaths:
    def test_container_mount_path_remapped_to_host_root(self, tmp_path: Path) -> None:
        server = _load_server()
        out = server._resolved_file_paths(
            {"file_paths": ["/workspace/a.md"], "workspace": str(tmp_path)}
        )
        assert len(out) == 1
        assert Path(out[0]) == tmp_path / "a.md"

    def test_relative_path_anchored_to_root(self, tmp_path: Path) -> None:
        server = _load_server()
        out = server._resolved_file_paths(
            {"file_paths": ["docs/b.md"], "project_root": str(tmp_path)}
        )
        assert len(out) == 1
        assert Path(out[0]) == tmp_path / "docs" / "b.md"

    def test_absolute_host_path_passthrough(self, tmp_path: Path) -> None:
        server = _load_server()
        host = str(tmp_path / "c.md")
        out = server._resolved_file_paths({"file_paths": [host], "workspace": str(tmp_path)})
        assert out == [host]

    def test_without_injection_passthrough_untouched(self) -> None:
        server = _load_server()
        out = server._resolved_file_paths({"file_paths": ["README.md"]})
        assert out == ["README.md"]

    def test_missing_or_invalid_returns_none(self) -> None:
        server = _load_server()
        assert server._resolved_file_paths({}) is None
        assert server._resolved_file_paths({"file_paths": []}) is None
        assert server._resolved_file_paths({"file_paths": "not-a-list"}) is None
        assert server._resolved_file_paths({"file_paths": [42, None]}) is None


class TestHandlerWiring:
    async def test_choice_mode_passes_resolved_file_paths(
        self, server_with_spy, tmp_path: Path
    ) -> None:
        """choice 模式：file_paths 翻译为宿主绝对路径后传入 service。"""
        server, spy = server_with_spy
        (tmp_path / "plan.md").write_text("# plan\n", encoding="utf-8")

        await server.human_interaction(
            mode="choice",
            title="请审批",
            options=["批准", "拒绝"],
            file_paths=["plan.md", "/workspace/plan.md"],
            workspace=str(tmp_path),
            pipeline_id="pipe-1",
        )

        assert len(spy.choice_calls) == 1
        assert spy.choice_calls[0]["file_paths"] == [str(tmp_path / "plan.md")] * 2

    async def test_conversation_mode_passes_resolved_file_paths(
        self, server_with_spy, tmp_path: Path
    ) -> None:
        """conversation 模式：同 choice 的翻译与接线。"""
        server, spy = server_with_spy
        (tmp_path / "spec.md").write_text("# spec\n", encoding="utf-8")

        await server.human_interaction(
            mode="conversation",
            title="讨论方案",
            file_paths=["spec.md"],
            workspace=str(tmp_path),
            pipeline_id="pipe-2",
        )

        assert len(spy.conversation_calls) == 1
        assert spy.conversation_calls[0]["file_paths"] == [str(tmp_path / "spec.md")]

    async def test_notification_mode_does_not_carry_file_paths(self, server_with_spy) -> None:
        """notification 模式：schema 声明不支持 file_paths，不向下传。"""
        server, spy = server_with_spy

        await server.human_interaction(
            mode="notification",
            title="进度",
            file_paths=["x.md"],
            workspace=r"D:\ws",
        )

        assert len(spy.notification_calls) == 1
        assert spy.notification_calls[0].get("file_paths") is None

    async def test_choice_mode_without_file_paths_stays_none(
        self, server_with_spy
    ) -> None:
        """未传 file_paths：service 收到 None（缺省语义不变）。"""
        server, spy = server_with_spy

        await server.human_interaction(
            mode="choice", title="确认", options=["ok"], pipeline_id="pipe-3",
        )

        assert spy.choice_calls[0]["file_paths"] is None
