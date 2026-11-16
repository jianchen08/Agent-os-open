# @feature: FP-GATE 内核库直连守卫 | @ci: python-coverage
"""check_kernel_db_direct_reads.py 单测（审查总册 20261014 C2 建闸，刀3 收紧）。

行为契约（断输入→输出，不钉实现）：
- 命中样例红：生产码出现 sqlite3.connect 且超出基线 → exit 1；
- 干净样例绿：无命中（或命中均在基线内）→ exit 0；
- 自有库豁免名单：EXEMPT_FILES 登记件（插件自有数据面/契约测试替身）不入
  扫描面；kernel_db.py 退场后该路径**不再豁免**（共享契约模块已删）；
- 测试文件豁免：test_*.py / tests/ 目录（非生产码）、非 .py 文件；
- 基线锁：存量计数持平放行、超量红（棘轮只减不增）；
- fail-closed：基线含绝对路径键（历史缺陷形态）→ exit 2 拒判；
- 负控制（防恒绿）：真实仓状态与基线一致，检查器子进程实跑退出 0。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_kernel_db_direct_reads", SCRIPTS / "check_kernel_db_direct_reads.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _make_repo(
    tmp_path: Path,
    files: dict[str, str],
    baseline_lines: list[str] | None = None,
) -> Path:
    """临时仓树：plugins/shared 源文件 + 可选基线，模块 ROOT 指向它。"""
    repo = tmp_path / "repo"
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if baseline_lines is not None:
        (repo / ".github").mkdir(parents=True, exist_ok=True)
        (repo / ".github" / "check_kernel_db_direct_reads_baseline.txt").write_text(
            "\n".join(baseline_lines) + "\n", encoding="utf-8"
        )
    return repo


_HIT = "import sqlite3\nconn = sqlite3.connect(db_path)\n"


def test_hit_sample_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命中样例红：生产码直连且基线无账 → exit 1。"""
    repo = _make_repo(tmp_path, {"plugins/shared/foo/plugin.py": _HIT})
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 1


def test_clean_sample_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """干净样例绿：无命中 → exit 0。"""
    repo = _make_repo(tmp_path, {"plugins/shared/foo/plugin.py": "x = 1\n"})
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 0


def test_own_db_exempt_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """自有库豁免名单：EXEMPT_FILES 登记件（自有数据面/契约测试替身）不入扫描面。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/trace_ingestion/trace_ingest_store.py": _HIT,
            "plugins/shared/_libs/testing/db_read_fake.py": _HIT,
        },
    )
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 0


def test_kernel_db_path_no_longer_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kernel_db.py 退场后其路径不再豁免（契约唯一形态=内核服务，直连零口径）。"""
    repo = _make_repo(tmp_path, {"plugins/shared/kernel_db.py": _HIT})
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 1


def test_test_files_exempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试文件豁免：平铺 test_*.py 与 tests/ 目录均不入扫描面。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/foo/test_foo.py": _HIT,
            "plugins/shared/foo/tests/test_bar.py": _HIT,
        },
    )
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 0


def test_baseline_locks_count_growth_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基线锁：存量持平放行；计数上涨红（棘轮只减不增）。"""
    src = {"plugins/shared/monitoring/server.py": _HIT}
    repo = _make_repo(
        tmp_path, src, ["# 基线\nplugins/shared/monitoring/server.py: 1"]
    )
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 0

    repo2 = _make_repo(
        tmp_path,
        {"plugins/shared/monitoring/server.py": _HIT + "conn2 = sqlite3.connect(p)\n"},
        ["# 基线\nplugins/shared/monitoring/server.py: 1"],
    )
    monkeypatch.setattr(mod, "ROOT", repo2)
    assert mod.main() == 1


def test_absolute_baseline_key_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fail-closed：基线含机器绝对路径键（历史缺陷形态）→ exit 2 拒判。"""
    repo = _make_repo(
        tmp_path,
        {"plugins/shared/foo/plugin.py": _HIT},
        ["D:/somewhere/plugins/shared/foo/plugin.py: 1"],
    )
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 2


def test_new_plugin_file_not_in_baseline_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基线只锁在册文件：新插件文件出现直连即红（不在册 = 无账可销）。"""
    repo = _make_repo(
        tmp_path,
        {
            "plugins/shared/monitoring/server.py": _HIT,
            "plugins/shared/newcomer/plugin.py": _HIT,
        },
        ["plugins/shared/monitoring/server.py: 1"],
    )
    monkeypatch.setattr(mod, "ROOT", repo)
    assert mod.main() == 1


def test_negative_control_real_repo_green() -> None:
    """负控制（防恒绿）：真实仓状态与基线一致，检查器子进程实跑退出 0。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_kernel_db_direct_reads.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
