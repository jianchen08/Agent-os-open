# @feature: FP-GATE 构建产物禁入提交闸 | @ci: python-coverage
"""check_no_build_artifacts 判定面 + 真实 git 链端到端回归。

意图：两次产物入库事故（2026-09-01 target-rel-alt 1394 件、2026-10-28
target-cov-g2 5.0 GB）都是 auto-save 盲扫 + .gitignore 漏配——本闸守
「已跟踪面不得出现产物路径/超限大文件」。断行为：产物形状必红、源码
形状必绿、真实 tmp 仓提交产物后 CLI 必非零。
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

_GIT = ["git", "-c", "user.name=test", "-c", "user.email=t@t.example"]


class TestDenyPatterns:
    def test_artifact_shapes_catched(self):
        # 两组有区分度的产物形状：cargo 备用构建目录 + 交付产物/二进制副产物
        caught = [
            "kernel/target-cov-g2/llvm-cov-target/debug/agentos_kernel.pdb",
            "kernel/target/debug/deps/libagentos_api.rlib",
            "node_modules/react/index.js",
            "plugins/shared/x/node_modules/y/z.js",
            "app-mobile/www/index.html",
            "app-mobile/lingxi-agentos-arm64-phone.apk",
            "release/0.2.2/lingxi-agentos-setup-0.2.2.exe",
            "release/win-unpacked/灵汐助手.exe",
            "pack_linux.tar.gz",
        ]
        for path in caught:
            assert guard.match_denied(path), path

    def test_source_paths_pass(self):
        passed = [
            "kernel/crates/api/src/ws_session.rs",
            "kernel/crates/api/Cargo.toml",
            "frontend/src/components/chat/MessageItem.tsx",
            "plugins/shared/llm/plugin.json",
            "docs/decisions/2026-10-29-build-artifact-commit-guard.md",
            "scripts/check_no_build_artifacts.py",
            "release/0.2.2/delivery_manifest.json",
        ]
        for path in passed:
            assert guard.match_denied(path) is None, path


class TestSizeCeiling:
    def test_oversize_flagged(self):
        big = ("kernel/crates/x/some-asset.bin", guard.MAX_TRACKED_BYTES + 1)
        assert guard.find_violations([big]) == [
            f"{big[0]}（{(big[1]) / 1048576:.1f} MiB > "
            f"{guard.MAX_TRACKED_BYTES / 1048576:.0f} MiB 上限）"
        ]

    def test_normal_size_passes_even_if_pattern_misses(self):
        ok = ("docs/working/some-report.md", 1024)
        assert guard.find_violations([ok]) == []


class TestEndToEnd:
    """真实 git 链（tmp 仓）：提交产物 → CLI 红；干净仓 → CLI 绿。"""

    def _commit(self, repo: Path, paths: dict[str, str]) -> None:
        for rel, text in paths.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(_GIT + ["-C", str(repo), "add", "-A"], check=True)
        subprocess.run(_GIT + ["-C", str(repo), "commit", "-m", "t"], check=True)

    def _init(self, tmp_path: Path) -> Path:
        subprocess.run(_GIT + ["-C", str(tmp_path), "init", "-q"], check=True)
        return tmp_path

    def test_repo_with_artifact_is_red(self, tmp_path: Path, capsys) -> None:
        repo = self._init(tmp_path)
        self._commit(
            repo,
            {
                "kernel/crates/a/src/lib.rs": "fn main() {}",
                "kernel/target-cov-g2/debug/agentos_kernel.pdb": "x" * 64,
            },
        )
        assert guard.main(["--repo", str(repo)]) == 1
        assert "kernel/target-cov-g2" in capsys.readouterr().out

    def test_clean_repo_is_green(self, tmp_path: Path) -> None:
        repo = self._init(tmp_path)
        self._commit(repo, {"kernel/crates/a/src/lib.rs": "fn main() {}"})
        assert guard.main(["--repo", str(repo)]) == 0

    def test_range_scan_catches_added_then_removed(self, tmp_path: Path) -> None:
        # 「区间内提交后又删」：HEAD 面干净，但区间对象面必须红。
        # 区间基 = 产物提交**之前**的干净提交（对应 CI push 事件的 before 提交）。
        repo = self._init(tmp_path)
        self._commit(repo, {"kernel/crates/a/src/lib.rs": "fn main() {}"})  # 基
        base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self._commit(
            repo,
            {"app-mobile/www/index.html": "<html/>"},
        )
        subprocess.run(_GIT + ["-C", str(repo), "rm", "-q", "app-mobile/www/index.html"], check=True)
        subprocess.run(_GIT + ["-C", str(repo), "commit", "-m", "rm"], check=True)
        assert guard.main(["--repo", str(repo)]) == 0  # HEAD 面确实干净
        assert guard.main(["--repo", str(repo), "--range", f"{base}..HEAD"]) == 1
