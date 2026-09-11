# @feature: FP-GATE 前端 any 棘轮 + 大文件冻结 | @ci: python-coverage
"""check_frontend_any_baseline 单测（批次F 2026-09-08）。

覆盖可单测面：
1. 生产码扫描口径（排除 __tests__ / *.test.* / endpoints.generated.ts / 非 ts）；
2. any 棘轮方向（超基线红并列位置、低于基线提示收紧、基线缺失 fail-loud）；
3. >1000 行文件冻结（清单内增长红、新入列红、缩短提示不红、≤1000 不入册）；
4. --init 写入/收紧（拒绝上调 any 基线、拒绝放宽冻结上限）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


any_base = _load("check_frontend_any_baseline")


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离扫描树：ROOT/SCAN_DIR 指向 tmp，返回生产码 src 目录。"""
    src = tmp_path / "frontend" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(any_base, "ROOT", tmp_path)
    monkeypatch.setattr(any_base, "SCAN_DIR", src)
    return src


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── 扫描口径 ──────────────────────────────────────────────────────


def test_count_scopes_production_only(tree: Path) -> None:
    _write(tree / "a.ts", "const x: any = 1\nconst y = v as any\nok\n")
    _write(tree / "b.test.tsx", "const z: any = 1\n")
    _write(tree / "__tests__" / "c.ts", "const w: any = 1\n")
    _write(tree / "endpoints.generated.ts", "const g: any = 1\n")
    _write(tree / "d.css", "/* : any */\n")

    hits = any_base.count_any_hits(tree)

    assert [(p.name, lineno) for p, lineno, _ in hits] == [("a.ts", 1), ("a.ts", 2)]


# ── any 棘轮 ──────────────────────────────────────────────────────


def test_any_over_baseline_fails_and_lists_hits(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write(tree / "a.ts", "const x: any = 1\nconst y = v as any\n")
    baseline = _write(tmp_path / "any-baseline.txt", "frontend_any=1\n")

    assert any_base.check_any_baseline(tree, baseline) is True
    out = capsys.readouterr().out
    assert "a.ts:1" in out and "a.ts:2" in out


def test_any_under_baseline_passes_with_tighten_hint(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write(tree / "a.ts", "const x: any = 1\n")
    baseline = _write(tmp_path / "any-baseline.txt", "frontend_any=5\n")

    assert any_base.check_any_baseline(tree, baseline) is False
    assert "收紧" in capsys.readouterr().out


def test_any_at_baseline_passes(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write(tree / "a.ts", "const x: any = 1\n")
    baseline = _write(tmp_path / "any-baseline.txt", "frontend_any=1\n")

    assert any_base.check_any_baseline(tree, baseline) is False
    assert "持平" in capsys.readouterr().out


def test_missing_any_baseline_fails_loud(tree: Path, tmp_path: Path) -> None:
    _write(tree / "a.ts", "const x: any = 1\n")

    with pytest.raises(RuntimeError, match="缺失"):
        any_base.check_any_baseline(tree, tmp_path / "nope.txt")


# ── >1000 行文件冻结 ─────────────────────────────────────────────


def _big_file(tree: Path, rel: str, lines: int) -> Path:
    return _write(tree / rel, "\n".join(f"line-{i}" for i in range(lines)) + "\n")


def test_large_file_growth_fails(tree: Path, tmp_path: Path) -> None:
    _big_file(tree, "big.ts", 1005)
    baseline = _write(tmp_path / "large.txt", "frontend/src/big.ts 1001\n")

    assert any_base.check_large_files(tree, baseline) is True


def test_new_over_limit_file_fails(tree: Path, tmp_path: Path) -> None:
    _big_file(tree, "new-big.ts", 1001)
    baseline = _write(tmp_path / "large.txt", "# empty\n")

    assert any_base.check_large_files(tree, baseline) is True


def test_large_file_shrink_passes_with_hint(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _big_file(tree, "big.ts", 1001)
    baseline = _write(tmp_path / "large.txt", "frontend/src/big.ts 1100\n")

    assert any_base.check_large_files(tree, baseline) is False
    assert "收紧" in capsys.readouterr().out


def test_at_limit_file_not_enrolled(tree: Path, tmp_path: Path) -> None:
    _big_file(tree, "edge.ts", 1000)
    baseline = _write(tmp_path / "large.txt", "# empty\n")

    assert any_base.check_large_files(tree, baseline) is False


# ── main 集成（模块常量已由 fixture 指向 tmp）────────────────────


def test_main_green_on_compliant_tree(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tree / "a.ts", "const x = 1\n")
    monkeypatch.setattr(
        any_base, "ANY_BASELINE_FILE", _write(tmp_path / "any-baseline.txt", "frontend_any=0\n")
    )
    monkeypatch.setattr(
        any_base,
        "LARGE_FILES_BASELINE_FILE",
        _write(tmp_path / "large-baseline.txt", "# empty\n"),
    )

    assert any_base.main([]) == 0


def test_main_red_on_any_growth(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tree / "a.ts", "const x: any = 1\nconst y: any = 2\n")
    monkeypatch.setattr(
        any_base, "ANY_BASELINE_FILE", _write(tmp_path / "any-baseline.txt", "frontend_any=1\n")
    )
    monkeypatch.setattr(
        any_base,
        "LARGE_FILES_BASELINE_FILE",
        _write(tmp_path / "large-baseline.txt", "# empty\n"),
    )

    assert any_base.main([]) == 1


# ── --init 写入/收紧 ─────────────────────────────────────────────


def test_init_writes_and_refuses_raising_any_baseline(tree: Path, tmp_path: Path) -> None:
    any_file = tmp_path / "any-baseline.txt"
    large_file = tmp_path / "large-baseline.txt"

    _write(tree / "a.ts", "const x: any = 1\n")
    any_base.write_baselines(tree, any_file, large_file)
    assert "frontend_any=1" in any_file.read_text(encoding="utf-8")

    _write(tree / "b.ts", "const y = v as any\n")
    with pytest.raises(SystemExit):
        any_base.write_baselines(tree, any_file, large_file)


def test_init_refuses_relaxing_frozen_cap(tree: Path, tmp_path: Path) -> None:
    any_file = tmp_path / "any-baseline.txt"
    large_file = tmp_path / "large-baseline.txt"

    _big_file(tree, "big.ts", 1001)
    any_base.write_baselines(tree, any_file, large_file)
    assert "frontend/src/big.ts 1001" in large_file.read_text(encoding="utf-8")

    _big_file(tree, "big.ts", 1002)
    with pytest.raises(SystemExit):
        any_base.write_baselines(tree, any_file, large_file)
