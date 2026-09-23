# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""bootstrap_plugin 单测——sys.path 引导的布局指纹/注入序/幂等/裸名安全边界。

覆盖（ADR 2026-09-08-plugin-bootstrap-sink 决策 1）：
- 标准三层布局与深层嵌套（tools/external_mcp/godot_mcp 形态）同一共享根判定；
- 顶级插件（plugins/shared/<name>）父级即命中；
- 指纹缺失回退三级上溯；
- user_root 扁平单插件副本（BUG-56）：经 SDK 自身安装位置锚回仓库共享根；
- 注入终态序 [shared_root, plugin_dir]、幂等、group_root 只返回不注入；
- 真实仓库布局冒烟（真实依赖路径，repo 缺席时跳过）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import agentos_plugin_sdk.bootstrap as bootstrap_module
from agentos_plugin_sdk.bootstrap import bootstrap_plugin, find_shared_root


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


# ---- BUG-56：user_root 扁平单插件副本的共享根解析 ----


def _make_flat_user_root(tmp_path: Path) -> Path:
    """user_root 扁平副本布局：<root>/user_root/plugins/<name>/（祖先无指纹）。"""
    plugin_dir = tmp_path / "user_root" / "plugins" / "context_build"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "server.py").write_text("# stub\n", encoding="utf-8")
    return plugin_dir


def _anchor_sdk_to_fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 SDK 锚钉到 tmp 仿仓库布局（hermetic，不依赖运行宿主仓库），返回仿共享根。

    仿仓库：repo/plugins/sdk/src/agentos_plugin_sdk/bootstrap.py（SDK 安装位）
    + repo/plugins/shared/{system,tools,pipeline}（指纹齐备的共享根）。
    """
    fake_pkg = tmp_path / "repo" / "plugins" / "sdk" / "src" / "agentos_plugin_sdk"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(bootstrap_module, "__file__", str(fake_pkg / "bootstrap.py"))
    shared = tmp_path / "repo" / "plugins" / "shared"
    for group in ("system", "tools", "pipeline"):
        (shared / group).mkdir(parents=True, exist_ok=True)
    return shared


def test_bootstrap_flat_copy_resolves_via_sdk_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扁平单插件副本（plugins/<名>，祖先无指纹）：SDK 位置锚解析回仓库共享根。"""
    shared = _anchor_sdk_to_fake_repo(tmp_path, monkeypatch)
    plugin_dir = _make_flat_user_root(tmp_path)

    result = bootstrap_plugin(plugin_dir / "server.py")

    assert result.shared_root == str(shared)
    assert result.plugin_dir == str(plugin_dir)
    assert sys.path[:2] == [str(shared), str(plugin_dir)]


def test_bootstrap_flat_copies_grouped_and_flat_shapes_same_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多副本两形态（plugins/<名> 与 plugins/<分类>/<名>）解析到同一共享根。"""
    shared = _anchor_sdk_to_fake_repo(tmp_path, monkeypatch)
    flat = tmp_path / "user_root" / "plugins" / "context_build"
    flat.mkdir(parents=True)
    (flat / "server.py").write_text("# stub\n", encoding="utf-8")
    grouped = tmp_path / "user_root" / "plugins" / "modes" / "mode_coding"
    grouped.mkdir(parents=True)
    (grouped / "server.py").write_text("# stub\n", encoding="utf-8")

    for plugin_dir in (flat, grouped):
        result = bootstrap_plugin(plugin_dir / "server.py")
        assert result.shared_root == str(shared)

    assert sys.path.count(str(shared)) == 1


def test_bootstrap_flat_copy_nonrepo_sdk_keeps_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK 非仓库安装（site-packages 平拷贝）：锚不可用，保持旧上三级回退。"""
    fake_pkg = tmp_path / "venv" / "Lib" / "site-packages" / "agentos_plugin_sdk"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(bootstrap_module, "__file__", str(fake_pkg / "bootstrap.py"))
    plugin_dir = _make_flat_user_root(tmp_path)

    result = bootstrap_plugin(plugin_dir / "server.py")

    # 锚失效（site-packages 上溯无指纹）→ 旧回退：插件目录上三级
    assert result.shared_root == str(tmp_path)
    assert sys.path[:2] == [str(tmp_path), str(plugin_dir)]


def test_find_shared_root_exposes_single_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find_shared_root 公开单点：与 bootstrap_plugin 同一判定（fs_tools 复用面）。"""
    shared = _anchor_sdk_to_fake_repo(tmp_path, monkeypatch)
    plugin_dir = _make_flat_user_root(tmp_path)

    assert find_shared_root(plugin_dir / "server.py") == str(shared)
    # 深层文件（包内模块 __file__ 形态）同样解析
    pkg_file = plugin_dir / "src" / "pkg_inner" / "mod.py"
    pkg_file.parent.mkdir(parents=True)
    pkg_file.write_text("# stub\n", encoding="utf-8")
    assert find_shared_root(pkg_file) == str(shared)


def test_bootstrap_flat_copy_real_user_root_smoke() -> None:
    """真实 user_root 副本冒烟（R192 现场）：锚定真实仓库共享根。"""
    repo_root = Path(__file__).resolve().parents[3]
    copy_server = repo_root / "user_root" / "plugins" / "context_build" / "server.py"
    if not copy_server.is_file():
        pytest.skip("user_root 扁平副本不在位（无用户空间环境）")

    result = bootstrap_plugin(copy_server)

    assert Path(result.shared_root) == repo_root / "plugins" / "shared"
    assert os.path.isdir(os.path.join(result.shared_root, "system"))
