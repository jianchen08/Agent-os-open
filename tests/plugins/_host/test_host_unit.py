# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""合宿宿主单元测试：watchdog、成员发现、成员加载 fail-fast、聚合命名空间。

断行为不断实现：watchdog 用注入 fake clock + 缩短阈值/间隔参数化（禁止
真实 sleep 30s）；成员发现/加载经 ``host._load_members``/``host.main`` 的
可观察结果（路径/退出码/错误信息）断言。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agentos_plugin_sdk import AgentOSPlugin, CohostServer

import host

pytestmark = pytest.mark.unit


def _make_member_plugin(plugin_name: str, tool_name: str = "echo") -> AgentOSPlugin:
    """构造单工具假成员 plugin 实例（不落盘，纯内存）。"""
    plugin = AgentOSPlugin(plugin_name)

    @plugin.tool(name=tool_name, schema={"type": "object"}, description="unit probe")
    async def echo() -> dict:
        return {"who": plugin_name}

    return plugin


# ── watchdog 阈值配置 ────────────────────────────────────


class TestWatchdogStallSecs:
    """AGENTOS_HOST_WATCHDOG_SECS 解析：合法覆盖默认，非法/非正回退 30s。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("7.5", 7.5), ("1", 1.0), ("", 30.0), ("abc", 30.0), ("0", 30.0), ("-3", 30.0)],
    )
    def test_env_parsing(self, raw: str, expected: float) -> None:
        assert host._watchdog_stall_secs({"AGENTOS_HOST_WATCHDOG_SECS": raw}) == expected

    def test_env_missing_uses_default(self) -> None:
        assert host._watchdog_stall_secs({}) == host._WATCHDOG_STALL_SECS_DEFAULT


# ── watchdog 心跳停滞自杀 ────────────────────────────────


class TestLoopWatchdog:
    """watchdog 线程：心跳停滞超阈值自杀（exit_fn(1)），健康打点不触发。"""

    @pytest.mark.parametrize("stall_secs", [30.0, 0.5])
    def test_fires_when_heartbeat_stalled(self, stall_secs: float) -> None:
        """心跳停滞超过阈值（含默认 30s 量级，fake clock 推进）→ exit_fn(1)。"""
        now = {"t": 1000.0}
        clock = lambda: now["t"]  # noqa: E731 — 测试内 fake clock
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat, stall_secs, check_interval_secs=0.005, clock=clock, exit_fn=exits.append
        )
        watchdog.start()
        now["t"] += stall_secs + 1.0  # 停滞严格超过阈值
        deadline = time.monotonic() + 5.0
        while not exits and time.monotonic() < deadline:
            time.sleep(0.005)
        watchdog.stop()
        assert exits == [1]

    def test_no_fire_when_beats_keep_arriving(self) -> None:
        """健康循环：每轮推进 29s（< 30s）即打点重置，永不触发。"""
        now = {"t": 0.0}
        clock = lambda: now["t"]  # noqa: E731 — 测试内 fake clock
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat, 30.0, check_interval_secs=0.005, clock=clock, exit_fn=exits.append
        )
        watchdog.start()
        for _ in range(6):
            now["t"] += 29.0
            heartbeat.beat()
            time.sleep(0.01)  # 给 watchdog 线程检查窗口（真实时间仅 10ms/轮）
        watchdog.stop()
        assert exits == []

    def test_no_fire_at_exact_threshold(self) -> None:
        """停滞恰好等于阈值不触发（严格大于语义）。"""
        now = {"t": 100.0}
        clock = lambda: now["t"]  # noqa: E731 — 测试内 fake clock
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat, 30.0, check_interval_secs=0.005, clock=clock, exit_fn=exits.append
        )
        watchdog.start()
        now["t"] += 30.0
        time.sleep(0.05)
        watchdog.stop()
        assert exits == []

    def test_stop_prevents_exit_even_if_stalled(self) -> None:
        """stop 先于停滞被观察到 → 线程退出，不自杀。"""
        now = {"t": 0.0}
        clock = lambda: now["t"]  # noqa: E731 — 测试内 fake clock
        heartbeat = host._Heartbeat(clock=clock)
        exits: list[int] = []
        watchdog = host._LoopWatchdog(
            heartbeat, 30.0, check_interval_secs=0.005, clock=clock, exit_fn=exits.append
        )
        watchdog.start()
        watchdog.stop()
        now["t"] += 10_000.0  # 停滞巨大
        time.sleep(0.05)
        assert exits == []


# ── 成员发现 ─────────────────────────────────────────────


class TestMemberDiscovery:
    """成员 id → 插件目录定位：manifest id 优先、目录名兜底、形态过滤。"""

    def test_manifest_id_takes_precedence_over_dir_name(self, make_member) -> None:
        """manifest id 命中优先于目录名命中（内核以 manifest id 标识插件）。"""
        dir_a = make_member("system", "x", "key")
        dir_b = make_member("tools", "key", "other_id")
        by_manifest, by_dir = host._scan_plugin_dirs(dir_a.parents[1])
        assert host._resolve_member_dir("key", by_manifest, by_dir) == dir_a
        assert host._resolve_member_dir("other_id", by_manifest, by_dir) == dir_b

    def test_dir_name_fallback(self, make_member) -> None:
        """目录名兜底定位（manifest id 与目录名不一致时的第二解析路径）。"""
        member_dir = make_member("pipeline/output", "plain", "unrelated_manifest_id")
        by_manifest, by_dir = host._scan_plugin_dirs(member_dir.parents[2])
        assert host._resolve_member_dir("plain", by_manifest, by_dir) == member_dir

    def test_dirs_without_server_py_ignored(self, shared_tree: Path) -> None:
        """无 server.py 的目录（原生/cdylib 形态）不进入索引。"""
        native_dir = shared_tree / "pipeline" / "core" / "native_only"
        native_dir.mkdir(parents=True)
        (native_dir / "plugin.json").write_text('{"id": "native_only"}', encoding="utf-8")
        by_manifest, by_dir = host._scan_plugin_dirs(shared_tree)
        assert host._resolve_member_dir("native_only", by_manifest, by_dir) is None

    def test_corrupt_manifest_resolvable_by_dir_name(self, shared_tree: Path) -> None:
        """损坏的 plugin.json 不阻塞扫描，目录名索引仍可命中。"""
        broken_dir = shared_tree / "system" / "corrupt"
        broken_dir.mkdir()
        (broken_dir / "plugin.json").write_text("not a json", encoding="utf-8")
        (broken_dir / "server.py").write_text("plugin = None\n", encoding="utf-8")
        by_manifest, by_dir = host._scan_plugin_dirs(shared_tree)
        assert host._resolve_member_dir("corrupt", by_manifest, by_dir) == broken_dir

    def test_venv_and_node_modules_pruned_from_scan(self, shared_tree: Path) -> None:
        """重型目录（.venv/node_modules/__pycache__）不进扫描索引。

        真实 plugins/shared 下每插件带 .venv、dsh_adapter 带 node_modules peer
        装载区，全树扫描必须剪枝（卡死回归测试：曾因 rglob 跟随 junction 遍历
        外部仓库实测挂起）。
        """
        venv_plugin = shared_tree / "tools" / "venv_holder"
        venv_plugin.mkdir(parents=True)
        (venv_plugin / "plugin.json").write_text('{"id": "venv_holder"}', encoding="utf-8")
        (venv_plugin / "server.py").write_text("plugin = None\n", encoding="utf-8")
        (venv_plugin / ".venv").mkdir()
        (venv_plugin / ".venv" / "plugin.json").write_text(
            '{"id": "fake_nested"}', encoding="utf-8"
        )
        (venv_plugin / "node_modules").mkdir()
        (venv_plugin / "node_modules" / "plugin.json").write_text(
            '{"id": "fake_nested2"}', encoding="utf-8"
        )
        by_manifest, by_dir = host._scan_plugin_dirs(shared_tree)
        assert host._resolve_member_dir("venv_holder", by_manifest, by_dir) == venv_plugin
        assert host._resolve_member_dir("fake_nested", by_manifest, by_dir) is None
        assert host._resolve_member_dir("fake_nested2", by_manifest, by_dir) is None

    def test_symlink_dir_not_followed(self, shared_tree: Path) -> None:
        """符号链接目录不进入扫描（junction 循环剪链：rglob 曾实测卡死）。"""
        if not hasattr(os, "symlink"):
            pytest.skip("平台无 symlink")
        outside = shared_tree.parent / "outside_repo"
        outside.mkdir()
        (outside / "plugin.json").write_text('{"id": "outside_id"}', encoding="utf-8")
        (outside / "server.py").write_text("plugin = None\n", encoding="utf-8")
        link_dir = shared_tree / "system" / "link_holder"
        link_dir.mkdir()
        try:
            os.symlink(outside, link_dir / "linked", target_is_directory=True)
        except OSError:
            pytest.skip("symlink 创建失败（Windows 权限）")
        by_manifest, by_dir = host._scan_plugin_dirs(shared_tree)
        assert host._resolve_member_dir("outside_id", by_manifest, by_dir) is None


# ── 成员加载与 fail-fast ─────────────────────────────────


class TestMemberLoadFailFast:
    """成员加载 fail-fast：任一成员失败 → main 退出码 1 + 明确错误。"""

    def test_unknown_member_fail_fast(self, shared_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = host.main(
            ["--group", "light", "--slot", "1", "--members", "alpha_id,ghost"],
            shared_root=shared_tree,
        )
        assert rc == 1
        assert "ghost" in capsys.readouterr().err

    def test_broken_member_fail_fast(self, make_member, capsys: pytest.CaptureFixture[str]) -> None:
        broken_dir = make_member("system", "broken", "broken_id")
        (broken_dir / "server.py").write_text("raise ImportError('broken on purpose')\n", encoding="utf-8")
        rc = host.main(
            ["--group", "light", "--slot", "1", "--members", "broken_id"],
            shared_root=broken_dir.parents[1],
        )
        assert rc == 1
        assert "broken_id" in capsys.readouterr().err

    def test_member_without_plugin_object_fail_fast(
        self, make_member, capsys: pytest.CaptureFixture[str]
    ) -> None:
        no_plugin_dir = make_member("system", "emptyish", "emptyish_id")
        (no_plugin_dir / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
        rc = host.main(
            ["--group", "light", "--slot", "1", "--members", "emptyish_id"],
            shared_root=no_plugin_dir.parents[1],
        )
        assert rc == 1
        assert "emptyish_id" in capsys.readouterr().err

    def test_empty_members_fail_fast(self, shared_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = host.main(["--group", "light", "--slot", "1", "--members", ","], shared_root=shared_tree)
        assert rc == 1
        assert "至少需要一个成员" in capsys.readouterr().err

    def test_duplicate_members_fail_fast(self, shared_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = host.main(
            ["--group", "light", "--slot", "1", "--members", "alpha_id,alpha_id"],
            shared_root=shared_tree,
        )
        assert rc == 1
        assert "重复" in capsys.readouterr().err


class TestMemberLoading:
    """多成员同进程加载：平铺裸名隔离 + 聚合命名空间。"""

    def test_members_load_independently_with_namespaced_tools(self, shared_tree: Path) -> None:
        members = host._load_members(shared_tree, ["alpha_id", "beta_id", "gamma_id"])
        # 各成员持有独立 AgentOSPlugin 实例（进程内多实例）
        assert set(members) == {"alpha_id", "beta_id", "gamma_id"}
        assert len({id(m) for m in members.values()}) == 3
        # 工具以 {plugin_id}. 前缀聚合
        server = CohostServer(members)
        assert server.tool_names == [
            "alpha_id.echo",
            "alpha_id.last_lifecycle",
            "beta_id.echo",
            "beta_id.last_lifecycle",
            "gamma_id.echo",
            "gamma_id.last_lifecycle",
        ]


# ── SDK 聚合服务端 ───────────────────────────────────────


class TestCohostServerAggregation:
    """CohostServer 聚合构造：命名空间前缀与冲突 fail-fast。"""

    def test_tool_names_namespaced(self) -> None:
        server = CohostServer({"m1": _make_member_plugin("m1"), "m2": _make_member_plugin("m2")})
        assert server.tool_names == ["m1.echo", "m2.echo"]

    def test_empty_members_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one member"):
            CohostServer({})

    def test_namespace_conflict_rejected(self) -> None:
        """成员 id 含点号导致聚合工具名冲突 → 构造期 fail-fast。"""
        with pytest.raises(ValueError, match="conflict"):
            CohostServer({"a": _make_member_plugin("a", tool_name="b.echo"), "a.b": _make_member_plugin("a.b")})
