# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""合宿宿主单成员热重载单元测试：``_MemberLoader.reload`` 的裸名遮蔽纪律。

契约（合宿成员粒度热重载的隔离不变量）：
- reload 单成员 = 摘除全部已登记裸名模块与文件归属本目录的运行期懒载
  模块 → 唯一名重 exec 该成员 → **恢复他人模块、排除自身旧模块**：静息态
  sys.modules 裸名槽位不残留被重载成员的旧代码对象，他人模块对象原样保留；
- reload 只更新目标成员的 owned 登记（成员间隔离不被破坏）；
- 编排入口 ``_build_reload_handler``：按成员 id 定位目录 → 遮蔽纪律重载，
  与首次装载共用同一 loader（owned 表连续性是遮蔽判定的前提）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

import host

pytestmark = pytest.mark.unit


def _write_member(root: Path, dirname: str, plugin_id: str, marker: str) -> Path:
    """落盘一个最小 sidecar 成员：server.py 暴露 plugin 实例 + 裸名 marker/plugin.py。"""
    d = root / "tools" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text('{"id": "%s"}' % plugin_id, encoding="utf-8")
    (d / "marker.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (d / "plugin.py").write_text(
        "from marker import MARKER\nVALUE = MARKER\n", encoding="utf-8"
    )
    (d / "server.py").write_text(
        "from plugin import VALUE\n"
        "from agentos_plugin_sdk import AgentOSPlugin\n"
        f"plugin = AgentOSPlugin({plugin_id!r})\n"
        "@plugin.tool(name='probe', schema={'type': 'object'})\n"
        "async def probe() -> dict:\n"
        "    return {'value': VALUE}\n",
        encoding="utf-8",
    )
    return d


def _module_file(module) -> str | None:
    f = getattr(module, "__file__", None)
    return os.path.normcase(os.path.abspath(f)) if f else None


@pytest.fixture()
def shared_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_interpreter_state():
    """裸名模块导入面是进程级全局态：每用例快照 sys.modules/sys.path，
    用后恢复——防同进程内其他用例的成员模块（指向已删 tmp 目录）串扰。
    """
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    yield
    sys.modules.clear()
    sys.modules.update(saved_modules)
    sys.path[:] = saved_path


def _owned_module(loader: host._MemberLoader, plugin_id: str, name: str):
    return loader._owned[plugin_id][name]


def _boot_two_members(shared_root: Path) -> tuple[host._MemberLoader, Path, Path]:
    dir_a = _write_member(shared_root, "a_service", "a_service", "A1")
    dir_b = _write_member(shared_root, "b_service", "b_service", "B1")
    loader = host._MemberLoader()
    loader.load("a_service", dir_a)
    loader.load("b_service", dir_b)
    return loader, dir_a, dir_b


class TestMemberLoaderReload:
    """reload 的裸名遮蔽不变量（平铺裸名隔离，host.py 模块 docstring 约束）。"""

    def test_reload_later_member_keeps_first_member_resident(
        self, shared_root: Path
    ) -> None:
        """reload 第二成员：首个成员的裸名静息态驻留不被顶掉。"""
        loader, _dir_a, dir_b = _boot_two_members(shared_root)
        resident = sys.modules["plugin"]
        assert _module_file(resident) == _module_file(
            _owned_module(loader, "a_service", "plugin")
        )

        old_b_plugin_mod = _owned_module(loader, "b_service", "plugin")
        plugin_b2 = loader.reload("b_service", dir_b)
        assert plugin_b2 is not None

        # 静息态不变量：裸名槽位仍是首个成员 a 的模块
        assert sys.modules["plugin"] is resident
        # b 的登记表换成新 exec 的新模块对象（同名不同对象，代码取自 b 目录）
        new_b_plugin_mod = _owned_module(loader, "b_service", "plugin")
        assert new_b_plugin_mod is not old_b_plugin_mod
        assert new_b_plugin_mod.VALUE == "B1"
        # a 的登记不受影响
        assert _owned_module(loader, "a_service", "plugin") is resident

    def test_reload_first_member_refreshes_resident_slots(
        self, shared_root: Path
    ) -> None:
        """reload 首成员：静息态裸名槽位刷新为它的新模块，他人模块原样恢复。"""
        loader, dir_a, _dir_b = _boot_two_members(shared_root)

        old_a_plugin_mod = _owned_module(loader, "a_service", "plugin")
        b_plugin_mod_before = _owned_module(loader, "b_service", "plugin")
        b_marker_mod_before = _owned_module(loader, "b_service", "marker")

        loader.reload("a_service", dir_a)

        # 首成员 reload 后：裸名槽位 = a 的新模块（旧对象不再占槽）
        fresh = sys.modules["plugin"]
        assert fresh is not old_a_plugin_mod
        assert _owned_module(loader, "a_service", "plugin") is fresh
        # 他人（b）模块对象原样保留（登记与 sys.modules 双面）
        assert _owned_module(loader, "b_service", "plugin") is b_plugin_mod_before
        assert _owned_module(loader, "b_service", "marker") is b_marker_mod_before

    def test_reload_updates_owned_table_only_for_target(
        self, shared_root: Path
    ) -> None:
        """reload 只更新目标成员的 owned 登记表，他人登记原样。"""
        loader, _dir_a, dir_b = _boot_two_members(shared_root)
        a_owned_before = dict(loader._owned["a_service"])

        loader.reload("b_service", dir_b)

        assert loader._owned["a_service"] == a_owned_before


class TestReloadMemberOrchestration:
    """_build_reload_handler：定位成员目录 → 遮蔽纪律重载（与首载同 loader）。"""

    async def test_handler_reloads_and_shares_loader_owned_table(
        self, shared_root: Path
    ) -> None:
        _write_member(shared_root, "a_service", "a_service", "A1")
        _write_member(shared_root, "b_service", "b_service", "B1")
        loader = host._MemberLoader()
        members, _ = host._load_members(shared_root, ["a_service", "b_service"], loader=loader)
        assert set(members) == {"a_service", "b_service"}

        handler = host._build_reload_handler(shared_root, loader)
        plugin_a2 = await handler("a_service")

        assert plugin_a2 is not None
        # 同一 loader：reload 后 owned 表连续（a 为新 exec，b 原样）
        assert "a_service" in loader._owned and "b_service" in loader._owned

    async def test_handler_unknown_member_raises(self, shared_root: Path) -> None:
        _write_member(shared_root, "a_service", "a_service", "A1")
        loader = host._MemberLoader()
        host._load_members(shared_root, ["a_service"], loader=loader)
        handler = host._build_reload_handler(shared_root, loader)

        with pytest.raises(host.CohostError):
            await handler("ghost")


def _write_lazy_member(root: Path, dirname: str, plugin_id: str, marker: str) -> Path:
    """落盘一个运行期懒加载形态成员：helper.py 仅在工具函数内 import。

    复刻 multimodal asr / scene routes_scene / task_service storage 三处
    真实形态：函数级懒加载自身目录裸名模块，顶层不 import（owned 表因此
    不含 helper——修复前 reload 不刷新它）。
    """
    d = root / "tools" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(f'{{"id": "{plugin_id}"}}', encoding="utf-8")
    (d / "helper.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (d / "server.py").write_text(
        "from agentos_plugin_sdk import AgentOSPlugin\n"
        f"plugin = AgentOSPlugin({plugin_id!r})\n"
        "@plugin.tool(name='probe', schema={'type': 'object'})\n"
        "async def probe() -> dict:\n"
        "    import helper\n"
        "    return {'value': helper.MARKER}\n",
        encoding="utf-8",
    )
    return d


def _call_probe(plugin_id: str) -> dict:
    """经唯一名模块调用成员的 probe 工具（返回懒加载 helper 的当前值）。"""
    member_mod = sys.modules[host._MEMBER_MODULE_PREFIX + plugin_id]
    return asyncio.run(member_mod.probe())


class TestReloadRefreshesRuntimeLazyModules:
    """reload 必须刷新运行期懒加载模块（审查总册 P1-19 / I2）。

    根因：owned 表只在成员 exec 期间收集，函数级懒加载的模块运行期才
    import、不进 owned——reload 不摘除则 sys.modules 旧缓存让懒加载点
    持续跑旧代码，须整组重启才刷新。修复后按文件归属摘除未登记成员模块。
    """

    def test_lazy_module_code_takes_effect_after_reload(self, shared_root: Path) -> None:
        """re-register 场景红→绿：成员热重载后懒加载代码生效。

        新旧 MARKER 值长度必须不同：pyc 校验是（秒级 mtime, size）二元组
        （Python 全局语义，与合宿机制无关），同秒同长度写入会让重新 import
        命中旧字节码——size 差异使 pyc 必然失效，本测试靶是 sys.modules
        缓存刷新而非 pyc 失效语义。
        """
        dir_m = _write_lazy_member(shared_root, "m_service", "m_service", "V1")
        loader = host._MemberLoader()
        loader.load("m_service", dir_m)
        assert _call_probe("m_service") == {"value": "V1"}

        # 成员代码变更（helper.py 磁盘更新）→ 热重载 → 新代码生效
        (dir_m / "helper.py").write_text("MARKER = 'V2-longer'\n", encoding="utf-8")
        loader.reload("m_service", dir_m)

        assert _call_probe("m_service") == {"value": "V2-longer"}

    def test_reload_does_not_reserve_stale_lazy_module_slot(self, shared_root: Path) -> None:
        """reload 后静息态不残留旧懒载模块：旧 helper 不再驻留 sys.modules
        （与已登记裸名槽位的刷新语义一致，下次运行期 import 从磁盘新读）。"""
        dir_m = _write_lazy_member(shared_root, "m_service", "m_service", "V1")
        loader = host._MemberLoader()
        loader.load("m_service", dir_m)
        assert _call_probe("m_service") == {"value": "V1"}
        stale_helper = sys.modules["helper"]

        (dir_m / "helper.py").write_text("MARKER = 'V2-longer'\n", encoding="utf-8")
        loader.reload("m_service", dir_m)

        assert "helper" not in sys.modules or sys.modules["helper"] is not stale_helper

    def test_reload_member_leaves_other_member_lazy_modules_resident(
        self, shared_root: Path
    ) -> None:
        """摘除按文件归属限定被重载成员目录：他人成员的运行期懒载模块原样
        驻留（a 懒载 helper_a 后 reload b，a 的 helper_a 不被顶掉）。"""
        dir_a = _write_lazy_member(shared_root, "a_service", "a_service", "A1")
        dir_b = _write_member(shared_root, "b_service", "b_service", "B1")
        loader = host._MemberLoader()
        loader.load("a_service", dir_a)
        assert _call_probe("a_service") == {"value": "A1"}  # 触发 a 的运行期懒载
        helper_a = sys.modules["helper"]

        loader.load("b_service", dir_b)
        loader.reload("b_service", dir_b)

        assert sys.modules["helper"] is helper_a
        assert _call_probe("a_service") == {"value": "A1"}
