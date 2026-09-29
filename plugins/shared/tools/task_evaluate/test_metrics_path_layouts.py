# @feature: FP-0.2.〇 管道引擎与插件执行模型(评估指标路径三布局) | @vision: V3 可嵌入 | @ci: python-coverage
# @ci: python-coverage
"""指标配置坐标解析的三布局单测（_executor.default_metrics_path）。

背景（2026-09-29 装机实证）：装机用户空间为平铺布局
（<agentos>/plugins/task_evaluate/_executor.py），原「向上 4 级拼
config/plugins/evaluation/evaluation_metrics.yaml」在平铺布局下落到
<APPDATA>/config/...（不存在）→ load 空表 → 所有 tool 型指标
「未在 evaluation_metrics.yaml 定义」诚实失败（工作已完成的任务 0/1）。
现契约 = 祖先逐级探测，首个含该 yaml 的祖先即根，三布局单算法通吃：

- 仓库布局：plugins/shared/tools/task_evaluate → 仓库根（4 级）；
- 装机树布局：resources/plugins/shared/tools/task_evaluate → resources（4 级）；
- 用户平铺布局：plugins/task_evaluate → agentos 根（2 级）。

外加「全 miss 回落向上 4 级」性质断言（历史行为兼容，调用方总拿到字符串）。
布局经模块 __file__ 替身模拟（安装位置是外部环境条件，与 tmp 文件系统 mock
同级），被测函数为真实实现。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[1] / "system" / "tasks"

for _d in (_TE_DIR, _TASKS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

_PROBE_PARTS = ("config", "plugins", "evaluation", "evaluation_metrics.yaml")

# 布局名 → (布局根目录名, 插件文件相对布局根路径)
_LAYOUTS = {
    "repo": ("repo", "plugins/shared/tools/task_evaluate/_executor.py"),
    "installed_tree": ("resources", "plugins/shared/tools/task_evaluate/_executor.py"),
    "user_flat": ("agentos", "plugins/task_evaluate/_executor.py"),
}


def _load_executor_module() -> Any:
    """按唯一模块名加载真实 _executor.py（防与其它平铺测试互相污染）。"""
    name = "task_eval_metrics_path_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TE_DIR / "_executor.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exec_mod() -> Any:
    return _load_executor_module()


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_metrics_path_hits_each_layout(
    exec_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    root_seg, rel_plugin = _LAYOUTS[layout]
    root = tmp_path / root_seg
    plugin_file = root.joinpath(*Path(rel_plugin).parts)
    plugin_file.parent.mkdir(parents=True)
    expected = root.joinpath(*_PROBE_PARTS)
    expected.parent.mkdir(parents=True)
    expected.write_text("metrics: []\n", encoding="utf-8")
    monkeypatch.setattr(exec_mod, "__file__", str(plugin_file))

    resolved = Path(exec_mod.default_metrics_path())

    assert resolved == expected
    assert resolved.is_file()  # 性质：命中即真实存在，不是拼出的幽灵路径
    # 类静态入口与模块函数同源
    assert Path(exec_mod.PipelineEvaluationExecutor._default_metrics_path()) == expected


def test_metrics_path_falls_back_to_four_up_when_all_miss(
    exec_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """沿途任何祖先都无该 yaml → 回落历史行为：向上 4 级拼相对尾（无论存在与否）。"""
    plugin_file = tmp_path / "deep" / "nest" / "alone" / "_executor.py"
    plugin_file.parent.mkdir(parents=True)
    monkeypatch.setattr(exec_mod, "__file__", str(plugin_file))

    resolved = Path(exec_mod.default_metrics_path())

    assert resolved == plugin_file.parent.parents[3].joinpath(*_PROBE_PARTS)
