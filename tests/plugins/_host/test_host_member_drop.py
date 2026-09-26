# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-test
"""合宿宿主单成员卸载单元测试：``_MemberLoader.drop_member`` 的摘除纪律。

契约（ADR 2026-09-24-member-granularity-unload-mode-panel-warmup）：
- drop 摘除被卸成员的唯一名模块（含 ``name.*`` 子模块）与其 owned 裸名表中
  **文件归属本成员目录**的驻留模块；
- 裸名槽位被他人模块驻留（载入次序的遮蔽恢复所致）时不摘——那是别人的
  活模块；他人 owned 登记不受影响；
- 未登记成员 drop = 0（no-op）；drop 后同一 loader 重载该成员 = 从磁盘
  新 exec（唯一名槽位已腾空，无旧代码对象复用）。
"""

from __future__ import annotations

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
        f"plugin = AgentOSPlugin({plugin_id!r})\n",
        encoding="utf-8",
    )
    return d


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


def _unique_module_name(plugin_id: str) -> str:
    import re

    return host._MEMBER_MODULE_PREFIX + re.sub(r"\W", "_", plugin_id)


def test_drop_removes_unique_module_and_owned_bare_names(tmp_path: Path) -> None:
    """drop 后：唯一名模块与该成员的裸名驻留模块腾空，owned 表清账。"""
    d = _write_member(tmp_path, "alpha", "alpha", "a1")
    loader = host._MemberLoader()
    loader.load("alpha", d)

    unique = _unique_module_name("alpha")
    assert unique in sys.modules, "前置：唯一名模块已驻留"
    assert loader.drop_member("alpha") > 0
    assert unique not in sys.modules, "唯一名模块必须被摘除"
    assert "marker" not in sys.modules and "plugin" not in sys.modules
    assert loader._owned.get("alpha") is None and loader._dirs.get("alpha") is None


def test_drop_keeps_other_members_resident_masked_slots(tmp_path: Path) -> None:
    """裸名碰撞遮蔽：后载成员的裸名模块被先载者驻留（遮蔽恢复），drop 后载
    成员不得动先载者的活模块；先载者 drop 才腾空。"""
    d_a = _write_member(tmp_path, "alpha", "alpha", "a1")
    d_b = _write_member(tmp_path, "beta", "beta", "b1")
    loader = host._MemberLoader()
    loader.load("alpha", d_a)
    loader.load("beta", d_b)

    resident = sys.modules.get("marker")
    assert resident is not None
    marker_file = getattr(resident, "__file__", "")

    removed = loader.drop_member("beta")
    assert "marker" in sys.modules, "beta 的裸名槽位由 alpha 驻留，drop beta 不得腾空"
    assert getattr(sys.modules.get("marker"), "__file__", "") == marker_file
    assert _unique_module_name("beta") not in sys.modules, "beta 唯一名模块必须摘除"
    assert loader._owned.get("beta") is None

    assert loader.drop_member("alpha") > 0
    assert "marker" not in sys.modules, "alpha drop 后其驻留的裸名模块腾空"
    assert removed >= 0


def test_drop_unknown_member_is_noop(tmp_path: Path) -> None:
    """未登记成员 drop = 0，sys.modules 不动。"""
    before = set(sys.modules)
    assert host._MemberLoader().drop_member("ghost") == 0
    assert set(sys.modules) == before


def test_drop_then_reload_execs_fresh_from_disk(tmp_path: Path) -> None:
    """drop 后同 loader 重载：唯一名槽位已腾空，重 exec 产出新模块对象。"""
    d = _write_member(tmp_path, "alpha", "alpha", "v1")
    loader = host._MemberLoader()
    first = loader.load("alpha", d)
    unique = _unique_module_name("alpha")
    first_module = sys.modules[unique]

    loader.drop_member("alpha")
    (d / "marker.py").write_text("MARKER = 'v2'\n", encoding="utf-8")
    second = loader.load("alpha", d)

    assert second is not first
    assert sys.modules[unique] is not first_module, "重载必须产出新代码对象"
