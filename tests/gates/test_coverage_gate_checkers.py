# @feature: FP-GATE 覆盖率棘轮门禁 | @ci: python-coverage
"""覆盖率棘轮门禁检查器单测（ADR 2026-08-20）。

覆盖检查器的可单测面：
1. check_python_coverage_baseline：coverage.xml line-rate 解析 + 基线对照 +
   --init 拒降 + **自动棘轮（2026-08-21 用户裁决：绿跑即写 floor(实测)+1，
   只替换数值行保留归因注释；红跑不动文件）**；
2. check_diff_coverage：unified diff / coverage.xml / lcov 解析 + 真 git 仓库集成
   （红/绿/缺文件 fail-loud/[skip-diff-cov] 逃生）；
3. check_frontend_baseline：vitest 覆盖率%解析（text-summary / All files 表 / 无数据）；
4. check_rust_coverage_baseline：lcov line% 解析 + 自动棘轮同款行为。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
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


py_cov = _load("check_python_coverage_baseline")
diff_cov = _load("check_diff_coverage")
fe_base = _load("check_frontend_baseline")
rust_cov = _load("check_rust_coverage_baseline")


# ── check_python_coverage_baseline ────────────────────────────────

COVERAGE_XML = """<?xml version="1.0" ?>
<coverage line-rate="0.5123" branch-rate="0" lines-valid="100" version="7.15.4">
  <sources><source>{root}\\plugins</source></sources>
  <packages><package name="pkg">
    <classes><class filename="shared\\x\\server.py" name="server.py">
      <methods/>
      <lines>
        <line hits="1" number="1"/>
        <line hits="0" number="2"/>
      </lines>
    </class></classes>
  </package></packages>
</coverage>
"""


def _write_cov_xml(tmp_path: Path, line_rate: str) -> Path:
    xml = COVERAGE_XML.format(root=tmp_path.as_posix()).replace(
        'line-rate="0.5123"', f'line-rate="{line_rate}"'
    )
    p = tmp_path / "coverage.xml"
    p.write_text(xml, encoding="utf-8")
    return p


class TestPythonCoverageBaseline:
    def test_parse_line_rate(self, tmp_path: Path) -> None:
        assert py_cov.parse_coverage_line_pct(_write_cov_xml(tmp_path, "0.5123")) == pytest.approx(51.23)

    def test_parse_empty_report_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "coverage.xml"
        p.write_text("<coverage />", encoding="utf-8")
        assert py_cov.parse_coverage_line_pct(p) is None

    def test_below_baseline_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        monkeypatch.setattr(py_cov, "BASELINE_FILE", tmp_path / "baseline.txt")
        (tmp_path / "baseline.txt").write_text("python_line_coverage=51.5\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.5123"))])
        assert py_cov.main() == 1
        assert "低于基线" in capsys.readouterr().err

    def test_at_or_above_baseline_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(py_cov, "BASELINE_FILE", tmp_path / "baseline.txt")
        (tmp_path / "baseline.txt").write_text("python_line_coverage=51.23\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.5123"))])
        assert py_cov.main() == 0

    def test_init_refuses_to_lower(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(py_cov, "BASELINE_FILE", tmp_path / "baseline.txt")
        (tmp_path / "baseline.txt").write_text("python_line_coverage=60\n", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.5123")), "--init"]
        )
        assert py_cov.main() == 1

    def test_init_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(py_cov, "BASELINE_FILE", bf)
        bf.write_text("python_line_coverage=50\n", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.5123")), "--init"]
        )
        assert py_cov.main() == 0
        assert "python_line_coverage=51.23" in bf.read_text(encoding="utf-8")

    def test_green_run_auto_ratchets_to_next_integer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # 2026-08-21 用户裁决：绿跑自动把基线写到 floor(实测)+1（47.48→48 式）
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(py_cov, "BASELINE_FILE", bf)
        bf.write_text("# 归因注释\npython_line_coverage=51.0\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.5123"))])
        assert py_cov.main() == 0
        text = bf.read_text(encoding="utf-8")
        assert "python_line_coverage=52.00" in text
        assert "# 归因注释" in text  # 只替换数值行，归因注释保留
        assert "自动棘轮" in capsys.readouterr().out

    def test_red_run_leaves_baseline_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(py_cov, "BASELINE_FILE", bf)
        bf.write_text("python_line_coverage=60\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.5123"))])
        assert py_cov.main() == 1
        assert bf.read_text(encoding="utf-8") == "python_line_coverage=60\n"

    def test_exact_integer_measured_ratchets_plus_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 实测恰为整数（52.00）且等于基线 → 绿，棘轮到 53（保持恒高于实测）
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(py_cov, "BASELINE_FILE", bf)
        bf.write_text("python_line_coverage=52.00\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--xml", str(_write_cov_xml(tmp_path, "0.52"))])
        assert py_cov.main() == 0
        assert "python_line_coverage=53.00" in bf.read_text(encoding="utf-8")


# ── check_diff_coverage：纯解析函数 ────────────────────────────────

class TestParsers:
    def test_parse_unified_diff_u0(self) -> None:
        text = (
            "diff --git a/plugins/shared/x.py b/plugins/shared/x.py\n"
            "--- a/plugins/shared/x.py\n"
            "+++ plugins/shared/x.py\n"
            "@@ -5,2 +5,3 @@\n"
            "@@ -10 +12,0 @@\n"  # 纯删除：新增侧计数 0
            "@@ -0,0 +20,4 @@\n"
        )
        added = diff_cov.parse_unified_diff(text)
        assert added == {"plugins/shared/x.py": [5, 6, 7, 20, 21, 22, 23]}

    def test_parse_unified_diff_devnull(self) -> None:
        text = "--- /dev/null\n+++ plugins/new.py\n@@ -0,0 +1,2 @@\n"
        assert diff_cov.parse_unified_diff(text) == {"plugins/new.py": [1, 2]}

    def test_parse_lcov_absolute_sf_paths(self) -> None:
        text = (
            "SF:D:\\repo\\frontend\\src\\a.ts\n"
            "DA:1,1\n"
            "DA:2,0\n"
            "end_of_record\n"
        )
        # ROOT 前缀剥除只在命中仓库根时生效；此处仅验证反斜杠规范化与命中解析
        m = diff_cov.parse_lcov(text)
        assert "frontend/src/a.ts" in m or "D:/repo/frontend/src/a.ts" in m
        entry = m.get("frontend/src/a.ts") or m.get("D:/repo/frontend/src/a.ts")
        assert entry == {1: True, 2: False}

    def test_parse_coverage_xml_source_relative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 真实 coverage.py 7.x 格式：filename 相对 <source> 根（如 plugins）
        monkeypatch.setattr(diff_cov, "ROOT", Path("D:/repo"))
        text = COVERAGE_XML.format(root="D:/repo")
        m = diff_cov.parse_coverage_xml(text)
        assert m["plugins/shared/x/server.py"] == {1: True, 2: False}

    def test_parse_coverage_xml_repo_relative_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 旧/异构格式：filename 直接是仓库相对路径（无匹配 source）
        monkeypatch.setattr(diff_cov, "ROOT", Path("D:/repo"))
        text = (
            '<coverage line-rate="0"><packages><package><classes>'
            '<class filename="kernel/crates/a.rs"><lines>'
            '<line hits="1" number="3"/>'
            "</lines></class></classes></package></packages></coverage>"
        )
        m = diff_cov.parse_coverage_xml(text)
        assert m["kernel/crates/a.rs"] == {3: True}

    def test_norm_path_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(diff_cov, "ROOT", Path("D:/repo"))
        assert diff_cov.norm_path("b\\plugins\\x.py") == "plugins/x.py"
        assert diff_cov.norm_path("D:\\repo\\kernel\\crates\\a.rs") == "kernel/crates/a.rs"
        assert diff_cov.norm_path("./frontend/src/a.ts") == "frontend/src/a.ts"


# ── check_diff_coverage：真 git 仓库集成 ────────────────────────────

def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "HOME": str(repo),
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """两提交临时仓库：c1 建文件，c2 改动（含覆盖/未覆盖行）+ 新增未测文件。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "plugins" / "shared").mkdir(parents=True)
    (repo / "plugins" / "shared" / "x.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    (repo / "plugins" / "shared" / "x.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c2")
    monkeypatch.setattr(diff_cov, "ROOT", repo)
    return repo


def _run_diff_cov(monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    argv = ["check_diff_coverage.py", *extra]
    monkeypatch.setattr(sys, "argv", argv)
    return diff_cov.main()


class TestDiffCoverageIntegration:
    def test_uncovered_changed_line_fails(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # 真实格式：filename 相对 <source>（=repo/plugins），经 resolve 映射到仓库相对路径
        xml = tmp_path / "cov.xml"
        src = (git_repo / "plugins").as_posix()
        xml.write_text(
            f'<coverage line-rate="0.5"><sources><source>{src}</source></sources>'
            '<packages><package><classes>'
            '<class filename="shared\\x.py"><lines>'
            '<line hits="1" number="1"/><line hits="1" number="2"/><line hits="0" number="3"/>'
            "</lines></class></classes></package></packages></coverage>",
            encoding="utf-8",
        )
        rc = _run_diff_cov(
            monkeypatch,
            "--coverage-file", str(xml), "--format", "xml",
            "--scope", "plugins", "--ext", ".py",
            "--range", "HEAD^..HEAD",
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "plugins/shared/x.py" in out
        assert "3" in out

    def test_fully_covered_passes(self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        xml = tmp_path / "cov.xml"
        xml.write_text(
            '<coverage line-rate="1"><packages><package><classes>'
            '<class filename="plugins\\shared\\x.py"><lines>'
            '<line hits="1" number="1"/><line hits="1" number="2"/><line hits="1" number="3"/>'
            "</lines></class></classes></package></packages></coverage>",
            encoding="utf-8",
        )
        rc = _run_diff_cov(
            monkeypatch,
            "--coverage-file", str(xml), "--format", "xml",
            "--scope", "plugins", "--ext", ".py",
            "--range", "HEAD^..HEAD",
        )
        assert rc == 0

    def test_missing_in_scope_file_fails_loud(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 改动文件在 scope 但不在覆盖率文件 → fail-loud（度量面漂移）
        xml = tmp_path / "cov.xml"
        xml.write_text('<coverage line-rate="0"><packages/></coverage>', encoding="utf-8")
        rc = _run_diff_cov(
            monkeypatch,
            "--coverage-file", str(xml), "--format", "xml",
            "--scope", "plugins", "--ext", ".py",
            "--range", "HEAD^..HEAD",
        )
        assert rc == 1

    def test_skip_marker_escapes(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        xml = tmp_path / "cov.xml"
        xml.write_text('<coverage line-rate="0"><packages/></coverage>', encoding="utf-8")
        _git(git_repo, "commit", "-q", "--allow-empty", "-m", "chore [skip-diff-cov]")
        rc = _run_diff_cov(
            monkeypatch,
            "--coverage-file", str(xml), "--format", "xml",
            "--scope", "plugins", "--ext", ".py",
            "--range", "HEAD^..HEAD",
        )
        assert rc == 0
        assert "[skip-diff-cov]" in capsys.readouterr().out

    def test_push_mode_defaults_to_last_commit(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # 无 --range/--base/GITHUB_BASE_REF → HEAD^..HEAD（c1..c2 有可度量行）
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        xml = tmp_path / "cov.xml"
        xml.write_text(
            '<coverage line-rate="1"><packages><package><classes>'
            '<class filename="plugins\\shared\\x.py"><lines>'
            '<line hits="1" number="1"/><line hits="1" number="2"/><line hits="1" number="3"/>'
            "</lines></class></classes></package></packages></coverage>",
            encoding="utf-8",
        )
        rc = _run_diff_cov(
            monkeypatch, "--coverage-file", str(xml), "--format", "xml",
            "--scope", "plugins", "--ext", ".py",
        )
        assert rc == 0
        assert "HEAD^..HEAD" in capsys.readouterr().out

    def test_omitted_paths_not_measured(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tests/ 下的改动文件按口径 omit，不要求出现在覆盖率文件里
        (git_repo / "plugins" / "shared" / "tests").mkdir(parents=True)
        (git_repo / "plugins" / "shared" / "tests" / "test_x.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-q", "-m", "c3 add test")
        xml = tmp_path / "cov.xml"
        xml.write_text('<coverage line-rate="0"><packages/></coverage>', encoding="utf-8")
        rc = _run_diff_cov(
            monkeypatch,
            "--coverage-file", str(xml), "--format", "xml",
            "--scope", "plugins", "--ext", ".py",
            "--omit", "/tests/",
            "--range", "HEAD^..HEAD",
        )
        assert rc == 0  # 无可度量源码行 → 放行


# ── check_rust_coverage_baseline：解析 + 自动棘轮 ─────────────────

LCOV_50 = "SF:crates/a.rs\nDA:1,1\nDA:2,0\nend_of_record\n"


class TestRustCoverageBaseline:
    def _lcov(self, tmp_path: Path) -> Path:
        p = tmp_path / "coverage.lcov"
        p.write_text(LCOV_50, encoding="utf-8")
        return p

    def test_parse_lcov_line_pct(self, tmp_path: Path) -> None:
        assert rust_cov.parse_lcov_line_pct(self._lcov(tmp_path)) == pytest.approx(50.0)

    def test_green_auto_ratchets_to_next_integer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # 实测 50.0 恰为整数且等于基线 → 绿，棘轮 51.0（恒高于实测）
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(rust_cov, "BASELINE_FILE", bf)
        bf.write_text("# 归因注释\nrust_line_coverage=50.0\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--lcov", str(self._lcov(tmp_path))])
        assert rust_cov.main() == 0
        text = bf.read_text(encoding="utf-8")
        assert "rust_line_coverage=51.0" in text
        assert "# 归因注释" in text
        assert "自动棘轮" in capsys.readouterr().out

    def test_below_baseline_fails_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(rust_cov, "BASELINE_FILE", bf)
        bf.write_text("rust_line_coverage=60.0\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["x", "--lcov", str(self._lcov(tmp_path))])
        assert rust_cov.main() == 1
        assert bf.read_text(encoding="utf-8") == "rust_line_coverage=60.0\n"

    def test_init_refuses_lowering(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(rust_cov, "BASELINE_FILE", bf)
        bf.write_text("rust_line_coverage=60.0\n", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv", ["x", "--lcov", str(self._lcov(tmp_path)), "--init"]
        )
        assert rust_cov.main() == 1

    def test_init_writes_exact_measured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(rust_cov, "BASELINE_FILE", bf)
        bf.write_text("rust_line_coverage=45.0\n", encoding="utf-8")
        monkeypatch.setattr(
            sys, "argv", ["x", "--lcov", str(self._lcov(tmp_path)), "--init"]
        )
        assert rust_cov.main() == 0
        assert "rust_line_coverage=50.0" in bf.read_text(encoding="utf-8")


# ── check_frontend_baseline：vitest 失败数解析 ─────────────────────

class TestFrontendVitestFailureParse:
    def test_failed_summary(self) -> None:
        out = " Test Files  1 failed (1)\n      Tests  3 failed | 2018 passed (2021)\n"
        assert fe_base.parse_vitest_failures(out) == 3

    def test_all_green_summary_is_zero(self) -> None:
        # 全绿摘要无 failed 段（基线收紧到 0 后的常态）→ 0，而非误判度量链断裂
        out = " Test Files  246 passed (246)\n      Tests  2021 passed (2021)\n"
        assert fe_base.parse_vitest_failures(out) == 0

    def test_ansi_colored_all_green_summary_is_zero(self) -> None:
        out = (
            "\x1b[32m\x1b[1m Test Files \x1b[22m\x1b[1m\x1b[32m246 passed\x1b[39m\x1b[22m\n"
            "\x1b[32m\x1b[1m      Tests \x1b[22m\x1b[1m\x1b[32m2021 passed\x1b[39m\x1b[22m \x1b[2m(\x1b[22m\x1b[2m2021\x1b[22m\x1b[2m)\x1b[22m\n"
        )
        assert fe_base.parse_vitest_failures(out) == 0

    def test_missing_summary_raises(self) -> None:
        with pytest.raises(RuntimeError):
            fe_base.parse_vitest_failures("some arbitrary output without summary")


# ── check_frontend_baseline：覆盖率%解析 ───────────────────────────

class TestFrontendCoverageParse:
    def test_parse_text_summary(self) -> None:
        out = (
            "========== Coverage summary ==========\n"
            "Statements   : 55.23% ( 1234/2231 )\n"
            "Branches     : 41.2% ( 300/728 )\n"
            "Functions    : 60.00% ( 200/333 )\n"
            "Lines        : 56.78% ( 1265/2227 )\n"
            "=======================================\n"
        )
        assert fe_base.parse_vitest_coverage_pct(out) == pytest.approx(56.78)

    def test_parse_all_files_table_row(self) -> None:
        out = (
            "File          | % Stmts | % Branch | % Funcs | % Lines | Uncovered Line #s\n"
            "All files     |   51.2  |    38.4  |   47.6  |   52.9  |\n"
        )
        assert fe_base.parse_vitest_coverage_pct(out) == pytest.approx(52.9)

    def test_no_coverage_data_returns_none(self) -> None:
        assert fe_base.parse_vitest_coverage_pct("Tests  3 failed | 1923 passed\n") is None

    def test_baseline_roundtrip_with_coverage(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bf = tmp_path / "baseline.txt"
        monkeypatch.setattr(fe_base, "BASELINE_FILE", bf)
        fe_base.write_baseline(3, 5, 52.9)
        assert fe_base.read_baseline() == (3, 5, 52.9)
