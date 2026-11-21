# @feature: FP-GATE 前端测试基线锁 | @ci: python-coverage
"""check_frontend_baseline.py 单测。

行为契约（断输入→输出，不钉实现）：
- eslint error 解析：problems 汇总行命中即取值（error/warning 单复数同权）；
- ESLINT_EXIT 退出码标记协议（2026-11-16 fail-loud 加固）：汇总行缺失时
  标记 0 = 干净通过记 0；标记非 0 = eslint 崩溃/配置错抛错；标记缺失 =
  无法区分干净与崩溃抛错——消灭"崩溃输出被静默记 0 过闸"的 fail-open 假绿；
- warning 解析：errors 解析器校验通过后取 warnings 列；无汇总行记 0；
- write_baseline 原位保真：只替换三管理键，注释链与未知键行
  （eslint_warnings 棘轮行）原样保留；账本不存在时写最小新账本；
  注释行含 '=' 不误判为键行；
- read_baseline：四键解析；eslint_warnings 缺省 -1（维度跳过哨兵）；
- main 棘轮语义：error/warn 超基线红、降基线绿且不自动收紧、持平绿；
  声明 --eslint-file 而文件为空 = 度量链断裂抛错（不静默记 0）。

无真仓负控制：本脚本为纯解析/比较器（真跑 npm lint 约 80s 不入 unit 车道），
防恒绿由 frontend-lint 活体闸承担（pre-commit 快检 + CI frontend-test 双挂）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_frontend_baseline", SCRIPTS / "check_frontend_baseline.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


# ── parse_eslint_errors：汇总行解析 ──────────────────────────────────────


def test_parses_errors_from_problems_line(mod):
    out = "src/a.ts\n  1:10  error  msg  rule\n\n✖ 853 problems (33 errors, 820 warnings)"
    assert mod.parse_eslint_errors(out) == 33


def test_parses_singular_forms(mod):
    assert mod.parse_eslint_errors("✖ 1 problem (1 error, 0 warnings)") == 1
    assert mod.parse_eslint_errors("✖ 5 problems (0 errors, 5 warnings)") == 0


def test_problems_line_parses_even_without_exit_marker(mod):
    # 汇总行在 = eslint 确实跑过，标记可有可无（兼容手工 tee 旧产物）
    assert mod.parse_eslint_errors("✖ 2 problems (2 errors, 0 warnings)") == 2


# ── parse_eslint_errors：ESLINT_EXIT 标记协议（fail-loud） ───────────────


def test_exit_marker_zero_no_problems_is_clean(mod):
    assert mod.parse_eslint_errors("ESLINT_EXIT=0") == 0


def test_exit_marker_nonzero_no_problems_raises(mod):
    # eslint 崩溃/配置错误：非零退出且无汇总行，禁止记 0 过闸
    with pytest.raises(RuntimeError, match="崩溃"):
        mod.parse_eslint_errors("Oops. Something went wrong.\nESLINT_EXIT=2")


def test_missing_marker_no_problems_raises(mod):
    # 无汇总行又无标记 = 无法区分干净与崩溃，fail-loud（原 fail-open 假绿面）
    with pytest.raises(RuntimeError, match="ESLINT_EXIT"):
        mod.parse_eslint_errors("")


def test_empty_output_raises(mod):
    with pytest.raises(RuntimeError):
        mod.parse_eslint_errors("   \n")


# ── parse_eslint_warnings ────────────────────────────────────────────────


def test_parses_warnings(mod):
    assert mod.parse_eslint_warnings("✖ 853 problems (33 errors, 820 warnings)") == 820
    assert mod.parse_eslint_warnings("✖ 1 problem (0 errors, 1 warning)") == 1


def test_warnings_zero_when_no_problems(mod):
    # 仅在 errors 解析器校验通过后可达（干净/标记 0 场景）
    assert mod.parse_eslint_warnings("ESLINT_EXIT=0") == 0


# ── write_baseline：原位保真 ─────────────────────────────────────────────


def _ledger(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "frontend-baseline.txt"
    f.write_text(body, encoding="utf-8")
    return f


def test_write_baseline_preserves_comments_and_unknown_keys(mod, tmp_path, monkeypatch):
    ledger = _ledger(
        tmp_path,
        "# 头注释\n"
        "# 收紧归因（2026-10-30）：844→843\n"
        "vitest_failures=3\n"
        "eslint_errors=700\n"
        "vitest_coverage_pct=99.00\n"
        "# 843→0（2026-11-16 warn 棘轮退役）\n"
        "eslint_warnings=0\n"
        "unknown_future_key=42\n",
    )
    monkeypatch.setattr(mod, "BASELINE_FILE", ledger)
    mod.write_baseline(vitest=1, eslint=689, coverage=99.57)
    text = ledger.read_text(encoding="utf-8")
    assert "vitest_failures=1" in text
    assert "eslint_errors=689" in text
    assert "vitest_coverage_pct=99.57" in text
    assert "eslint_warnings=0" in text  # 棘轮行不被抹
    assert "unknown_future_key=42" in text  # 未知键不被抹
    assert "# 收紧归因（2026-10-30）：844→843" in text  # 注释链不被抹
    assert "# 843→0（2026-11-16 warn 棘轮退役）" in text


def test_write_baseline_comment_with_equals_is_not_a_key(mod, tmp_path, monkeypatch):
    ledger = _ledger(
        tmp_path,
        "# eslint_errors=999 出现在注释里不是键行\nvitest_failures=0\n",
    )
    monkeypatch.setattr(mod, "BASELINE_FILE", ledger)
    mod.write_baseline(vitest=0, eslint=0, coverage=1.0)
    text = ledger.read_text(encoding="utf-8")
    assert "# eslint_errors=999 出现在注释里不是键行" in text
    assert text.count("eslint_errors=") == 2  # 注释 1 处 + 管理键 1 处


def test_write_baseline_creates_minimal_ledger_when_absent(mod, tmp_path, monkeypatch):
    ledger = tmp_path / "frontend-baseline.txt"
    assert not ledger.exists()
    monkeypatch.setattr(mod, "BASELINE_FILE", ledger)
    mod.write_baseline(vitest=0, eslint=5, coverage=50.0)
    text = ledger.read_text(encoding="utf-8")
    assert "vitest_failures=0" in text
    assert "eslint_errors=5" in text
    assert "vitest_coverage_pct=50.00" in text


# ── read_baseline ────────────────────────────────────────────────────────


def test_read_baseline_parses_four_keys(mod, tmp_path, monkeypatch):
    ledger = _ledger(
        tmp_path,
        "vitest_failures=2\neslint_errors=10\nvitest_coverage_pct=99.57\neslint_warnings=7\n",
    )
    monkeypatch.setattr(mod, "BASELINE_FILE", ledger)
    assert mod.read_baseline() == (2, 10, 99.57, 7)


def test_read_baseline_warnings_default_skip(mod, tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, "vitest_failures=0\neslint_errors=0\nvitest_coverage_pct=1.00\n")
    monkeypatch.setattr(mod, "BASELINE_FILE", ledger)
    assert mod.read_baseline() == (0, 0, 1.00, -1)


# ── main：棘轮语义 ───────────────────────────────────────────────────────


def _run_main(mod, monkeypatch, tmp_path, ledger_body: str, eslint_artifact: str):
    ledger = _ledger(tmp_path, ledger_body)
    artifact = tmp_path / "eslint_out.txt"
    artifact.write_text(eslint_artifact, encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", ledger)
    monkeypatch.setattr(sys, "argv", ["check_frontend_baseline.py", "--eslint-file", str(artifact)])
    return mod.main()


def test_main_flat_passes(mod, tmp_path, monkeypatch):
    rc = _run_main(
        mod,
        monkeypatch,
        tmp_path,
        "vitest_failures=0\neslint_errors=689\nvitest_coverage_pct=99.57\neslint_warnings=0\n",
        "✖ 689 problems (689 errors, 0 warnings)\nESLINT_EXIT=1\n",
    )
    assert rc == 0


def test_main_error_increase_fails(mod, tmp_path, monkeypatch):
    rc = _run_main(
        mod,
        monkeypatch,
        tmp_path,
        "vitest_failures=0\neslint_errors=689\nvitest_coverage_pct=99.57\neslint_warnings=0\n",
        "✖ 690 problems (690 errors, 0 warnings)\nESLINT_EXIT=1\n",
    )
    assert rc == 1


def test_main_warning_increase_fails(mod, tmp_path, monkeypatch):
    rc = _run_main(
        mod,
        monkeypatch,
        tmp_path,
        "vitest_failures=0\neslint_errors=0\nvitest_coverage_pct=99.57\neslint_warnings=5\n",
        "✖ 6 problems (0 errors, 6 warnings)\nESLINT_EXIT=1\n",
    )
    assert rc == 1


def test_main_decrease_hints_no_auto_tighten(mod, tmp_path, monkeypatch, capsys):
    rc = _run_main(
        mod,
        monkeypatch,
        tmp_path,
        "vitest_failures=0\neslint_errors=689\nvitest_coverage_pct=99.57\neslint_warnings=0\n",
        "✖ 688 problems (688 errors, 0 warnings)\nESLINT_EXIT=1\n",
    )
    assert rc == 0
    assert "手动收紧" in capsys.readouterr().out
    # 基线文件不被自动改写
    assert "eslint_errors=689" in (tmp_path / "frontend-baseline.txt").read_text(encoding="utf-8")


def test_main_empty_artifact_fails_loud(mod, tmp_path, monkeypatch):
    # 声明了 --eslint-file 而产物为空 = 度量链断裂，不静默记 0
    with pytest.raises(RuntimeError):
        _run_main(
            mod,
            monkeypatch,
            tmp_path,
            "vitest_failures=0\neslint_errors=0\nvitest_coverage_pct=1.00\n",
            "",
        )


def test_main_crash_artifact_fails_loud(mod, tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="崩溃"):
        _run_main(
            mod,
            monkeypatch,
            tmp_path,
            "vitest_failures=0\neslint_errors=0\nvitest_coverage_pct=1.00\n",
            "Oops. Something went wrong.\nESLINT_EXIT=2\n",
        )
