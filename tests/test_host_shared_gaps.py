# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""合宿宿主（plugins/shared/_host/host.py）分支补测。

行为契约（断输入→输出/副作用，不钉实现）：
- _default_shared_root：宿主文件上级目录即 plugins/shared（成员发现根）
- _scan_plugin_dirs：插件根不可用 → 跳过该根不报错（可用根照常索引）
- _LoopWatchdog.start：重复启动幂等（单 watchdog 线程，停滞时恰一次自杀）
- _heartbeat_loop：取消前持续打点（心跳时间戳单调推进）
- _serve：serve 正常返回后心跳任务被取消、watchdog 停止（否则 asyncio.run
  无法退出——测试能返回即清理发生的可观察证明）；serve 异常原样传播且
  清理仍执行
- main：成员加载成功 happy path → 构造 CohostServer 聚合命名空间工具 →
  进入 serve 循环 → rc=0，stderr 打印成员数与聚合工具数

防御分支（实测 Python 3.12，真实输入不可达，以协作者故障注入覆盖）：
_exec_member_module 的 ``spec is None or spec.loader is None``——
spec_from_file_location 对任意字符串路径（含不存在的 .py 文件）恒返回带
SourceFileLoader 的 spec，而 ``plugin_dir / "server.py"`` 的 location 恒非
None。护栏行为按其本义对 ``importlib.util.spec_from_file_location`` 注入
None / loader=None 两种故障形态覆盖（TestExecMemberModuleSpecGuard），
不硬凑真实输入。

测试基建与 tests/plugins/_host/ 同款：HOST_DIR 注入 sys.path 后裸名
``import host``；合成成员复刻轻插件结构（plugin.json + plugin.py +
server.py，平铺裸名 import）。成员加载按 cohost 静息态语义会把首个成员
的裸名模块留在 sys.modules，_guard_flat_names 逐出测试期间新增模块，
防同进程后续测试平铺名串扰。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from agentos_plugin_sdk import CohostServer

_REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_DIR = _REPO_ROOT / "plugins" / "shared" / "_host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

import host  # noqa: E402

pytestmark = pytest.mark.unit

_MEMBER_SERVER_TEMPLATE = '''\
"""合成成员插件（测试用）：平铺 import plugin.py，暴露身份。"""
from agentos_plugin_sdk import AgentOSPlugin

from plugin import MEMBER_NAME

plugin = AgentOSPlugin("{plugin_name}")


@plugin.tool(
    name="echo",
    schema={{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": ["text"]}},
    description="Echo member identity",
)
async def echo(text: str) -> dict:
    return {{"member": MEMBER_NAME, "text": text}}


@plugin.tool(
    name="last_lifecycle",
    schema={{"type": "object", "properties": {{}}}},
    description="Lifecycle payloads received so far",
)
async def last_lifecycle() -> dict:
    return {{"member": MEMBER_NAME}}
'''


def _write_member(shared_root: Path, group_rel: str, dir_name: str, manifest_id: str) -> Path:
    """在假 shared 树下写入合成成员目录（plugin.json + plugin.py + server.py）。"""
    member_dir = shared_root / group_rel / dir_name
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / "plugin.json").write_text(
        json.dumps({"id": manifest_id, "host_type": "sidecar"}),
        encoding="utf-8",
    )
    (member_dir / "plugin.py").write_text(f'MEMBER_NAME = "{dir_name}"\n', encoding="utf-8")
    (member_dir / "server.py").write_text(
        _MEMBER_SERVER_TEMPLATE.format(plugin_name=dir_name), encoding="utf-8"
    )
    return member_dir


@pytest.fixture(autouse=True)
def _guard_flat_names():
    """逐出测试期间新增的 sys.modules 条目并还原 sys.path（成员 exec 会留下
    裸名 ``plugin`` 与 ``_cohost_member_*``，静息态常驻是运行时语义、不是
    测试进程语义）。

    前置逐出裸名 ``plugin``：全车道共跑下，先行的插件测试会把
    tool_schema_validator 等同名 plugin.py 以裸名抢注进 sys.modules——
    成员装载按裸名导入会命中该缓存，报 MEMBER_NAME ImportError（批九
    全量实锤）。逐出后 import 机制按本用例的 sys.path 重新装载。"""
    sys.modules.pop("plugin", None)
    modules_before = frozenset(sys.modules)
    path_before = list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in modules_before]:
        sys.modules.pop(name, None)
    sys.path[:] = path_before


# ── 成员发现根与分组扫描 ─────────────────────────────────


class TestSharedRootAndScan:
    def test_default_shared_root_is_plugins_shared(self) -> None:
        root = host._default_shared_root()
        assert root == HOST_DIR.parent
        assert (root / "_host").is_dir()

    def test_scan_skips_absent_group_roots(self, tmp_path: Path) -> None:
        """插件根不可用 → 空索引不报错；根存在则照常索引其下成员目录。"""
        by_manifest, by_dir = host._scan_plugin_dirs([tmp_path])
        assert by_manifest == {}
        assert by_dir == {}

        member = _write_member(tmp_path, "system", "alpha", "alpha_id")
        by_manifest, by_dir = host._scan_plugin_dirs([tmp_path])
        assert by_manifest == {"alpha_id": member}
        assert by_dir == {"alpha": member}


# ── watchdog 启动幂等 ────────────────────────────────────


class TestWatchdogStartIdempotent:
    def test_double_start_yields_single_exit(self) -> None:
        """重复 start 不叠加线程：停滞时恰一次 exit(1)（多线程会 exit 两次）。"""
        now = {"t": 1000.0}
        clock = lambda: now["t"]  # noqa: E731 — fake clock 注入点（生产侧契约）
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat, 30.0, check_interval_secs=0.005, clock=clock, exit_fn=exits.append
        )
        watchdog.start()
        watchdog.start()
        now["t"] += 31.0  # 停滞严格超过阈值
        deadline = time.monotonic() + 5.0
        while not exits and time.monotonic() < deadline:
            time.sleep(0.005)
        watchdog.stop()
        assert exits == [1]


# ── 心跳循环与主循环编排 ─────────────────────────────────


class TestHeartbeatLoop:
    def test_beats_until_cancelled(self) -> None:
        heartbeat = host._Heartbeat()
        first = heartbeat.last_beat

        async def _drive() -> None:
            task = asyncio.create_task(host._heartbeat_loop(heartbeat, interval_secs=0.001))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_drive())
        assert heartbeat.last_beat > first, "循环存活期间心跳时间戳必须推进"


class TestServeOrchestration:
    def test_serve_completes_and_stops_background_tasks(self) -> None:
        """serve 返回后 _serve 必须能退出：心跳任务被取消、watchdog 停止——
        任一清理缺失，asyncio.run 都无法返回（后台任务挂起事件循环）。"""

        class _Server:
            def __init__(self) -> None:
                self.served = 0

            async def serve(self) -> None:
                self.served += 1

        server = _Server()
        asyncio.run(host._serve(server, stall_secs=30.0))
        assert server.served == 1

    def test_serve_propagates_serve_exception_after_cleanup(self) -> None:
        class _Broken:
            async def serve(self) -> None:
                raise RuntimeError("stdio broken")

        with pytest.raises(RuntimeError, match="stdio broken"):
            asyncio.run(host._serve(_Broken(), stall_secs=30.0))


# ── 成员装载 spec 守卫（协作者故障注入）──────────────────


class _SpecWithoutLoader:
    """spec 替身：loader 位为 None（守卫第二判据）。"""

    loader = None


class TestExecMemberModuleSpecGuard:
    """_exec_member_module 的 spec 守卫（靶行 host.py 281：raise CohostError）。

    真实输入不可达（模块 docstring 说明：.py 路径恒返回带 SourceFileLoader 的
    spec）；对协作者 ``importlib.util.spec_from_file_location`` 注入两种故障
    形态（返回 None / 返回 loader=None 的 spec），验证 fail-fast 契约：
    CohostError 携带 plugin_id 与 server.py 路径，且守卫先于模块登记触发
    （sys.modules 不残留成员槽位）。
    """

    @pytest.mark.parametrize(
        ("inject_none_spec", "shape"),
        [(True, "spec-none"), (False, "loader-none")],
    )
    def test_spec_guard_raises_cohost_error_with_member_id_and_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        inject_none_spec: bool,
        shape: str,
    ) -> None:
        del shape  # 仅作用例标识
        fake_spec = None if inject_none_spec else _SpecWithoutLoader()
        monkeypatch.setattr(
            host.importlib.util,
            "spec_from_file_location",
            lambda name, location, **_kw: fake_spec,
        )

        with pytest.raises(host.CohostError) as exc_info:
            host._MemberLoader()._exec_member_module("ghost_member", tmp_path)

        msg = str(exc_info.value)
        assert "ghost_member" in msg, "报错必须携带成员 plugin_id"
        assert str(tmp_path / "server.py") in msg, "报错必须携带 server.py 路径"
        assert "无法" in msg and "spec" in msg
        assert "_cohost_member_ghost_member" not in sys.modules, (
            "守卫先于模块登记触发，sys.modules 不得残留成员槽位"
        )


# ── 入口 happy path ──────────────────────────────────────


class TestMainSuccessPath:
    def test_main_serves_aggregated_members_and_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "shared"
        _write_member(root, "system", "alpha", "alpha_id")
        served: list[tuple[Any, float]] = []

        async def _fake_serve(
            server: CohostServer, stall_secs: float = host._WATCHDOG_STALL_SECS_DEFAULT
        ) -> None:
            served.append((server, stall_secs))

        monkeypatch.setattr(host, "_serve", _fake_serve)
        monkeypatch.delenv("AGENTOS_HOST_WATCHDOG_SECS", raising=False)

        rc = host.main(
            ["--group", "light", "--slot", "2", "--members", "alpha_id"], shared_root=root
        )

        assert rc == 0
        assert len(served) == 1
        server, stall_secs = served[0]
        assert isinstance(server, CohostServer)
        assert server.tool_names == ["alpha_id.echo", "alpha_id.last_lifecycle"]
        assert stall_secs == host._WATCHDOG_STALL_SECS_DEFAULT
        err = capsys.readouterr().err
        assert "group=light" in err
        assert "members=1" in err
        assert "tools=2" in err
