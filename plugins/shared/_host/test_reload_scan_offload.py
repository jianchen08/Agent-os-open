# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""_reload 清单扫描线程卸载测试（2026-11-18 装机事故 1b）。

合宿装载请求路径（cohost._handle_load_request → host._reload →
``_scan_plugin_dirs``）的 os.walk 全量扫描直跑合宿事件循环线程，100+
插件目录慢盘实测停滞 30.5s（stall 取证 host_stall_20261118T115732Z）。

契约：
- 扫描期间事件循环心跳持续推进——同步阻塞实现下扫描全程占循环，心跳
  恒停在首轮（与 tasks/test_orchestration.py 批次 1 同款判别法）；
- 卸载不改定位语义：已知成员 reload 成功返回插件实例，未知成员
  fail-closed 抛 CohostError（定位失败）。

延迟注入点 = os.walk（真实扫描路径走到该调用），两组延迟量级有区分度。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import host  # noqa: E402

pytestmark = pytest.mark.unit


def _write_member(root: Path, dirname: str, plugin_id: str) -> Path:
    """落盘一个最小 sidecar 成员（plugin.json + server.py 暴露 plugin 实例）。"""
    d = root / "tools" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        f'{{"id": "{plugin_id}", "version": "1.0.0"}}', encoding="utf-8"
    )
    (d / "server.py").write_text(
        "from agentos_plugin_sdk import AgentOSPlugin\n"
        "plugin = AgentOSPlugin(tools=[])\n",
        encoding="utf-8",
    )
    return d


class TestReloadScanOffload:
    """_reload 扫描线程卸载：装载请求处理期间事件循环不被扫描阻塞。"""

    def test_reload_scan_keeps_loop_heartbeating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """os.walk 延迟注入下 reload 扫描期间循环心跳持续推进（两组延迟有区分度）。"""
        _write_member(tmp_path, "alpha", "p_alpha")
        _write_member(tmp_path, "beta", "p_beta")

        shared_root = tmp_path
        real_walk = os.walk

        async def _fake_load(plugin_id: str, plugin_dir: Path) -> Any:
            return object()

        loader = type(
            "_Loader",
            (),
            {"reload": staticmethod(lambda plugin_id, plugin_dir: _fake_load(plugin_id, plugin_dir))},
        )()

        for delay in (0.05, 0.10):
            def slow_walk(top, *args: Any, _real=real_walk, _delay=delay, **kwargs: Any):
                time.sleep(_delay)
                return _real(top, *args, **kwargs)

            monkeypatch.setattr(os, "walk", slow_walk)
            heartbeats = 0

            async def _run() -> Any:
                nonlocal heartbeats
                scan_done = asyncio.Event()
                handler = host._build_reload_handler(shared_root, loader)

                async def _heartbeat() -> None:
                    nonlocal heartbeats
                    while not scan_done.is_set():
                        heartbeats += 1
                        await asyncio.sleep(0.01)

                hb = asyncio.create_task(_heartbeat())
                plugin = await handler("p_alpha")
                scan_done.set()
                await hb
                return plugin

            plugin = asyncio.run(_run())
            assert plugin is not None
            # 阻塞实现下扫描全程占循环 → 心跳恒为 1；卸载后至少推进数轮
            assert heartbeats >= 3

    def test_reload_keeps_semantics_after_offload(self, tmp_path: Path) -> None:
        """卸载不改定位语义：已知成员成功、未知成员 fail-closed 抛定位失败。"""
        from host import CohostError

        _write_member(tmp_path, "alpha", "p_alpha")

        async def _fake_load(plugin_id: str, plugin_dir: Path) -> Any:
            return object()

        loader = type(
            "_Loader",
            (),
            {"reload": staticmethod(lambda plugin_id, plugin_dir: _fake_load(plugin_id, plugin_dir))},
        )()
        handler = host._build_reload_handler(tmp_path, loader)

        known = asyncio.run(handler("p_alpha"))
        assert known is not None
        with pytest.raises(CohostError, match="定位失败"):
            asyncio.run(handler("p_absent"))
