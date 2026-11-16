# @feature: task_submit 编排模块自定位加载 | @ci: python-coverage
"""task_submit → orchestration 懒加载布局适应性测试。

orchestration.py 是 tasks 域平铺模块，位于 task_submit 插件目录的同级 tasks
子目录；tasks 域 server.py 的 bootstrap 只覆盖其自身装载语境，本 sidecar
运行期裸名 import 不保证命中。_load_orchestration_module 的契约：

1. 平铺布局（sys.path 无任何 tasks 目录）：按同级 tasks 候选定位加载成功；
2. dev 布局（tasks 目录已在 sys.path）：裸名 import 直接命中真实模块；
3. 候选全缺：fail-honest 抛错，错误信息携带全部候选路径清单；
4. 候选存在但模块执行失败：原错误上抛且 sys.modules 不留半初始化模块；
5. 幂等：重复调用返回同一模块对象（sys.modules 命中，不重复执行）。

布局用 tmp 目录镜像真实文件模拟（零 mock，真实依赖：tool.py / 平铺模块 /
orchestration.py 全部为仓内真源副本）。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._stdlib_guard import ensure_stdlib_module

pytestmark = pytest.mark.unit

# 车道共跑防线：tool.py 模块级 `from types import SimpleNamespace` 会被插件
# 同名裸模块劫持破坏，装载前恢复 stdlib 绑定（同 test_task_submit_params）。
ensure_stdlib_module("types")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TS_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "tasks"
#: tool.py 模块级依赖的共享根平铺模块（镜像 tmp 布局时随行复制）
_SHARED_FLAT_MODULES = ("state_fields.py", "mode_keys.py", "task_birth.py", "time_iso.py")

#: 本测试接触的裸名模块（代际还原面）
_BARE_NAMES = ("orchestration", "state_fields", "mode_keys", "task_birth", "time_iso")


def _load_tool_module(tool_path: Path, mod_name: str) -> Any:
    """以唯一模块名加载指定 tool.py（进程内缓存，与既有 task_submit 测试同款）。"""
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, tool_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


def _make_mirror(tmp_path: Path, *, with_tasks: bool, orchestration_body: str | None = None) -> Path:
    """镜像仓内真实布局到 tmp：shared/{平铺模块, task_submit/tool.py} [+ tasks/orchestration.py]。

    返回 tmp 共享根。tool.py / 平铺模块 / orchestration.py 均为仓内真源副本
    （orchestration_body 给定时以该内容替代——坏模块形态用）。
    """
    shared_root = tmp_path / "shared"
    (shared_root / "task_submit").mkdir(parents=True)
    for name in _SHARED_FLAT_MODULES:
        shutil.copy2(_REPO_ROOT / "plugins" / "shared" / name, shared_root / name)
    shutil.copy2(_TS_DIR / "tool.py", shared_root / "task_submit" / "tool.py")
    if with_tasks:
        (shared_root / "tasks").mkdir()
        if orchestration_body is None:
            shutil.copy2(_TASKS_DIR / "orchestration.py", shared_root / "tasks" / "orchestration.py")
        else:
            (shared_root / "tasks" / "orchestration.py").write_text(
                orchestration_body, encoding="utf-8"
            )
    return shared_root


def _purge_bare_names() -> None:
    """逐出全部相关裸名（后续导入按当时 sys.path 重新解析）。"""
    for name in _BARE_NAMES:
        sys.modules.pop(name, None)


def _tasks_dirs_on_sys_path() -> list[str]:
    """sys.path 上存在 orchestration.py 的目录（平铺环境模拟的清除面）。"""
    hits = []
    for entry in sys.path:
        try:
            if (Path(entry) / "orchestration.py").is_file():
                hits.append(entry)
        except OSError:
            continue
    return hits


@pytest.fixture(autouse=True)
def _restore_sys_state():
    """sys.path / sys.modules 代际还原：本测试对全局导入态的全部扰动可回退。"""
    path_snapshot = list(sys.path)
    mods_snapshot = {
        name: sys.modules[name]
        for name in (*_BARE_NAMES, "task_submit_lazy_flat", "task_submit_lazy_dev", "task_submit_lazy_broken")
        if name in sys.modules
    }
    _purge_bare_names()
    yield
    sys.path[:] = path_snapshot
    for name in (*_BARE_NAMES, "task_submit_lazy_flat", "task_submit_lazy_dev", "task_submit_lazy_broken"):
        sys.modules.pop(name, None)
    sys.modules.update(mods_snapshot)


# ── 1. 平铺布局（装机形态）：sys.path 无 tasks 目录，同级候选定位加载 ──


class TestFlatLayout:
    def test_loads_via_sibling_candidate_without_sys_path(self, tmp_path: Path) -> None:
        """平铺环境：裸名不可达 → 同级 tasks 候选显式定位加载成功。"""
        shared_root = _make_mirror(tmp_path, with_tasks=True)
        tool = _load_tool_module(shared_root / "task_submit" / "tool.py", "task_submit_lazy_flat")

        for entry in _tasks_dirs_on_sys_path():
            sys.path.remove(entry)

        loaded = tool._load_orchestration_module()

        expected = shared_root / "tasks" / "orchestration.py"
        assert Path(loaded.__file__).resolve() == expected.resolve()
        # 性质断言：加载到的是功能完备的编排模块（非空壳）
        assert loaded.AUTONOMOUS_ORCHESTRATION_KEY == "autonomous"
        assert callable(loaded.discover_orchestrations)
        assert hasattr(loaded, "OrchestrationError")
        # 副作用契约：已注册 sys.modules，后续裸名导入命中同一对象
        assert sys.modules["orchestration"] is loaded

    def test_second_call_is_same_module_object(self, tmp_path: Path) -> None:
        """幂等性质：二次调用返回同一模块对象（不重复执行模块体）。"""
        shared_root = _make_mirror(tmp_path, with_tasks=True)
        tool = _load_tool_module(shared_root / "task_submit" / "tool.py", "task_submit_lazy_flat")
        for entry in _tasks_dirs_on_sys_path():
            sys.path.remove(entry)
        first = tool._load_orchestration_module()
        second = tool._load_orchestration_module()
        assert first is second


# ── 2. dev 布局：tasks 目录在 sys.path，裸名 import 直接命中 ──


class TestDevLayout:
    def test_bare_import_hits_real_repo_module(self) -> None:
        """dev 环境（tasks 目录已注入）：走裸名快路径，返回仓内真实编排模块。"""
        if str(_SDK_DIR) not in sys.path:
            sys.path.insert(0, str(_SDK_DIR))
        if str(_TASKS_DIR) not in sys.path:
            sys.path.insert(0, str(_TASKS_DIR))
        tool = _load_tool_module(_TS_DIR / "tool.py", "task_submit_lazy_dev")

        loaded = tool._load_orchestration_module()

        assert Path(loaded.__file__).resolve() == (_TASKS_DIR / "orchestration.py").resolve()
        assert loaded.AUTONOMOUS_ORCHESTRATION_KEY == "autonomous"
        assert callable(loaded.resolve_orchestration)


# ── 3. 候选全缺：fail-honest 报错带路径清单 ──


class TestAllCandidatesMissing:
    def test_missing_everywhere_raises_with_candidate_list(self, tmp_path: Path) -> None:
        """平铺环境且 tasks 目录不存在：RuntimeError 信息携带全部候选路径。"""
        shared_root = _make_mirror(tmp_path, with_tasks=False)
        tool = _load_tool_module(shared_root / "task_submit" / "tool.py", "task_submit_lazy_flat")
        for entry in _tasks_dirs_on_sys_path():
            sys.path.remove(entry)

        with pytest.raises(RuntimeError) as exc_info:
            tool._load_orchestration_module()

        message = str(exc_info.value)
        assert "orchestration" in message
        expected_candidate = shared_root / "tasks" / "orchestration.py"
        assert str(expected_candidate) in message
        # 性质：清单完整——每个已知候选都被点名
        for candidate in tool._orchestration_candidate_paths():
            assert str(candidate) in message


# ── 4. 候选存在但模块体执行失败：原错误上抛 + sys.modules 无残留 ──


class TestBrokenCandidate:
    def test_exec_failure_propagates_and_leaves_no_partial_module(self, tmp_path: Path) -> None:
        """候选文件存在但执行炸（坏模块形态）：原错误原样上抛，sys.modules 不留毒。"""
        shared_root = _make_mirror(
            tmp_path, with_tasks=True, orchestration_body="raise RuntimeError('boom-broken-module')\n"
        )
        tool = _load_tool_module(shared_root / "task_submit" / "tool.py", "task_submit_lazy_broken")
        for entry in _tasks_dirs_on_sys_path():
            sys.path.remove(entry)

        with pytest.raises(RuntimeError, match="boom-broken-module"):
            tool._load_orchestration_module()

        assert "orchestration" not in sys.modules
