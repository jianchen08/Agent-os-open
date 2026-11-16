# @feature: FP-GATE-META 机械闸受保护清单 | @ci: python-coverage
"""受保护清单检查器回归（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 2）。

机械闸不可改的显式化执法：门禁配置/基线棘轮/评估指标/渲染规定源码列入清单，
diff 命中即红；唯一放行 = gate-change 独立任务（ADR + commit message
``[gate-change:`` 标记 + 全量档佐证）。

断行为口径：清单解析 / 命中判定 / 变更集三级回退 / strict 豁免语义。
外部依赖（git 子进程、HEAD commit message）通过 monkeypatch 注入（合规 mock：
外部依赖）；活体用例在真实仓库跑 CLI 未命中场景（真实链路兜底）。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_gate_protected.py"
_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_gate_protected", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_gate_protected", _mod)
_spec.loader.exec_module(_mod)

parse_protected_list = _mod.parse_protected_list
match_hits = _mod.match_hits
resolve_changed_files = _mod.resolve_changed_files


class TestParseProtectedList:
    """清单解析：注释行与空行剔除、路径保序。"""

    def test_parses_paths_skips_comments_and_blank_lines(self) -> None:
        text = (
            "# 头注释\n"
            "\n"
            "scripts/run_gates.py\n"
            "  \n"
            "  # 缩进注释\n"
            ".github/\n"
            "config/plugins/evaluation/evaluation_metrics.yaml\n"
        )
        assert parse_protected_list(text) == [
            "scripts/run_gates.py",
            ".github/",
            "config/plugins/evaluation/evaluation_metrics.yaml",
        ]

    def test_empty_text_yields_empty_list(self) -> None:
        assert parse_protected_list("") == []
        assert parse_protected_list("# 只有注释\n\n") == []


class TestMatchHits:
    """命中判定：文件精确、目录前缀（尾 /）、未命中——三组区分输入。"""

    def test_exact_file_pattern_matches_only_that_file(self) -> None:
        patterns = ["scripts/run_gates.py"]
        assert match_hits(["scripts/run_gates.py", "docs/a.md"], patterns) == [
            "scripts/run_gates.py"
        ]
        # 同目录兄弟文件不命中（精确 ≠ 前缀）
        assert (
            match_hits(["scripts/run_gates_backup.py"], patterns) == []
        )

    def test_directory_prefix_pattern_matches_descendants(self) -> None:
        patterns = [".github/"]
        assert match_hits([".github/mypy-baseline.txt"], patterns) == [
            ".github/mypy-baseline.txt"
        ]
        assert match_hits([".github/workflows/ci.yml"], patterns) == [
            ".github/workflows/ci.yml"
        ]
        # 前缀字符串相近但不在目录内：不命中
        assert match_hits([".githubx/other.txt"], patterns) == []

    def test_mixed_patterns_and_no_hit(self) -> None:
        patterns = ["scripts/run_gates.py", "docs/graph/"]
        assert match_hits(["kernel/src/lib.rs"], patterns) == []
        assert match_hits(
            ["docs/graph/viewer.html", "plugins/x/server.py"], patterns
        ) == ["docs/graph/viewer.html"]


class TestResolveChangedFiles:
    """变更集三级回退：显式 --changed-files 优先 > --range > base 合并基 > HEAD^ 兜底。"""

    def test_explicit_files_take_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            _mod, "_git_changed", lambda *a, **k: pytest.fail("不应触底 git 调用")
        )
        got = resolve_changed_files(changed_files=["a.py"], range_=None, base=None)
        assert got == ["a.py"]

    def test_range_falls_back_to_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_git(range_: str | None, base: str | None) -> list[str]:
            captured["range_"] = range_
            captured["base"] = base
            return ["x.py"]

        monkeypatch.setattr(_mod, "_git_changed", fake_git)
        got = resolve_changed_files(changed_files=None, range_="A..B", base=None)
        assert got == ["x.py"]
        assert captured["range_"] == "A..B"

    def test_no_args_uses_default_fallback_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_git(range_: str | None, base: str | None) -> list[str]:
            captured["range_"] = range_
            captured["base"] = base
            return []

        monkeypatch.setattr(_mod, "_git_changed", fake_git)
        resolve_changed_files(changed_files=None, range_=None, base="origin/dev/0.2")
        assert captured["range_"] is None
        assert captured["base"] == "origin/dev/0.2"


class TestStrictExemptionSemantics:
    """strict 豁免语义：本地提交阶段 message 未定 → 忽略豁免；CI 默认认标记。"""

    def _hits_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            _mod, "parse_protected_list", lambda *a, **k: ["scripts/run_gates.py"]
        )
        monkeypatch.setattr(
            _mod,
            "resolve_changed_files",
            lambda *a, **k: ["scripts/run_gates.py"],
        )

    def test_strict_ignores_marker_and_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._hits_setup(monkeypatch)
        monkeypatch.setattr(
            _mod, "head_commit_message", lambda: "feat: x [gate-change: docs/a.md]"
        )
        rc = _mod.main(["--strict", "--changed-files", "scripts/run_gates.py"])
        assert rc == 1
        assert "gate-change" in capsys.readouterr().err  # 拒绝信息须指路豁免流程

    def test_marker_message_allows_when_not_strict(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._hits_setup(monkeypatch)
        monkeypatch.setattr(
            _mod, "head_commit_message", lambda: "feat(gates): x [gate-change: ADR]"
        )
        rc = _mod.main(["--changed-files", "scripts/run_gates.py"])
        assert rc == 0
        # 放行必须可感知（禁静默降级）：stdout 须有全量档佐证提示
        assert "全量" in capsys.readouterr().out

    def test_hit_without_marker_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._hits_setup(monkeypatch)
        monkeypatch.setattr(_mod, "head_commit_message", lambda: "feat: plain")
        assert _mod.main(["--changed-files", "scripts/run_gates.py"]) == 1


class TestNoHitPasses:
    def test_clean_changes_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _mod, "parse_protected_list", lambda *a, **k: ["scripts/run_gates.py"]
        )
        monkeypatch.setattr(
            _mod, "resolve_changed_files", lambda *a, **k: ["plugins/x/server.py"]
        )
        assert _mod.main(["--changed-files", "plugins/x/server.py"]) == 0


class TestLiveCLI:
    """活体：真实仓库 CLI 未命中场景必绿（真实链路兜底，不依赖 HEAD 状态）。"""

    def test_clean_file_list_passes_on_real_repo(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--changed-files", "README.md"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr

    def test_protected_list_file_exists_and_covers_core_surfaces(self) -> None:
        """清单文件在位且覆盖方案 §3.6 列举的核心执法面。"""
        list_path = Path(__file__).resolve().parents[2] / ".github" / "gate-protected-paths.txt"
        text = list_path.read_text(encoding="utf-8")
        patterns = parse_protected_list(text)
        joined = "\n".join(patterns)
        # 方案 §3.6 显式列举的五类执法面
        assert "scripts/run_gates.py" in patterns  # run_gates 配置
        assert ".github/" in patterns  # 各 baseline 棘轮（目录整护）
        assert "config/plugins/evaluation/evaluation_metrics.yaml" in patterns
        assert "docs/graph/viewer.html" in patterns
        assert match_hits(["docs/schemas/文档类型规范.md"], patterns) == [
            "docs/schemas/文档类型规范.md"
        ]  # frontmatter schema（目录整护）
        # 自举：检查器自身与钩子安装面在列（CI 也是 CI 的被检对象）
        assert "scripts/check_gate_protected.py" in patterns
        # E0-38 修复轮：旧面 scripts/hooks/ 摘除后清出清单（回流即红），
        # 现役钩子面 githooks 必须仍在列
        assert "scripts/hooks/" not in patterns
        assert "scripts/githooks/" in patterns
        del joined

    def test_doc_frontmatter_checker_is_protected(self) -> None:
        """E0-40/X1：现役执法件 check_doc_frontmatter.py 本体在保护清单。

        该件由 commit cdfb0e0da 入提交面并接线 docs 车道（check_pre_commit
        LANE_GATES docs），不在清单 = 改执法器本体无需 gate-change（保护洞）。

        防拟合反例：不在清单的普通 scripts 路径仍 miss——本条目是精确文件
        匹配，扩围 ≠ scripts/ 目录整护。
        """
        list_path = Path(__file__).resolve().parents[2] / ".github" / "gate-protected-paths.txt"
        patterns = parse_protected_list(list_path.read_text(encoding="utf-8"))
        assert "scripts/check_doc_frontmatter.py" in patterns
        assert match_hits(["scripts/check_doc_frontmatter.py"], patterns) == [
            "scripts/check_doc_frontmatter.py"
        ]
        assert match_hits(["scripts/gen_working_index.py"], patterns) == []
        assert match_hits(["scripts/check_doc_frontmatter_backup.py"], patterns) == []


def _run_git(cwd: Path, *args: str) -> None:
    """临时仓 git 调用（显式身份，免宿主全局配置依赖）。"""
    proc = subprocess.run(
        ["git", "-c", "user.email=gate@probe.local", "-c", "user.name=GateProbe", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def _commit_cjk_protected_change(tmp_path: Path, message: str) -> Path:
    """临时仓：seed 提交后追加 docs/schemas/中文名文件的一次提交。"""
    _run_git(tmp_path, "init", "-q", ".")
    # CI 默认 core.quotepath=true 是本用例的判别力来源，显式锁定
    _run_git(tmp_path, "config", "core.quotepath", "true")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(tmp_path, "add", "-A")
    _run_git(tmp_path, "commit", "-q", "-m", "seed")
    target = tmp_path / "docs" / "schemas" / "文档类型规范.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")
    _run_git(tmp_path, "add", "-A")
    _run_git(tmp_path, "commit", "-q", "-m", message)
    return tmp_path


class TestCjkProtectedPathDetection:
    """CJK 文件名受保护路径必被检出（core.quotepath 默认 true 下不得 miss）。

    P-04 探针实测：旧实现走 ``git diff --name-only``（无 ``-z``），
    ``docs/schemas/文档类型规范.md`` 被输出成带引号的八进制转义
    ``"docs/schemas/\\346\\226\\207..."``，前缀 ``docs/schemas/`` 必 miss——
    受保护 schema 的中文名文件改动在 CI 端静默放行（探针旧版 exit=0）。
    """

    def test_git_changed_returns_unquoted_cjk_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _commit_cjk_protected_change(tmp_path, "docs: schema no marker")
        monkeypatch.chdir(tmp_path)
        paths = _mod._git_changed("HEAD^..HEAD", None)
        assert paths == ["docs/schemas/文档类型规范.md"]
        patterns = parse_protected_list(
            (_REPO / ".github" / "gate-protected-paths.txt").read_text(encoding="utf-8")
        )
        assert match_hits(paths, patterns) == ["docs/schemas/文档类型规范.md"]

    def test_multiple_paths_split_on_nul(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _commit_cjk_protected_change(tmp_path, "docs: schema + sibling")
        (tmp_path / "plugins").mkdir()
        (tmp_path / "plugins" / "other.py").write_text("y\n", encoding="utf-8")
        _run_git(tmp_path, "add", "-A")
        _run_git(tmp_path, "commit", "-q", "-m", "docs: sibling")
        monkeypatch.chdir(tmp_path)
        assert _mod._git_changed("HEAD^..HEAD", None) == ["plugins/other.py"]


class TestCliEndToEndMarkerSemantics:
    """端到端双向：受保护路径 diff 无标记红 / 带标记绿（真实 git 链路）。

    脚本与清单副本进临时仓（保护清单相对脚本自定位），跑 CLI 本体——
    即 CI job 对 PR/push 范围执行的那条命令。
    """

    def _probe(self, tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
        (tmp_path / "scripts").mkdir()
        (tmp_path / ".github").mkdir()
        shutil.copy2(_SCRIPT, tmp_path / "scripts" / "check_gate_protected.py")
        shutil.copy2(
            _REPO / ".github" / "gate-protected-paths.txt",
            tmp_path / ".github" / "gate-protected-paths.txt",
        )
        _commit_cjk_protected_change(tmp_path, message)
        return subprocess.run(
            [
                sys.executable,
                str(tmp_path / "scripts" / "check_gate_protected.py"),
                "--range",
                "HEAD^..HEAD",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )

    def test_protected_without_marker_is_red(self, tmp_path: Path) -> None:
        proc = self._probe(tmp_path, "docs: schema no marker")
        assert proc.returncode == 1, proc.stdout
        assert "docs/schemas/文档类型规范.md" in proc.stderr

    def test_protected_with_marker_is_green(self, tmp_path: Path) -> None:
        proc = self._probe(
            tmp_path,
            "docs: schema with gate change\n\n[gate-change: docs/decisions/adr.md]",
        )
        assert proc.returncode == 0, proc.stderr
        assert "全量" in proc.stdout  # 放行须可感知（禁静默降级）


class TestCiWiringUnconditional:
    """gate-protected 必须挂恒跑 job（P-04：车道短路 = 静默绕过）。

    旧接线只挂 python-lint（``if: python_full``），纯 docs/ 或纯 config/ 变更
    跳过该 job；而保护清单含 docs/schemas/、docs/graph/viewer.html 与两个
    config/ 文件——标记校验在那儿根本没跑。本用例锁「至少一个载体 job 无 if:」。
    """

    def test_gate_protected_has_unconditional_carrier(self) -> None:
        ci = yaml.safe_load(
            (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        carriers = [
            name
            for name, job in ci["jobs"].items()
            if any("gate-protected" in (step.get("run") or "") for step in job.get("steps", []))
        ]
        assert carriers, "gate-protected 未接线任何 CI job（gate-wiring V1 应红）"
        assert any(
            "if" not in ci["jobs"][name] for name in carriers
        ), f"gate-protected 仅挂在带 if: 的车道 job：{carriers}（短路=静默绕过）"
