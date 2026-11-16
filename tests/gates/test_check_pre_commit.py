# @feature: FP-GATE pre-commit 车道感知 CI 触发器 | @ci: python-coverage
"""check_pre_commit 判定面 + 真实 git 链端到端回归。

意图（用户裁定 2026-10-30「所有提交都能触发 CI；文档触发文档检查；大体积
产物爆红不让提交」）：断行为——车道映射按改动面选取对应快门禁（docs 车道
含 graph-check，P-03）；产物形状暂存即红（真实 tmp 仓 CLI 非零）；doc-only
提交选中文档车道（dry-run 验证不实际跑门禁）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_scripts = str(ROOT / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import check_no_build_artifacts as guard  # noqa: E402
import check_pre_commit as pc  # noqa: E402

_GIT = ["git", "-c", "user.name=test", "-c", "user.email=t@t.example"]


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env_git = [*_GIT]
    subprocess.run([*env_git, "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("", encoding="utf-8")
    subprocess.run([*env_git, "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run([*env_git, "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _stage(repo: Path, rel: str, content: bytes) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    subprocess.run([*_GIT, "add", rel], cwd=repo, check=True)


class TestClassifyLanes:
    def test_doc_only_selects_doc_gates(self):
        got = pc.classify_lanes(["docs/working/某台账.md"])
        assert got["gates"] == (
            "doc-baseline-sync",
            "doc-paths",
            "traceability-gate",
            "graph-check",
            "doc-frontmatter",
            "working-index-fresh",
        )

    def test_kernel_commit_selects_kernel_gates(self):
        got = pc.classify_lanes(["kernel/crates/api/src/ws_session.rs"])
        assert got["gates"] == ("kernel-fmt", "kernel-clippy")

    def test_mixed_lanes_dedupe_and_keep_order(self):
        got = pc.classify_lanes(
            [
                "docs/decisions/x.md",
                "kernel/crates/api/src/a.rs",
                "plugins/shared/llm/adapter.py",
                "scripts/check_x.py",
                "config/agents/main/agentos.yaml",
                "frontend/src/App.tsx",
                "plugins/sdk/pyproject.toml",
            ]
        )
        assert got["gates"] == (
            "doc-baseline-sync",
            "doc-paths",
            "traceability-gate",
            "graph-check",
            "doc-frontmatter",
            "working-index-fresh",
            "kernel-fmt",
            "kernel-clippy",
            "plugins-lint",
            "sdk-lint",
            "scripts-lint",
            "config-static",
            "frontend-lint",
        )

    def test_unmatched_paths_empty(self):
        assert pc.classify_lanes(["README.md.bak.tmp"])["gates"] == ()


class TestArtifactRedGate:
    def test_deny_path_and_oversize_detected_on_staged_blobs(self, tmp_path):
        repo = _init_repo(tmp_path)
        _stage(repo, "kernel/target-cov-g2/debug/x.pdb", b"fake pdb")
        reds = pc.check_staged_artifacts(repo, ["kernel/target-cov-g2/debug/x.pdb"])
        assert len(reds) == 1 and "产物路径命中禁入清单" in reds[0]

    def test_normal_source_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _stage(repo, "kernel/crates/api/src/a.rs", b"fn main() {}")
        assert pc.check_staged_artifacts(repo, ["kernel/crates/api/src/a.rs"]) == []


def _run_cli(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_pre_commit.py"), "--repo", str(repo), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestEndToEnd:
    def test_staged_artifact_blocks(self, tmp_path):
        """产物形状暂存即红（真实 tmp 仓 CLI 非零），且阻断发生在车道快检之前。"""
        repo = _init_repo(tmp_path)
        _stage(repo, "release/0.2.2/setup.exe", b"MZ" + b"\0" * 64)
        proc = _run_cli(repo)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "爆红" in proc.stdout

    def test_doc_commit_triggers_doc_lane(self, tmp_path):
        """doc-only 提交选中文档车道（dry-run 断言选取面，不实跑门禁）。"""
        repo = _init_repo(tmp_path)
        _stage(repo, "docs/working/台账.md", "# 台账\n".encode("utf-8"))
        proc = _run_cli(repo, "--dry-run")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "doc-baseline-sync" in proc.stdout and "traceability-gate" in proc.stdout
        assert "graph-check" in proc.stdout

    def test_oversize_blob_blocks(self, tmp_path):
        """超限大文件（>95MiB 硬限）暂存即红——不依赖路径形状，纯大小执法。"""
        repo = _init_repo(tmp_path)
        big = b"\0" * (guard.MAX_TRACKED_BYTES + 1)
        _stage(repo, "data/blob.bin", big)
        proc = _run_cli(repo)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "硬限" in proc.stdout
