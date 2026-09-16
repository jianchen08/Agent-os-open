# @feature: FP-0.2.二 FFmpeg 推流控制面 | @ci: python-coverage
"""stream_control 测试：命令构造纯函数 / 进程生命周期（伪 runner）/ 状态查询。

FFmpeg 真进程不在单测范围（真推流属 T3.3 外部依赖）；
时序与副作用经注入的 ProcessRunner 端口断言。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_PLUGIN_DIR))


def _load(filename: str, mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load("tool.py", "stream_control_tool")
StreamControlTool = _mod.StreamControlTool
build_ffmpeg_args = _mod.build_ffmpeg_args


def test_build_args_loop_playlist_with_ai_badge() -> None:
    """D2 底线：AI 角标必须烧录（drawtext 在 args 中，且文本含 AI 标识）。"""
    args = build_ffmpeg_args(
        inputs=["a.mp4", "b.mp4"],
        rtmp_url="rtmp://x/live/key",
        overlay_text="AI 生成内容 · 待机中",
        width=1280,
        height=720,
    )
    joined = " ".join(args)
    assert "-stream_loop" in joined
    assert "drawtext" in joined
    assert "AI 生成内容" in joined
    assert args[-1] == "rtmp://x/live/key"
    assert "-f" in joined and "flv" in joined


def test_build_args_without_overlay_has_no_drawtext() -> None:
    args = build_ffmpeg_args(
        inputs=["a.mp4"], rtmp_url="rtmp://x", overlay_text="", width=640, height=360
    )
    assert "drawtext" not in " ".join(args)


class FakeProc:
    def __init__(self, name: str) -> None:
        self.name = name
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self.procs: list[FakeProc] = []

    async def start(self, args: list[str]) -> Any:
        self.started.append(args)
        proc = FakeProc(f"p{len(self.started)}")
        self.procs.append(proc)
        return proc


def test_start_stop_roundtrip(tmp_path: Path) -> None:
    runner = FakeRunner()
    tool = StreamControlTool(runner=runner, rtmp_url="rtmp://x/live/k")
    result = asyncio.run(tool.execute({
        "action": "start",
        "inputs": [str(tmp_path / "a.mp4")],
        "overlay_text": "AI 生成内容",
    }))
    assert result.success, result.error
    assert len(runner.started) == 1
    status = asyncio.run(tool.execute({"action": "status"}))
    assert status.output["running"] is True
    stop = asyncio.run(tool.execute({"action": "stop"}))
    assert stop.success
    assert runner.procs[0].terminated
    status2 = asyncio.run(tool.execute({"action": "status"}))
    assert status2.output["running"] is False


def test_start_twice_rejected_until_stop(tmp_path: Path) -> None:
    """单推流面：已在推时再次 start → 显式拒绝（防双推流）。"""
    tool = StreamControlTool(runner=FakeRunner(), rtmp_url="rtmp://x")
    asyncio.run(tool.execute({"action": "start", "inputs": ["a.mp4"], "overlay_text": "AI"}))
    second = asyncio.run(tool.execute({"action": "start", "inputs": ["b.mp4"], "overlay_text": "AI"}))
    assert not second.success
    assert second.error_code == "ALREADY_STREAMING"


def test_rtmp_unset_is_explicit() -> None:
    tool = StreamControlTool(runner=FakeRunner(), rtmp_url=None)
    result = asyncio.run(tool.execute({"action": "start", "inputs": ["a.mp4"], "overlay_text": "AI"}))
    assert not result.success
    assert result.error_code == "RTMP_UNSET"


def test_stop_when_not_running_is_noop_success() -> None:
    tool = StreamControlTool(runner=FakeRunner(), rtmp_url="rtmp://x")
    result = asyncio.run(tool.execute({"action": "stop"}))
    assert result.success
    assert result.output["running"] is False


def test_bad_action_rejected() -> None:
    tool = StreamControlTool(runner=FakeRunner(), rtmp_url="rtmp://x")
    result = asyncio.run(tool.execute({"action": "dance"}))
    assert not result.success
    assert result.error_code == "BAD_ACTION"


def test_plugin_json_matches_tool() -> None:
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = manifest["capabilities"]["tools"][0]
    definition = StreamControlTool.get_tool_definition()
    assert declared["name"] == definition.name
    assert set(declared["input_schema"]["required"]) == set(definition.input_schema["required"])
