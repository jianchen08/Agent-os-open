# @feature: FP-0.2.〇 隔离工作区 工具执行 | @ci: python-coverage
"""worktree_merge 缺口分支补测。

覆盖既有套件未触达的分支（行号锚定 2026-09-13 插桩车道）：
- ``_parse_porcelain_path``：短行/纯删除/重命名/带引号路径/空路径；
- ``_pending_products``：status 读取失败、porcelain 异形行跳过、分支差异读取
  失败（保留 uncommitted 位）、diff 异形行跳过、diff 侧重命名与空路径；
- ``_content_equal``：单侧缺失（OSError→False）与字节级相等/不等；
- ``on_eval_passed`` 前置守卫：缺 project_root / 缺 branch / 产物快照失败透传；
- ``_verify_products_arrived``：project_root 不存在、应删仍在、应到未到、
  问题清单 ≥10 截断、全到达成功路径。

外部依赖仅 git CLI 与 tmp 目录；git 输出异常面经 monkeypatch 注入（外部边界）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

import worktree_merge  # noqa: E402 — 依赖 conftest 的 sys.path 注入
from worktree_merge import WorktreeMerger  # noqa: E402


def git(*args: str, cwd: Path | str | None = None) -> tuple[int, str, str]:
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def _make_repo(base: Path, name: str = "project") -> Path:
    proj = base / name
    proj.mkdir()
    git("init", "-b", "main", cwd=proj)
    git("config", "user.email", "test@test.com", cwd=proj)
    git("config", "user.name", "Test", cwd=proj)
    (proj / "hello.txt").write_text("hello", encoding="utf-8")
    git("add", "-A", cwd=proj)
    git("commit", "-m", "init", cwd=proj)
    return proj


# ── _parse_porcelain_path（status 行解析）────────────────────


class TestParsePorcelainPath:
    @pytest.mark.parametrize("entry", ["", "AB", "XY ", "A   "])
    def test_malformed_entries_return_none(self, entry: str) -> None:
        """短于 4 字符或路径段剥离后为空的行 → None（不可解析）。"""
        assert WorktreeMerger._parse_porcelain_path(entry) is None

    def test_plain_add_and_delete_paths(self) -> None:
        assert WorktreeMerger._parse_porcelain_path("A  src/new.py") == ("src/new.py", False)
        assert WorktreeMerger._parse_porcelain_path(" D  src/gone.py") == ("src/gone.py", True)
        assert WorktreeMerger._parse_porcelain_path("D  src/gone.py") == ("src/gone.py", True)

    def test_rename_takes_new_side_as_added(self) -> None:
        """重命名取 new 侧按新增核验，不核验 old 侧。"""
        parsed = WorktreeMerger._parse_porcelain_path("R  old.txt -> new.txt")
        assert parsed == ("new.txt", False)

    def test_quoted_path_with_spaces_unquoted(self) -> None:
        parsed = WorktreeMerger._parse_porcelain_path('M  "my file.txt"')
        assert parsed == ("my file.txt", False)


# ── _pending_products（产物快照两路并集）────────────────────


def _stub_git_call(calls: list[tuple[str, tuple[int, str, str]]], rc_out_err: tuple[int, str, str]):
    """注入按调用序回放的 _run_git 桩，并记录收到的参数。"""

    def fake(*args: str, **kw: Any) -> tuple[int, str, str]:
        calls.append((" ".join(args), rc_out_err))
        return rc_out_err

    return fake


class TestPendingProductsBranches:
    def test_status_read_failure_reports_workspace_error(self, monkeypatch: Any) -> None:
        m = WorktreeMerger()
        monkeypatch.setattr(
            m, "_run_git", _stub_git_call([], (128, "", "fatal: not a git repository"))
        )
        result = m._pending_products("D:/no_ws", "D:/proj", "task/t1")
        assert result["error"] != "" and "工作区状态读取失败" in result["error"]
        assert result["items"] == {} and result["uncommitted"] is False

    def test_status_unparseable_lines_skipped(self, monkeypatch: Any) -> None:
        """porcelain 异形行（<4 字符）逐行跳过，不进期望集也不置 uncommitted。"""
        m = WorktreeMerger()
        monkeypatch.setattr(
            m,
            "_run_git",
            _stub_git_call(
                [],
                (0, "??\nAB\nM  real.txt\n", ""),
            ),
        )
        result = m._pending_products("ws", "proj", "task/t1")
        assert result["error"] == ""
        assert result["items"] == {"real.txt": False}
        assert result["uncommitted"] is True

    def test_branch_diff_read_failure_keeps_uncommitted_flag(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """分支差异读取失败：报错且保留 status 侧已判定的 uncommitted 位。"""
        proj = _make_repo(tmp_path)
        (proj / "dirty.txt").write_text("uncommitted", encoding="utf-8")
        m = WorktreeMerger()
        original = m._run_git

        def fake(*args: str, **kw: Any) -> tuple[int, str, str]:
            if "diff" in args:
                return (128, "", "fatal: bad revision")
            return original(*args, **kw)

        monkeypatch.setattr(m, "_run_git", fake)
        result = m._pending_products(str(proj), str(proj), "task/missing")
        assert "分支差异读取失败" in result["error"]
        assert result["uncommitted"] is True

    def test_diff_malformed_and_rename_and_empty_paths(self, monkeypatch: Any) -> None:
        """diff --name-status 输出异常面：无 tab 行跳过、重命名取 new 侧、空路径跳过。"""
        m = WorktreeMerger()

        def fake(*args: str, **kw: Any) -> tuple[int, str, str]:
            if "diff" in args:
                out = "R100\told_a.txt -> new_a.txt\nmalformed-no-tab\nA\t\nM\tkept.txt\n"
                return (0, out, "")
            return (0, "", "")

        monkeypatch.setattr(m, "_run_git", fake)
        result = m._pending_products("ws", "proj", "task/t1")
        assert result["error"] == ""
        assert result["items"] == {"new_a.txt": False, "kept.txt": False}
        assert result["uncommitted"] is False

    def test_real_repo_union_of_uncommitted_and_branch_diff(self, tmp_path: Path) -> None:
        """真仓两路并集性质：未提交产物 ∪ 分支未并入产物，删除项标记 True。"""
        proj = _make_repo(tmp_path)
        wt = tmp_path / "wt_union"
        git("worktree", "add", "-b", "task/union", str(wt), cwd=proj)
        (wt / "committed.txt").write_text("committed", encoding="utf-8")
        git("add", "-A", cwd=wt)
        git("commit", "-m", "branch work", cwd=wt)
        git("rm", "hello.txt", cwd=wt)
        git("commit", "-m", "drop baseline file", cwd=wt)
        (wt / "uncommitted.txt").write_text("wip", encoding="utf-8")

        result = WorktreeMerger()._pending_products(str(wt), str(proj), "task/union")
        assert result["error"] == ""
        items = result["items"]
        assert items["uncommitted.txt"] is False
        assert items["committed.txt"] is False
        assert items["hello.txt"] is True
        assert result["uncommitted"] is True


# ── _content_equal（OSError 面）─────────────────────────────


class TestContentEqual:
    def test_missing_side_is_false(self, tmp_path: Path) -> None:
        m = WorktreeMerger()
        a = tmp_path / "a.txt"
        a.write_text("x", encoding="utf-8")
        missing = tmp_path / "missing.txt"
        assert m._content_equal(a, missing) is False
        assert m._content_equal(missing, a) is False

    def test_byte_level_comparison(self, tmp_path: Path) -> None:
        m = WorktreeMerger()
        (tmp_path / "l.txt").write_bytes(b"same-bytes")
        (tmp_path / "r.txt").write_bytes(b"same-bytes")
        (tmp_path / "d.txt").write_bytes(b"other-bytes")
        assert m._content_equal(tmp_path / "l.txt", tmp_path / "r.txt") is True
        assert m._content_equal(tmp_path / "l.txt", tmp_path / "d.txt") is False


# ── on_eval_passed 前置守卫与快照失败透传 ────────────────────


class TestOnEvalPassedGuards:
    def test_missing_project_root_is_failure(self) -> None:
        result = WorktreeMerger().on_eval_passed("t1", "D:/wt", {"branch": "task/t1"})
        assert result == {"success": False, "error": "缺少 project_root 信息"}

    def test_missing_branch_is_failure(self, tmp_path: Path) -> None:
        result = WorktreeMerger().on_eval_passed(
            "t1", "D:/wt", {"project_root": str(tmp_path), "branch": ""}
        )
        assert result == {"success": False, "error": "缺少 branch 信息，ws_meta 不完整"}

    def test_pending_snapshot_error_propagates_verbatim(self, tmp_path: Path) -> None:
        """产物快照失败：错误原样透传（读失败 ≠ 合并失败，文案不得误标）。"""
        proj = _make_repo(tmp_path)
        result = WorktreeMerger().on_eval_passed(
            "t1", str(tmp_path / "no_such_wt"), {"project_root": str(proj), "branch": "main"}
        )
        assert result["success"] is False
        assert "工作区状态读取失败" in result["error"]
        assert "worktree 合并失败" not in result["error"]


# ── _verify_products_arrived（产物到达对比）──────────────────


class TestVerifyProductsArrived:
    def test_project_root_missing_is_failure(self, tmp_path: Path) -> None:
        ok, msg = WorktreeMerger()._verify_products_arrived(
            str(tmp_path / "ws"), str(tmp_path / "no_root"), {"a.txt": False}
        )
        assert ok is False and "project_root 不存在" in msg

    def test_all_products_arrived_is_success(self, tmp_path: Path) -> None:
        ws, proj = tmp_path / "ws", tmp_path / "proj"
        ws.mkdir()
        proj.mkdir()
        for name in ("a.txt", "sub/b.txt"):
            (ws / name).parent.mkdir(parents=True, exist_ok=True)
            (ws / name).write_text(name, encoding="utf-8")
            (proj / name).parent.mkdir(parents=True, exist_ok=True)
            (proj / name).write_text(name, encoding="utf-8")
        ok, msg = WorktreeMerger()._verify_products_arrived(
            str(ws), str(proj), {"a.txt": False, "sub/b.txt": False}
        )
        assert ok is True and "产物全部到达" in msg

    def test_deleted_product_still_present_is_problem(self, tmp_path: Path) -> None:
        ws, proj = tmp_path / "ws", tmp_path / "proj"
        ws.mkdir()
        proj.mkdir()
        (proj / "should_be_gone.txt").write_text("still here", encoding="utf-8")
        ok, msg = WorktreeMerger()._verify_products_arrived(
            str(ws), str(proj), {"should_be_gone.txt": True}
        )
        assert ok is False and "应已删除仍存在" in msg

    def test_missing_product_is_problem(self, tmp_path: Path) -> None:
        ws, proj = tmp_path / "ws", tmp_path / "proj"
        ws.mkdir()
        proj.mkdir()
        (ws / "never_merged.txt").write_text("only in wt", encoding="utf-8")
        ok, msg = WorktreeMerger()._verify_products_arrived(
            str(ws), str(proj), {"never_merged.txt": False}
        )
        assert ok is False and "never_merged.txt" in msg

    def test_problem_list_capped_at_ten(self, tmp_path: Path) -> None:
        """问题清单 ≥10 截断：截断只发生在内容不一致分支后，纯缺失项不触发；
        9 缺失 + 2 不一致 = 第 11 项触发 break，尾部缺失项不再核验。"""
        ws, proj = tmp_path / "ws", tmp_path / "proj"
        ws.mkdir()
        proj.mkdir()
        for i in range(9):
            (ws / f"missing_{i}.txt").write_text("x", encoding="utf-8")
        for name in ("diff_1.txt", "diff_2.txt"):
            (ws / name).write_text("wt", encoding="utf-8")
            (proj / name).write_text("main", encoding="utf-8")
        (ws / "late.txt").write_text("never checked", encoding="utf-8")
        pending = {f"missing_{i}.txt": False for i in range(9)}
        pending["diff_1.txt"] = False
        pending["diff_2.txt"] = False
        pending["late.txt"] = False
        ok, msg = WorktreeMerger()._verify_products_arrived(str(ws), str(proj), pending)
        assert ok is False
        assert msg.count(".txt") == 10
        assert "late.txt" not in msg

    def test_deleted_satisfied_and_content_mismatch(self, tmp_path: Path) -> None:
        """已删除项满足（不报）与内容不一致项同场判定。"""
        ws, proj = tmp_path / "ws", tmp_path / "proj"
        ws.mkdir()
        proj.mkdir()
        (ws / "diff.txt").write_text("wt version", encoding="utf-8")
        (proj / "diff.txt").write_text("main version", encoding="utf-8")
        ok, msg = WorktreeMerger()._verify_products_arrived(
            str(ws), str(proj), {"diff.txt": False, "gone.txt": True}
        )
        assert ok is False
        assert "内容不一致" in msg
        assert "gone.txt" not in msg

    def test_verify_merge_in_main_fully_merged_is_true(self, tmp_path: Path) -> None:
        """``_verify_merge_in_main``：分支全部并入当前分支（HEAD..branch 空）→ True。"""
        proj = _make_repo(tmp_path)
        git("branch", "task/merged", cwd=proj)
        assert WorktreeMerger()._verify_merge_in_main("task/merged", cwd=proj) is True
