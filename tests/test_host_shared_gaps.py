# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""合宿宿主（plugins/shared/_host/host.py）分支补测。

行为契约（断输入→输出/副作用，不钉实现）：
- _default_shared_root：宿主文件上级目录即 plugins/shared（成员发现根）
- _scan_plugin_dirs：插件根不可用 → 跳过该根不报错（可用根照常索引）；
  同 id 双源裁决（BuiltinFirst 版本口径）：用户副本 semver 严格大于内置才
  接管，==/</版本缺失/不可解析均内置赢（fail-closed），版本缺失/不可解析
  落败告警；manifest id 与目录名两索引同规；多用户源归约取最高
- _load_members：事故形态回归锁——用户 stale 低版本副本 import 即炸时，
  裁决选中内置副本，加载成功不崩
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


def _write_member(
    shared_root: Path,
    group_rel: str,
    dir_name: str,
    manifest_id: str,
    version: str | None = None,
) -> Path:
    """在假 shared 树下写入合成成员目录（plugin.json + plugin.py + server.py）。

    ``version``：写入 manifest version 字段（双源裁决测试需显式版本）；
    ``None`` 缺省不写（锁定「缺失 version 字段」降级语义的测试依赖该形态）。
    """
    member_dir = shared_root / group_rel / dir_name
    member_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"id": manifest_id, "host_type": "sidecar"}
    if version is not None:
        manifest["version"] = version
    (member_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (member_dir / "plugin.py").write_text(f'MEMBER_NAME = "{dir_name}"\n', encoding="utf-8")
    (member_dir / "server.py").write_text(
        _MEMBER_SERVER_TEMPLATE.format(plugin_name=dir_name), encoding="utf-8"
    )
    return member_dir


def _dual_roots(tmp_path: Path) -> tuple[Path, Path]:
    """构造双源裁决的根对：返回（内置根， 用户根），次序同 _member_roots。"""
    builtin_root = tmp_path / "builtin_shared"
    user_root = tmp_path / "user_plugins"
    builtin_root.mkdir()
    user_root.mkdir()
    return builtin_root, user_root


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


# ── 双源裁决（BuiltinFirst 版本口径，对齐内核 ADR 2026-09-20）──


class TestScanBuiltinFirstAdjudication:
    """_scan_plugin_dirs 同 id 双源裁决：用户副本 semver 严格大于内置才接管。

    事故锚（2026-10-15 装机审批 502）：用户空间 stale 副本 approval 1.2.2
    （< 内置 1.2.3）曾被「同 id 用户赢」规则选中 → import 已删模块 → 宿主
    fail-fast 崩循。裁决语义与内核 loader.rs ``should_replace``（BuiltinFirst）
    同规：持平或任一侧版本缺失/不可解析 → 内置赢（fail-closed，内置为权威
    分发物）。两张索引（manifest id / 目录名）必须同规。
    """

    @pytest.mark.parametrize(
        ("user_version", "builtin_version", "expected", "case"),
        [
            ("1.2.2", "1.2.3", "builtin", "user-lower-incident-shape"),
            ("1.2.3", "1.2.3", "builtin", "equal"),
            ("1.2.4", "1.2.3", "user", "user-strictly-newer"),
            ("1.10.0", "1.9.9", "user", "numeric-not-lexicographic"),
            ("1.2.4-rc.1", "1.2.4", "builtin", "prerelease-below-release"),
            ("1.2.4", "1.2.4-rc.1", "user", "release-above-prerelease"),
            (None, "1.2.3", "builtin", "user-version-missing"),
            ("not-semver", "1.2.3", "builtin", "user-version-unparseable"),
            ("1.2.4", None, "builtin", "builtin-version-missing"),
        ],
    )
    def test_adjudication_matrix_both_indexes(
        self,
        tmp_path: Path,
        user_version: str | None,
        builtin_version: str | None,
        expected: str,
        case: str,
    ) -> None:
        """版本裁决矩阵：user<builtin→内置；==→内置；>→用户；版本缺失/不可
        解析 fail-closed 内置。manifest id 与目录名两张索引同规（数字段按数值
        比较而非字典序；预发布 < 正式版——semver 2.0.0 全序）。"""
        del case  # 仅作用例标识
        builtin_root, user_root = _dual_roots(tmp_path)
        builtin_dir = _write_member(builtin_root, "shared", "approval", "approval_service", builtin_version)
        user_dir = _write_member(user_root, "approval", "approval", "approval_service", user_version)

        by_manifest, by_dir = host._scan_plugin_dirs([builtin_root, user_root])

        winner = user_dir if expected == "user" else builtin_dir
        assert by_manifest.get("approval_service") == winner
        assert by_dir.get("approval") == winner, "目录名索引必须与 manifest id 索引同规"

    def test_multiple_user_sources_take_strictly_newest(self, tmp_path: Path) -> None:
        """多用户源同 id：逐对归约取版本最高者（严格大于内置的候选里最新），
        同版本持平保留先发现者（目录名字典序），与内核多源归约语义一致。"""
        builtin_root, user_root = _dual_roots(tmp_path)
        _write_member(builtin_root, "shared", "plug", "dup_id", "1.0.0")
        _write_member(user_root, "a_dir", "plug", "dup_id", "2.0.0")
        _write_member(user_root, "b_dir", "plug", "dup_id", "1.5.0")

        by_manifest, by_dir = host._scan_plugin_dirs([builtin_root, user_root])

        assert by_manifest["dup_id"] == user_root / "a_dir" / "plug"
        assert by_dir["plug"] == user_root / "a_dir" / "plug"

    def test_tie_between_user_sources_keeps_first_found(self, tmp_path: Path) -> None:
        builtin_root, user_root = _dual_roots(tmp_path)
        _write_member(builtin_root, "shared", "plug", "dup_id", "1.0.0")
        _write_member(user_root, "a_dir", "plug", "dup_id", "2.0.0")
        _write_member(user_root, "b_dir", "plug", "dup_id", "2.0.0")

        by_manifest, _ = host._scan_plugin_dirs([builtin_root, user_root])

        assert by_manifest["dup_id"] == user_root / "a_dir" / "plug"

    @pytest.mark.parametrize(
        ("user_version", "case"),
        [(None, "missing"), ("garbage version", "unparseable")],
    )
    def test_user_version_degradation_warns(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        user_version: str | None,
        case: str,
    ) -> None:
        """用户副本因版本缺失/不可解析落败：fail-closed 内置 + 告警（该分裂
        形态此前完全静默，2026-10-15 事故链的配套可观测面）。"""
        del case  # 仅作用例标识
        builtin_root, user_root = _dual_roots(tmp_path)
        _write_member(builtin_root, "shared", "approval", "approval_service", "1.2.3")
        _write_member(user_root, "approval", "approval", "approval_service", user_version)

        with caplog.at_level("WARNING", logger="host"):
            by_manifest, _ = host._scan_plugin_dirs([builtin_root, user_root])

        assert by_manifest["approval_service"] == builtin_root / "shared" / "approval"
        assert any("approval_service" in rec.message for rec in caplog.records), (
            "用户副本版本降级落败必须告警且携带插件 id"
        )

    def test_no_warning_on_designed_arbitration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """按设计完成的版本裁决（用户版本可解析但更低/持平）不告警——告警
        只留给「版本缺失/不可解析」降级形态，避免正常对账刷屏。"""
        builtin_root, user_root = _dual_roots(tmp_path)
        _write_member(builtin_root, "shared", "approval", "approval_service", "1.2.3")
        _write_member(user_root, "approval", "approval", "approval_service", "1.2.2")

        with caplog.at_level("WARNING", logger="host"):
            host._scan_plugin_dirs([builtin_root, user_root])

        assert not [rec for rec in caplog.records if "approval_service" in rec.message]


class TestLoadMembersPicksBuiltinOverStaleUser:
    """事故形态端到端回归锁：_load_members 全链路（双根发现 → 裁决 → 加载）。

    用户根经环境变量解析（生产路径），内置副本健康、用户副本 import 即炸
    （复刻 stale 1.2.2 `from kernel_db import ...` 形态）——修复前用户副本被
    选中加载即 CohostError，修复后加载内置副本成功。"""

    def test_loads_builtin_copy_when_user_copy_is_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builtin_root, user_root = _dual_roots(tmp_path)
        healthy = _write_member(builtin_root, "shared", "approval", "approval_service", "1.2.3")
        stale = _write_member(user_root, "approval", "approval", "approval_service", "1.2.2")
        (stale / "server.py").write_text(
            "raise ModuleNotFoundError(\"No module named 'kernel_db'\")\n", encoding="utf-8"
        )
        monkeypatch.setenv("AGENTOS_USER_PLUGINS_DIR", str(user_root))

        members, failures = host._load_members(builtin_root, ["approval_service"])
        assert failures == []

        assert set(members) == {"approval_service"}
        assert members["approval_service"].name == "approval"
        assert healthy.is_dir()
        assert stale.is_dir()


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
    def test_main_serves_aggregated_members_then_hard_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务终局走 _graceful_hard_exit（os._exit(0) 不经解释器收尾），
        exit code 语义与清理顺序在 test_host_exit_cleanup 另测。"""
        root = tmp_path / "shared"
        _write_member(root, "system", "alpha", "alpha_id")
        served: list[tuple[Any, float, str | None]] = []
        hard_exits: list[int] = []

        async def _fake_serve(
            server: CohostServer,
            stall_secs: float = host._WATCHDOG_STALL_SECS_DEFAULT,
            *,
            host_name: str | None = None,
        ) -> None:
            served.append((server, stall_secs, host_name))

        def _fake_hard_exit() -> None:
            hard_exits.append(0)

        monkeypatch.setattr(host, "_serve", _fake_serve)
        monkeypatch.setattr(host, "_graceful_hard_exit", _fake_hard_exit)
        monkeypatch.delenv("AGENTOS_HOST_WATCHDOG_SECS", raising=False)

        host.main(
            ["--group", "light", "--slot", "2", "--members", "alpha_id"], shared_root=root
        )

        assert hard_exits == [0]
        assert len(served) == 1
        server, stall_secs, host_name = served[0]
        assert isinstance(server, CohostServer)
        assert server.tool_names == ["alpha_id.echo", "alpha_id.last_lifecycle"]
        assert stall_secs == host._WATCHDOG_STALL_SECS_DEFAULT
        assert host_name == "light:2", "宿主名随 spawn 参数进入 watchdog 取证头"
        err = capsys.readouterr().err
        assert "group=light" in err
        assert "members=1" in err
        assert "tools=2" in err
