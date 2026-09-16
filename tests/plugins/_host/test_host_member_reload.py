# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""合宿宿主单成员热重载单元测试：``_MemberLoader.reload`` 的裸名遮蔽纪律。

契约（合宿成员粒度热重载的隔离不变量）：
- reload 单成员 = 摘除全部已登记裸名模块 → 唯一名重 exec 该成员 →
  **恢复他人模块、排除自身旧模块**：静息态 sys.modules 裸名槽位不残留
  被重载成员的旧代码对象，他人模块对象原样保留；
- reload 只更新目标成员的 owned 登记（成员间隔离不被破坏）；
- 编排入口 ``_build_reload_handler``：按成员 id 定位目录 → 遮蔽纪律重载，
  与首次装载共用同一 loader（owned 表连续性是遮蔽判定的前提）。
"""

from __future__ import annotations

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
        members = host._load_members(shared_root, ["a_service", "b_service"], loader=loader)
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
