#!/usr/bin/env python3
"""director 服务面——直播编排系统插件。

服务（capabilities.services，供执行面/触发器/面板跨插件调用）：
- director.get_status        状态快照（状态机/游标/台账/插播队列/看门狗判据）
- director.boot              手动开播（OFFLINE → BOOTING，动作串由调用方执行）
- director.feed_danmaku      弹幕入聚合窗（channel_bilibili 轮询投喂）
- director.flush_aggregation 聚合窗冲刷（有效意图挂起等 IDLE）
- director.run_segment       跑一次 W2 段生成（显式意图或消费挂起意图）
- director.observe_tick      执行面节拍：观测进 → 触发器推导 → 动作串出
- director.trip / recover    看门狗人工注弧 / 恢复（演练通道）

端口绑定（真机联调校准点，P2 接线）：剧本生成（LLM 能力）、渲染
（tool-executor → video_gen.render_segment）、弹幕来源
（channel_bilibili 服务）需经内核能力句柄绑定；未绑定时：
- 剧本/渲染/抽取端口调用 → 显式异常（不伪装成功）
- 内容过滤端口 → **fail-closed**（拦截并注明 FILTER_PORT_UNBOUND）
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("director")

_CORE: Any = None
_PORT_STATE: dict[str, Any] = {
    "script_bound": False,
    "renderer_bound": False,
}


def _load_core_modules() -> dict[str, Any]:
    """按真实导入名加载本目录模块（server 场景与测试同源，防双实例）。"""
    here = str(Path(__file__).parent)
    loaded: dict[str, Any] = {}
    for filename, mod_name in (
        ("state_machine.py", "state_machine"),
        ("cursor.py", "cursor"),
        ("aggregator.py", "aggregator"),
        ("core.py", "director_core_mod"),
    ):
        spec = importlib.util.spec_from_file_location(mod_name, Path(here) / filename)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        loaded[mod_name] = mod
    return loaded


def _data_dir() -> str:
    return os.environ.get("AGENTOS_LIVESTREAM_DATA_DIR", "data/livestream")


def _unbound(port: str) -> Any:
    async def _raise(*_args: Any, **_kw: Any) -> Any:
        raise RuntimeError(f"director 端口未绑定（真机联调校准点）: {port}")

    return _raise


def _filter_fail_closed(*_args: Any, **_kw: Any) -> Any:
    async def _deny(_narration: str, _subtitle: str, _prompts: list[str]) -> tuple[bool, str]:
        return False, "FILTER_PORT_UNBOUND"

    return _deny


def _get_core() -> Any:
    """惰性构建 DirectorCore（默认端口：未绑定面显式失败/拦截）。"""
    global _CORE
    if _CORE is not None:
        return _CORE
    mods = _load_core_modules()
    data_dir = _data_dir()
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    cursor = mods["cursor"].TimelineCursor(os.path.join(data_dir, "cursor.json"))
    metrics = mods["director_core_mod"].DirectorMetrics()
    pipeline = mods["director_core_mod"].SegmentPipeline(
        cursor=cursor,
        script_gen=_unbound("script_gen(LLM)"),
        content_filter=_filter_fail_closed(),
        renderer=_unbound("renderer(video_gen)"),
        extractor=_unbound("frame_extractor(ffmpeg)"),
        library=_JsonlLibrary(os.path.join(data_dir, "library.jsonl")),
        metrics=metrics,
    )
    _CORE = mods["director_core_mod"].DirectorCore(
        sm=mods["state_machine"].LiveStateMachine(now=time.monotonic()),
        aggregator=mods["aggregator"].DanmakuAggregator(),
        cursor=cursor,
        pipeline=pipeline,
        metrics=metrics,
        data_dir=data_dir,
    )
    return _CORE


class _JsonlLibrary:
    """内容库索引缺省实现：JSONL 追加落盘（真机可换 manifest 读写）。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def add(self, meta: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")


def _now() -> float:
    return time.monotonic()


# ══ 服务面（声明即契约，G2 校验声明=实现）══════════════════════════


@plugin.tool(
    name="director.get_status",
    schema={"type": "object", "properties": {}},
    description="直播编排状态快照（状态机/游标/台账/插播队列/看门狗判据）",
)
async def get_status() -> dict[str, Any]:
    """状态快照。"""
    return _get_core().status()


@plugin.tool(
    name="director.boot",
    schema={"type": "object", "properties": {}},
    description="手动开播：OFFLINE → BOOTING，返回需执行的动作串",
)
async def boot() -> dict[str, Any]:
    """手动开播。"""
    core = _get_core()
    ok, state, actions = core.sm.transition(_mods()["state_machine"].Trigger.BOOT, now=_now())
    return {"ok": ok, "state": state.value, "actions": list(actions)}


def _mods() -> dict[str, Any]:
    if "state_machine" not in sys.modules:
        _load_core_modules()
    return {name: sys.modules[name] for name in ("state_machine",)}


@plugin.tool(
    name="director.feed_danmaku",
    schema={
        "type": "object",
        "properties": {
            "user": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["user", "text"],
    },
    description="弹幕入聚合窗（channel_bilibili 轮询投喂）",
)
async def feed_danmaku(user: str = "", text: str = "") -> dict[str, Any]:
    """弹幕投喂。"""
    _get_core().feed_danmaku(_now(), user, text)
    return {"ok": True}


@plugin.tool(
    name="director.flush_aggregation",
    schema={"type": "object", "properties": {}},
    description="聚合窗冲刷：有效意图挂起等 IDLE 执行",
)
async def flush_aggregation() -> dict[str, Any]:
    """窗口冲刷。"""
    result = await _get_core().flush_aggregation(_now())
    return {
        "valid": result.valid,
        "intent": result.intent,
        "distinct_users": result.distinct_users,
    }


@plugin.tool(
    name="director.run_segment",
    schema={
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "显式意图（空则消费挂起意图）"},
            "transition": {"type": "string", "enum": ["continue", "cut"]},
        },
    },
    description="跑一次 W2 段生成（离线生产脚本亦走此口）",
)
async def run_segment(intent: str = "", transition: str = "continue") -> dict[str, Any]:
    """段生成。"""
    core = _get_core()
    if intent:
        core.pending_intent = intent
        core.pending_representatives = []
    outcome = await core.run_pending_segment(transition)
    if outcome is None:
        return {"ok": False, "reason": "NO_PENDING_INTENT"}
    return {
        "ok": outcome.accepted,
        "stage": outcome.stage,
        "reason": outcome.reason,
        "video_path": outcome.video_path,
        "seg_no": outcome.seg_no,
    }


@plugin.tool(
    name="director.observe_tick",
    schema={
        "type": "object",
        "properties": {
            "nobody_secs": {"type": "number"},
            "queue_empty": {"type": "boolean"},
            "schedule_end": {"type": "boolean"},
        },
    },
    description="执行面节拍：观测进 → 触发器推导 → 动作串出（triggers_ext 定时调用）",
)
async def observe_tick(
    nobody_secs: float = 0.0, queue_empty: bool = True, schedule_end: bool = False
) -> dict[str, Any]:
    """状态机节拍。"""
    ok, state, actions = _get_core().tick(
        _now(),
        queue_empty=queue_empty,
        nobody_secs=nobody_secs,
        schedule_end=schedule_end,
    )
    return {"ok": ok, "state": state.value, "actions": list(actions)}


@plugin.tool(
    name="director.trip",
    schema={
        "type": "object",
        "properties": {"detail": {"type": "string"}},
        "required": ["detail"],
    },
    description="看门狗人工注弧（演练通道）：活动态 → FAULT + 通知动作",
)
async def trip(detail: str = "manual_trip") -> dict[str, Any]:
    """注弧进 FAULT。"""
    core = _get_core()
    ok, state, actions = core.sm.transition(
        _mods()["state_machine"].Trigger.WATCHDOG_TRIP,
        ctx={"has_fault_context": True, "fault_detail": detail},
        now=_now(),
    )
    return {"ok": ok, "state": state.value, "actions": list(actions)}


@plugin.tool(
    name="director.recover",
    schema={"type": "object", "properties": {}},
    description="故障恢复：FAULT → IDLE（清故障上下文）",
)
async def recover() -> dict[str, Any]:
    """人工恢复。"""
    core = _get_core()
    ok, state, actions = core.sm.transition(
        _mods()["state_machine"].Trigger.RECOVER,
        ctx={"has_fault_context": True},
        now=_now(),
    )
    return {"ok": ok, "state": state.value, "actions": list(actions)}


if __name__ == "__main__":
    plugin.run()
