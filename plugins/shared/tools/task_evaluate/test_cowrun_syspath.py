# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""共跑回归：workspace_lifecycle 与 task_evaluate 测试同进程共跑必须全绿。

pytest 9 对带 ``__init__.py`` 的插件目录（``pipeline`` 包链）在用例 setup 阶段经
``import_path(prepend)`` 前置 pkg_root（plugins/shared），其守卫只比较
``sys.path[0]``——共跑场景下 sys.path 会合法地出现第二条 plugins/shared。
这要求 sys.path 唯一性类断言只能约束"本模块自举的增量"（集合语义），
不能假设进程级全局恰一；本测试把两文件共跑作为契约钉死。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WL_FILE = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "input" / "workspace_lifecycle" / "test_plugin.py"
_TE_FILE = Path(__file__).resolve().parent / "test_module_gaps.py"


def test_cowrun_pipeline_and_task_evaluate_single_process() -> None:
    """两文件以同一 pytest 进程共跑：退出码 0（子进程隔离，防自我递归）。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_WL_FILE),
            str(_TE_FILE),
            "-q",
            "--no-header",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert proc.returncode == 0, f"co-run failed:\n{proc.stdout}\n{proc.stderr}"
