# @feature: FP-0.2.平台差异收敛 | @ci: python-coverage
"""路径大小写 canon 单源测试（多端平台差异普查 ⚠️-2 收敛回归锚）。

锁定契约：
1. canon 一致性：同一路径不同写法（冗余段/多余分隔符/大小写）经
   ``_libs.paths.canon_path`` 归一后相等；大小写折叠语义按平台分支断言
   （nt 折叠 / POSIX 保留）——断言各自平台的契约，不 skip；
2. 消费方单源同一性：project_registry / task_submit 绑定单源函数对象
   （防再复制漂移；task_submit 经文件路径装载免裸名 ``tool`` 串扰）；
3. SDK 包边界副本对齐：``permission_checker._canon_path`` 因 SDK 包边界
   不可 import 共享根，副本与单源同输入同输出由本文件钉死（改动须同刀）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from _libs.paths import canon_path  # noqa: E402
from project_manager import project_registry as pr  # noqa: E402

from agentos_plugin_sdk.permission_checker import _canon_path as sdk_canon_path  # noqa: E402

pytestmark = pytest.mark.unit


def _load_task_submit_tool() -> Any:
    """按文件路径装载 task_submit/tool.py（免裸名 ``tool`` 跨插件串扰）。"""
    path = _SHARED_ROOT / "task_submit" / "tool.py"
    spec = importlib.util.spec_from_file_location("task_submit_tool_paths_canon_test", path)
    assert spec is not None, "spec_from_file_location 必命中"
    assert spec.loader is not None, "文件源 loader 必在"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════
# 1. canon 一致性（同路径不同写法 → 相等）
# ════════════════════════════════════════════════════════════


def test_canon_collapses_redundant_segments() -> None:
    """冗余 ``.`` 段 / 重复分隔符 / ``..`` 归一人写形态。"""
    sep = os.sep
    assert canon_path(f"a{sep}.{sep}b") == canon_path(f"a{sep}b")
    assert canon_path(f"a{sep}{sep}b") == canon_path(f"a{sep}b")
    assert canon_path(f"a{sep}x{sep}..{sep}b") == canon_path(f"a{sep}b")


def test_canon_case_folding_by_platform() -> None:
    """nt：大小写变体归一相等；POSIX：保留大小写（各平台契约都断言）。"""
    if os.name == "nt":
        assert canon_path("C:\\Project\\SRC") == canon_path("c:\\project\\src")
        assert canon_path("C:\\Project\\SRC") == "c:\\project\\src"
    else:
        assert canon_path("/tmp/Var/FILE") == "/tmp/Var/FILE"


def test_canon_is_pure_normpath_plus_nt_lower() -> None:
    """性质锚：canon == normpath + nt 小写折叠（≥2 组输入，含反斜杠形态）。"""
    inputs = ["a/B/c", "a\\B\\c" if os.name == "nt" else "a/B/c", "D:/X/../Y" if os.name == "nt" else "/x/../y"]
    for raw in inputs:
        expected = os.path.normpath(raw)
        if os.name == "nt":
            expected = expected.lower()
        assert canon_path(raw) == expected


# ════════════════════════════════════════════════════════════
# 2. 消费方单源同一性
# ════════════════════════════════════════════════════════════


def test_project_registry_binds_single_source() -> None:
    """project_registry 归一面绑定 ``_libs.paths.canon_path`` 函数对象。"""
    assert pr.canon_path is canon_path
    # 登记归一 = abspath 前置（调用方语义）+ canon 尾（单源语义）
    for raw in ("a/B", "a\\B" if os.name == "nt" else "a/B"):
        assert pr._norm_path(raw) == canon_path(os.path.abspath(raw))


def test_task_submit_binds_single_source() -> None:
    """task_submit 挂靠 workspace 等价判定绑定单源函数对象。"""
    tool_mod = _load_task_submit_tool()
    assert tool_mod.canon_path is canon_path


# ════════════════════════════════════════════════════════════
# 3. SDK 包边界副本对齐（同输入同输出）
# ════════════════════════════════════════════════════════════


def test_sdk_copy_aligned_with_single_source() -> None:
    """SDK 副本与单源同输入同输出（包边界不可 import，防漂移靠对齐断言）。"""
    inputs = [
        "a/B/c",
        "a/B/../c",
        "a//b",
        "C:\\Project\\SRC" if os.name == "nt" else "/tmp/Var",
        "mixed/Case/Path" if os.name == "nt" else "/mixed/Case",
    ]
    for raw in inputs:
        assert sdk_canon_path(raw) == canon_path(raw), raw
