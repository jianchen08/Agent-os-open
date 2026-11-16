# @feature: FP-0.2.二 工具插件加载基面 | @ci: python-coverage
"""tool_server_base 加载基面直调单测：与具体插件无关的分支面。

遮蔽防护与失败信封的行为契约由四件消费插件（browser / community_ops /
web_ext / search）的既有套件执法（decoy 常驻/不常驻、信封字段
断言均在彼处）；本文件只补 helper 直调可达、插件套件触不到的分支。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_SHARED_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402

pytestmark = pytest.mark.unit


def test_load_tool_class_missing_tool_py_raises(tmp_path: Path) -> None:
    """tool.py 缺失 → FileNotFoundError 上抛（错装包 fail-loud，不静默吞）。"""
    with pytest.raises(FileNotFoundError):
        load_tool_class(str(tmp_path), "no_such_impl", "NoTool")


def test_envelope_carries_error_code_and_status() -> None:
    """信封支线：error_code 非空随封携带；status 显式传入时补 HTTP 语义位。"""

    class _FailedResult:
        success: bool = False
        error: str = "boom"
        error_code: str | None = "E_X"

    result: Any = _FailedResult()
    assert tool_result_envelope(result, status=500) == {
        "success": False,
        "error": "boom",
        "error_code": "E_X",
        "status": 500,
    }
