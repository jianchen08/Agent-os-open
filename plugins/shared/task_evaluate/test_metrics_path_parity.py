# @feature: FP-0.2.〇 管道引擎与插件执行模型(评估指标路径单源) | @vision: V3 可嵌入 | @ci: python-coverage
"""两侧指标路径探测等价测试（B11-M3 对偶复制收敛 _libs 单源后的对账面）。

task_evaluate.default_metrics_path 与 task_submit._metrics_config_path 历史为
同一「祖先逐级探测 evaluation_metrics.yaml」算法的两份对偶复制（docstring
互认同步义务）；抽取 _libs.evaluation_metrics_path 单源后，本测试锁两侧在
同一目录树下同结果——任何一侧私改坐标或回退本地实现即红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TE_DIR.parents[2]
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"

for _p in (str(_SDK_DIR), str(_TOOL_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_PROBE_PARTS = ("config", "plugins", "evaluation", "evaluation_metrics.yaml")

# 布局名 → (布局根目录名, 插件层相对布局根路径)；两侧插件为同树兄弟目录
_LAYOUTS = {
    "repo": ("repo", "plugins/shared"),
    "installed_tree": ("resources", "plugins/shared"),
    "user_flat": ("agentos", "plugins"),
}


def _load_plugin_module(unique_name: str, file_path: Path) -> Any:
    """按唯一模块名加载真实插件模块（防与其它平铺测试互相污染）。"""
    if unique_name in sys.modules:
        return sys.modules[unique_name]
    spec = importlib.util.spec_from_file_location(unique_name, file_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exec_mod() -> Any:
    return _load_plugin_module(
        "task_eval_metrics_path_parity_executor", _TE_DIR / "_executor.py"
    )


@pytest.fixture(scope="module")
def tool_mod() -> Any:
    return _load_plugin_module(
        "task_submit_metrics_path_parity_tool", _TOOL_DIR / "tool.py"
    )


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_both_sides_probe_same_path_in_same_tree(
    exec_mod: Any,
    tool_mod: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    """同一目录树：两侧探测同结果（task_submit Path 形态 ≡ task_evaluate str 形态）。"""
    root_seg, rel_plugins = _LAYOUTS[layout]
    root = tmp_path / root_seg
    plugins_root = root / Path(rel_plugins)
    te_file = plugins_root / "task_evaluate" / "_executor.py"
    ts_file = plugins_root / "task_submit" / "tool.py"
    te_file.parent.mkdir(parents=True)
    ts_file.parent.mkdir(parents=True)
    expected = root.joinpath(*_PROBE_PARTS)
    expected.parent.mkdir(parents=True)
    expected.write_text("metrics: []\n", encoding="utf-8")

    # 布局经模块 __file__ 替身模拟（安装位置是外部环境条件，与 tmp 文件系统
    # mock 同级），被测函数为真实实现
    monkeypatch.setattr(exec_mod, "__file__", str(te_file))
    monkeypatch.setattr(tool_mod, "__file__", str(ts_file))

    from_tool = tool_mod._metrics_config_path()
    from_exec = Path(exec_mod.default_metrics_path())

    assert from_tool == expected
    assert from_exec == expected
    assert from_tool == from_exec
    assert from_tool.is_file()  # 性质：命中即真实存在，不是拼出的幽灵路径


def test_both_sides_fallback_identical_when_all_miss(
    exec_mod: Any, tool_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全 miss 树：两侧同回落（探测起点向上 3 级拼相对尾），路径相等。"""
    te_file = tmp_path / "deep" / "nest" / "alone" / "task_evaluate" / "_executor.py"
    te_file.parent.mkdir(parents=True)
    ts_file = tmp_path / "deep" / "nest" / "alone" / "task_submit" / "tool.py"
    ts_file.parent.mkdir(parents=True)
    monkeypatch.setattr(exec_mod, "__file__", str(te_file))
    monkeypatch.setattr(tool_mod, "__file__", str(ts_file))

    from_tool = tool_mod._metrics_config_path()
    from_exec = Path(exec_mod.default_metrics_path())

    assert from_tool == from_exec
    # 性质：回落形态 = 插件目录向上 3 级拼相对尾（兄弟插件目录同根）
    assert from_tool == te_file.parent.parents[2].joinpath(*_PROBE_PARTS)
    assert not from_tool.exists()  # 全 miss 回落允许幽灵路径（调用方自有降级）
