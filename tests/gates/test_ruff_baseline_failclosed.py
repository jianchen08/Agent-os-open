# @feature: FP-GATE 覆盖率棘轮门禁 | @vision: V3 可嵌入 | @ci: python-coverage
"""check_ruff_baseline.py fail-closed 行为测试（P0 门禁可信性）。

修复前形态：`parse_findings` 在日志无 `Found N error` 汇总行时返回 0，
配合 scripts 车道基线 0 → 空日志 / ruff 未安装 / 输出截断 / 格式变更
一律判绿（恒真分支）。本测试锁修复后的契约：

- 测不到（无汇总行 / 空日志 / ruff 启动失败 / 日志文件缺失）→ 红，
  「没数到」不得推断为「零违规」；
- 基线不可读（文件缺失 / 缺键 / 值非法）→ 红，`--name` 拼错不得静默当 0；
- 含 invalid-syntax → 红（lint 盲区）；
- 只有「有汇总行 + 基线可读」才进入正常比对（> 红 / = 绿 / < 绿）。

每个负样本都是"构造违规 → 必须红"，与旧实现（红变绿）形成行为差异证据。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_ruff_baseline.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("check_ruff_baseline_fc", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把基线目录钉到 tmp（.github 形态），返回该目录。"""
    gh = tmp_path / ".github"
    gh.mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return gh


def _write_baseline(gh: Path, name: str, body: str) -> None:
    (gh / f"ruff-{name}-baseline.txt").write_text(body, encoding="utf-8")


def _run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["check_ruff_baseline.py", *argv])
    rc = mod.main()
    return rc, capsys.readouterr().out


def _log(tmp_path: Path, text: str) -> str:
    p = tmp_path / "rufflog.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parse_findings_no_summary_returns_none() -> None:
    """无汇总行 → None（不是 0）：这是修复前恒真分支的根点。"""
    assert mod.parse_findings("a.py:1:1: E501 too long\n") is None
    assert mod.parse_findings("") is None
    assert mod.parse_findings("ruff_commit_face: ruff 启动失败 [Errno 2] No such file\n") is None
    # 对照：有汇总行才给数
    assert mod.parse_findings("Found 7 errors.\n") == 7
    assert mod.parse_findings("a.py:1:1: E501 x\nFound 1 error.\n") == 1


@pytest.mark.parametrize(
    ("log_text", "why"),
    [
        ("a.py:1:1: E501 too long\n", "有明细无汇总行（截断/格式变更）"),
        ("", "空日志（ruff 未跑）"),
        ("ruff_commit_face: ruff 启动失败 [Errno 2] No such file or directory: 'ruff'\n", "ruff 未安装"),
        ("error: unexpected argument '--bogus' found\n", "ruff 参数错误"),
    ],
    ids=["no-summary-line", "empty-log", "ruff-missing", "ruff-arg-error"],
)
def test_unmeasurable_log_is_red(
    lane: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    log_text: str,
    why: str,
) -> None:
    """负样本：测不到即红——基线 0 也不能判绿（修复前这四例全绿）。"""
    _write_baseline(lane, "scripts", "ruff_findings=0\n")
    rc, out = _run(monkeypatch, capsys, ["--name", "scripts", "--from-file", _log(tmp_path, log_text)])

    assert rc == 1, f"{why} 必须红，实际 {rc}"
    assert "Found N error" in out
    assert "按失败处理" in out


def test_missing_log_file_is_red(
    lane: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """负样本：日志文件不存在（ruff 未执行/未落盘）→ 红。"""
    _write_baseline(lane, "scripts", "ruff_findings=0\n")
    rc, out = _run(monkeypatch, capsys, ["--name", "scripts", "--from-file", str(tmp_path / "nope.txt")])

    assert rc == 1
    assert "日志文件不存在" in out


def test_missing_baseline_file_is_red(
    lane: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """负样本：--name 拼错（基线文件不存在）→ 红，不得静默按基线 0 判绿。"""
    rc, out = _run(monkeypatch, capsys, ["--name", "typo-lane", "--from-file", _log(tmp_path, "Found 0 errors.\n")])

    assert rc == 1
    assert "基线不可读" in out


@pytest.mark.parametrize(
    "baseline_body",
    ["", "other_key=3\n", "ruff_findings=abc\n", "ruff_findings=-1\n"],
    ids=["empty-file", "key-absent", "not-int", "negative"],
)
def test_unparsable_baseline_is_red(
    lane: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    baseline_body: str,
) -> None:
    """负样本：基线文件存在但值不可用 → 红（无法比对即不判绿）。"""
    _write_baseline(lane, "scripts", baseline_body)
    rc, out = _run(monkeypatch, capsys, ["--name", "scripts", "--from-file", _log(tmp_path, "Found 0 errors.\n")])

    assert rc == 1
    assert "基线不可读" in out


def test_invalid_syntax_is_red(
    lane: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """含 invalid-syntax → 红（基线计数不可信），即便计数等于基线。"""
    _write_baseline(lane, "scripts", "ruff_findings=1\n")
    log = "x.py:1:8: invalid-syntax: Expected an expression\nFound 1 error.\n"
    rc, out = _run(monkeypatch, capsys, ["--name", "scripts", "--from-file", _log(tmp_path, log)])

    assert rc == 1
    assert "invalid-syntax" in out


@pytest.mark.parametrize(
    ("current", "baseline", "expect_rc"),
    [(0, 0, 0), (5, 5, 0), (4, 5, 0), (6, 5, 1), (1, 0, 1)],
    ids=["zero=zero", "equal", "shrunk", "grown", "new-over-zero"],
)
def test_ratchet_comparison(
    lane: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current: int,
    baseline: int,
    expect_rc: int,
) -> None:
    """只减不增：= 绿 / < 绿 / > 红（负样本 current>baseline 必红）。"""
    _write_baseline(lane, "plugins", f"ruff_findings={baseline}\n")
    suffix = "" if current == 1 else "s"
    rc, _out = _run(
        monkeypatch, capsys, ["--name", "plugins", "--from-file", _log(tmp_path, f"Found {current} error{suffix}.\n")]
    )

    assert rc == expect_rc


def test_init_prints_measured_value(
    lane: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--init 打印实测值；基线文件在不在都不影响（bootstrap 语义）。"""
    rc, out = _run(
        monkeypatch, capsys, ["--name", "fresh-lane", "--init", "--from-file", _log(tmp_path, "Found 12 errors.\n")]
    )

    assert rc == 0
    assert out.strip() == "ruff_findings=12"


def test_init_without_summary_line_is_red(
    lane: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """负样本：--init 也要求可测量——测不到不许打印一个"假基线"。"""
    rc, out = _run(monkeypatch, capsys, ["--name", "scripts", "--init", "--from-file", _log(tmp_path, "boom\n")])

    assert rc == 1
    assert "ruff_findings=" not in out


def test_real_baseline_files_readable() -> None:
    """仓内两条车道基线都可解析（门禁开关态与脚本契约一致，防改坏名）。"""
    real = _load()
    for name in ("scripts", "plugins"):
        value = real.read_baseline(real.baseline_path(name))
        assert value is not None, f".github/ruff-{name}-baseline.txt 必须含 ruff_findings=<非负整数>"
