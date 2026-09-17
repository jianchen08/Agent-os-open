# @feature: FP-GATE 覆盖率棘轮门禁 | @ci: python-coverage
"""check_rust_test_baseline.parse_failures 回归测试。

重点守住「方式3 编译失败兜底」的行首锚定：测试名可含 "error::tests"
（plugin-loader error 模块），无锚点子串匹配会把全绿跑误判成 1 失败
（2026-09-17 kernel-test 假红实证）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_rust_test_baseline.py"
_spec = importlib.util.spec_from_file_location("check_rust_test_baseline", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_rust_test_baseline", _mod)
_spec.loader.exec_module(_mod)

parse_failures = _mod.parse_failures


def test_counts_failed_from_test_result_lines():
    out = "\n".join(
        [
            "test a ... ok",
            "test b ... FAILED",
            "test result: FAILED. 1 passed; 1 failed; 0 ignored",
            "test result: ok. 3 passed; 0 failed; 0 ignored",
        ]
    )
    assert parse_failures(out) == 1


def test_all_green_with_error_module_test_names_is_zero():
    """全绿输出含 test error::tests::...（测试名自带 error: 子串）→ 0，非误报。"""
    out = "\n".join(
        [
            "test error::tests::from_core_plugin_error_maps_to_load_failed_with_reason ... ok",
            "test error::tests::display_renders_every_variant_context ... ok",
            "test result: ok. 211 passed; 0 failed; 1 ignored",
        ]
    )
    assert parse_failures(out) == 0


def test_compile_error_at_line_start_counts_one():
    out = "\n".join(
        [
            "   Compiling agentos-core v0.2.0",
            "error[E0308]: mismatched types",
        ]
    )
    assert parse_failures(out) == 1


def test_compile_error_plain_at_line_start_counts_one():
    assert parse_failures("error: could not compile `agentos-core` (bin)") == 1


def test_indented_error_line_is_not_compile_failure():
    """缩进的 error: 行不是 cargo 诊断（cargo 诊断恒顶格）→ 不触发兜底。"""
    assert parse_failures("    error: nested log line inside test output") == 0
