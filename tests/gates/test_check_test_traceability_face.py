# @feature: FP-GATE-META 元门禁 gate-wiring | @ci: python-coverage
"""check_test_traceability 提交面过滤契约测试。

锁 2026-11-07 裁定：门禁评估面 = HEAD + git 索引（tracked + staged）——
未跟踪且未暂存的跨会话 WIP 文件不属任何提交、CI 干净检出也无此物，
不计入未标记基线（实证：平行会话在飞内核拆分留下 5 个未标记
#[cfg(test)] 新文件，工作区 344 > 基线 339 误红）。文件一旦暂存即入
索引面，照样受非法标记与基线锁约束，无逃逸口。

scripts/ 非包，经 importlib 唯名模块加载；ROOT 打桩到 tmp_path，
不读真实仓状态（git_index_files 一例真实依赖除外）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_test_traceability.py"


@pytest.fixture
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("ctt_face_test_mod", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["ctt_face_test_mod"] = m
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    return m


def test_in_commit_face_scans_all_when_index_none(mod: Any, tmp_path: Path) -> None:
    """非 git 环境（index=None）→ 全量扫描（独立校验运行不缩面）。"""
    p = tmp_path / "tests" / "test_x.py"
    p.parent.mkdir(parents=True)
    p.write_text("", encoding="utf-8")

    assert mod.in_commit_face(p, None) is True


def test_in_commit_face_matches_index_by_posix_relpath(mod: Any, tmp_path: Path) -> None:
    """命中以 POSIX 相对路径比对：索引内有 → 提交面；索引外 → WIP 排除。"""
    tracked = tmp_path / "tests" / "tracked_test.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("", encoding="utf-8")
    wip = tmp_path / "tests" / "wip_test.py"
    wip.write_text("", encoding="utf-8")

    index = {"tests/tracked_test.py"}

    assert mod.in_commit_face(tracked, index) is True
    assert mod.in_commit_face(wip, index) is False


def test_in_commit_face_outside_root_defaults_in(mod: Any, tmp_path: Path) -> None:
    """ROOT 之外的路径（防御面）→ 视为提交面，不静默吞掉。"""
    outside = tmp_path.parent / "elsewhere_test.py"

    assert mod.in_commit_face(outside, {"nothing"}) is True


def test_git_index_files_contains_self() -> None:
    """真实依赖一例：git 可用时索引面必含本脚本自身（tracked）。"""
    spec = importlib.util.spec_from_file_location("ctt_face_real_mod", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["ctt_face_real_mod"] = m
    spec.loader.exec_module(m)

    index = m.git_index_files()

    assert index is not None
    assert "scripts/check_test_traceability.py" in index


def test_git_index_files_none_when_not_a_repo(
    mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git 失败（非仓目录）→ None 兜底全量扫描，不崩不缩面。"""
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_args, **_kw: type("R", (), {"returncode": 128, "stdout": b""})(),
    )

    assert mod.git_index_files() is None


# ── 内容面（HEAD + 暂存）：tracked 文件未暂存改动不污染评估 ──────────────


class _GitR:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_git_face_changes_parses_staged_and_dirty(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """-z 输出按 NUL 拆集合：cached→暂存面、工作区 diff→脏面。"""

    def fake_run(cmd: list[str], **_kw: Any) -> Any:
        if "--cached" in cmd:
            return _GitR(0, b"tests/a.py\0tests/b.py\0")
        return _GitR(0, b"tests/b.py\0")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.git_face_changes() == ({"tests/a.py", "tests/b.py"}, {"tests/b.py"})


def test_git_face_changes_none_on_git_failure(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """git diff 失败 → None（调用方退化为全盘面，宁多扫不漏扫）。"""
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_args, **_kw: _GitR(128, b""),
    )
    assert mod.git_face_changes() is None


def test_read_commit_face_text_dirty_tracked_reads_head(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脏面 tracked 文件读 HEAD 内容：盘面未暂存改动（含非法标记）不进评估。"""
    p = tmp_path / "tests" / "t.py"
    p.parent.mkdir(parents=True)
    p.write_text("# @feature: FP-ILLEGAL-WIP-MARKER", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> Any:
        seen.append(cmd)
        return _GitR(0, b"# @feature: FP-GATE-META\n")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    text = mod.read_commit_face_text(p, "tests/t.py", (set(), {"tests/t.py"}))

    assert "# @feature: FP-GATE-META" in text
    assert "FP-ILLEGAL-WIP-MARKER" not in text
    assert any("HEAD:tests/t.py" in " ".join(c) for c in seen)


def test_read_commit_face_text_staged_reads_index(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """暂存文件读索引内容（:rel）：暂存即入评估面，无逃逸口。"""
    p = tmp_path / "tests" / "t.py"
    p.parent.mkdir(parents=True)
    p.write_text("# @feature: FP-GATE-META\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> Any:
        seen.append(cmd)
        return _GitR(0, b"# @feature: FP-GATE-META-STAGED\n")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    text = mod.read_commit_face_text(p, "tests/t.py", ({"tests/t.py"}, set()))

    assert "# @feature: FP-GATE-META-STAGED" in text
    assert any(
        ":tests/t.py" in " ".join(c) and "HEAD:" not in " ".join(c) for c in seen
    )


def test_read_commit_face_text_clean_tracked_reads_disk_without_git(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """干净 tracked 文件读盘（盘面 ≡ HEAD）：零 git 调用，快路径。"""
    p = tmp_path / "tests" / "t.py"
    p.parent.mkdir(parents=True)
    p.write_text("# @feature: FP-GATE-META\n", encoding="utf-8")

    def fail_run(*_args: Any, **_kw: Any) -> Any:
        raise AssertionError("干净文件不应触发 git show")

    monkeypatch.setattr(mod.subprocess, "run", fail_run)
    assert "# @feature: FP-GATE-META" in mod.read_commit_face_text(p, "tests/t.py", (set(), set()))


def test_read_commit_face_text_git_failure_falls_back_to_disk(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git show 失败（tracked 脏面但 HEAD 异常）→ 盘面兜底，不崩不漏。"""
    p = tmp_path / "tests" / "t.py"
    p.parent.mkdir(parents=True)
    p.write_text("# @feature: FP-GATE-META\n", encoding="utf-8")
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: _GitR(128, b""))

    assert "# @feature: FP-GATE-META" in mod.read_commit_face_text(p, "tests/t.py", (set(), {"tests/t.py"}))


def test_read_commit_face_text_staged_deleted_returns_empty(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """暂存删除：盘上无文件且索引读取失败 → 空串（删除的文件不贡献标记）。"""
    p = tmp_path / "tests" / "t.py"  # 故意不创建
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: _GitR(128, b""))

    assert mod.read_commit_face_text(p, "tests/t.py", ({"tests/t.py"}, set())) == ""


def test_read_commit_face_text_none_env_reads_disk(mod: Any, tmp_path: Path) -> None:
    """非 git 环境（changes=None）→ 全盘面，行为与收敛前一致。"""
    p = tmp_path / "tests" / "t.py"
    p.parent.mkdir(parents=True)
    p.write_text("# @feature: FP-GATE-META\n", encoding="utf-8")

    assert "# @feature: FP-GATE-META" in mod.read_commit_face_text(p, "tests/t.py", None)
