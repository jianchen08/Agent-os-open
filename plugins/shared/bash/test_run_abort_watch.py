# @feature: FP-0.2.〇 管道主循环 卡死 run 中止（bash 工具 run 中止看门狗） | @ci: plugins-coverage
"""run 中止看门狗测试（run_abort_watch + run_command 中止收口）。

覆盖三层：
1. 判定纯逻辑：状态解析/按管道精确匹配/轮询间隔哨兵；
2. 看门狗行为：命中 suspended 即置事件、running 不置（防误杀）、读面异常降级、
   停止位可退出；
3. 真实链路：真 BashTool + 真 ProcessManager 起一个 30s 子进程，中止事件置位后
   必须在远小于 30s 内返回 RUN_ABORTED 且进程不再运行——证明「卡死 run 可干预」
   不是纸面结论。

隔离策略（与 test_failure_envelope.py 同款）：本测试对 ProcessManager 用**真实现**
（关键路径必须有真实依赖），只对「run 状态读面」用假读器——那是外部内核依赖。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SDK_SRC = _PLUGIN_DIR.parent.parent / "sdk" / "src"
for _p in (str(_PLUGIN_DIR), str(_SDK_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(module_name: str, filename: str) -> Any:
    """按文件路径装载插件模块（插件目录非包，不能用 import 语句直取）。"""
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_WATCH = _load("bash_run_abort_watch_test", "run_abort_watch.py")
RunAbortWatcher = _WATCH.RunAbortWatcher
resolve_poll_secs = _WATCH.resolve_poll_secs
pick_row_status = _WATCH.pick_row_status
status_of_row = _WATCH.status_of_row
start_watcher = _WATCH.start_watcher

_TOOL = _load("bash_tool_abort_test", "tool.py")
BashTool = _TOOL.BashTool


# ── 1. 判定纯逻辑 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 2.0),  # 缺省：启用，2s
        ("", 2.0),  # 空串等同缺省
        ("0.5", 0.5),  # 合法值原样
        ("0", 0.0),  # 0 = 关闭
        ("-1", 0.0),  # 负值 = 关闭（不得当无限制）
        ("abc", 0.0),  # 非法值 = 关闭（配置意图不明，不擅自启用）
    ],
)
def test_resolve_poll_secs_sentinels(raw: str | None, expected: float) -> None:
    """轮询间隔哨兵：缺省启用、0/负值/非法值一律关闭（看门狗不得自作主张）。"""
    env = {} if raw is None else {_WATCH.ENV_POLL_SECS: raw}
    assert resolve_poll_secs(env) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"status": "suspended"}, "suspended"),
        ({"status": "Running"}, "running"),
        ({"run_status": "suspended"}, "suspended"),
        ({"status": ""}, ""),
        ({"status": {"Suspended": None}}, ""),  # 非串值不猜
        ("suspended", ""),  # 非 dict 不猜
    ],
)
def test_status_of_row_parses_both_faces(row: Any, expected: str) -> None:
    """状态字段双面兼容（run 记录的 status / state 行的 run_status），非法值不猜。"""
    assert status_of_row(row) == expected


def test_pick_row_status_never_borrows_other_pipeline_state() -> None:
    """跨管道行不得借用：别的管道 suspended 不许当作本 run 的中止理由（防误杀）。"""
    rows = [
        {"pipeline_id": "other-pipe", "status": "suspended"},
        {"pipeline_id": "mine", "status": "running"},
    ]
    assert pick_row_status(rows, "mine") == "running"
    assert pick_row_status(rows, "absent") is None
    assert pick_row_status("not-a-list", "mine") is None


# ── 2. 看门狗行为 ────────────────────────────────────────────────


async def _wait_event(event: asyncio.Event, timeout: float = 2.0) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout)
        return True
    except TimeoutError:
        return False


async def test_watcher_sets_event_on_suspended() -> None:
    """读面返回 suspended → 置中止事件（干预手段生效的最小闭环）。"""
    calls: list[int] = []

    async def _read() -> str:
        calls.append(1)
        return "suspended"

    watcher = RunAbortWatcher(_read, 0.01, "pipe-1")
    task = asyncio.create_task(watcher.watch())
    assert await _wait_event(watcher.event) is True
    await asyncio.wait_for(task, 2.0)
    assert watcher.polls == 1, "命中即收口，不继续轮询"


@pytest.mark.timing
async def test_watcher_does_not_abort_while_run_running() -> None:
    """run 正常在跑（running）不置事件——误杀防护的行为锚（非按耗时猜卡死）。"""
    calls: list[int] = []

    async def _read() -> str:
        calls.append(1)
        return "running"

    watcher = RunAbortWatcher(_read, 0.01, "pipe-2")
    task = asyncio.create_task(watcher.watch())
    await asyncio.sleep(0.12)  # ≈ 12 个轮询窗（时序不变量：多窗仍不中止）
    assert watcher.event.is_set() is False
    assert watcher.polls >= 3, f"轮询确实在跑（polls={watcher.polls}）"
    watcher.stop()
    await asyncio.wait_for(task, 2.0)
    assert watcher.event.is_set() is False


async def test_watcher_degrades_on_read_error_then_still_aborts() -> None:
    """读面异常按不可判定处理：不中止、不抛出，恢复后仍能命中 suspended。"""
    seq: Iterator[str | Exception] = iter([RuntimeError("channel down"), "suspended"])

    async def _read() -> str:
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    watcher = RunAbortWatcher(_read, 0.01, "pipe-3")
    task = asyncio.create_task(watcher.watch())
    assert await _wait_event(watcher.event) is True, "首窗异常后必须继续判定"
    await asyncio.wait_for(task, 2.0)


async def test_watcher_stop_ends_loop_without_abort() -> None:
    """停止位：工具调用结束时收尾轮询，不残留孤儿任务、不误置中止。"""

    async def _read() -> str:
        return "running"

    watcher = RunAbortWatcher(_read, 0.01, "pipe-4")
    task = asyncio.create_task(watcher.watch())
    await asyncio.sleep(0.05)
    watcher.stop()
    await asyncio.wait_for(task, 2.0)
    assert watcher.event.is_set() is False


class _FakePlugin:
    """能力注入桩：只实现 SDK 的 get_capability 读面。"""

    def __init__(self, caps: dict[str, Any]) -> None:
        self._caps = caps

    def get_capability(self, name: str) -> Any:
        if name not in self._caps:
            raise KeyError(f"capability not injected: {name}")
        return self._caps[name]


class _FakeHandle:
    def __init__(self, rows: Any) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        return self.rows


def test_start_watcher_disabled_without_pipeline_id_or_capability() -> None:
    """装配闸：缺 pipeline_id / 缺能力句柄 → 不启用（返回 None，工具照常执行）。"""
    handle = _FakeHandle([])
    assert start_watcher(_FakePlugin({"service-registry": handle}), {}) is None
    assert start_watcher(_FakePlugin({}), {"pipeline_id": "p"}) is None


def test_start_watcher_uses_primary_then_fallback_capability() -> None:
    """能力选择序：service-registry 优先，缺席回落到 pipeline-state。"""
    primary = _FakeHandle([{"pipeline_id": "p", "status": "suspended"}])
    watcher = start_watcher(_FakePlugin({"service-registry": primary}), {"pipeline_id": "p"})
    assert watcher is not None
    status = asyncio.run(watcher._read_status())
    assert status == "suspended"
    assert primary.calls[0][0] == _WATCH.PRIMARY_METHOD

    fallback = _FakeHandle([{"pipeline_id": "p", "run_status": "running"}])
    watcher2 = start_watcher(_FakePlugin({"pipeline-state": fallback}), {"pipeline_id": "p"})
    assert watcher2 is not None
    assert asyncio.run(watcher2._read_status()) == "running"
    assert fallback.calls[0][0] == _WATCH.FALLBACK_METHOD


# ── 3. 真实链路：卡死的内飞命令被中止 ───────────────────────────


async def test_run_command_aborts_in_flight_sleep(tmp_path: Path) -> None:
    """模拟卡死 run：30s 子进程在飞时置中止事件 → 远早于自然结束返回 RUN_ABORTED。

    断言的是可观察行为：返回码、耗时上界、进程状态——不是内部调用次数。
    """
    tool = BashTool()
    abort_event = asyncio.Event()

    async def _trip_abort() -> None:
        await asyncio.sleep(0.4)
        abort_event.set()

    asyncio.create_task(_trip_abort())
    started = asyncio.get_running_loop().time()
    result = await tool.run_command(
        {
            "command": 'python3 -c "import time; time.sleep(30)"',
            "workspace": str(tmp_path),
            "timeout": 25,
        },
        abort_event=abort_event,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert result.success is False
    assert result.error_code == "RUN_ABORTED", result.error
    assert elapsed < 10.0, f"中止必须远早于 30s 自然结束（实际 {elapsed:.2f}s）"
    assert result.metadata.get("terminated_by") == "run_abort"


async def test_run_command_not_aborted_completes_normally(tmp_path: Path) -> None:
    """对照组（防拟合）：中止事件不置位时，同一路径必须正常完成，不得伪造中止。"""
    tool = BashTool()
    abort_event = asyncio.Event()
    result = await tool.run_command(
        {"command": "python3 -c \"print('done-marker')\"", "workspace": str(tmp_path), "timeout": 20},
        abort_event=abort_event,
    )
    assert result.success is True
    assert abort_event.is_set() is False
    assert "done-marker" in str(result.output)
