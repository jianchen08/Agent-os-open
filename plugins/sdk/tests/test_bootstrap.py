# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""bootstrap_plugin 单测——sys.path 引导的布局指纹/注入序/幂等/裸名安全边界。

覆盖（ADR 2026-09-08-plugin-bootstrap-sink 决策 1）：
- 标准三层布局与深层嵌套（tools/external_mcp/godot_mcp 形态）同一共享根判定；
- 顶级插件（plugins/shared/<name>）父级即命中；
- 指纹缺失回退三级上溯；
- 注入终态序 [shared_root, plugin_dir]、幂等、group_root 只返回不注入；
- 真实仓库布局冒烟（真实依赖路径，repo 缺席时跳过）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentos_plugin_sdk.bootstrap import bootstrap_plugin


@pytest.fixture(autouse=True)
def _restore_sys_path() -> None:  # type: ignore[misc]
    """每用例前后快照/恢复 sys.path——引导是进程级副作用，不得泄漏到相邻用例。"""
    snapshot = list(sys.path)
    yield  # type: ignore[misc]
    sys.path[:] = snapshot


def _make_plugin(root: Path, rel: str) -> Path:
    """在 root 下造 <rel>/server.py 布局（含 0.2 三分组指纹目录），返回插件目录。"""
    for group in ("system", "tools", "pipeline"):
        (root / group).mkdir(parents=True, exist_ok=True)
    plugin_dir = root / rel
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "server.py").write_text("# stub\n", encoding="utf-8")
    return plugin_dir


@pytest.mark.parametrize(
    "rel",
    ["pipeline/input/track", "pipeline/output/tool_cache_writer", "tools/search", "system/workspace"],
    ids=["pipeline-input", "pipeline-output", "tools", "system"],
)
def test_bootstrap_standard_layouts(tmp_path: Path, rel: str) -> None:
    """标准布局四组输入：三键正确 + 终态序 [shared, plugin] + 仅两项新增。"""
    plugin_dir = _make_plugin(tmp_path, rel)
    sys.path.insert(0, str(plugin_dir / "preexisting"))  # 既有条目必须保持在注入项之后

    result = bootstrap_plugin(plugin_dir / "server.py")

    assert result.plugin_dir == str(plugin_dir)
    assert result.group_root == str(plugin_dir.parent)
    assert result.shared_root == str(tmp_path)
    # 字面值（注入位置）+ 性质（终态序与仅两项新增）
    assert sys.path[:2] == [str(tmp_path), str(plugin_dir)]
    added = set(sys.path) - set(sys.path[2:])
    assert added == {str(tmp_path), str(plugin_dir)}


def test_bootstrap_deep_nested_layout_same_root(tmp_path: Path) -> None:
    """godot_mcp 深层嵌套：指纹判定与目录深度解耦，命中同一共享根。"""
    plugin_dir = _make_plugin(tmp_path, "tools/external_mcp/godot_mcp")

    result = bootstrap_plugin(plugin_dir / "server.py")

    assert result.shared_root == str(tmp_path)
    assert sys.path[0] == str(tmp_path)


def test_bootstrap_top_level_plugin_parent_is_shared_root(tmp_path: Path) -> None:
    """顶级插件（plugins/shared/db_admin 形态）：父级即共享根，立即命中。"""
    plugin_dir = _make_plugin(tmp_path, "db_admin")

    result = bootstrap_plugin(plugin_dir / "server.py")

    assert result.shared_root == str(tmp_path)
    assert result.group_root == str(tmp_path)


def test_bootstrap_fingerprint_missing_falls_back_three_up(tmp_path: Path) -> None:
    """非 0.2 布局（裸 a/b/c）：回退插件目录上三级，仍完成两项注入。"""
    plugin_dir = tmp_path / "a" / "b" / "c"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "server.py").write_text("# stub\n", encoding="utf-8")

    result = bootstrap_plugin(str(plugin_dir / "server.py"))

    # 上三级：c→b→a→tmp_path（标准 {system,tools,pipeline}/<插件> 深度对齐）
    assert result.shared_root == str(tmp_path)
    assert sys.path[:2] == [str(tmp_path), str(plugin_dir)]


def test_bootstrap_idempotent_no_duplicates(tmp_path: Path) -> None:
    """幂等：连调两次（合宿 re-exec 语境），sys.path 无重复条目。"""
    plugin_dir = _make_plugin(tmp_path, "system/scene")

    bootstrap_plugin(plugin_dir / "server.py")
    first = list(sys.path)
    bootstrap_plugin(plugin_dir / "server.py")

    assert sys.path == first
    assert sys.path.count(str(plugin_dir)) == 1
    assert sys.path.count(str(tmp_path)) == 1


def test_bootstrap_promotes_stale_entries_to_front(tmp_path: Path) -> None:
    """驻留但被挤后的条目：引导后重申解析优先（车道共跑语境回归）。

    车道进程里本插件目录与共享根可能已在 sys.path 深处、且兄弟插件目录
    占据前位——只判在场会让 ``from plugin import`` 解析到他人同名实现。
    """
    plugin_dir = _make_plugin(tmp_path, "pipeline/input/environment_lifecycle")
    other_dir = tmp_path / "pipeline" / "output" / "other"
    other_dir.mkdir(parents=True, exist_ok=True)
    # 污染现场：两项已在 sys.path 深处，他插件目录占 sys.path[0]
    sys.path.append(str(plugin_dir))
    sys.path.append(str(tmp_path))
    sys.path.insert(0, str(other_dir))

    bootstrap_plugin(plugin_dir / "server.py")

    # 字面值（终态前两位）+ 性质（去重，不重复条目）
    assert sys.path[:2] == [str(tmp_path), str(plugin_dir)]
    assert sys.path.count(str(plugin_dir)) == 1
    assert sys.path.count(str(tmp_path)) == 1


def test_bootstrap_group_root_returned_but_not_injected(tmp_path: Path) -> None:
    """裸名安全边界：组根只返回不注入——兄弟插件裸名不得因此跨插件可达。"""
    plugin_dir = _make_plugin(tmp_path, "pipeline/input/prompt_build")

    result = bootstrap_plugin(plugin_dir / "server.py")

    assert result.group_root == str(plugin_dir.parent)
    assert result.group_root not in sys.path


def test_bootstrap_accepts_str_and_pathlike(tmp_path: Path) -> None:
    """entry_file 契约：str 与 Path 等价。"""
    plugin_dir = _make_plugin(tmp_path, "tools/task")

    via_path = bootstrap_plugin(plugin_dir / "server.py")
    sys.path[:] = [p for p in sys.path if p not in (str(plugin_dir), str(tmp_path))]
    via_str = bootstrap_plugin(str(plugin_dir / "server.py"))

    assert via_path == via_str


def test_bootstrap_real_repo_layout() -> None:
    """真实仓库布局冒烟：指纹步行在真实 plugins/shared 上命中共享根。"""
    repo_root = Path(__file__).resolve().parents[3]
    track_server = repo_root / "plugins" / "shared" / "pipeline" / "output" / "track" / "server.py"
    if not track_server.is_file():
        pytest.skip("仓库布局不在位（独立 SDK 安装环境）")

    result = bootstrap_plugin(track_server)

    assert Path(result.shared_root).name == "shared"
    assert Path(result.shared_root) == repo_root / "plugins" / "shared"
    assert os.path.isdir(os.path.join(result.shared_root, "system"))
    # 成员性（位置序由受控 tmp_path 用例断言——真实会话引导会把两项置前）
    assert result.shared_root in sys.path
    assert result.plugin_dir in sys.path


def test_bootstrap_extra_injects_shared_root_subpaths(tmp_path: Path) -> None:
    """extra 契约：共享根相对子路径按序注入（终态序与调用序相反，目录缺失跳过）。"""
    plugin_dir = _make_plugin(tmp_path, "tools/task")
    (tmp_path / "system" / "tasks").mkdir(parents=True, exist_ok=True)  # extra 目标 1
    # "system/missing" 不创建 → 非目录跳过

    result = bootstrap_plugin(
        plugin_dir / "server.py",
        extra=(os.path.join("system", "tasks"), "system", "system/missing"),
    )

    tasks_dir = str(tmp_path / "system" / "tasks")
    system_dir = str(tmp_path / "system")
    assert tasks_dir in sys.path
    assert system_dir in sys.path
    # 逐条 insert(0)：后注入的更靠前（system → tasks → shared_root → plugin_dir）
    assert sys.path.index(system_dir) < sys.path.index(tasks_dir) < sys.path.index(result.shared_root)


def test_bootstrap_extra_idempotent(tmp_path: Path) -> None:
    """extra 幂等：重复引导不产生重复条目。"""
    plugin_dir = _make_plugin(tmp_path, "tools/task")
    (tmp_path / "system" / "tasks").mkdir(parents=True, exist_ok=True)
    extra = (os.path.join("system", "tasks"), "system")

    first = bootstrap_plugin(plugin_dir / "server.py", extra=extra)
    sys.path[:] = [p for p in sys.path if p not in (str(plugin_dir), str(tmp_path))]
    second = bootstrap_plugin(plugin_dir / "server.py", extra=extra)

    assert first == second
    tasks_dir = str(tmp_path / "system" / "tasks")
    assert sys.path.count(tasks_dir) == 1
