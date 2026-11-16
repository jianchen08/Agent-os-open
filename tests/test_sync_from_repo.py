# @feature: FP-0.2.dev_toolkit | @ci: python-coverage
"""sync_from_repo.py 单测：精确镜像语义/漂移检测/垃圾清除/真镜像一致性。

行为契约（断输入→输出）：
- sync 后镜像恰好等于真源收录集（排除项只作用于真源侧）；增/改/删三向同步；
- check 分类报 missing/extra/differing，字节级；真源缺失报错；
- 镜像里混进的运行垃圾（真源不存在、Windows 只读位文件）被 sync 清除；
- 真仓镜像一致性（提交态防漂移门检）。
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "skill-dev-toolkit" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("sync_from_repo", SCRIPTS_DIR / "sync_from_repo.py")
sfr = importlib.util.module_from_spec(_spec)
sys.modules["sync_from_repo"] = sfr
_spec.loader.exec_module(sfr)

pytestmark = pytest.mark.unit


def _make_source(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (root / "__pycache__" / "a.cpython-312.pyc").parent.mkdir()
    (root / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")


def test_sync_exact_mirror_three_way(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_source(src)
    sfr.sync(src, dst)
    assert (dst / "a.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert (dst / "sub" / "b.py").is_file()
    assert not (dst / "__pycache__").exists()
    assert sfr.check(src, dst) == {"missing": [], "extra": [], "differing": []}

    (src / "a.py").write_text("print('v2')\n", encoding="utf-8")
    (src / "sub" / "b.py").unlink()
    (src / "c.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    result = sfr.sync(src, dst)
    assert result["copied"] == ["a.py", "c.sh"]
    assert result["removed"] == ["sub/b.py"]
    assert not (dst / "sub" / "b.py").exists()
    assert (dst / "a.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert not list(dst.rglob("sub"))


def test_check_reports_missing_extra_differing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_source(src)
    sfr.sync(src, dst)
    (dst / "a.py").write_text("mutated\n", encoding="utf-8")
    (dst / "extra.txt").write_text("x", encoding="utf-8")
    (src / "sub" / "b.py").unlink()
    diff = sfr.check(src, dst)
    assert diff["differing"] == ["a.py"]
    assert sorted(diff["extra"]) == ["extra.txt", "sub/b.py"]
    assert diff["missing"] == []


def test_check_missing_target_all_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_source(src)
    diff = sfr.check(src, tmp_path / "nope")
    assert sorted(diff["missing"]) == ["a.py", "sub/b.py"]
    assert diff["extra"] == []


def test_sync_purges_readonly_junk_in_target(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_source(src)
    junk = dst / "eval_bench" / ".ai_workspaces" / "obj"
    junk.mkdir(parents=True)
    ro_file = junk / "read_only.bin"
    ro_file.write_bytes(b"x")
    ro_file.chmod(stat.S_IWRITE)
    sfr.sync(src, dst)
    assert not (dst / "eval_bench").exists()
    assert sfr.check(src, dst) == {"missing": [], "extra": [], "differing": []}


def test_sync_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(sfr.SyncError, match="真源"):
        sfr.sync(tmp_path / "nope", tmp_path / "dst")


def test_cli_check_drift_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_source(src)
    sfr.sync(src, dst)
    (dst / "a.py").write_text("mutated\n", encoding="utf-8")
    rc = sfr.main(["--check", "--source", str(src), "--target", str(dst)])
    assert rc == 1
    assert "镜像漂移" in capsys.readouterr().err


def test_real_repo_mirror_consistent() -> None:
    diff = sfr.check(REPO_ROOT / "scripts", REPO_ROOT / "skills" / "skill-dev-toolkit" / "scripts" / "repo")
    assert diff == {"missing": [], "extra": [], "differing": []}, f"真仓镜像漂移: {json.dumps(diff)[:400]}"
