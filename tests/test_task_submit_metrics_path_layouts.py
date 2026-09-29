# @feature: FP-0.2.〇 管道引擎与插件执行模型(task_submit 指标路径布局) | @vision: V3 可嵌入 | @ci: python-coverage
# @ci: python-coverage
"""task_submit 指标配置坐标解析的三布局单测（tool._metrics_config_path）。

与 plugins/shared/tools/task_evaluate/test_metrics_path_layouts.py 互为对偶
（同一解析契约、同一装机根因）：用户空间平铺布局（<agentos>/plugins/
task_submit/tool.py）下原「向上 4 级」落到 <APPDATA>/config/...（不存在）→
指标表加载为空 → 提交期校验 fail-open 放行、评估期指标诚实失败。现契约 =
祖先逐级探测（首个含该 yaml 的祖先即根）+ 全 miss 回落向上 4 级，三布局
（仓库 / 装机树 / 用户平铺）单算法通吃。

布局经模块 __file__ 替身模拟（安装位置是外部环境条件），被测函数为真实实现。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"

_PROBE_PARTS = ("config", "plugins", "evaluation", "evaluation_metrics.yaml")

# 布局名 → (布局根目录名, 插件文件相对布局根路径)
_LAYOUTS = {
    "repo": ("repo", "plugins/shared/tools/task_submit/tool.py"),
    "installed_tree": ("resources", "plugins/shared/tools/task_submit/tool.py"),
    "user_flat": ("agentos", "plugins/task_submit/tool.py"),
}


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path():
    """模块级 sys.path 注入（teardown 恢复），同 test_task_submit_params 口径。"""
    added: list[str] = []
    for _p in (_SDK_DIR, _TASKS_DIR, _TOOL_DIR, _SYSTEM_DIR):
        s = str(_p)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    yield
    for s in added:
        sys.path.remove(s)


@pytest.fixture(scope="module")
def tool_module() -> Any:
    """用 importlib 以唯一模块名加载真实 tool.py（防平铺 tool 冲突）。"""
    spec = importlib.util.spec_from_file_location(
        "task_submit_metrics_path_test_mod", _TOOL_DIR / "tool.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_metrics_config_path_hits_each_layout(
    tool_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    root_seg, rel_plugin = _LAYOUTS[layout]
    root = tmp_path / root_seg
    plugin_file = root.joinpath(*Path(rel_plugin).parts)
    plugin_file.parent.mkdir(parents=True)
    expected = root.joinpath(*_PROBE_PARTS)
    expected.parent.mkdir(parents=True)
    expected.write_text("metrics: []\n", encoding="utf-8")
    monkeypatch.setattr(tool_module, "__file__", str(plugin_file))

    resolved = tool_module._metrics_config_path()

    assert Path(resolved) == expected
    assert Path(resolved).is_file()  # 性质：命中即真实存在


def test_metrics_config_path_falls_back_to_four_up_when_all_miss(
    tool_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """沿途任何祖先都无该 yaml → 回落历史行为：向上 4 级拼相对尾。"""
    plugin_file = tmp_path / "deep" / "nest" / "alone" / "tool.py"
    plugin_file.parent.mkdir(parents=True)
    monkeypatch.setattr(tool_module, "__file__", str(plugin_file))

    resolved = Path(tool_module._metrics_config_path())

    assert resolved == plugin_file.parent.parents[3].joinpath(*_PROBE_PARTS)
