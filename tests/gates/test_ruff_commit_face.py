# @feature: FP-GATE-META 元门禁 gate-wiring | @ci: python-coverage
"""ruff_commit_face 提交面扫描器契约测试。

锁 ADR 2026-11-07-gate-evaluation-face-commit-scope 的分类行为：
未跟踪不扫、未暂存改动按索引内容扫、其余走批量目录扫描；
输出末尾恰一条 `Found N errors.` 供 check_ruff_baseline 原样消费。

scripts/ 非包，经 importlib 唯名模块加载；git/ruff 子进程不真跑
（分类是纯函数行为，端到端冒烟一例走真实仓 scripts/ 域）。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ruff_commit_face.py"


@pytest.fixture
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("rcf_test_mod", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["rcf_test_mod"] = m
    spec.loader.exec_module(m)
    return m


def _status_z(entries: list[str]) -> bytes:
    return "\0".join(entries).encode("utf-8")


def test_split_faces_untracked_excluded(mod: Any) -> None:
    """`??` 未跟踪 → excluded（批量扫描剔除，不计入基线）。"""
    excluded, dirty = mod.split_faces(
        _status_z(["?? plugins/shared/mode_roleplay/rp_game.py", " M plugins/a.py", ""])
    )

    assert excluded == {"plugins/shared/mode_roleplay/rp_game.py"}
    assert dirty == {"plugins/a.py"}


def test_split_faces_unstaged_dirty_uses_index(mod: Any) -> None:
    """Y=M/D（未暂存改动/删除）→ dirty（单独按索引内容扫）。"""
    excluded, dirty = mod.split_faces(
        _status_z(
            [
                "MM plugins/staged_and_edited.py",
                " M plugins/unstaged_edit.py",
                " D plugins/unstaged_delete.py",
                "M  plugins/staged_only.py",
            ]
        )
    )

    assert excluded == set()
    assert dirty == {
        "plugins/staged_and_edited.py",
        "plugins/unstaged_edit.py",
        "plugins/unstaged_delete.py",
    }, "仅暂存（M␣）磁盘=索引，走批量扫描，不进 dirty"


def test_split_faces_windows_path_normalized(mod: Any) -> None:
    """Windows 反斜杠路径归一为 POSIX（与 --exclude / git show :path 口径一致）。"""
    excluded, dirty = mod.split_faces(_status_z(["?? plugins\\win\\untracked.py"]))

    assert excluded == {"plugins/win/untracked.py"}
    assert dirty == set()


def test_split_faces_malformed_entry_ignored(mod: Any) -> None:
    """短条目（空段/残段）跳过不崩。"""
    excluded, dirty = mod.split_faces(_status_z(["", "M", "?? plugins/ok.py"]))

    assert excluded == {"plugins/ok.py"}
    assert dirty == set()


def test_strip_and_count_takes_last_summary(
    mod: Any,
) -> None:
    """真实 ruff 输出形态：Found 行剥净、计数取其值，warnings 与 hidden fixes 保留。"""
    out = (
        "warning: Invalid `# noqa` directive on a.py:1\n"
        "a.py:1:1: E501 too long\n"
        "Found 2 errors.\n"
        "2 hidden fixes can be enabled with the `--unsafe-fixes` option.\n"
    )

    body, count = mod._strip_and_count(out)

    assert count == 2
    assert "Found 2 errors" not in body
    assert "E501 too long" in body
    assert "hidden fixes" in body
    assert "warning: Invalid" in body


def test_run_emits_single_found_line(
    mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端形态：多条 ruff 输出汇总后恰一条 Found 行，计数相加、明细保留。"""
    bulk = "a.py:1:1: E501 too long\n"
    dirty_run = "b.py:2:1: F401 unused\n"

    def fake_ruff(args: list[str], stdin: bytes | None = None) -> tuple[str, int]:
        if stdin is None:
            return bulk, 2
        return dirty_run, 1

    monkeypatch.setattr(
        mod, "_git", lambda *a: _status_z([" M plugins/b.py", ""]) if "status" in a[0] else b"x"
    )
    monkeypatch.setattr(mod, "_ruff", fake_ruff)

    rc = mod.run(["plugins/"])
    out = capsys.readouterr().out

    assert rc == 0
    assert len(re.findall(r"^Found \d+ errors?", out, re.M)) == 1, out
    assert re.search(r"^Found 3 errors\.$", out, re.M), out
    assert "a.py:1:1: E501" in out
    assert "b.py:2:1: F401" in out
    assert "── 提交面（索引内容）" in out, "dirty 文件须以索引内容段呈现"


def test_run_excludes_per_path_repeated_flag(
    mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ruff --exclude 单值语义回归锁：逐路径重复旗标，不把路径漏成位置参数。"""
    captured: list[list[str]] = []

    def fake_ruff(args: list[str], stdin: bytes | None = None) -> tuple[str, int]:
        if stdin is None:
            captured.append(args)
            return "", 0
        return "", 0

    status = _status_z(
        ["?? plugins/u1.py", "?? plugins/u2.py", " M plugins/dirty.py", ""]
    )
    monkeypatch.setattr(
        mod, "_git", lambda *a: status if "status" in a[0] else b"blob"
    )
    monkeypatch.setattr(mod, "_ruff", fake_ruff)

    mod.run(["plugins/"])

    args = captured[0]
    assert args.count("--exclude") == 3, args
    idx = args.index("--exclude")
    assert args[idx + 1 : idx + 2] == ["plugins/dirty.py"], "sorted 序首个被剔的是 dirty"
    assert {"plugins/u1.py", "plugins/u2.py"} <= set(args)


def test_run_falls_back_to_full_scan_without_git(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """git 不可用（_git 返回 None）→ 全量扫描（CI 独立运行等价旧行为）。"""
    calls: list[list[str]] = []

    def fake_ruff(args: list[str], stdin: bytes | None = None) -> tuple[str, int]:
        calls.append(args)
        return "Found 7 errors.\n", 7

    monkeypatch.setattr(mod, "_git", lambda *_a: None)
    monkeypatch.setattr(mod, "_ruff", fake_ruff)

    mod.run(["scripts/"])

    assert calls == [["scripts/"]]


def test_real_repo_smoke_single_found_line(mod: Any) -> None:
    """真实依赖一例：真实仓 scripts/ 域跑通，末尾恰一条 Found 行且计数为非负整数。"""
    r = subprocess.run(
        (sys.executable, str(_SCRIPT), "scripts/"),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert r.returncode == 0
    found = re.findall(r"^Found (\d+) errors?", r.stdout, re.M)
    assert len(found) == 1, f"Found 行必须恰一条：{found}"
    assert int(found[0]) >= 0


def test_ruff_parses_singular_found_line(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单数形态 `Found 1 error.`（N=1 无 s）必须可解析——stdin 单违规面
    此前因复核实查复数形态而误判"缺汇总行"（2026-11-16 触发：本地 dirty
    .py 面首次出现单违规文件）。"""
    out = "x.py:1:1: E402 Module level import not at top of file\nFound 1 error.\n"

    class _FakeResult:
        returncode = 1
        stdout = out.encode("utf-8")
        stderr = b""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeResult())
    body, count = mod._ruff(["--stdin-filename", "x.py", "-"])

    assert count == 1
    assert "Found" not in body
    assert "E402" in body


def test_ruff_parses_zero_violation_all_checks_passed(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零违规形态 `All checks passed!`（退出码 0，无 Found 行）按退出码记 0——
    清洁扫描是确定语义，不是"没数到"（此前同样误判"缺汇总行"）。"""
    out = "All checks passed!\n"

    class _FakeResult:
        returncode = 0
        stdout = out.encode("utf-8")
        stderr = b""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeResult())
    body, count = mod._ruff(["--stdin-filename", "clean.py", "-"])

    assert count == 0
    assert "All checks passed!" in body


def test_ruff_still_fails_without_any_summary(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有违规明细但无任何汇总行（截断/内部错误）仍 fail-closed（计数 None），
    防零违规兜底把"跑不起来"读成"零违规"（假绿）。"""
    out = "x.py:1:1: E501 too long\n"

    class _FakeResult:
        returncode = 1
        stdout = out.encode("utf-8")
        stderr = b""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeResult())
    body, count = mod._ruff(["--stdin-filename", "x.py", "-"])

    assert count is None
    assert "缺 `Found N errors.` 汇总行" in body
